# -*- coding: utf-8 -*-
"""Extract text from the two PDFs into UTF-8 txt files with page markers."""
import sys
from pypdf import PdfReader

JOBS = [
    (r"G:\hello\学习资料\Moshi.pdf", r"G:\hello\agent-ai语音\_pdf_extract\Moshi.txt"),
    (r"G:\hello\学习资料\2025.naacl-long.484.pdf", r"G:\hello\agent-ai语音\_pdf_extract\Behavior-SD.txt"),
]

for src, dst in JOBS:
    reader = PdfReader(src)
    parts = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            text = f"[EXTRACT ERROR page {i+1}: {e}]"
        parts.append(f"\n===== PAGE {i+1}/{len(reader.pages)} =====\n{text}")
    full = "".join(parts)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(full)
    print(f"{dst}: pages={len(reader.pages)} chars={len(full)}")