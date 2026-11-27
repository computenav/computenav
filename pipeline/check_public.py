#!/usr/bin/env python3
"""Pre-publish check for everything that is served publicly.

Fails (exit 1) if any public file contains
  * a non-USD currency amount: a currency symbol (EUR/GBP/JPY/CNY/INR/KRW...
    symbols) anywhere, or an ISO code such as EUR, GBP, CNY, JPY next to a
    number, or a JSON key naming a non-USD rate (e.g. usd_per_eur) with a
    numeric value;
  * non-English (CJK) text;
  * (with --private-latest) any value that the small-sample rule hides today,
    e.g. a method leg with n<3, a derived rent median / new-card anchor, or a
    NAV / composite that would let one be backed out. This covers the
    composite members and the separate sub-indices (AMD Instinct MI300X).

Usage:  python3 pipeline/check_public.py            # checks docs/ and README.md
        python3 pipeline/check_public.py --private-latest <run>/output/latest.json
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


LEGS = ("market", "income", "cost")
DERIVED = {"income": ("rental_median_usd_per_hour",), "cost": ("new_card_usd",)}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def hidden_values(priv: dict, pub: dict) -> list[tuple[str, float]]:
    """Private values that the public latest marks as hidden today."""
    out = []
    for k, pm in (pub.get("models") or {}).items():
        if not pm:
            continue
        m = (priv.get("models") or {}).get(k) or {}
        ins = pm.get("insufficient_sample") or {}
        for leg in LEGS:
            if ins.get(leg):
                for f in (f"{leg}_usd",) + DERIVED.get(leg, ()):
                    if _num(m.get(f)):
                        out.append((f"{k}.{f}", float(m[f])))
        if pm.get("nav_hidden") and _num(m.get("nav_indicative_usd")):
            out.append((f"{k}.nav_indicative_usd", float(m["nav_indicative_usd"])))
        # SEA basket members (private navs live under models.SEA.sea_members).
        pmem, vmem = pm.get("sea_members") or {}, m.get("sea_members") or {}
        for mk, ps in pmem.items():
            vs = vmem.get(mk) or {}
            ins = (ps or {}).get("insufficient_sample") or {}
            for leg in LEGS:
                if ins.get(leg):
                    for f in (f"{leg}_usd",) + DERIVED.get(leg, ()):
                        if _num(vs.get(f)):
                            out.append((f"{k}.{mk}.{f}", float(vs[f])))
            if (ps or {}).get("nav_hidden") and _num(vs.get("nav_indicative_usd")):
                out.append((f"{k}.{mk}.nav_indicative_usd", float(vs["nav_indicative_usd"])))
    pc, c = pub.get("composite") or {}, priv.get("composite") or {}
    if pc.get("nav_hidden") and _num(c.get("nav_indicative_usd")):
        out.append(("composite.nav_indicative_usd", float(c["nav_indicative_usd"])))
    return out


def _numbers(obj, path="", skip=None):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _numbers(v, f"{path}.{k}", skip)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _numbers(v, f"{path}[{i}]", skip)
    elif _num(obj):
        yield path, float(obj)


def _variants(v: float) -> set[str]:
    return {f"{v:.2f}", f"{v:,.2f}", repr(round(v, 2)), f"{v:,.2f}".rstrip("0").rstrip(".")}


def leak_problems(priv: dict, root: Path = ROOT) -> list[str]:
    """Fail if a value hidden by the small-sample rule appears in any public file.
    A hidden value that coincides exactly with a value legitimately published
    in today's public latest.json is reported as a warning only (collision)."""
    pub = json.loads((root / "docs" / "data" / "latest.json").read_text(encoding="utf-8"))
    hidden = hidden_values(priv, pub)
    # Legitimately published value fields of today's latest (for collisions).
    fields = ("nav_indicative_usd", "market_usd", "income_usd", "cost_usd",
              "rental_median_usd_per_hour", "new_card_usd")
    published = {float(m[f]) for m in (pub.get("models") or {}).values() if m
                 for f in fields if _num(m.get(f))}
    if _num((pub.get("composite") or {}).get("nav_indicative_usd")):
        published.add(float(pub["composite"]["nav_indicative_usd"]))
    for m in (pub.get("models") or {}).values():
        for sm in ((m or {}).get("sea_members") or {}).values():
            for f in fields:
                if _num((sm or {}).get(f)):
                    published.add(float(sm[f]))
    probs = []
    for name, hv in hidden:
        if any(abs(hv - pv) < 0.005 for pv in published):
            # Only possible if the same number is legitimately published elsewhere.
            print(f"PUBLIC CHECK WARNING: hidden {name} equals a published value; skipped", file=sys.stderr)
            continue
        for p in public_files(root):
            rel = str(p.relative_to(root))
            text = p.read_text(encoding="utf-8")
            if p.suffix == ".json":
                for path, v in _numbers(json.loads(text)):
                    # Sample counts, HTTP status and placeholder parameters are not prices.
                    # A hidden rent median of 4.005 would otherwise "match" income_n=4
                    # because abs(4 - 4.005) < 0.005 in IEEE float.
                    leaf = path.rsplit(".", 1)[-1]
                    if leaf in {
                        "market_n", "income_n", "cost_n", "n_models", "observation_count",
                        "official_observation_count", "sold_count_today", "schema_version",
                        "legs_present", "legs_used", "http_status", "min_n",
                        "remaining_life_years", "book_life_years", "hours_per_year",
                    } or leaf.endswith("_n") or leaf.endswith("_count") or leaf.endswith("_years"):
                        continue
                    if abs(v - hv) < 0.005:
                        probs.append(f"{rel}{path}: hidden value {name} published")
            for s in _variants(hv):
                if len(s.replace(",", "").replace(".", "")) >= 4 and re.search(rf"(?<![0-9.,]){re.escape(s)}(?![0-9])", text):
                    probs.append(f"{rel}: hidden value {name} appears as text {s!r}")
    return sorted(set(probs))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--private-latest", help="private run output/latest.json; enables the hidden-value leak check")
    a = ap.parse_args(argv)
    probs = check()
    if a.private_latest:
        probs += leak_problems(json.loads(Path(a.private_latest).read_text(encoding="utf-8")))
    for p in probs:
        print("PUBLIC CHECK FAILED:", p)
    if probs:
        return 1
    print("public check OK: USD only, English only" + (", no hidden (n<3) values" if a.private_latest else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
