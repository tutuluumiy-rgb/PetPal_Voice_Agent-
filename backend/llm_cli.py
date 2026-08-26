"""LLM 生成独立测试 CLI — 只测提示词与生成，不碰 ASR/TTS/VAD/WS

为什么用它：
- 系统提示词 = 真实后端同一套组装（prompt_loader.build_system_prompt(mode)：
  personality + voice_style + agent.md + 模式提示词 + 工具目录 + 模式标记 + 用户档案）
- 生成走 providers.get_llm() 的 chat_stream（纯生成、逐句流式打印），与你改提示词后的
  真实闲聊链路共用同一份输入
- 启动即打印「整条系统提示词的 MD5 + 摘要」——A/B 时换提示词后 MD5 会变，即证明已生效

用法：
    python llm_cli.py [--mode chat|work] [--dump-prompt] [--use-prompt FILE] [--agent]

参数：
    --mode chat|work   组装哪种模式提示词（默认 chat）
    --dump-prompt      打印整条系统提示词后退出（A/B 对比用）
    --use-prompt FILE  用 FILE 的内容替换 chat_system_prompt.md 再组装
                       （不改真实文件；适合把保存的旧版提示词拿来 A/B）
    --agent            用 agent_chat（含工具自路由循环）而非纯 chat_stream

交互命令：
    exit / quit / Ctrl+C   退出
    :reset                 清空对话历史
    :prompt                打印当前整条系统提示词
    :hash                  打印当前系统提示词 MD5
    :mode chat|work        切换模式（重新组装提示词）
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # backend/.env


# ── 参数 ─────────────────────────────────


def _parse_args(argv: list[str]):
    mode = "chat"
    dump = False
    use_prompt: str | None = None
    agent = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1] if argv[i + 1] in ("chat", "work") else "chat"
            i += 2
        elif a == "--dump-prompt":
            dump = True
            i += 1
        elif a == "--use-prompt" and i + 1 < len(argv):
            use_prompt = argv[i + 1]
            i += 2
        elif a == "--agent":
            agent = True
            i += 1
        else:
            print(f"未知参数: {a}")
            print(__doc__)
            sys.exit(2)
    return mode, dump, use_prompt, agent


def _apply_prompt_override(use_prompt: str) -> None:
    """把 chat_system_prompt.md 的内容替换成 FILE（不改真实文件）。"""
    if not os.path.isfile(use_prompt):
        print(f"[llm-cli] 找不到提示词文件: {use_prompt}")
        sys.exit(2)
    import prompt_loader as pl

    original = pl.load_prompt

    def patched(name: str) -> str:
        if name == "chat_system_prompt.md":
            with open(use_prompt, encoding="utf-8") as f:
                return f.read().strip()
        return original(name)

    pl.load_prompt = patched
    print(f"[llm-cli] 已用 {use_prompt} 替换 chat_system_prompt.md 参与组装（未改真实文件）")


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _summary(p: str) -> str:
    """第一行 + 前 120 字（折叠换行），用于快速肉眼核对。"""
    first = p.splitlines()[0] if p else "(空)"
    body = " ".join(line.strip() for line in p.splitlines()[1:] if line.strip())[:120]
    return f"{first} :: {body}"


def _print_prompt(p: str) -> None:
    print("── 系统提示词（全文） ──")
    print(p)
    print("──────────────────────")


# ── 主循环 ─────────────────────────────────


async def _run(mode: str, agent: bool):
    # 同步全局模式状态：chat_stream / agent_chat 内部按它组装提示词
    from mode_state import get_mode_state

    get_mode_state().switch(mode)

    from prompt_loader import build_system_prompt
    from providers import get_llm

    llm = get_llm()
    print(f"[llm-cli] LLM: {llm.__class__.__name__}  model={llm.model}")
    print(f"[llm-cli] 模式: {mode}")
    print(f"[llm-cli] 系统提示词 MD5: {_md5(build_system_prompt(mode))}")
    print(f"[llm-cli] 摘要: {_summary(build_system_prompt(mode))}")
    print("\n交互开始（exit 退出；:reset 清历史；:prompt 看提示词；:hash 看 MD5；:mode chat|work 切模式）")

    history: list[dict] = []

    while True:
        try:
            text = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[llm-cli] 再见")
            break
        if not text:
            continue

        if text in ("exit", "quit", "退出"):
            break
        if text == ":reset":
            history.clear()
            print("[llm-cli] 历史已清空")
            continue
        if text == ":prompt":
            _print_prompt(build_system_prompt(get_mode_state().get_mode()))
            continue
        if text == ":hash":
            print(f"[llm-cli] MD5 = {_md5(build_system_prompt(get_mode_state().get_mode()))}")
            continue
        if text.startswith(":mode"):
            parts = text.split()
            if len(parts) == 2 and parts[1] in ("chat", "work"):
                get_mode_state().switch(parts[1])
                history.clear()
                p = build_system_prompt(parts[1])
                print(f"[llm-cli] 已切换 {parts[1]}，历史已清空，新 MD5={_md5(p)}")
            else:
                print("[llm-cli] 用法: :mode chat|work")
            continue

        # 生成（chat_stream 会把当前 text 作为最后一条 user；history 只放过往对话）
        history = history[-40:]  # 与真实后端一致：保留最近 20 条消息（user+assistant 各一）
        print("西西> ", end="", flush=True)
        reply = ""
        try:
            if agent:
                async for kind, *rest in llm.agent_chat(text, history):
                    if kind == "progress":
                        print(f"\n〔进度〕{rest[0]}", flush=True)
                    elif kind == "reply":
                        print(rest[0], end="", flush=True)
                        reply += rest[0]
            else:
                async for sentence, _emotion in llm.chat_stream(text, history):
                    print(sentence, end="", flush=True)
                    reply += sentence
            print()
        except asyncio.CancelledError:
            print("\n[llm-cli] 已中断")
            continue
        except Exception as e:
            print(f"\n[llm-cli] 生成失败: {e}")
            continue

        if reply.strip():
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})


def main() -> None:
    mode, dump, use_prompt, agent = _parse_args(sys.argv[1:])
    if use_prompt:
        _apply_prompt_override(use_prompt)
    if dump:
        import prompt_loader as pl

        p = pl.build_system_prompt(mode)
        _print_prompt(p)
        print(f"[llm-cli] MD5 = {_md5(p)}")
        return
    try:
        asyncio.run(_run(mode, agent))
    except KeyboardInterrupt:
        print("\n[llm-cli] 已退出")
    except Exception as e:
        print(f"\n[llm-cli] 启动失败: {e}")


if __name__ == "__main__":
    main()