#!/usr/bin/env python3
"""Pre-publish check for everything that is served publicly.

Fails (exit 1) if any public file contains
  * a non-USD currency amount: a currency symbol (EUR/GBP/JPY/CNY/INR/KRW...
    symbols) anywhere, or an ISO code such as EUR, GBP, CNY, JPY next to a
    number, or a JSON key naming a non-USD rate (e.g. usd_per_eur) with a
    numeric value;
  * non-English (CJK) text.

Usage:  python3 pipeline/check_public.py            # checks docs/ and README.md
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODES = r"(?:EUR|GBP|CNY|RMB|JPY|CAD|AUD|CHF|HKD|SGD|INR|KRW|SEK|NOK|DKK|PLN|MXN|BRL|TWD|NZD|ZAR|RUB|TRY)"
SYMBOLS = "€£¥₹₩₽₺₪₫₱"
NUM = r"[0-9][0-9,.]*"
PATTERNS = [
    re.compile(f"[{SYMBOLS}]"),
    re.compile(rf"\b{CODES}\s*{NUM}"),
    re.compile(rf"{NUM}\s*{CODES}\b"),
    re.compile(rf"\b(?:C|A|HK|S|NZ)\$\s*{NUM}"),
]
CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
RATE_KEY = re.compile(rf"(?i)(?:^|_)(?:{CODES.lower()[3:-1]})(?:_|$)")


def text_problems(text: str, where: str) -> list[str]:
    out = []
    for pat in PATTERNS:
        for m in pat.finditer(text):
            out.append(f"{where}: non-USD amount {m.group(0)!r}")
    m = CJK.search(text)
    if m:
        out.append(f"{where}: non-English text {text[max(0, m.start()-10):m.end()+10]!r}")
    return out


def json_problems(obj, where: str) -> list[str]:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if RATE_KEY.search(k) and isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(f"{where}.{k}: non-USD rate/amount field")
            out += json_problems(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += json_problems(v, f"{where}[{i}]")
    elif isinstance(obj, str):
        out += text_problems(obj, where)
    return out


def public_files(root: Path = ROOT):
    yield root / "README.md"
    for p in sorted((root / "docs").rglob("*")):
        if p.is_file() and p.suffix.lower() in {".html", ".md", ".js", ".json", ".css", ".txt"}:
            yield p


def check(root: Path = ROOT) -> list[str]:
    problems = []
    for p in public_files(root):
        rel = str(p.relative_to(root))
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            problems += json_problems(json.loads(text), rel)
        else:
            problems += text_problems(text, rel)
    return problems


def main():
    probs = check()
    for p in probs:
        print("PUBLIC CHECK FAILED:", p)
    if probs:
        return 1
    print("public check OK: USD only, English only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
