"""TTS 合成独立测试 CLI — 手输文字 → 真实 TTS 合成 → 播放 / 存盘

复用真实链路：providers.get_tts()（阿里云 Qwen3-TTS realtime，24kHz 16bit 单声道 PCM）。
音量 / 音色 / 音调默认读 backend/data/voice_settings.json（和真实后端一致）；
--voice/--volume/--pitch 为【内存级临时覆盖】，不改设置文件、不动真实后端。
播放用 winsound（Windows 自带库），无需额外依赖。

用法：
    python tts_cli.py "你好呀"                 # 合成并播放
    python tts_cli.py --list-voices            # 列出可选音色
    python tts_cli.py "欢迎回来" --voice Cherry --volume 70 --pitch 60
    python tts_cli.py "欢迎回来" --instr "慢一点，带点委屈"   # 自定义语气指令
    python tts_cli.py "存一下" --save out.wav --no-play

交互（不带文本参数进入，默认【向导模式】）：
    第 1 步  选情绪模板（0-6，回车跳过）
    第 2 步  选语气指令（0-?，回车跳过）——自动与情绪拼成一条 instructions
    第 3 步  输入文本合成；循环输入继续合成
             m         重新选情绪/指令
             q / exit  退出
             save=xxx.wav  存档（本次及之后）
             play on|off   播放开关
    （--cmd 进入自由命令模式：voice= / volume= / pitch= / model= / instr= / lib= / emo= 等）

命令行加速（跳过向导）：
    python tts_cli.py "文本" --instr "指令" --save out.wav
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import wave

from dotenv import load_dotenv

load_dotenv()  # backend/.env

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPWIDTH = 2  # 16-bit


# ── 参数 ─────────────────────────────────


def _parse_args(argv: list[str]):
    text: str | None = None
    list_voices = False
    save_path: str | None = None
    no_play = False
    cmd_mode = False
    overrides: dict[str, str] = {}
    i = 0
    pos: list[str] = []
    while i < len(argv):
        a = argv[i]
        if a == "--list-voices":
            list_voices = True
        elif a == "--cmd":
            cmd_mode = True
        elif a == "--save" and i + 1 < len(argv):
            save_path = argv[i + 1]
            i += 1
        elif a == "--no-play":
            no_play = True
        elif a in ("--voice", "--volume", "--pitch", "--model", "--instr") and i + 1 < len(argv):
            overrides[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("-"):
            print(f"未知参数: {a}")
            print(__doc__)
            sys.exit(2)
        else:
            pos.append(a)
        i += 1
    if pos:
        text = " ".join(pos)
    return text, list_voices, save_path, no_play, cmd_mode, overrides


def _show_voices() -> None:
    from voice_catalog import list_voices

    info = list_voices()
    print(f"[tts-cli] 当前 TTS 模型: {info['model'] or '(未设置)'}  当前音色: {info['current']}")
    for v in info["voices"]:
        mark = " ← 当前" if v["id"] == info["current"] else ""
        print(f"  {v['id']:<12} {v['label']}{mark}")


# ── 内存级覆盖（不改 voice_settings.json） ─────────────────


_OVERRIDES: dict[str, str] = {}


def _session_params() -> dict | None:
    """按 CLI 临时设置构造 TTS params；未设置时返回 None（走真实默认设置）。

    - instr=xxx（自定义指令）→ 用 A 格式 {"instructions": ..., "speech_rate": None}，
      speech_rate=None 由 SDK 保持默认（providers/tts.py 分支判断依赖 speech_rate 键存在）；
    - emo=xxx（情绪模板）→ 用 B 格式 {"emotion": ...}，由 providers/tts.py 查 EMOTION_INSTRUCTIONS。
    """
    if _OVERRIDES.get("_instr"):
        return {"instructions": _OVERRIDES["_instr"], "speech_rate": None}
    if _OVERRIDES.get("_emo"):
        return {"emotion": _OVERRIDES["_emo"]}
    return None


def _apply_overrides() -> None:
    """把 CLI 的音色/音量/音调临时注入 voice_settings.load_voice_settings（内存态）。"""
    if not _OVERRIDES:
        return
    import voice_settings

    orig = voice_settings.load_voice_settings

    def patched():
        base = orig()
        if _OVERRIDES.get("voice"):
            base["voice"] = _OVERRIDES["voice"]
        if _OVERRIDES.get("volume"):
            try:
                base["volume"] = max(0, min(100, int(_OVERRIDES["volume"])))
            except ValueError:
                pass
        if _OVERRIDES.get("pitch"):
            try:
                base["pitch"] = max(0, min(100, int(_OVERRIDES["pitch"])))
            except ValueError:
                pass
        return base

    voice_settings.load_voice_settings = patched
    print(f"[tts-cli] 临时覆盖: {_OVERRIDES}（仅内存，不影响设置文件）")


def _build_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPWIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _play(wav_bytes: bytes) -> None:
    if not wav_bytes:
        return
    try:
        from winsound import PlaySound, SND_MEMORY

        PlaySound(wav_bytes, SND_MEMORY)  # 阻塞播放，播完返回
        print("[tts-cli] 播放完毕")
    except Exception as e:
        print(f"[tts-cli] 播放失败（可加 --no-play 只合成）: {e}")


# ── 合成 ─────────────────────────────────


async def _synth_once(tts, text: str, save_path: str | None, play: bool, params: dict | None = None) -> None:
    print(f"[tts-cli] 合成: {text[:40]}" + (f"  instr={params.get('instructions', params.get('emotion', ''))[:30]}" if params else ""))
    chunks: list[bytes] = []
    try:
        async for chunk in tts.synth_stream(text, params):  # params 留空 → 走真实设置（含 CLI 覆盖）
            if chunk:
                chunks.append(chunk)
    except Exception as e:
        print(f"[tts-cli] 合成失败: {e}")
        return
    pcm = b"".join(chunks)
    if not pcm:
        print("[tts-cli] 无音频输出（可能 TTS 连接异常/额度问题）")
        return
    wav = _build_wav(pcm)
    dur = len(pcm) / SAMPLE_RATE / SAMPWIDTH
    print(f"[tts-cli] 音频 {len(pcm)} 字节 ≈ {dur:.1f}s")
    if save_path:
        with open(save_path, "wb") as f:
            f.write(wav)
        print(f"[tts-cli] 已保存: {save_path}")
    if play:
        _play(wav)


# ── 向导模式：选情绪 → 选指令 → 输文本 ─────────────────


def _pick(title: str, presets: list, allow_skip: bool = True):
    """打印菜单并让用户选一条；回车跳过返回 None；越界重输。"""
    print(f"\n===== {title}（输入编号选择" + ("；直接回车跳过" if allow_skip else "") + "） =====")
    for i, (name, text, note) in enumerate(presets):
        shown = text if len(text) <= 44 else text[:44] + "…"
        print(f"  {i:>2}  {name:<6} {note}  → {shown}")
    while True:
        s = input(f"{title}> ").strip()
        if s == "" and allow_skip:
            return None
        try:
            idx = int(s)
        except ValueError:
            print(f"无效编号，请输入 0~{len(presets)-1}" + (" 或直接回车跳过" if allow_skip else ""))
            continue
        if 0 <= idx < len(presets):
            return presets[idx]
        print(f"编号越界（0~{len(presets)-1}）")


def _compose_instructions(emo_preset, human_preset) -> str:
    """情绪模板 + 人味指令 → 一条 instructions（API 只能传一条，用句号拼接）。"""
    parts = []
    if emo_preset:
        parts.append(emo_preset[1])
    if human_preset:
        parts.append(human_preset[1])
    return "。".join(parts)


async def _wizard(tts) -> None:
    """向导模式：先选情绪模板 → 再选语气指令 → 循环输入文本合成。"""
    from tts_instruct_lab import EMOTION_PRESETS, HUMAN_FLAVOR_PRESETS

    play = True
    from voice_catalog import list_voices

    print("\n╔══ 向导模式 ══╗\n"
          "先选【音色】→ 再选【情绪模板】（底色）→ 再选【语气指令】（人味控制），自动拼成一条 instructions。\n"
          "之后直接输文本合成；m 重新选择（音色/情绪/指令）；q 退出；save=xxx.wav 存档；play on|off 播放开关；\n"
          "    voice=xxx 可随时快速切音色（list 列全部音色）。")
    voice_menu = [(v["id"], v["label"], "") for v in list_voices()["voices"]]
    while True:
        vpick = _pick("音色", voice_menu, allow_skip=True)
        if vpick:
            _OVERRIDES["voice"] = vpick[0]
            _apply_overrides()
            print(f"[tts-cli] 音色 → {vpick[0]}（{vpick[1]}）")
        else:
            print("[tts-cli] 音色 → （跳过，走当前默认）")
        emo = _pick("情绪模板", EMOTION_PRESETS, allow_skip=True)
        human = _pick("语气指令", HUMAN_FLAVOR_PRESETS, allow_skip=True)
        composed = _compose_instructions(emo, human)
        if composed:
            print(f"\n[tts-cli] 当前组合 → 情绪: {emo[0] if emo else '（无）'}  指令: {human[0] if human else '（无）'}")
            print(f"[tts-cli] 最终 instructions: “{composed}”")
        else:
            print("\n[tts-cli] 当前组合 → （未选，走默认设置）")
        while True:
            line = input("文本> ").strip()
            if not line:
                continue
            if line in ("q", "quit", "exit", "退出"):
                print("[tts-cli] 再见")
                return
            if line == "m":
                print("\n—— 重新选择情绪/指令 ——")
                break
            if line.startswith("save="):
                _OVERRIDES["_save"] = line.split("=", 1)[1].strip()
                print(f"[tts-cli] 存档 → {_OVERRIDES['_save']}")
                continue
            if line.startswith("voice=") or line.startswith("v="):
                _OVERRIDES["voice"] = line.split("=", 1)[1].strip()
                _apply_overrides()
                print(f"[tts-cli] 音色 → {_OVERRIDES['voice']}（list 可看全部音色）")
                continue
            if line == "list":
                _show_voices()
                continue
            if line.startswith("play "):
                play = line.split(" ", 1)[1].strip() != "off"
                print(f"[tts-cli] 播放 {'开' if play else '关'}")
                continue
            params = {"instructions": composed, "speech_rate": None} if composed else None
            await _synth_once(tts, line, _OVERRIDES.get("_save"), play, params)


async def _free_mode(tts) -> None:
    """自由命令模式（--cmd）：lib / instr= / emo= / voice= / volume= / pitch= / model= / save= / play。"""
    play = True
    print("\n自由命令模式（exit 退出；lib 看指令库；voice=xxx / volume=80 / pitch=60 / model=xxx / instr=xxx / lib=编号 / emo=开心 / save=xxx.wav / play on|off；先 lib 选指令）")
    while True:
        try:
            text = input("文本> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[tts-cli] 再见")
            break
        if not text:
            continue
        if text in ("exit", "quit", "退出"):
            break
        if text == "list":
            _show_voices()
            continue
        if text.startswith("voice=") or text.startswith("v="):
            _OVERRIDES["voice"] = text.split("=", 1)[1].strip()
            _apply_overrides()
            print(f"[tts-cli] 音色 → {_OVERRIDES['voice']}")
            continue
        if text.startswith("volume=") or text.startswith("vol="):
            _OVERRIDES["volume"] = text.split("=", 1)[1].strip()
            _apply_overrides()
            continue
        if text.startswith("pitch="):
            _OVERRIDES["pitch"] = text.split("=", 1)[1].strip()
            _apply_overrides()
            continue
        if text.startswith("model="):
            import providers.tts as _pt

            _pt.TTS_MODEL = text.split("=", 1)[1].strip()
            print(f"[tts-cli] TTS 模型（内存）→ {_pt.TTS_MODEL}")
            continue
        if text.startswith("instr="):
            val = text.split("=", 1)[1].strip()
            _OVERRIDES.pop("_emo", None)
            if val:
                _OVERRIDES["_instr"] = val
                print(f"[tts-cli] 语气指令 → “{val}”")
            else:
                _OVERRIDES.pop("_instr", None)
                print("[tts-cli] 语气指令已清除 → 回到默认设置")
            continue
        if text == "lib" or text.startswith("lib="):
            from tts_instruct_lab import ALL_PRESETS, list_presets_text, get_preset

            if text == "lib":
                print(list_presets_text())
                print(f"[tts-cli] 当前指令: {_OVERRIDES.get('_instr', '(未设置，走默认)')}")
                continue
            try:
                idx = int(text.split("=", 1)[1].strip())
            except ValueError:
                print("[tts-cli] lib=<编号>，例如 lib=3")
                continue
            hit = get_preset(idx)
            if hit is None:
                print(f"[tts-cli] 编号越界（0~{len(ALL_PRESETS)-1}），输入 lib 看菜单")
                continue
            name, instr, note = hit
            _OVERRIDES.pop("_emo", None)
            _OVERRIDES["_instr"] = instr
            print(f"[tts-cli] 指令库[{idx}] {name}（{note}）→ “{instr}”")
            continue
        if text.startswith("emo="):
            from voice_style import EMOTION_INSTRUCTIONS

            val = text.split("=", 1)[1].strip()
            _OVERRIDES.pop("_instr", None)
            if val in EMOTION_INSTRUCTIONS:
                _OVERRIDES["_emo"] = val
                print(f"[tts-cli] 情绪模板 → {val}: “{EMOTION_INSTRUCTIONS[val]}”")
            else:
                print(f"[tts-cli] 未知情绪：{val}（可选：{'/'.join(EMOTION_INSTRUCTIONS)}）")
            continue
        if text.startswith("save="):
            _OVERRIDES["_save"] = text.split("=", 1)[1].strip()
            continue
        if text.startswith("play "):
            play = text.split(" ", 1)[1].strip() != "off"
            print(f"[tts-cli] 播放 {'开' if play else '关'}")
            continue
        await _synth_once(tts, text, _OVERRIDES.get("_save"), play, _session_params())


def main() -> None:
    text, list_voices, save_path, no_play, cmd_mode, overrides = _parse_args(sys.argv[1:])

    if list_voices:
        _show_voices()
        return

    if overrides:
        _OVERRIDES.update(overrides)
        _apply_overrides()
    if save_path:
        _OVERRIDES["_save"] = save_path

    try:
        from providers import get_tts

        tts = get_tts()
    except Exception as e:
        print(f"[tts-cli] 初始化 TTS 失败（检查 DASHSCOPE_API_KEY / TTS_PROVIDER）: {e}")
        sys.exit(1)
    print(f"[tts-cli] TTS: {tts.__class__.__name__}")

    try:
        if text:
            asyncio.run(_synth_once(tts, text, save_path or _OVERRIDES.get("_save"), not no_play, _session_params()))
        elif cmd_mode:
            asyncio.run(_free_mode(tts))
        else:
            asyncio.run(_wizard(tts))
    except KeyboardInterrupt:
        print("\n[tts-cli] 已退出")


if __name__ == "__main__":
    main()