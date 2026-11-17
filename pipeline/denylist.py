#!/usr/bin/env python3
"""Competitor denylist (Wilson, 2026-11-17).

Silicon Data, Ornn and SemiAnalysis are competitors. None of their data may
enter the ComputeNAV NAV calculation or dashboard data — not as an input and
not as validation or calibration. Any source id, name, domain or URL matching
a term below is refused (pipeline, CI pre-compute check, and public export).
The methodology may still describe them as competitors in prose only.
"""
from __future__ import annotations

import re

DENY_TERMS = (
    "silicondata", "silicon data", "silicon-data", "silicon_data",
    "ornn", "ornnai",
    "semianalysis", "semi analysis", "semi-analysis", "semi_analysis",
)
DENY_DOMAINS = ("silicondata.com", "ornn.com", "ornnai.com", "semianalysis.com")
_RE = re.compile("|".join(re.escape(t) for t in DENY_TERMS + DENY_DOMAINS), re.I)

# Fields that identify where a data point came from.
SOURCE_FIELDS = ("id", "source_id", "source_name", "name", "url", "source_url", "final_url", "fx_source")


def matches(text) -> str | None:
    if text is None:
        return None
    m = _RE.search(str(text))
    return m.group(0) if m else None


def source_hits(obj, where="root") -> list[str]:
    """Recursively find source-identifying fields that match the denylist."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SOURCE_FIELDS and isinstance(v, str) and matches(v):
                hits.append(f"{where}.{k}={v!r}")
            hits += source_hits(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += source_hits(v, f"{where}[{i}]")
    return hits


def text_hits(obj, where="root") -> list[str]:
    """Any string anywhere (used for public JSON, which must not name them)."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if matches(k):
                hits.append(f"{where} key {k!r}")
            hits += text_hits(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += text_hits(v, f"{where}[{i}]")
    elif isinstance(obj, str) and matches(obj):
        hits.append(f"{where}={obj[:80]!r}")
    return hits
