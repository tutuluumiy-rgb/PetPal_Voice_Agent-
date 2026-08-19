#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KWS 唤醒词模型下载脚本（本机运行，需联网）
=========================================================================
把 sherpa-onnx KWS 模型拉到 frontend/resources/kws/（主进程 sherpa-onnx-node 读取）：
  encoder/decoder/joiner.onnx + tokens.txt + keywords 表

设计原则：
  - 默认直接用「官方 JS 示例给出的确切 URL」（release tag `kws-models`
    sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2），不再猜 HF repo id。
  - `--list` 列全部 release 资产；`--download '<精确资产名>'` 手动指定。

使用：
    cd frontend
    python scripts/download_kws.py          # 默认下载官方 KWS 模型
    python scripts/download_kws.py --list   # 只列资产清单（核对命名）
    python scripts/download_kws.py --download '<精确资产名>'   # 手动指定
"""

import json
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

# ------------------------------ 配置 ------------------------------
REPO_URL = "https://api.github.com/repos/k2-fsa/sherpa-onnx"
# 目标目录：主进程 KWS 读取的模型目录（main/kws.ts 的 modelDir() 首选）。
# 模型仅供主进程原生读取，不入 renderer public 静态资源。
DEST = Path(__file__).resolve().parents[1] / "resources" / "kws"

# 官方 JS 示例（keyword_spotter.html）给出的「确定可用」KWS 模型下载 URL。
# sherpa-onnx 把 KWS 模型发布在 release tag `kws-models` 下（不在主 release per_page 前 30）。
KWS_MODEL_TAR_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
)


def http_get(url: str, timeout: int = 60, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "petpal-kws-downloader", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                import time
                time.sleep(2)
    raise last


def http_download(url: str, dest: Path, timeout: int = 600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)  # 确保目标目录存在
    req = urllib.request.Request(url, headers={"User-Agent": "petpal-kws-downloader"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def list_release_assets() -> list:
    """拉取所有 release 的资产列表，返回 [{name, browser_download_url}]。"""
    print("[kws] 查询 sherpa-onnx GitHub Releases 资产清单 ...")
    url = REPO_URL + "/releases?per_page=30"
    data = json.loads(http_get(url))
    out = []
    for rel in data:
        for a in rel.get("assets", []):
            out.append(a)
    print(f"[kws] 共发现 {len(out)} 个 release 资产。")
    return out


def extract_kws_model(tarball: Path) -> None:
    """解压 tarball：把 encoder/decoder/joiner.onnx + tokens/keywords 拷到 DEST 根。
    兼容 sherpa 打包命名（如 encoder-epoch-0-avg-99.onnx、bpe.model 等）。"""
    DEST.mkdir(parents=True, exist_ok=True)
    print(f"[kws] 解压 {tarball.name} ...")
    extracted = []
    with tarfile.open(tarball, "r:*") as tf:
        for m in tf.getmembers():
            bname = os.path.basename(m.name)
            if not bname:
                continue
            low = bname.lower()
            fh = None
            target = None
            if low.endswith(".onnx"):
                if "encoder" in low and "decoder" not in low and "joiner" not in low:
                    target = "encoder.onnx"
                elif "decoder" in low:
                    target = "decoder.onnx"
                elif "joiner" in low:
                    target = "joiner.onnx"
            elif low in ("tokens.txt", "keywords.txt", "keywords.bpe", "keywords"):
                target = bname
            elif low.endswith(".txt") and (("token" in low) or ("keyword" in low)):
                target = bname
            if not target:
                continue
            fh = tf.extractfile(m)
            if fh:
                (DEST / target).write_bytes(fh.read())
                extracted.append(target)
                print(f"      -> {target}  (原 {bname})")
    if "encoder.onnx" not in extracted:
        # 便于诊断：列出 tarball 里到底有哪些文件
        print("[kws] 注意：未解压出 encoder.onnx。tarball 内文件清单：")
        with tarfile.open(tarball, "r:*") as tf:
            for m in tf.getmembers():
                if m.isfile():
                    print("      ", m.name)
        print("[kws] 请把上面清单回传，以便校正解压规则。")


def print_keywords() -> None:
    kw_file = None
    for name in ("keywords.txt", "keywords.bpe", "keywords"):
        cand = DEST / name
        if cand.is_file():
            kw_file = cand
            break
    if kw_file:
        print("\n===== 模型自带关键词表（从中挑一个中文词作唤醒词） =====")
        print(kw_file.read_text(encoding="utf-8", errors="replace"))
        print("========================================================")
    else:
        print("[kws] 未找到关键词表（模型含 wenetspeech KWS 时应有 keywords 表）。")


def main() -> int:
    only_list = "--list" in sys.argv
    forced = None
    if "--download" in sys.argv:
        i = sys.argv.index("--download")
        if i + 1 < len(sys.argv):
            forced = sys.argv[i + 1]

    # ---- 默认：用官方示例给出、确定可用的模型 URL 直接下载 ----
    if not only_list and not forced:
        url = KWS_MODEL_TAR_URL
        name = url.rstrip("/").split("/")[-1]
        tl = DEST / name
        print(f"[kws] 官方 KWS 模型: {name}")
        if not tl.exists():
            print("[kws] 下载中（较大，请耐心；出现 http 报错可改走 --list 查清单）...")
            http_download(url, tl)
        else:
            print(f"[kws] 已存在，跳过下载: {tl.name}")
        extract_kws_model(tl)
        if "--keep" not in sys.argv:
            try:
                os.remove(tl)
            except OSError:
                pass
        print_keywords()
        print("\n[kws] 完成（模型部分）。")
        print("  · 推理：主进程 main/kws.ts（sherpa-onnx-node，npm i sherpa-onnx-node）。")
        print("  · 从上方关键词表挑一个中文词，填入 ContextCard.vue 的 wakeKeyword。")
        print("  · 查全部 release 资产：python scripts/download_kws.py --list")
        return 0

    # ---- --list：仅列真实资产清单（便于核对命名） ----
    assets = list_release_assets()
    if only_list:
        print("\n全部 release 资产名（截断较长列表，供核对 kws 相关）：")
        for a in assets:
            n = a["name"]
            if "kws" in n.lower() or "wasm" in n.lower():
                print("   *", n)
        return 0

    # ---- --download '<精确资产名>'：从 API 清单按名下载 ----
    hit = next((a for a in assets if a["name"] == forced), None)
    if not hit:
        print(f"[kws] 未找到资产名为 '{forced}' 的发布物。可用含 kws/wasm 的资产：")
        for a in assets:
            print("   -", a["name"])
        return 1
    print(f"\n按 --download 指定下载: {hit['name']}")
    tl = DEST / hit["name"]
    if not tl.exists():
        print("[kws] 下载中……")
        http_download(hit["browser_download_url"], tl)
    extract_kws_model(tl)
    if "--keep" not in sys.argv:
        try:
            os.remove(tl)
        except OSError:
            pass
    print_keywords()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
