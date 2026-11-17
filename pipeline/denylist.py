#!/usr/bin/env python3
"""Competitor denylist for the public export and CI checks.

Silicon Data, Ornn and SemiAnalysis are competitors, and Compute Exchange is
a Silicon Data affiliate. None of their data may enter the ComputeNAV NAV
calculation or dashboard data (as input, or as validation/calibration).

Matching is structured, by *source identity only*:
  * source id            (exact id, or id matching a competitor name pattern)
  * URL host / domain    (host equal to, or a subdomain of, a denied domain)
  * registered name      (the source's own name field)
Free text (error messages, notes, page bodies) is never scanned, so a third
party mentioning e.g. a SemiAnalysis award does not trigger the guard.
The pipeline itself enforces upstream's COMPETITOR_EXCLUDED_SOURCES; this
module is an independent second line at export / CI time.
"""
from __future__ import annotations

import re
import urllib.parse

DENY_IDS = frozenset({
    "silicondata", "silicon_data", "sdh100rt", "sdh100", "gpu_residual_value",
    "ornn", "ornnai", "ornn_rvs", "ornn_ocpi", "ornn_depreciation",
    "semianalysis", "semi_analysis",
    "compute_exchange", "computeexchange",
})
DENY_DOMAINS = frozenset({
    "silicondata.com", "ornn.com", "ornnai.com", "semianalysis.com", "compute.exchange",
})
# Applied only to source ids and registered source names.
DENY_NAME_RE = re.compile(
    r"silicon[\s._-]?data|\bsdh100(?:rt)?\b|\bornn(?:ai)?\b|semi[\s._-]?analysis|compute[\s._-]?exchange",
    re.I,
)

ID_FIELDS = ("id", "source_id")
NAME_FIELDS = ("name", "source_name")
URL_FIELDS = ("url", "source_url", "final_url")


def host_of(url) -> str:
    try:
        h = urllib.parse.urlparse(str(url or "").strip()).hostname or ""
    except ValueError:
        return ""
    h = h.lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def match_id(v) -> str | None:
    s = str(v or "").strip().lower()
    if not s:
        return None
    if s in DENY_IDS:
        return s
    m = DENY_NAME_RE.search(s)
    return m.group(0) if m else None


def match_name(v) -> str | None:
    m = DENY_NAME_RE.search(str(v or ""))
    return m.group(0) if m else None


def match_url(v) -> str | None:
    h = host_of(v)
    for d in DENY_DOMAINS:
        if h == d or h.endswith("." + d):
            return d
    return None


def match_source(source_id=None, name=None, url=None) -> str | None:
    return match_id(source_id) or match_name(name) or match_url(url)


def source_hits(obj, where="root") -> list[str]:
    """Recursively check identity fields (id / name / URL) of any record."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                hit = (match_id(v) if k in ID_FIELDS else match_name(v) if k in NAME_FIELDS
                       else match_url(v) if k in URL_FIELDS else None)
                if hit:
                    hits.append(f"{where}.{k}={v!r} (matched {hit!r})")
            else:
                hits += source_hits(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += source_hits(v, f"{where}[{i}]")
    return hits
