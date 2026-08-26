"""MiniMax TTS Provider：Speech 2.8 T2A v2 双向流式（WebSocket 长连接）+ HTTP 流式可切换

接口核对（依据官方 bidi 文档：platform.minimaxi.com/docs/api-reference/speech-t2a-websocket-bidi）：
- 端点:      wss://{host}/ws/v1/t2a_v2_bidi   （bidi：服务端自动攒句，支持逐字/逐 token 输入）
- 鉴权:      握手 Header `Authorization: Bearer {api_key}`
- 事件（客户端 → 服务端）:
    task_start（配置）→ task_continue{text}（可任意粒度，服务端攒句）
    task_cancel（打断，返回 task_canceled 后可继续，不需重建连接）
    task_flush（催尾句残留，不结束会话）→ task_finish（先合成残留再关连接）
- 事件（服务端 → 客户端）:
    connected_success → task_started → sentence_start / task_continued{data.audio hex} / sentence_end
    task_canceled / task_flushed / task_finished / task_failed
- 三层结束语义：is_final=本次请求音频结束；sentence_end=当前句结束；task_finished=会话结束
- 错误码:  0 正常；1000/1001/1002/1004/1039/1042/2013 错误；2201 连接空闲超时被关（见保活）；
           2202 非法事件; 2204 单条 continue 超 1w 字符被跳过（连接会话保持）;
           2205 排队过多（非限流，稍后重发即可，勿重连）; 2206 事件顺序非法
- 连接保活: 空闲约 120s 服务端关闭（返回 2201）。服务端【不主动发 ping】，客户端需定期发
           WebSocket ping（服务端回 pong 刷新活跃），仅 TCP 存活不够。
- 情绪:     voice_setting.emotion ═默认不传══让 MiniMax 按文本自动挑；显式传 params["emotion_enum"]
- text 控制（仅 2.8）：停顿 <#x#>；拟声 (breath)(sighs)(laughs)...（见 INTERJECTIONS）

本实现（MINIMAX_TRANSPORT=ws）：
    长连接复用：懒建立 → 连接期内多句 task_continue → keepalive ping（25s）→
    打断/异常时断连（下次自动重连）；task_cancel 保连接作为下一步优化。
HTTP 传输（MINIMAX_TRANSPORT=http）仍保留（实测首包 0.2~0.3s，最快）。

用法（backend/.env）：
    TTS_PROVIDER=minimax
    MINIMAX_API_KEY=xxx
    MINIMAX_MODEL=speech-2.8-turbo
    MINIMAX_VOICE_ID=Chinese_worker_female
    MINIMAX_BASE_URL=https://api.minimaxi.com
    MINIMAX_TRANSPORT=ws|http   （默认 http）
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import threading
import time

import aiohttp
import requests

from dotenv import load_dotenv

load_dotenv()

from .base import TTSProvider  # noqa: E402

# 中文情绪标签 → MiniMax voice_setting.emotion 枚举（默认不启用，交由服务端自动挑情绪）
EMOTION_MAP = {
    "开心": "happy", "兴奋": "happy", "好奇": "surprised",
    "委屈": "sad", "难过": "sad", "困": "calm", "平静": "calm",
}

EMOTION_ENUMS = {"happy", "sad", "angry", "fearful", "disgusted", "surprised", "calm", "fluent", "whisper"}

# MiniMax 官方支持的拟声标签（半角圆括号包裹，仅 speech-2.8-* 有效）
INTERJECTIONS = {
    "laughs", "chuckle", "coughs", "clear-throat", "groans", "breath", "pant",
    "inhale", "exhale", "gasps", "sniffs", "snorts", "burps", "lip-smacking",
    "humming", "hissing", "emm", "sneezes",
}
_INTERJ_RE = "|".join(sorted(INTERJECTIONS))

DEFAULT_BASE_URL = "https://api.minimaxi.com"

# 空闲保活：bidi 服务端约 120s 空闲断开（2201），客户端每 25s ping 刷新
KEEPALIVE_INTERVAL = 25


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class MiniMaxTTS(TTSProvider):
    """MiniMax T2A v2 流式合成（bidi WebSocket 长连接 / HTTP 可切换，hex → PCM bytes）。"""

    def __init__(self):
        self.api_key = os.getenv("MINIMAX_API_KEY")
        self.model = os.getenv("MINIMAX_MODEL", "speech-2.8-turbo")
        self.voice_id = os.getenv("MINIMAX_VOICE_ID", "socialmedia_female_2_v1")
        self.base_url = os.getenv("MINIMAX_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.transport = os.getenv("MINIMAX_TRANSPORT", "http").strip().lower()  # ws|http
        self.sample_rate = int(os.getenv("MINIMAX_SAMPLE_RATE", "24000"))
        self.audio_format = os.getenv("MINIMAX_FORMAT", "pcm").strip().lower()
        self.text_normalization = os.getenv("MINIMAX_TEXT_NORMALIZATION", "false").strip().lower() in ("1", "true", "yes")
        if not self.api_key:
            raise RuntimeError("未配置 MINIMAX_API_KEY，请在 backend/.env 里填写")

        self.cancel_event = threading.Event()
        self.first_audio_time = None  # 暴露给 main.py（TTS 首包时间）
        self._http_session = None  # HTTP keep-alive
        self._active_resp = None  # 当前句 HTTP 流（打断即断连）
        self._ws = None  # bidi WebSocket 长连接
        self._ws_session = None  # aiohttp ClientSession
        self._ws_ready = False  # 连接上是否已完成一次 task_start（可发 task_continue）
        self._keepalive_task = None

    def cancel(self):
        """打断时调用：停止当前合成（HTTP 立即断流；WS 由 synth 检测后断连重连）"""
        self.cancel_event.set()
        resp = getattr(self, "_active_resp", None)
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    async def preheat(self, params: dict | None = None):
        """预热：异步建 WS 长连接 + 完成 task_started，使首句合成可立即 task_continue。

        适用场景：用户唤醒/进对话时立刻调（不阻塞前端其他操作），把原本
        5.5s 的「建连 + task_start」提前到唤醒时间窗内跑完。
        唤醒→用户开口通常有 1~3s，足够建连完成（实测 ~2s）。

        幂等：已建好（_ws_ready=True）则直接返回。
        并发安全：单 asyncio 循环里串行调用不会有 race；多并发时 _ensure_ws 内部
        复用 _ws 检查，最多多发一次 ws_connect（无害）。

        情绪：预热固定 emotion_enum=calm（情绪状态机默认），task_started 后
        voice_setting 锁定。首句若 emotion_state 不是平静，TTS 仍按 calm 渲染
        —— 这与「现有实现不预热」的效果一致（不引入回归）。后续句 task_continue
        切情绪不会更新 voice_setting，这是 MiniMax 协议本身的限制。
        """
        if self.transport != "ws":
            return  # HTTP 模式无预热意义（每次请求都新连接）
        ws = await self._ensure_ws()
        if self._ws_ready:
            return
        await self._start_task({"emotion_enum": "calm"})

    # ── 文本标签规范化：修正 LLM 常写错的 MiniMax 标签，避免被当文字读出 ──
    def _clean_text(self, text: str) -> str:
        text = re.sub(r"[〈<＜【\s]*#([0-9]+(?:\.[0-9]+)?)#[〉>＞】\s]*",
                      lambda m: f"<#{m.group(1)}#>", text)
        text = re.sub(r"[〈<＜\(（]\s*(" + _INTERJ_RE + r")\s*[〉>＞\)）]",
                      lambda m: f"({m.group(1)})", text, flags=re.I)
        text = re.sub(r"[〈<＜【]([^#<>(){}\[\]<>（）]{1,12})[〉>＞】]", r"\1", text)
        text = re.sub(r"（([^（）]{1,12})）", r"\1", text)
        return text

    # ── 参数映射：project 参数 → MiniMax voice_setting ──
    def _map_params(self, params: dict | None) -> dict:
        params = params or {}
        speed = _clamp(float(params.get("speech_rate") or 1.0), 0.5, 2.0)
        vol = _clamp(float(params.get("volume") or 50) / 10.0, 0.0, 10.0)
        pitch_rate = float(params.get("pitch_rate") or 1.0)
        pitch = int(_clamp(round((pitch_rate - 1.0) * 30), -12, 12))
        vs = {"voice_id": self.voice_id, "speed": speed, "vol": vol, "pitch": pitch}
        enum = params.get("emotion_enum")
        if enum in EMOTION_ENUMS:
            vs["emotion"] = enum
        return vs

    def _config(self, params: dict | None) -> dict:
        voice_setting = self._map_params(params)
        voice_setting["text_normalization"] = bool(self.text_normalization)
        return {
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": self.sample_rate,
                "format": self.audio_format,
                "channel": 1,
            },
        }

    # ── URL ──
    def _http_url(self) -> str:
        return f"{self.base_url}/v1/t2a_v2"

    def _ws_url(self) -> str:
        base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/ws/v1/t2a_v2_bidi"

    # ── WS 长连接管理 ──
    async def _keepalive(self, ws):
        """连接期每 KEEPALIVE_INTERVAL 秒发 ping，防止 120s 空闲被 2201 关闭。"""
        try:
            while not ws.closed and self._ws is ws:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if ws.closed or self._ws is not ws:
                    return
                try:
                    await ws.ping()
                except Exception:
                    if self._ws is ws:
                        self._ws = None  # ping 失败 → 连接失效，下次重连
                    return
        except asyncio.CancelledError:
            pass

    async def _ensure_ws(self):
        """返回可用长连接；未建/已断则新建（仅负责连接 + 首次 task_start）。"""
        # 修复：原逻辑 `not (self._ws_session or aiohttp.ClientSession()).closed` 会临时 new 一个 ClientSession
        # 触发 aiohttp 「unclosed client session」警告，且 .closed 取错属性。
        # 改为只检查 self._ws_session（存在则用，不存在视为已建好的"空"标志）。
        if (self._ws is not None and not self._ws.closed
                and (self._ws_session is None or not self._ws_session.closed)):
            return self._ws
        if self._ws_session is None or self._ws_session.closed:
            self._ws_session = aiohttp.ClientSession()
        ws = await self._ws_session.ws_connect(
            self._ws_url(),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=15,
        )
        self._ws = ws
        self._ws_ready = False
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = asyncio.create_task(self._keepalive(ws))
        return ws

    async def _close_ws(self):
        """断开长连接（打断/异常/正常关闭），并停 keepalive。"""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        ws, self._ws = self._ws, None
        self._ws_ready = False
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _start_task(self, params: dict | None):
        """在新连接上发起 task_start，等 task_started 后置 _ws_ready。"""
        cfg = self._config(params)
        start = {"event": "task_start", "model": self.model, **cfg}
        ws = self._ws
        await ws.send_str(json.dumps(start))
        while True:
            msg = await ws.receive()
            if msg.type not in (aiohttp.WSMsgType.TEXT,):
                continue
            data = json.loads(msg.data)
            base = data.get("base_resp", {})
            if base.get("status_code", 0) != 0:
                raise RuntimeError(f"MiniMax[{base.get('status_code')}]: {base.get('status_msg', '')}")
            if data.get("event") == "task_started":
                self._ws_ready = True
                return
            if data.get("event") == "connected_success":
                continue

    # ── 入口：按 transport 分发 ──
    async def synth_stream(self, text: str, params: dict | None = None):
        if self.transport == "ws":
            async for c in self._synth_ws(text, params):
                yield c
        else:
            async for c in self._synth_http(text, params):
                yield c

    # ── HTTP 流式（保留；实测首包 0.2~0.3s）──
    async def _synth_http(self, text: str, params: dict | None = None):
        self.cancel_event.clear()
        self.first_audio_time = None
        audio_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()
        t_start = time.time()
        cfg = self._config(params)
        body = {
            "model": self.model,
            "text": self._clean_text(text),
            "stream": True,
            **cfg,
            "stream_options": {"exclude_aggregated_audio": True},
        }
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "authorization": "Bearer " + self.api_key,
        }

        def _run():
            resp = None
            try:
                if self._http_session is None:
                    self._http_session = requests.Session()
                resp = self._http_session.post(
                    self._http_url(), headers=headers, json=body,
                    stream=True, timeout=(10, 120),
                )
                self._active_resp = resp
                if resp.status_code != 200:
                    audio_queue.put(("error", f"HTTP {resp.status_code}: {resp.text[:200]}"))
                    return
                for raw in resp.iter_lines(decode_unicode=True):
                    if self.cancel_event.is_set():
                        break
                    if not raw:
                        continue
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    base_resp = data.get("base_resp", {})
                    if base_resp.get("status_code", 0) != 0:
                        audio_queue.put(("error", f"MiniMax[{base_resp.get('status_code')}]: {base_resp.get('status_msg', '')}"))
                        return
                    audio = data.get("data", {}).get("audio")
                    if audio:
                        try:
                            audio_queue.put(("data", bytes.fromhex(audio)))
                        except ValueError:
                            pass
                audio_queue.put(("done", None))
            except Exception as e:  # noqa: BLE001
                audio_queue.put(("error", repr(e)))
            finally:
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                self._active_resp = None
                done_event.set()

        threading.Thread(target=_run, daemon=True).start()
        while True:
            if self.cancel_event.is_set():
                done_event.set()
                break
            try:
                kind, payload = audio_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "data":
                if self.first_audio_time is None:
                    self.first_audio_time = round(time.time() - t_start, 2)
                yield payload
            elif kind == "done":
                break
            elif kind == "error":
                print(f"[MiniMax HTTP 错误] {payload}")
                break

    # ── bidi WebSocket 长连接流式 ──
    async def _synth_ws(self, text: str, params: dict | None = None):
        self.cancel_event.clear()
        self.first_audio_time = None
        t_start = time.time()
        phrase = self._clean_text(text)

        ws = await self._ensure_ws()
        try:
            if not self._ws_ready:
                await self._start_task(params)  # 新连接：task_start → task_started
            await ws.send_str(json.dumps({"event": "task_continue", "text": phrase}))
            while True:
                if self.cancel_event.is_set():
                    break
                msg = await ws.receive()
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                    print(f"[MiniMax WS] 服务端关闭连接 (ws.closed={ws.closed}, code={getattr(msg, 'data', '')})")
                    await self._close_ws()
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                base = data.get("base_resp", {})
                code = base.get("status_code", 0)
                if code != 0:
                    print(f"[MiniMax WS] code={code} msg={base.get('status_msg', '')}")
                    if code in (2204, 2205):  # 软失败：跳过/稍后重发，连接会话保持
                        break
                    await self._close_ws()
                    raise RuntimeError(f"MiniMax[{code}]: {base.get('status_msg', '')}")
                ev = data.get("event")
                if ev == "connected_success":
                    continue
                if ev == "task_started":
                    self._ws_ready = True
                    continue
                if ev == "task_continued":
                    audio = data.get("data", {}).get("audio")
                    if audio:
                        if self.first_audio_time is None:
                            self.first_audio_time = round(time.time() - t_start, 2)
                        try:
                            yield bytes.fromhex(audio)
                        except ValueError:
                            pass
                    continue
                if ev == "sentence_end":
                    break  # 本句音频结束；连接保持（长连接复用）
                if ev in ("task_canceled", "task_flushed", "task_finished"):
                    break
                if ev == "task_failed":
                    await self._close_ws()
                    raise RuntimeError(f"MiniMax task_failed: {msg.data}")
        finally:
            if self.cancel_event.is_set():
                # 打断：断开当前连接（下次自动重连；task_cancel 保连接为下一步优化）
                await self._close_ws()

    async def speak_and_send(self, ws, text: str, session_id: str, params: dict | None = None):
        """流式合成并通过 WebSocket 发送给前端"""
        print(f"[MiniMax TTS] 开始合成: {text[:30]}")
        await ws.send_json({"type": "tts_start", "session_id": session_id, "text": text})
        try:
            async for chunk in self.synth_stream(text, params):
                await ws.send_bytes(chunk)
        except Exception as e:
            import traceback
            print(f"[MiniMax TTS] 合成异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            raise
        print(f"[MiniMax TTS] 合成完成并发送")
        await ws.send_json({"type": "tts_end", "session_id": session_id})

    @staticmethod
    def known_voice_ids() -> list[str]:
        return [
            "socialmedia_female_1_v1", "socialmedia_female_2_v1",
            "voice_agent_Female_Phone_4", "voice_agent_Male_Phone_1", "voice_agent_Male_Phone_2",
            "English_StressedLady", "English_SentimentalLady", "English_radiant_girl",
            "English_WiseScholar", "English_Persuasive_Man", "English_Explanatory_Man",
            "japanese_male_social_media_1_v2", "japanese_female_social_media_1_v2",
            "French_CasualMan", "Spanish_Narrator", "Arabic_CalmWoman", "German_PlayfulMan",
        ]