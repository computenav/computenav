#!/usr/bin/env python3
"""ComputeNAV v0 daily pipeline.

Fetches public sources that worked in the 2026-11-16 survey, archives raw
snapshots (append-only) with timestamp + SHA-256, normalizes observations into
SQLite, and computes *indicative* per-model NAV values.

Sold transactions are expected to be zero until licensed market-tape access
exists. Income and cost legs use clearly labeled to-be-calibrated placeholders.

Usage:
    python3 /workspace/computenav/run_daily.py
    python3 /workspace/computenav/run_daily.py --sea-trial
    # SEA sub-index is off unless COMPUTENAV_ENABLE_SEA=1 and DATA_DIR is not
    # the official /workspace/computenav tree (or COMPUTENAV_SEA_ALLOW_OFFICIAL=1).
"""

from __future__ import annotations

import csv
from fractions import Fraction
import hashlib
import os
import html as htmlmod
import json
import logging
import re
import sqlite3
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, date, timedelta, time as dt_time
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import sea_index

# CI hook (no-op by default): COMPUTENAV_DATA_DIR relocates all private outputs.
ROOT = Path(os.environ.get("COMPUTENAV_DATA_DIR") or "/workspace/computenav")
SNAPSHOT_ROOT = ROOT / "snapshots"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = ROOT / "logs"
DB_PATH = ROOT / "nav.db"
HISTORY_PATH = OUTPUT_DIR / "history.json"
LATEST_PATH = OUTPUT_DIR / "latest.json"
DASHBOARD_DATA_PATH = OUTPUT_DIR / "dashboard_data.json"

PT = ZoneInfo("America/Los_Angeles")
UA = "ComputeNAV-Research/0.1 (daily-pipeline; research@computenav.local; index research)"

METHODOLOGY_PAGE = (
    "https://cursor.com/canvas/store-b3c0dcca-8948-42ed-a766-f3a194df64ba/"
    "4d816195-f371-40f0-9682-8f228e266602"
)

# Placeholder assumptions (TO-BE-CALIBRATED). Not official parameters.
ASSUMPTIONS = {
    "status": "to-be-calibrated",
    "disclaimer_zh": "以下参数均为占位假设，未经指数委员会校准，不构成正式方法论参数。",
    "income": {
        "remaining_life_years": 4,
        "remaining_life_note": "占位：剩余经济寿命 4 年。CoreWeave 会计寿命为 6 年、IREN 质押 GPU 为 5 年，均只作参照，不等于本参数。",
        "utilization": 0.50,
        "utilization_note": "占位：利用率 50%，不得理解为已校准的市场利用率。",
        "opex_ratio": 0.25,
        "opex_note": "占位：运营成本（电费、机架、运维）按毛租金收入的 25% 扣除，未经电价或机柜成本实证。",
        "discount_rate_annual": 0.10,
        "discount_note": "占位：年折现率 10%。",
        "terminal_value": 0.0,
        "terminal_note": "占位：期末残值为 0，寿命期末不再另计 TV。",
        "hours_per_year": 8760,
        "rental_rate_rule": "取当日纳入收益法的按需/价目租金（USD/GPU-hour）的中位数；预留、现货、期限不明确的观察以及形态无法归属的观察不进入中位数。",
    },
    "cost": {
        "book_life_years_anchor": 5,
        "book_life_note": "占位：直线折旧年限取 IREN 质押 GPU 会计寿命 5 年作情景锚点（CoreWeave 计算设备为 6 年）。官方寿命待校准。",
        "unknown_age_remaining_fraction": 0.50,
        "age_note": "占位：二手观察普遍缺失机龄，成本法按「5 年直线、未知机龄取中点」使用 50% 剩余价值系数。不是已校准的残值曲线。",
        "new_price_source": "优先使用 Genius HPC 同页 New From（欧元未税经销商起价）；MI300X 成本法临时锚点取公开目录单模块 From（区间下限），不是 AMD MSRP，也不是美国渠道价。",
    },
    "market": {
        "sold_weight": "n/a (sold=0)",
        "listing_rule": "仅使用可解析的经销商挂牌（Genius HPC 在售、Creative Compute 询价）。AnchorGPU（未开放下单）、第三方汇总（Clustermelt/Mulligan）、不可售整机、以及未扣除主机部件的整机简单除法，不进入官方市场法中位数。",
        "aggregator": "unweighted median (weights to-be-calibrated)",
    },
    "composite": {
        "rule": "NVIDIA 四只子指数（H100 SXM / H100 PCIe / A100 80GB / A100 40GB）的等权平均（装机量权数待校准）。AMD Instinct MI300X 为独立子指数，不进入该综合，以保持综合历史连续。子指数指示性 NAV 为当日可得的市场法/收益法/成本法的等权平均（三法权重待校准）。",
    },
}

# Fetched and archived, but never used in official M/I/C legs or the composite.
# Methodology v0 §10: a source may enter the official calculation only if its
# licence allows benchmark/index use. sources.csv flags zero-weight / watch-only
# sources the same way. Do not skip the fetch: store them as observation-only.
OBSERVATION_ONLY_SOURCES = {
    "vast_public_feed": "方法论第 10 节：Vast.ai 公共 feed 写明用于指数/基准/衍生数据产品须向 data@vast.ai 申请许可；许可完成前不得进入官方计算，仅作研究对照。",
    "vast_bundles": "与公共 feed 相同的 Vast.ai 数据许可限制；许可完成前仅作观察。",
    "clustermelt": "sources.csv：v0 正式计算权重为 0，仅交叉核对。",
    "mulligan": "sources.csv：v0 正式计算权重为 0，仅观察（第三方转述 eBay 要价，未经本管道复验）。",
    "ebay_sold": "无许可的成交带访问；不得进入官方计算。",
    "ebay_active": "无许可的在售搜索访问；不得进入官方计算。",
    "ebay_sold_mi300x": "无许可的成交带访问（MI300X 检索）；不得进入官方计算，403 为预期，不得绕过。",
    "ebay_active_mi300x": "无许可的在售搜索访问（MI300X 检索）；不得进入官方计算，403 为预期，不得绕过。",
}

# Publication guards: a single official-source outage must not move a published NAV.
# MIN_LEG_SAMPLE_N is the *internal completeness* threshold (a leg is present when
# n >= 1). It is not the public small-sample hide rule (MIN_SAMPLE_N below,
# matching export_public.py). H100 SXM typically has market_n=1 and cost_n=1
# on a normal day, so a threshold of 3 would treat every normal day as degraded.
MIN_LEG_SAMPLE_N = 1
# Public small-sample hide rule. Used only for log redaction here; internal
# JSON / nav.db still store the full values. A method value with n < 3, any
# NAV or composite that would let such a value be backed out, and the cost
# anchor's new-card price must not appear in stdout / INFO / WARNING logs
# (GitHub Actions logs of the public repo are world-readable).
MIN_SAMPLE_N = 3
# Day-over-day |ΔNAV| / prior NAV above this, coinciding with an official source
# failure, is treated as a bad print and carried forward.
JUMP_GUARD_PCT = 0.25
LEG_SPECS = (
    ("market", "market_n", "market_usd"),
    ("income", "income_n", "income_usd"),
    ("cost", "cost_n", "cost_usd"),
)


def mark_observation_only(row: dict) -> dict:
    """Force official-leg flags off for unlicensed / watch-only sources."""
    sid = row.get("source_id")
    if sid not in OBSERVATION_ONLY_SOURCES:
        return row
    row["include_in_market"] = 0
    row["include_in_income"] = 0
    row["include_in_cost_new"] = 0
    reason = OBSERVATION_ONLY_SOURCES[sid]
    tag = "【仅观察，不进入官方计算】" + reason
    note = row.get("notes") or ""
    if "不进入官方计算" not in note:
        row["notes"] = (note + " " + tag).strip()
    return row


def is_official(row: dict, flag: str) -> bool:
    """Official v0 NVIDIA / MI300X legs. SEA rows are never official, even if a flag is set."""
    if not row.get(flag):
        return False
    if row.get("source_id") in OBSERVATION_ONLY_SOURCES:
        return False
    if sea_index.is_sea_source_id(row.get("source_id")):
        return False
    if row.get("index_family") in {"sea", "sea_ushq_compare"}:
        return False
    return True


# ---------------------------------------------------------------------------
# Competitor exclusion (hard rule)
# Silicon Data, Ornn, and SemiAnalysis data must never enter NAV calculation
# in any way — not as an input, and not as calibration, validation, or
# cross-check — and must not be stored as observations in nav.db.
# Research copies may remain in raw/ and samples/ only.
# Matching is by source id, display name, and URL/domain only, never by
# page-body mentions (a third-party site may cite SemiAnalysis in copy).
# Unlike OBSERVATION_ONLY_SOURCES, a hit is logged at ERROR and skipped
# entirely: no fetch, no snapshot write, no observation insert.
# ---------------------------------------------------------------------------
COMPETITOR_EXCLUDED_SOURCES = {
    # Explicit source ids (none of these are in SOURCE_PLAN today).
    "silicondata": "Silicon Data — 竞品数据，禁止进入 NAV 计算或观察库。",
    "silicon_data": "Silicon Data — 竞品数据，禁止进入 NAV 计算或观察库。",
    "sdh100rt": "Silicon Data SDH100RT / H100 租金指数 — 竞品数据，禁止进入 NAV 计算或观察库。",
    "sdh100": "Silicon Data H100 租金指数 — 竞品数据，禁止进入 NAV 计算或观察库。",
    "gpu_residual_value": "Silicon Data GPU Residual Value — 竞品数据，禁止进入 NAV 计算或观察库。",
    "ornn": "Ornn — 竞品数据，禁止进入 NAV 计算或观察库。",
    "ornn_rvs": "Ornn Residual Value Swap (RVS) — 竞品数据，禁止进入 NAV 计算或观察库。",
    "ornn_ocpi": "Ornn OCPI — 竞品数据，禁止进入 NAV 计算或观察库。",
    "ornn_depreciation": "Ornn Depreciation Curves — 竞品数据，禁止进入 NAV 计算或观察库。",
    "ornnai": "Ornn (ornnai.com) — 竞品数据，禁止进入 NAV 计算或观察库。",
    "semianalysis": "SemiAnalysis — 竞品数据，禁止进入 NAV 计算或观察库。",
    "semi_analysis": "SemiAnalysis — 竞品数据，禁止进入 NAV 计算或观察库。",
    # Silicon Data sister venue; market data is not licensed for index use.
    "compute_exchange": "Compute Exchange 为 Silicon Data 关联方，行情数据禁止进入 NAV 计算或观察库。",
    "american_compute": "American Compute — 竞品数据，禁止进入 NAV 计算或观察库。",
}

COMPETITOR_EXCLUDED_DOMAINS = {
    "silicondata.com",
    "ornn.com",
    "data.ornn.com",
    "ornnai.com",
    "semianalysis.com",
    "gpu-index.semianalysis.com",
    "compute.exchange",
}

# Case-insensitive patterns applied to "id + name + url" haystack.
COMPETITOR_EXCLUDED_PATTERNS = (
    (re.compile(r"silicon[\s._-]?data", re.I),
     "Silicon Data（含 SDH100RT / H100 租金指数、GPU Residual Value）— 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"sdh100rt|sdh100", re.I),
     "Silicon Data SDH100RT / H100 租金指数 — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"gpu\s*residual\s*value", re.I),
     "Silicon Data GPU Residual Value — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"\bornn(ai)?\b", re.I),
     "Ornn（含 RVS / Depreciation Curves / OCPI）— 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"residual\s*value\s*swap|\brvs\b", re.I),
     "Ornn Residual Value Swap (RVS) — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"\bocpi\b", re.I),
     "Ornn OCPI — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"depreciation\s*curves?", re.I),
     "Ornn Depreciation Curves — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"semi[\s._-]?analysis", re.I),
     "SemiAnalysis — 竞品数据，禁止进入 NAV 计算或观察库。"),
    (re.compile(r"compute[\s._-]?exchange", re.I),
     "Compute Exchange 为 Silicon Data 关联方，行情数据禁止进入 NAV 计算或观察库。"),
    (re.compile(r"\bamerican\s*compute\b", re.I),
     "American Compute — 竞品数据，禁止进入 NAV 计算或观察库。"),
)


def _registrable_domain(host: str) -> str:
    host = (host or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def competitor_exclusion_reason(
    source_id: Optional[str] = None,
    source_name: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Optional[str]:
    """Return a reason string if this source is a blocked competitor, else None."""
    sid = (source_id or "").strip()
    if sid in COMPETITOR_EXCLUDED_SOURCES:
        return COMPETITOR_EXCLUDED_SOURCES[sid]
    url = (source_url or "").strip()
    if url:
        try:
            host = _registrable_domain(urllib.parse.urlparse(url).hostname or "")
        except Exception:
            host = ""
        if host:
            for blocked in COMPETITOR_EXCLUDED_DOMAINS:
                if host == blocked or host.endswith("." + blocked):
                    return f"竞品/关联方域名 {host} — 禁止进入 NAV 计算或观察库。"
    hay = " ".join(x for x in (sid, source_name or "", url) if x)
    if hay:
        for pat, reason in COMPETITOR_EXCLUDED_PATTERNS:
            if pat.search(hay):
                return reason
    return None


def drop_competitor_observations(observations: list[dict], *, context: str = "") -> list[dict]:
    """Filter competitor rows out of an in-memory observation list. Log each drop."""
    kept: list[dict] = []
    for o in observations:
        reason = competitor_exclusion_reason(
            o.get("source_id"), o.get("source_name"), o.get("source_url"),
        )
        if reason:
            log.error(
                "COMPETITOR_EXCLUDED %s source_id=%s name=%s url=%s — dropped, not used in calculation: %s",
                context or "observation",
                o.get("source_id"), o.get("source_name"), o.get("source_url"), reason,
            )
            continue
        kept.append(o)
    return kept


def scrub_competitor_rows_from_db(conn: sqlite3.Connection) -> list[dict]:
    """List and delete competitor observations in nav.db. Raw/snapshot files are left untouched.

    Also reports (but does not delete) any matching source_health / snapshots table rows.
    """
    removed: list[dict] = []
    rows = [dict(r) for r in conn.execute("SELECT * FROM observations ORDER BY id").fetchall()]
    for o in rows:
        reason = competitor_exclusion_reason(
            o.get("source_id"), o.get("source_name"), o.get("source_url"),
        )
        if not reason:
            continue
        removed.append({
            "table": "observations",
            "id": o.get("id"),
            "run_id": o.get("run_id"),
            "source_id": o.get("source_id"),
            "source_name": o.get("source_name"),
            "source_url": o.get("source_url"),
            "asof_date_pt": o.get("asof_date_pt"),
            "reason": reason,
        })
        conn.execute("DELETE FROM observations WHERE id=?", (o["id"],))
        log.error(
            "COMPETITOR_EXCLUDED observation already in nav.db id=%s run_id=%s source_id=%s "
            "url=%s — deleted from observations (raw files kept): %s",
            o.get("id"), o.get("run_id"), o.get("source_id"), o.get("source_url"), reason,
        )
    if removed:
        conn.commit()

    for table, cols in (
        ("source_health", "id, run_id, source_id, url"),
        ("snapshots", "id, run_id, source_id, url, path"),
    ):
        for r in conn.execute(f"SELECT {cols} FROM {table}").fetchall():
            d = dict(r)
            reason = competitor_exclusion_reason(d.get("source_id"), None, d.get("url"))
            if reason:
                log.error(
                    "COMPETITOR_EXCLUDED row in %s (left in place; raw files kept): %s reason=%s",
                    table, d, reason,
                )
    return removed


# NVIDIA models that form the published composite. Do not add AMD keys here:
# the composite history must stay continuous.
MODEL_KEYS = ["H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB"]
COMPOSITE_KEYS = list(MODEL_KEYS)
# Official v0 also publishes a separate AMD Instinct MI300X sub-index.
# It is computed with the same three approaches and publication guards,
# but is not a member of COMPOSITE.
OFFICIAL_INDEX_KEYS = MODEL_KEYS + ["MI300X"]
MODEL_LABELS = {
    "H100_SXM": "H100 SXM",
    "H100_PCIe": "H100 PCIe",
    "A100_80GB": "A100 80GB",
    "A100_40GB": "A100 40GB",
    "MI300X": "AMD Instinct MI300X",
    "COMPOSITE": "综合（等权，待校准）",
}

# Log redaction: same small-sample / back-out rule as export_public.py.
# Calculation results are not affected. Do not log new_card_usd at all.


def _public_n(n) -> int:
    try:
        if n is None:
            return 0
        return int(n)
    except (TypeError, ValueError):
        return 0


def _leg_present_n(n) -> bool:
    return _public_n(n) > 0


def _leg_hidden_n(n) -> bool:
    n_int = _public_n(n)
    return n_int > 0 and n_int < MIN_SAMPLE_N


def _mask_rank(rows):
    m = [list(r) for r in rows]
    rank, col, ncols = 0, 0, (len(m[0]) if m else 0)
    while rank < len(m) and col < ncols:
        piv = next((i for i in range(rank, len(m)) if m[i][col] != 0), None)
        if piv is None:
            col += 1
            continue
        m[rank], m[piv] = m[piv], m[rank]
        for i in range(len(m)):
            if i != rank and m[i][col] != 0:
                f = m[i][col] / m[rank][col]
                m[i] = [a - f * b for a, b in zip(m[i], m[rank])]
        rank += 1
        col += 1
    return rank


def _mask_identifiable(equations, n_unknowns):
    """Indices of hidden legs uniquely determined by the given equations."""
    if not equations:
        return set()
    base = _mask_rank(equations)
    out = set()
    for u in range(n_unknowns):
        e = [Fraction(int(i == u)) for i in range(n_unknowns)]
        if _mask_rank(equations + [e]) == base:
            out.add(u)
    return out


def plan_public_log_masking(ns: dict, keys=None, with_composite: bool = True) -> dict:
    """Decide which NAVs / composite may appear in logs.

    ns: {model_key: {"market": n, "income": n, "cost": n}}. Same greedy
    identifiability rule as export_public.plan_masking: a value is hidden
    in logs if publishing it would uniquely reveal a method value with
    n < MIN_SAMPLE_N.
    """
    model_keys = list(keys) if keys is not None else list(COMPOSITE_KEYS)
    legs = ("market", "income", "cost")
    unknowns = [(k, leg) for k in model_keys for leg in legs if _leg_hidden_n(ns.get(k, {}).get(leg))]
    idx = {u: i for i, u in enumerate(unknowns)}
    nu = len(unknowns)

    def nav_row(k):
        present = [leg for leg in legs if _leg_present_n((ns.get(k) or {}).get(leg))]
        if not present:
            return None
        row = [Fraction(0)] * nu
        for leg in present:
            if (k, leg) in idx:
                row[idx[(k, leg)]] += Fraction(1, len(present))
        return row

    rows = {k: nav_row(k) for k in model_keys}
    nav_models = [k for k in model_keys if rows[k] is not None]
    comp_row = None
    if nav_models:
        comp_row = [Fraction(0)] * nu
        for k in nav_models:
            comp_row = [a + b / len(nav_models) for a, b in zip(comp_row, rows[k])]

    published, plan = [], {"nav": {}, "composite": None}
    for k in model_keys:
        r = rows[k]
        if r is None:
            plan["nav"][k] = "absent"
        elif nu == 0 or not _mask_identifiable(published + [r], nu):
            published.append(r)
            plan["nav"][k] = "published"
        else:
            plan["nav"][k] = "hidden"
    if not with_composite or comp_row is None:
        plan["composite"] = "absent"
    elif nu == 0 or not _mask_identifiable(published + [comp_row], nu):
        published.append(comp_row)
        plan["composite"] = "published"
    else:
        plan["composite"] = "hidden"
    plan["hidden_legs"] = unknowns
    return plan


def _ns_from_models(models: dict) -> dict:
    out = {}
    for k, m in (models or {}).items():
        if not isinstance(m, dict):
            continue
        if k == "COMPOSITE":
            continue
        out[k] = {
            "market": _public_n(m.get("market_n")),
            "income": _public_n(m.get("income_n")),
            "cost": _public_n(m.get("cost_n")),
        }
    return out


def _ns_from_index_rows(index_rows: list[dict]) -> dict:
    return _ns_from_models({r["model_key"]: r for r in (index_rows or []) if r.get("model_key")})


def _fmt_leg_for_log(value, n) -> str:
    n_int = _public_n(n)
    if n_int <= 0:
        return "n=0"
    if n_int < MIN_SAMPLE_N:
        return f"hidden n={n_int}"
    if value is None:
        return f"n={n_int}"
    return f"{round(float(value), 2)} n={n_int}"


def _fmt_nav_for_log(value, status: str) -> str:
    """raw_nav is never logged as a number (it is never public)."""
    if status != "published" or value is None:
        return "hidden"
    return f"{round(float(value), 2)}"


def public_log_nav_status(ns: dict) -> dict:
    """Return {model_key: 'published'|'hidden'|'absent', 'COMPOSITE': ...}."""
    members = [k for k in COMPOSITE_KEYS if k in ns]
    plan = plan_public_log_masking({k: ns[k] for k in members}, keys=members, with_composite=True)
    status = dict(plan["nav"])
    status["COMPOSITE"] = plan["composite"] or "absent"
    for k in ns:
        if k in status or k == "COMPOSITE":
            continue
        sub = plan_public_log_masking({k: ns[k]}, keys=[k], with_composite=False)
        status[k] = sub["nav"].get(k, "absent")
    return status


def log_index_summary(latest: dict, *, extra: str = "") -> None:
    """INFO lines for model NAVs and the composite, redacted for public logs."""
    models = latest.get("models") or {}
    ns = _ns_from_models(models)
    status = public_log_nav_status(ns)
    if extra:
        log.info(extra)
    for k, v in models.items():
        log.info(
            "NAV %s: indicative=%s market=%s income=%s cost=%s",
            k,
            _fmt_nav_for_log(v.get("nav_indicative_usd"), status.get(k, "hidden")),
            _fmt_leg_for_log(v.get("market_usd"), v.get("market_n")),
            _fmt_leg_for_log(v.get("income_usd"), v.get("income_n")),
            _fmt_leg_for_log(v.get("cost_usd"), v.get("cost_n")),
        )
    comp = latest.get("composite")
    if comp:
        log.info("COMPOSITE=%s", _fmt_nav_for_log(comp.get("nav_indicative_usd"), status.get("COMPOSITE", "hidden")))


def log_carry_forward(key: str, raw_nav, published_nav, from_asof, reason, index_rows) -> None:
    """WARNING carry-forward line: never print raw_nav; redact published NAV."""
    ns = _ns_from_index_rows(index_rows)
    status = public_log_nav_status(ns)
    # raw_nav / raw_composite is never published; always hidden in logs.
    pub = _fmt_nav_for_log(published_nav, status.get(key, "hidden"))
    if key == "COMPOSITE":
        log.warning(
            "CARRY_FORWARD COMPOSITE raw_nav=hidden published_nav=%s (%s)",
            pub, reason,
        )
    else:
        log.warning(
            "CARRY_FORWARD %s raw_nav=hidden published_nav=%s from %s (%s)",
            key, pub, from_asof, reason,
        )


# Research collections (chip events, raw quotes, age). Not inputs to the v0 official NAV.
PRICE_HISTORY_STARTS_PT = "2026-11-16"
AMD_MODELS = ("MI355X", "MI350X", "MI325X", "MI300X", "MI250X", "MI210", "MI250", "MI350P")
AMD_VRAM_HINT = {
    "MI300X": 192, "MI325X": 256, "MI350X": 288, "MI355X": 288,
    "MI250X": 128, "MI210": 64, "MI250": 128,
}
NEW_OBS_COLS = [
    "raw_price_text", "tenor", "region", "quantity_config", "availability",
    "age_months", "manufacture_date", "first_deploy_date", "age_source",
    "age_confidence", "vendor", "snapshot_path", "in_official_v0",
]

# Official vendor/press-release calendar dates only. Quarters without a day stay in notes.
# Verified 2026-11-17 PT from the cited official pages. No invented numbers or days.
CHIP_EVENTS_SEED = [
    {
        "model": "A100", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2020-05-14",
        "source_url": "https://nvidianews.nvidia.com/news/nvidias-new-ampere-data-center-gpu-in-full-production",
        "notes": "GTC 2020 official newsroom PR. Announced the first Ampere-architecture GPU, NVIDIA A100. The same PR says the A100 'is in full production and shipping to customers worldwide.' This launch SKU is the original A100 (40GB); the 80GB SKU was announced later.",
    },
    {
        "model": "A100", "vendor": "NVIDIA", "event_type": "ship", "event_date": "2020-05-14",
        "source_url": "https://nvidianews.nvidia.com/news/nvidias-new-ampere-data-center-gpu-in-full-production",
        "notes": "Same 2020-05-14 PR: 'in full production and shipping to customers worldwide.' GA is the same calendar day; no later ship day is stated.",
    },
    {
        "model": "A100 80GB", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2020-11-16",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-doubles-down-announces-a100-80gb-gpu-supercharging-worlds-most-powerful-gpu-for-ai-supercomputing",
        "notes": "SC20 official PR unveiling the A100 80GB GPU. DGX A100 and DGX Station A100 'expected to ship this quarter' (Q4 2020); OEM HGX systems expected in the first half of 2021. No precise ship calendar day is stated, so no ship row is seeded.",
    },
    {
        "model": "H100", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2022-03-22",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-announces-hopper-architecture-the-next-generation-of-accelerated-computing",
        "notes": "GTC official PR announcing the Hopper architecture and the first Hopper-based GPU, NVIDIA H100. The PR says 'NVIDIA H100 will be available starting in the third quarter' (2022) — a quarter, not a calendar day, so no Q3 ship row is seeded.",
    },
    {
        "model": "H100", "vendor": "NVIDIA", "event_type": "ship", "event_date": "2022-09-20",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-hopper-in-full-production",
        "notes": "Official PR: the H100 'is in full production', with global partners 'planning in October to roll out the first wave of products and services'. This row records the full-production announcement date; the October partner rollout has no single calendar day and is not invented.",
    },
    {
        "model": "H200", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2023-11-13",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-supercharges-hopper-the-worlds-leading-ai-computing-platform",
        "notes": "SC23 official PR introducing the NVIDIA HGX H200 / H200 Tensor Core GPU. 'The NVIDIA H200 will be available from global system manufacturers and cloud service providers starting in the second quarter of 2024.' No precise ship calendar day is stated, so no ship row is seeded.",
    },
    {
        "model": "B200", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2024-03-18",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-blackwell-platform-arrives-to-power-a-new-era-of-computing",
        "notes": "GTC official PR announcing the Blackwell platform, including the NVIDIA B200 Tensor Core GPU and the GB200 Grace Blackwell Superchip. A same-day companion PR said DGX SuperPOD with DGX GB200 and DGX B200 systems 'are expected to be available later this year' (2024). No precise ship calendar day is seeded.",
    },
    {
        "model": "B300", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2025-03-18",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-blackwell-ultra-ai-factory-platform-paves-way-for-age-of-ai-reasoning",
        "notes": "GTC official PR announcing NVIDIA Blackwell Ultra, including the HGX B300 NVL16 system and the GB300 NVL72 rack-scale solution. No precise public ship calendar day was verified from this PR at seeding, so no ship row is seeded.",
    },
    {
        "model": "GB300", "vendor": "NVIDIA", "event_type": "announce", "event_date": "2025-03-18",
        "source_url": "https://nvidianews.nvidia.com/news/nvidia-blackwell-ultra-ai-factory-platform-paves-way-for-age-of-ai-reasoning",
        "notes": "Same 2025-03-18 Blackwell Ultra PR. GB300 NVL72 is recorded as a distinct label from the HGX B300 board. No precise ship day seeded.",
    },
    {
        "model": "MI250X", "vendor": "AMD", "event_type": "announce", "event_date": "2021-11-08",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2021-11-8-new-amd-instinct-mi200-series-accelerators-bring-.html",
        "notes": "Official AMD PR announcing the Instinct MI200 series, including MI250X as the flagship ('the world’s fastest HPC and AI accelerator'). The PR discusses Frontier as expected to deliver more than 1.5 exaflops, but does not state a precise public calendar day for general shipment, so no ship row is seeded.",
    },
    {
        "model": "MI300X", "vendor": "AMD", "event_type": "announce", "event_date": "2023-06-13",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2023-6-13-amd-expands-leadership-data-center-portfolio-with-.html",
        "notes": "Official AMD 'Data Center and AI Technology Premiere' PR. CEO remarks refer to 'the launch of our Instinct MI300 accelerators planned for later this year' and the PR shares details on the next-generation Instinct products (MI300 series, including MI300X). Later-in-2023 is not a calendar day.",
    },
    {
        "model": "MI300X", "vendor": "AMD", "event_type": "GA", "event_date": "2023-12-06",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2023-12-6-amd-delivers-leadership-portfolio-of-data-center-a.html",
        "notes": "Official AMD PR: 'Today, AMD announced the availability of the AMD Instinct MI300X accelerators.' Recorded as GA/availability (public launch). No separate unexplained day is added.",
    },
    {
        "model": "MI325X", "vendor": "AMD", "event_type": "announce", "event_date": "2024-10-10",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2024-10-10-amd-delivers-leadership-ai-performance-with-amd-in.html",
        "notes": "Official AMD Advancing AI 2024 PR announcing MI325X. 'Currently on track for production shipments in Q4 2024' and 'widespread system availability … starting in Q1 2025' are quarters, not calendar days, so no ship row is seeded. The same PR previewed the MI350 series as 'on track to be available during the second half of 2025.'",
    },
    {
        "model": "MI350X", "vendor": "AMD", "event_type": "announce", "event_date": "2024-10-10",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2024-10-10-amd-delivers-leadership-ai-performance-with-amd-in.html",
        "notes": "Same 2024-10-10 PR: official preview of the next-generation Instinct MI350 series, 'on track to be available during the second half of 2025'. Individual MI350X vs MI355X SKUs are named in the 2025-06-12 launch PR.",
    },
    {
        "model": "MI355X", "vendor": "AMD", "event_type": "announce", "event_date": "2024-10-10",
        "source_url": "https://www.amd.com/en/newsroom/press-releases/2024-10-10-amd-delivers-leadership-ai-performance-with-amd-in.html",
        "notes": "Series-level official preview on 2024-10-10 (MI350 series). The MI355X SKU name is confirmed in the 2025-06-12 launch PR; this row records the series preview date, not a fabricated SKU-level reveal day.",
    },
    {
        "model": "MI350X", "vendor": "AMD", "event_type": "launch", "event_date": "2025-06-12",
        "source_url": "https://ir.amd.com/news-events/press-releases/detail/1255/amd-unveils-vision-for-an-open-ai-ecosystem-detailing-new-silicon-software-and-systems-at-advancing-ai-2025",
        "notes": "Official AMD Advancing AI 2025 PR (AMD IR / GlobeNewswire, released June 12, 2025). CEO: 'highlighted by the launch of our AMD Instinct MI350 series accelerators.' The series consists of MI350X and MI355X. 'Set for broad availability in 2H 2025' is not a single calendar day, so no ship row is seeded.",
    },
    {
        "model": "MI355X", "vendor": "AMD", "event_type": "launch", "event_date": "2025-06-12",
        "source_url": "https://ir.amd.com/news-events/press-releases/detail/1255/amd-unveils-vision-for-an-open-ai-ecosystem-detailing-new-silicon-software-and-systems-at-advancing-ai-2025",
        "notes": "Same 2025-06-12 official launch PR covering MI350X and MI355X. Broad availability in 2H 2025 (no calendar day seeded).",
    },
    {
        "model": "MI400", "vendor": "AMD", "event_type": "announce", "event_date": "2025-06-12",
        "source_url": "https://ir.amd.com/news-events/press-releases/detail/1255/amd-unveils-vision-for-an-open-ai-ecosystem-detailing-new-silicon-software-and-systems-at-advancing-ai-2025",
        "notes": "Upcoming product officially previewed in the same 2025-06-12 PR: next-generation Instinct MI400 Series GPUs in the 'Helios' rack-scale design. The PR does not give a public calendar ship or GA day. Seeded as an official upcoming announcement, not as a fabricated availability date.",
    },
]


def detect_instinct_model(text: str) -> Optional[str]:
    """Return an Instinct model string observed in text, or None. Does not invent SKUs."""
    if not text:
        return None
    u = re.sub(r"[\s_\-]", "", str(text).upper())
    # Longer tokens first; do not map MI350P to MI350X.
    for name in ("MI355X", "MI350X", "MI350P", "MI325X", "MI300X", "MI250X", "MI210"):
        if name in u:
            return name
    if re.search(r"MI250(?!X)", u):
        return "MI250"
    return None


def is_amd_model(model: Optional[str]) -> bool:
    m = (model or "").upper().replace(" ", "")
    return m in AMD_MODELS or bool(re.match(r"^MI\d+", m))


def mark_not_in_official_v0(row: dict, reason: str) -> dict:
    """Force official-leg flags off and tag the reason. Used for AMD and other non-v0 models."""
    row["include_in_market"] = 0
    row["include_in_income"] = 0
    row["include_in_cost_new"] = 0
    row["in_official_v0"] = 0
    note = row.get("notes") or ""
    tag = "【不进入 v0 官方计算】" + reason
    if "不进入 v0 官方计算" not in note:
        row["notes"] = (note + " " + tag).strip()
    return row


def parse_age_hints(*texts: Optional[str], asof_date_pt: Optional[str] = None) -> dict:
    """Parse age/condition hints from listing text. Numeric age is set only when explicitly stated.

    'used' / 'refurbished' / warranty remaining are hints, not invented age_months.
    """
    blob = " ".join(t for t in texts if t) or ""
    if not blob.strip():
        return {
            "age_months": None, "manufacture_date": None, "first_deploy_date": None,
            "age_source": None, "age_confidence": None,
        }
    out = {
        "age_months": None, "manufacture_date": None, "first_deploy_date": None,
        "age_source": None, "age_confidence": None,
    }
    # Explicit age in months or years.
    m = re.search(r"(?:(?:age|机龄)\s*(?:is|of|:)?\s*)(\d+(?:\.\d+)?)\s*(?:months?|mos?\.?|个月)", blob, re.I)
    if m:
        out["age_months"] = float(m.group(1))
        out["age_source"] = "listing text"
        out["age_confidence"] = "high"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|\s)?(?:years?|yrs?\.?|年)\s*old", blob, re.I)
    if m and out["age_months"] is None:
        out["age_months"] = float(m.group(1)) * 12.0
        out["age_source"] = "listing text"
        out["age_confidence"] = "high"
    m = re.search(
        r"(?:manufactured(?:\s+in)?|manufacture(?:d)?\s*date|date\s*of\s*manufacture|生产(?:日期)?|出厂(?:日期)?)\s*[:\s]*((?:19|20)\d{2}(?:[-/]\d{1,2}){0,2})",
        blob, re.I,
    )
    if m:
        out["manufacture_date"] = m.group(1).replace("/", "-")
        out["age_source"] = out["age_source"] or "listing text"
        if out["age_confidence"] != "high":
            out["age_confidence"] = "medium"
    m = re.search(
        r"(?:first\s+deploy(?:ed|ment)?|deployed(?:\s+in)?|首次部署|投入(?:使用)?)\s*[:\s]*((?:19|20)\d{2}(?:[-/]\d{1,2}){0,2})",
        blob, re.I,
    )
    if m:
        out["first_deploy_date"] = m.group(1).replace("/", "-")
        out["age_source"] = "listing text" if "deploy" in (m.group(0) or "").lower() or "部署" in m.group(0) else (out["age_source"] or "listing text")
        if "seller" in blob.lower() or "卖家" in blob:
            out["age_source"] = "seller statement"
        if out["age_confidence"] != "high":
            out["age_confidence"] = "medium"
    # Serial-number claims (only if the text itself says the age/date came from a serial).
    if re.search(r"\bserial\b|\bS/?N\b|序列号", blob, re.I) and (out["manufacture_date"] or out["age_months"] is not None):
        out["age_source"] = "serial"
    # Condition / warranty / cluster-pull hints: record source, never invent months.
    hint = bool(re.search(
        r"(?<![A-Za-z])(?:used|refurbished|refurb|pull(?:ed)?\s+from(?:\s+(?:a|the))?\s+cluster|seller[_\s-]?refurbished|used[_\s-]?tested)(?![A-Za-z])|翻新|二手|拆机|集群拆下|下架|已测",
        blob, re.I,
    ))
    warrant = re.search(r"(\d+)\s*[- ]?(?:month|mos?\.?|个月)\s*(?:warranty|质保)", blob, re.I)
    if out["age_months"] is None and out["manufacture_date"] is None and out["first_deploy_date"] is None:
        if hint or warrant:
            out["age_source"] = "listing text"
            out["age_confidence"] = "low"
            # warranty remaining is a hint only; original warranty length is unknown, so age_months stays null.
    return out


def infer_tenor(row: dict) -> Optional[str]:
    if row.get("tenor"):
        return row["tenor"]
    blob = " ".join(str(x) for x in (
        row.get("raw_label"), row.get("notes"), row.get("observation_type"), row.get("sold_vs_listed"),
    ) if x)
    low = blob.lower()
    if re.search(r"spot|现货", low):
        return "spot"
    if re.search(r"reserv|预留|12\s*mo|12\s*month|1-year|1 year|2 weeks|期限", low):
        if re.search(r"tenor unconfirmed|期限（按需/预留）未|期限未确认", blob, re.I):
            return "unconfirmed"
        return "reserved"
    if re.search(r"on[- ]?demand|按需", low):
        return "on-demand"
    if re.search(r"term\b|合同|committed", low):
        return "term"
    if re.search(r"tenor unconfirmed|期限未确认", low):
        return "unconfirmed"
    return None


def infer_raw_price_text(row: dict) -> Optional[str]:
    if row.get("raw_price_text"):
        return row["raw_price_text"]
    label = row.get("raw_label") or ""
    m = re.search(r"(€\s?[0-9,]+(?:\.\d+)?|£\s?[0-9,]+(?:\.\d+)?|\$\s?[0-9,]+(?:\.\d+)?(?:\s*/\s*(?:hr|hour|sec|GPU-hr|GPU/hour))?)", label)
    if m:
        return m.group(1).strip()
    if row.get("price") is None:
        return None
    ccy = (row.get("currency") or "").upper()
    price = row["price"]
    unit = row.get("unit") or ""
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(ccy, ccy + " ")
    suffix = ""
    if "hour" in unit or unit.endswith("_hour"):
        suffix = "/hr"
    elif unit == "per_second":
        suffix = "/sec"
    try:
        if float(price) >= 100:
            return f"{sym}{float(price):,.2f}{suffix}"
        return f"{sym}{float(price)}{suffix}"
    except (TypeError, ValueError):
        return None


def infer_availability(row: dict) -> Optional[str]:
    if row.get("availability"):
        return row["availability"]
    blob = " ".join(str(x) for x in (row.get("notes"), row.get("raw_label"), row.get("observation_type")) if x)
    m = re.search(r"stock=([A-Za-z]+)", blob)
    if m:
        return m.group(1)
    m = re.search(r"available=(True|False|true|false)", blob)
    if m:
        return "available" if m.group(1).lower() == "true" else "unavailable"
    if re.search(r"contact sales|联系销售", blob, re.I):
        return "contact_sales"
    if re.search(r"in stock|在库", blob, re.I):
        return "in_stock"
    if "listed_unavailable" in (row.get("observation_type") or ""):
        return "unavailable"
    return None


def infer_quantity_config(row: dict) -> Optional[str]:
    if row.get("quantity_config"):
        return row["quantity_config"]
    unit = row.get("unit") or ""
    if unit == "per_8gpu_server":
        return "8-gpu server"
    if unit == "per_8gpu_hour":
        return "8-gpu node; per-card hour derived"
    blob = " ".join(str(x) for x in (row.get("notes"), row.get("raw_label")) if x)
    m = re.search(r"num_gpus=(\d+)", blob)
    if m:
        return f"{m.group(1)} gpu"
    m = re.search(r"(\d+)\s*[×x]\s*(?:H100|A100|GPU|gpu)", blob)
    if m:
        return f"{m.group(1)}-gpu"
    m = re.search(r"Choose between (\d+) or (\d+) GPU", blob, re.I)
    if m:
        return f"{m.group(1)} or {m.group(2)} GPUs per droplet"
    return None


def infer_region(row: dict) -> Optional[str]:
    if row.get("region"):
        return row["region"]
    blob = " ".join(str(x) for x in (row.get("notes"), row.get("raw_label")) if x)
    m = re.search(r"\bloc=([^。;]+)", blob)
    if m:
        return m.group(1).strip()
    m = re.search(r"\(([A-Za-z .]+,\s*[A-Z]{2})\)", row.get("raw_label") or "")
    if m:
        return m.group(1).strip()
    return None


def set_in_official_v0(row: dict) -> dict:
    official = (
        row.get("model_key") in OFFICIAL_INDEX_KEYS
        and row.get("source_id") not in OBSERVATION_ONLY_SOURCES
        and not sea_index.is_sea_source_id(row.get("source_id"))
        and row.get("index_family") not in {"sea", "sea_ushq_compare"}
        and (
            bool(row.get("include_in_market"))
            or bool(row.get("include_in_income"))
            or bool(row.get("include_in_cost_new"))
        )
    )
    row["in_official_v0"] = 1 if official else 0
    return row


def enrich_observation(row: dict, snapshot_path: Optional[str] = None) -> dict:
    """Fill research fields without changing official-leg prices, keys, or inclusion flags for v0 models."""
    if is_amd_model(row.get("model")) or (row.get("model_key") in AMD_MODELS):
        row["vendor"] = row.get("vendor") or "AMD"
        # MI300X is an official v0 sub-index (not in the NVIDIA composite).
        # Other Instinct SKUs stay research-only.
        if row.get("model_key") != "MI300X":
            mark_not_in_official_v0(row, "AMD 观察，仅作研究/校准，不进入 v0 官方计算。")
        if not row.get("form_factor") and row.get("model") in {"MI300X", "MI325X", "MI350X", "MI355X", "MI250X", "MI250"}:
            # OAM is the usual Instinct form, but only fill when the source already implied it or left it blank
            # after a parser that knew the form. Leave blank rather than invent if truly unknown.
            pass
        if row.get("vram_gb") is None and row.get("model") in AMD_VRAM_HINT:
            # Do not invent VRAM from a spec sheet if the source did not state it.
            pass
    elif (row.get("model") or "").upper() in {"H100", "A100", "H200", "B200", "B300", "GB300"}:
        row["vendor"] = row.get("vendor") or "NVIDIA"
    if not row.get("tenor"):
        row["tenor"] = infer_tenor(row)
    if not row.get("raw_price_text"):
        row["raw_price_text"] = infer_raw_price_text(row)
    if not row.get("availability"):
        row["availability"] = infer_availability(row)
    if not row.get("quantity_config"):
        row["quantity_config"] = infer_quantity_config(row)
    if not row.get("region"):
        row["region"] = infer_region(row)
    age = parse_age_hints(
        row.get("raw_label"), row.get("notes"), row.get("condition"),
        asof_date_pt=row.get("asof_date_pt"),
    )
    for k, v in age.items():
        if row.get(k) in (None, ""):
            row[k] = v
    if snapshot_path and not row.get("snapshot_path"):
        row["snapshot_path"] = snapshot_path
    set_in_official_v0(row)
    return row


def observation_natural_key(row: dict) -> tuple:
    def _n(v):
        if v is None:
            return None
        if isinstance(v, float):
            return round(v, 10)
        return v
    return (
        row.get("source_id"),
        row.get("observation_type"),
        row.get("raw_label"),
        _n(row.get("price")),
        row.get("unit"),
        row.get("model"),
        row.get("form_factor"),
        row.get("vram_gb"),
    )


def migrate_schema(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    col_sql = {
        "raw_price_text": "TEXT",
        "tenor": "TEXT",
        "region": "TEXT",
        "quantity_config": "TEXT",
        "availability": "TEXT",
        "age_months": "REAL",
        "manufacture_date": "TEXT",
        "first_deploy_date": "TEXT",
        "age_source": "TEXT",
        "age_confidence": "TEXT",
        "vendor": "TEXT",
        "snapshot_path": "TEXT",
        "in_official_v0": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, typ in col_sql.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {typ}")
    conn.executescript(
        """
CREATE TABLE IF NOT EXISTS chip_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    vendor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_date TEXT,
    source_url TEXT,
    notes TEXT,
    verified_on_pt TEXT,
    UNIQUE(model, vendor, event_type, event_date, source_url)
);
CREATE INDEX IF NOT EXISTS idx_chip_events_date ON chip_events(event_date);
CREATE INDEX IF NOT EXISTS idx_obs_model_date ON observations(model, asof_date_pt);
"""
    )
    conn.execute(
        """
CREATE VIEW IF NOT EXISTS event_window_observations AS
SELECT
    e.id AS event_id,
    e.model AS event_model,
    e.vendor AS event_vendor,
    e.event_type,
    e.event_date,
    CAST(julianday(o.asof_date_pt) - julianday(e.event_date) AS INTEGER) AS days_from_event,
    o.id AS observation_id,
    o.run_id,
    o.asof_date_pt,
    o.model,
    o.model_key,
    o.vendor,
    o.source_id,
    o.source_name,
    o.source_url,
    o.observation_type,
    o.sold_vs_listed,
    o.tenor,
    o.form_factor,
    o.vram_gb,
    o.condition,
    o.currency,
    o.price,
    o.unit,
    o.usd_price,
    o.usd_per_card,
    o.usd_per_gpu_hour,
    o.include_in_market,
    o.include_in_income,
    o.include_in_cost_new,
    o.in_official_v0,
    o.snapshot_hash,
    o.snapshot_path,
    CASE WHEN o.asof_date_pt >= '2026-11-16' THEN 1 ELSE 0 END AS within_observed_history,
    'Price history only exists from 2026-11-16 PT onward; earlier window days are not backfilled.' AS history_note
FROM chip_events e
JOIN observations o
  ON e.event_date IS NOT NULL
 AND o.asof_date_pt BETWEEN date(e.event_date, '-90 days') AND date(e.event_date, '+180 days')
 AND (
        o.model = e.model
     OR (e.model = 'A100 80GB' AND o.model_key = 'A100_80GB')
     OR (e.model = 'A100' AND o.model = 'A100')
     OR (e.model = 'H100' AND o.model = 'H100')
     OR (e.model IN ('B300','GB300') AND o.model IN ('B300','GB300'))
     OR (e.model = o.model_key)
     OR (e.model = 'MI400' AND o.model LIKE 'MI400%')
 )
WHERE e.event_date IS NOT NULL;
"""
    )
    idx_existing = {r[1] for r in conn.execute("PRAGMA table_info(daily_index)").fetchall()}
    idx_col_sql = {
        "carried_forward": "INTEGER NOT NULL DEFAULT 0",
        "stale": "INTEGER NOT NULL DEFAULT 0",
        "carry_reason": "TEXT",
        "carried_from_asof": "TEXT",
        "legs_present": "INTEGER",
        "official_source_ok_n": "INTEGER",
        "raw_nav_indicative_usd": "REAL",
        "failed_sources_json": "TEXT",
        "index_run_id": "TEXT",
    }
    for col, typ in idx_col_sql.items():
        if col not in idx_existing:
            conn.execute(f"ALTER TABLE daily_index ADD COLUMN {col} {typ}")
    conn.commit()


def seed_chip_events(conn: sqlite3.Connection, verified_on_pt: str = "2026-11-17") -> int:
    q = (
        "INSERT OR IGNORE INTO chip_events "
        "(model, vendor, event_type, event_date, source_url, notes, verified_on_pt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)"
    )
    rows = [
        (e["model"], e["vendor"], e["event_type"], e["event_date"], e["source_url"], e["notes"], verified_on_pt)
        for e in CHIP_EVENTS_SEED
    ]
    before = conn.execute("SELECT COUNT(*) FROM chip_events").fetchone()[0]
    conn.executemany(q, rows)
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM chip_events").fetchone()[0]
    return after - before


def export_raw_quotes(conn: sqlite3.Connection, asof_date_pt: str, run_id: Optional[str] = None) -> Path:
    """Write every fetched observation (official and observation-only) for the date."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"raw_quotes_{asof_date_pt}.csv"
    if run_id:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE run_id=? ORDER BY source_id, id", (run_id,)
        ).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE asof_date_pt=? ORDER BY run_id, source_id, id",
            (asof_date_pt,),
        ).fetchall()]
    fields = [
        "id", "run_id", "asof_date_pt", "observed_at_utc",
        "source_id", "source_name", "source_url",
        "observation_type", "sold_vs_listed",
        "vendor", "model", "model_key", "form_factor", "vram_gb",
        "condition", "raw_price_text", "currency", "price", "unit",
        "usd_price", "usd_per_card", "usd_per_gpu_hour",
        "fx_usd_per_unit", "fx_source", "fx_asof_utc",
        "tenor", "region", "quantity_config", "availability",
        "age_months", "manufacture_date", "first_deploy_date", "age_source", "age_confidence",
        "include_in_market", "include_in_income", "include_in_cost_new", "in_official_v0",
        "raw_label", "notes", "snapshot_hash", "snapshot_path", "snapshot_id",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
    log.info("wrote raw quotes %s rows=%s", path, len(rows))
    return path


def write_event_windows(conn: sqlite3.Connection, asof_date_pt: str) -> Path:
    """For each chip event, emit T-90..T+180 daily per-model series from observations.

    Days before PRICE_HISTORY_STARTS_PT are included as calendar placeholders with
    null prices and within_observed_history=0. No invented backfill.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"event_windows_{asof_date_pt}.json"
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM chip_events WHERE event_date IS NOT NULL ORDER BY event_date, vendor, model, event_type"
    ).fetchall()]
    # Daily aggregates already observed.
    obs_rows = [dict(r) for r in conn.execute(
        """
        SELECT asof_date_pt, model, model_key, vendor,
               COUNT(*) AS n,
               SUM(CASE WHEN usd_per_card IS NOT NULL THEN 1 ELSE 0 END) AS n_card,
               SUM(CASE WHEN usd_per_gpu_hour IS NOT NULL THEN 1 ELSE 0 END) AS n_rental,
               AVG(usd_per_card) AS mean_usd_per_card,
               AVG(usd_per_gpu_hour) AS mean_usd_per_gpu_hour,
               MIN(usd_per_card) AS min_usd_per_card,
               MAX(usd_per_card) AS max_usd_per_card,
               MIN(usd_per_gpu_hour) AS min_usd_per_gpu_hour,
               MAX(usd_per_gpu_hour) AS max_usd_per_gpu_hour
        FROM observations
        GROUP BY asof_date_pt, model, model_key, vendor
        """
    ).fetchall()]

    def matches_event(e, o):
        if e["model"] == "A100 80GB":
            return o.get("model_key") == "A100_80GB" or (o.get("model") == "A100" and o.get("model_key") == "A100_80GB")
        if e["model"] == "A100":
            return o.get("model") == "A100"
        if e["model"] == "H100":
            return o.get("model") == "H100"
        if e["model"] in {"B300", "GB300"}:
            return o.get("model") in {"B300", "GB300"} or (o.get("model_key") or "") in {"B300", "GB300"}
        if e["model"] == "MI400":
            return (o.get("model") or "").startswith("MI400")
        return o.get("model") == e["model"] or o.get("model_key") == e["model"]

    start_hist = date.fromisoformat(PRICE_HISTORY_STARTS_PT)
    payload = {
        "asof_date_pt": asof_date_pt,
        "price_history_starts_pt": PRICE_HISTORY_STARTS_PT,
        "history_note_zh": "价格历史仅从 2026-11-16 PT（管线首次观察日）起存在；事件窗口 T-90 至 T+180 中早于该日的日期保留为日历位，价格为空，不做发明式回填。",
        "wayback_option_zh": (
            "可选但本次未实施：若某一已许可的官方经销商或租赁商公开页面在 Internet Archive Wayback Machine 上有快照，"
            "且该来源的条款允许为指数研究保存历史公开页，可用这些快照回填窗口内、2026-11-16 之前的观察。"
            "Wayback 快照仍须走与现网相同的解析、竞品排除和许可规则，并记录原始 archive.org URL 与时间戳。"
            "条款不明或不允许的来源不得回填。本次没有实施回填，也没有把任何 Wayback 数据写入观察库。"
        ),
        "window": {"days_before": 90, "days_after": 180},
        "events": [],
    }
    for e in events:
        ev_d = date.fromisoformat(e["event_date"])
        series = []
        for delta in range(-90, 181):
            d = ev_d + timedelta(days=delta)
            ds = d.isoformat()
            in_hist = d >= start_hist
            day_obs = [o for o in obs_rows if o["asof_date_pt"] == ds and matches_event(e, o)] if in_hist else []
            series.append({
                "date_pt": ds,
                "days_from_event": delta,
                "within_observed_history": in_hist,
                "observations_n": int(sum(o["n"] or 0 for o in day_obs)) if day_obs else (0 if in_hist else None),
                "n_card": int(sum(o["n_card"] or 0 for o in day_obs)) if day_obs else (0 if in_hist else None),
                "n_rental": int(sum(o["n_rental"] or 0 for o in day_obs)) if day_obs else (0 if in_hist else None),
                "mean_usd_per_card": None if not day_obs else (sum((o["mean_usd_per_card"] or 0) * (o["n_card"] or 0) for o in day_obs) / sum(o["n_card"] or 0 for o in day_obs) if sum(o["n_card"] or 0 for o in day_obs) else None),
                "mean_usd_per_gpu_hour": None if not day_obs else (sum((o["mean_usd_per_gpu_hour"] or 0) * (o["n_rental"] or 0) for o in day_obs) / sum(o["n_rental"] or 0 for o in day_obs) if sum(o["n_rental"] or 0 for o in day_obs) else None),
                "models_present": sorted({(o["model_key"] or o["model"]) for o in day_obs if o.get("model") or o.get("model_key")}) if day_obs else [],
            })
        n_hist = sum(1 for p in series if p["within_observed_history"])
        n_with = sum(1 for p in series if p["within_observed_history"] and (p["observations_n"] or 0) > 0)
        payload["events"].append({
            "event_id": e["id"],
            "model": e["model"],
            "vendor": e["vendor"],
            "event_type": e["event_type"],
            "event_date": e["event_date"],
            "source_url": e["source_url"],
            "notes": e["notes"],
            "window_start_pt": series[0]["date_pt"],
            "window_end_pt": series[-1]["date_pt"],
            "days_in_observed_history": n_hist,
            "days_with_observations": n_with,
            "daily": series,
        })
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    log.info("wrote event windows %s events=%s", path, len(events))
    return path


def _snapshot_rec_for_parse(conn: sqlite3.Connection, run_id: str, source_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE run_id=? AND source_id=? ORDER BY id DESC LIMIT 1",
        (run_id, source_id),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    path = d.get("path")
    body = b""
    if path and Path(path).exists():
        body = Path(path).read_bytes()
    else:
        fallback = SNAPSHOT_ROOT / run_id / f"{source_id}.bin"
        if fallback.exists():
            body = fallback.read_bytes()
            path = str(fallback)
    if not body:
        return None
    return {
        "source_id": source_id,
        "url": d.get("url"),
        "method": d.get("method") or "GET",
        "ok": bool(d.get("ok")),
        "status": d.get("http_status"),
        "error": d.get("error"),
        "body": body,
        "sha256": d.get("sha256"),
        "byte_count": d.get("byte_count") or len(body),
        "fetched_at_utc": d.get("fetched_at_utc"),
        "path": path,
        "snapshot_id": d.get("id"),
    }


def backfill_from_snapshots(conn: sqlite3.Connection, run_id: str, fx: "FX") -> dict:
    """Re-parse archived snapshots: enrich existing rows; insert newly extracted (e.g. AMD) rows.

    Does not delete rows and does not change official v0 inclusion flags or prices on existing NVIDIA rows.
    """
    existing = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    if not existing:
        return {"updated": 0, "inserted": 0}
    asof = existing[0]["asof_date_pt"]
    by_key = {}
    for r in existing:
        by_key.setdefault(observation_natural_key(r), []).append(r)

    # Enrich existing rows in place (new columns only).
    updated = 0
    snap_paths = {r["source_id"]: r["path"] for r in conn.execute(
        "SELECT source_id, path FROM snapshots WHERE run_id=?", (run_id,)
    ).fetchall()}
    for r in existing:
        before = {c: r.get(c) for c in NEW_OBS_COLS}
        # Preserve official flags / prices / model_key.
        flags = (r.get("include_in_market"), r.get("include_in_income"), r.get("include_in_cost_new"), r.get("model_key"), r.get("price"), r.get("usd_per_card"), r.get("usd_per_gpu_hour"))
        enrich_observation(r, snapshot_path=r.get("snapshot_path") or snap_paths.get(r.get("source_id")))
        # Restore flags and prices if this is a v0 NVIDIA official-key row or any pre-existing row.
        # Existing rows keep their stored official flags and numeric prices.
        r["include_in_market"], r["include_in_income"], r["include_in_cost_new"], r["model_key"], r["price"], r["usd_per_card"], r["usd_per_gpu_hour"] = flags
        if is_amd_model(r.get("model")) and r.get("model_key") != "MI300X":
            mark_not_in_official_v0(r, "AMD 观察，仅作研究/校准，不进入 v0 官方计算。")
        set_in_official_v0(r)
        after = {c: r.get(c) for c in NEW_OBS_COLS}
        if after != before:
            sets = ", ".join(f"{c}=?" for c in NEW_OBS_COLS)
            conn.execute(
                f"UPDATE observations SET {sets} WHERE id=?",
                [r.get(c) for c in NEW_OBS_COLS] + [r["id"]],
            )
            updated += 1

    inserted = 0
    for src in active_source_plan():
        parser = src.get("parser")
        if not parser:
            continue
        rec = _snapshot_rec_for_parse(conn, run_id, src["id"])
        if not rec or not rec.get("ok"):
            continue
        try:
            rows = parser(rec, fx) or []
        except Exception:
            log.exception("backfill parse failed for %s run %s", src["id"], run_id)
            continue
        for row in rows:
            row["run_id"] = run_id
            row["snapshot_id"] = rec.get("snapshot_id")
            row["snapshot_hash"] = rec.get("sha256")
            row["snapshot_path"] = rec.get("path")
            row["observed_at_utc"] = rec.get("fetched_at_utc")
            row["asof_date_pt"] = asof
            if not row.get("source_id"):
                row["source_id"] = src["id"]
            if not row.get("source_url"):
                row["source_url"] = rec.get("url")
            mark_observation_only(row)
            enrich_observation(row, snapshot_path=rec.get("path"))
            key = observation_natural_key(row)
            if key in by_key:
                continue
            # New row (typically AMD). Never enter v0 official calc unless it is a pre-existing NVIDIA official key
            # with flags already set — new extracts that are not already stored stay research-only if AMD.
            if row.get("model_key") not in OFFICIAL_INDEX_KEYS:
                # Non-v0 keys (other Instinct SKUs, NVL, etc.) stay research-only.
                row["include_in_market"] = 0
                row["include_in_income"] = 0
                row["include_in_cost_new"] = 0
            mark_observation_only(row)
            set_in_official_v0(row)
            if competitor_exclusion_reason(row.get("source_id"), row.get("source_name"), row.get("source_url")):
                continue
            if sea_index.entity_list_exclusion_reason(row.get("source_id"), row.get("source_name"), row.get("source_url")):
                continue
            insert_observations(conn, [row])
            by_key.setdefault(key, []).append(row)
            inserted += 1
            log.info("backfill inserted %s %s", row.get("source_id"), row.get("raw_label"))
    conn.commit()
    log.info("backfill run_id=%s updated_cols=%s inserted=%s", run_id, updated, inserted)
    return {"updated": updated, "inserted": inserted}



log = logging.getLogger("computenav")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_pt(dt: datetime) -> datetime:
    return dt.astimezone(PT)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def textify(raw: bytes) -> str:
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("latin-1", "replace")
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmlmod.unescape(s)
    return re.sub(r"\s+", " ", s)


def money_to_float(s: str) -> Optional[float]:
    s = re.sub(r"[€£$USD\s]", "", str(s).strip()).replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def median(values: list[float]) -> Optional[float]:
    vals = [v for v in values if v is not None and v > 0]
    if not vals:
        return None
    return float(statistics.median(vals))


def income_dcf(r_per_hour: float) -> float:
    inc = ASSUMPTIONS["income"]
    annual_net = r_per_hour * inc["hours_per_year"] * inc["utilization"] * (1.0 - inc["opex_ratio"])
    d = inc["discount_rate_annual"]
    years = int(inc["remaining_life_years"])
    total = 0.0
    for t in range(1, years + 1):
        total += annual_net / ((1.0 + d) ** t)
    total += float(inc["terminal_value"]) / ((1.0 + d) ** years)
    return total


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    method TEXT,
    fetched_at_utc TEXT NOT NULL,
    http_status INTEGER,
    ok INTEGER NOT NULL,
    error TEXT,
    sha256 TEXT,
    byte_count INTEGER,
    path TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id INTEGER,
    snapshot_hash TEXT,
    source_id TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    observation_type TEXT,
    sold_vs_listed TEXT,
    model TEXT,
    form_factor TEXT,
    vram_gb INTEGER,
    condition TEXT,
    currency TEXT,
    price REAL,
    unit TEXT,
    usd_price REAL,
    usd_per_card REAL,
    usd_per_gpu_hour REAL,
    fx_usd_per_unit REAL,
    fx_source TEXT,
    fx_asof_utc TEXT,
    observed_at_utc TEXT,
    asof_date_pt TEXT,
    notes TEXT,
    include_in_market INTEGER NOT NULL DEFAULT 0,
    include_in_income INTEGER NOT NULL DEFAULT 0,
    include_in_cost_new INTEGER NOT NULL DEFAULT 0,
    model_key TEXT,
    raw_label TEXT
);
CREATE TABLE IF NOT EXISTS daily_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_date_pt TEXT NOT NULL,
    computed_at_utc TEXT NOT NULL,
    computed_at_pt TEXT NOT NULL,
    model_key TEXT NOT NULL,
    market_usd REAL,
    income_usd REAL,
    cost_usd REAL,
    nav_indicative_usd REAL,
    market_n INTEGER,
    income_n INTEGER,
    cost_n INTEGER,
    rental_median_usd_per_hour REAL,
    new_card_usd REAL,
    quality_flag TEXT,
    assumptions_json TEXT,
    breakdown_json TEXT,
    carried_forward INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    carry_reason TEXT,
    carried_from_asof TEXT,
    legs_present INTEGER,
    official_source_ok_n INTEGER,
    raw_nav_indicative_usd REAL,
    failed_sources_json TEXT,
    index_run_id TEXT,
    UNIQUE(asof_date_pt, model_key)
);
CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    asof_date_pt TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    error TEXT,
    observation_count INTEGER NOT NULL DEFAULT 0,
    fetched_at_utc TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_date ON observations(asof_date_pt);
CREATE INDEX IF NOT EXISTS idx_idx_date ON daily_index(asof_date_pt);
"""


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    seed_chip_events(conn)
    return conn


def fetch(name, url, method="GET", data=None, headers=None, timeout=30):
    rec = {
        "source_id": name, "url": url, "method": method, "ok": False,
        "status": None, "error": None, "content_type": None, "body": b"",
        "sha256": None, "byte_count": 0, "final_url": url,
        "fetched_at_utc": iso(now_utc()),
    }
    h = {"User-Agent": UA, "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                rec.update(ok=True, status=resp.status,
                           content_type=resp.headers.get("Content-Type"),
                           body=body, sha256=hashlib.sha256(body).hexdigest(),
                           byte_count=len(body), final_url=resp.geturl(),
                           error=None)
            break
        except urllib.error.HTTPError as e:
            body = e.read() if e.fp else b""
            rec.update(status=e.code, error=f"HTTPError {e.code} {e.reason}",
                       content_type=e.headers.get("Content-Type") if e.headers else None,
                       body=body, sha256=hashlib.sha256(body).hexdigest() if body else None,
                       byte_count=len(body))
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
                continue
            break
    return rec


def save_snapshot(run_dir: Path, rec: dict) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{rec['source_id']}.bin"
    path.write_bytes(rec.get("body") or b"")
    meta = {k: v for k, v in rec.items() if k != "body"}
    (run_dir / f"{rec['source_id']}.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    rec["path"] = str(path)
    return path


class FX:
    def __init__(self):
        self.usd_per = {"USD": 1.0}
        self.source = None
        self.asof_utc = None
        self.raw_rates = {}

    def load(self, rec: dict) -> None:
        if not rec.get("ok") or not rec.get("body"):
            raise RuntimeError(f"FX fetch failed: {rec.get('error')}")
        data = json.loads(rec["body"].decode("utf-8"))
        rates = data.get("rates") or {}
        self.raw_rates = rates
        self.source = f"{data.get('provider') or 'open.er-api.com'} ({data.get('documentation') or 'https://open.er-api.com'})"
        self.asof_utc = data.get("time_last_update_utc") or rec["fetched_at_utc"]
        for ccy, per_usd in rates.items():
            if per_usd:
                self.usd_per[ccy] = 1.0 / float(per_usd)
        self.usd_per["USD"] = 1.0

    def to_usd(self, amount: float, currency: str) -> Optional[float]:
        ccy = (currency or "USD").upper()
        if ccy not in self.usd_per:
            return None
        return float(amount) * self.usd_per[ccy]

    def rate(self, currency: str) -> Optional[float]:
        return self.usd_per.get((currency or "USD").upper())


def obs(**kwargs) -> dict:
    base = dict(
        observation_type=None, sold_vs_listed=None, model=None, form_factor=None,
        vram_gb=None, condition=None, currency=None, price=None, unit=None,
        usd_price=None, usd_per_card=None, usd_per_gpu_hour=None,
        fx_usd_per_unit=None, fx_source=None, fx_asof_utc=None, notes="",
        include_in_market=0, include_in_income=0, include_in_cost_new=0,
        model_key=None, raw_label=None, source_name=None, source_url=None, source_id=None,
        raw_price_text=None, tenor=None, region=None, quantity_config=None, availability=None,
        age_months=None, manufacture_date=None, first_deploy_date=None,
        age_source=None, age_confidence=None, vendor=None, snapshot_path=None, in_official_v0=0,
    )
    base.update(kwargs)
    return base


def attach_fx(row: dict, fx: FX) -> dict:
    ccy = row.get("currency") or "USD"
    price = row.get("price")
    row["fx_usd_per_unit"] = fx.rate(ccy)
    row["fx_source"] = fx.source
    row["fx_asof_utc"] = fx.asof_utc
    if price is not None:
        usd = fx.to_usd(price, ccy)
        row["usd_price"] = usd
        unit = row.get("unit")
        if unit == "per_card" and usd is not None:
            row["usd_per_card"] = usd
        elif unit == "per_gpu_hour" and usd is not None:
            row["usd_per_gpu_hour"] = usd
        elif unit == "per_second" and usd is not None:
            row["usd_per_gpu_hour"] = usd * 3600.0
        elif unit == "per_8gpu_hour" and usd is not None:
            row["usd_per_gpu_hour"] = usd / 8.0
    return row


def assign_model_key(row: dict) -> dict:
    model = (row.get("model") or "").upper()
    form = (row.get("form_factor") or "").upper()
    vram = row.get("vram_gb")
    key = None
    if model == "H100":
        if form in {"SXM", "SXM5", "SXM4", "HGX", "NVLINK"}:
            key = "H100_SXM"
        elif form in {"PCIE", "PCI"}:
            key = "H100_PCIe"
    elif model == "A100":
        if vram == 40:
            key = "A100_40GB"
        elif vram == 80:
            key = "A100_80GB"
    elif model == "H200":
        # SEA regional layer. Not an official v0 NVIDIA composite key.
        if form in {"NVL"}:
            key = "H200_NVL"
        elif form in {"SXM", "SXM5", "HGX", "NVLINK"}:
            key = "H200_SXM"
        else:
            key = "H200"
    else:
        inst = detect_instinct_model(model) or detect_instinct_model(row.get("model") or "")
        if inst:
            key = inst
            row["vendor"] = row.get("vendor") or "AMD"
            row["model"] = inst if not row.get("model") else row.get("model")
            if (row.get("model") or "").upper().replace(" ", "") != inst:
                row["model"] = inst
    if key in MODEL_KEYS or (row.get("model") or "").upper() in {"H100", "A100", "H200"}:
        row["vendor"] = row.get("vendor") or "NVIDIA"
    row["model_key"] = key
    return row


def parse_anchorgpu(rec, fx):
    txt = textify(rec["body"])
    out = []
    specs = [
        ("H100", "SXM", 80, r"NVIDIA H100 SXM.{0,80}\$([0-9,]+)"),
        ("H100", "PCIe", 80, r"NVIDIA H100 PCIe.{0,80}\$([0-9,]+)"),
        ("A100", "SXM", 80, r"NVIDIA A100 SXM.{0,80}\$([0-9,]+)"),
    ]
    for model, form, vram, pat in specs:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if not price:
            continue
        out.append(attach_fx(assign_model_key(obs(
            source_id="anchorgpu", source_name="AnchorGPU used catalog", source_url=rec["url"],
            observation_type="listed_reference", sold_vs_listed="listed",
            model=model, form_factor=form, vram_gb=vram, condition="used",
            currency="USD", price=price, unit="per_card",
            notes="页面写明尚未开放下单；仅作参考，不进入官方市场法中位数。",
            include_in_market=0,
            raw_label=f"AnchorGPU {model} {form} {vram}GB ${price:,.0f}",
        )), fx))
    return out


def parse_genius(rec, fx):
    txt = textify(rec["body"])
    out = []
    specs = [
        ("H100", "PCIe", 80, r"NVIDIA H100 80GB PCIe.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", False),
        ("H100", "SXM", 80, r"NVIDIA H100 SXM 80 GB.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", False),
        ("H100", "NVL", 94, r"NVIDIA H100 NVL 94 GB.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", False),
        ("A100", "PCIe", 80, r"NVIDIA A100 80GB PCIe.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", False),
        ("A100", "SXM", 80, r"NVIDIA A100 80GB SXM.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", False),
        ("H100", "SXM", 80, r"NVIDIA DGX H100 8 × H100.{0,180}?Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)", True),
    ]
    for model, form, vram, pat, is_server in specs:
        m = re.search(pat, txt)
        if not m:
            continue
        used_price = money_to_float(m.group(1))
        new_price = money_to_float(m.group(2))
        if used_price:
            if is_server:
                out.append(attach_fx(assign_model_key(obs(
                    source_id="geniushpc", source_name="Genius HPC secondary market", source_url=rec["url"],
                    observation_type="listed_server", sold_vs_listed="listed",
                    model=model, form_factor=form, vram_gb=vram, condition="used_tested",
                    currency="EUR", price=used_price, unit="per_8gpu_server",
                    notes="DGX H100 8 卡整机，未税欧元。未扣除主机部件，不进入官方单卡市场法中位数。",
                    include_in_market=0,
                    raw_label=f"Genius HPC DGX H100 €{used_price:,.2f} ex-VAT",
                )), fx))
            else:
                out.append(attach_fx(assign_model_key(obs(
                    source_id="geniushpc", source_name="Genius HPC secondary market", source_url=rec["url"],
                    observation_type="listed", sold_vs_listed="listed",
                    model=model, form_factor=form, vram_gb=vram, condition="used_tested",
                    currency="EUR", price=used_price, unit="per_card",
                    notes="未税欧元，在库/18 个月质保（以页面为准）。经销商挂牌，非成交。",
                    include_in_market=1 if form != "NVL" else 0,
                    raw_label=f"Genius HPC {model} {form} {vram}GB €{used_price:,.2f} ex-VAT used",
                )), fx))
        if new_price and not is_server:
            out.append(attach_fx(assign_model_key(obs(
                source_id="geniushpc", source_name="Genius HPC New From", source_url=rec["url"],
                observation_type="new_from_list", sold_vs_listed="new_list",
                model=model, form_factor=form, vram_gb=vram, condition="new",
                currency="EUR", price=new_price, unit="per_card",
                notes="经销商 New From 起价，未税欧元；不是 NVIDIA MSRP，也不是美国渠道价。",
                include_in_cost_new=1 if form != "NVL" else 0,
                raw_label=f"Genius HPC New From {model} {form} {vram}GB €{new_price:,.2f}",
            )), fx))
        if new_price and is_server:
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="geniushpc", source_name="Genius HPC New From", source_url=rec["url"],
                observation_type="new_from_list_server", sold_vs_listed="new_list",
                model=model, form_factor=form, vram_gb=vram, condition="new_server",
                currency="EUR", price=new_price, unit="per_8gpu_server",
                notes="DGX H100 新机 New From，8 卡整机，未扣除主机部件，不进入官方单卡成本法锚点。",
                include_in_cost_new=0,
                raw_label=f"Genius HPC New From DGX H100 €{new_price:,.2f}",
                raw_price_text=f"€{new_price:,.2f}",
                quantity_config="8-gpu server",
            )), fx)))
    # AMD Instinct *card* SKUs only. Skip chassis lines that merely list MI210/MI350P as a compatible option
    # (those prices are for a configured server, often with other GPUs installed).
    for m in re.finditer(
        r"AMD(?: Instinct)?.{0,40}?(MI(?:355X|350X|325X|300X|250X|210))\s+"
        r"(.{0,80}?)Used & tested €([0-9,.]+)\s*ex-VAT New From €([0-9,.]+)",
        txt,
    ):
        name_ctx = (m.group(0) + " " + m.group(2))
        if re.search(r"Depends on selected| / | /", name_ctx) and re.search(r"H100|L40|RTX|H200|NVL", name_ctx):
            continue
        inst = detect_instinct_model(m.group(1))
        if not inst:
            continue
        used_price = money_to_float(m.group(3))
        new_price = money_to_float(m.group(4))
        form = "PCIe" if inst == "MI210" else "OAM"
        vram = AMD_VRAM_HINT.get(inst)
        official = (inst == "MI300X")
        if used_price:
            used_notes = "未税欧元，在库/质保以页面为准。经销商挂牌，非成交。"
            if official:
                used_notes += " MI300X 二手目录单卡挂牌，进入独立子指数市场法。"
            else:
                used_notes += "【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="geniushpc", source_name="Genius HPC secondary market", source_url=rec["url"],
                observation_type="listed", sold_vs_listed="listed",
                model=inst, form_factor=form, vram_gb=vram, condition="used_tested",
                currency="EUR", price=used_price, unit="per_card", vendor="AMD",
                raw_price_text=f"€{used_price:,.2f}",
                notes=used_notes,
                include_in_market=1 if official else 0,
                raw_label=f"Genius HPC {inst} €{used_price:,.2f} ex-VAT used",
            )), fx)))
        if new_price:
            new_notes = "经销商 New From 起价，未税欧元；不是 AMD MSRP，也不是美国渠道价。"
            if official:
                new_notes += " MI300X 进入独立子指数成本法锚点。"
            else:
                new_notes += "【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="geniushpc", source_name="Genius HPC New From", source_url=rec["url"],
                observation_type="new_from_list", sold_vs_listed="new_list",
                model=inst, form_factor=form, vram_gb=vram, condition="new",
                currency="EUR", price=new_price, unit="per_card", vendor="AMD",
                raw_price_text=f"€{new_price:,.2f}",
                notes=new_notes,
                include_in_cost_new=1 if official else 0,
                raw_label=f"Genius HPC New From {inst} €{new_price:,.2f}",
            )), fx)))
    return out


def parse_creative_product(rec, fx, default):
    txt = textify(rec["body"])
    raw = rec["body"].decode("utf-8", "replace")
    prices = re.findall(r"£\s?([0-9,]+(?:\.\d+)?)", raw + " " + txt)
    if not prices:
        return []
    price = money_to_float(prices[0])
    if not price:
        return []
    is_server = default.get("unit") == "per_8gpu_server"
    row = obs(
        source_id="creativecompute", source_name="Creative Compute refurbished", source_url=rec["url"],
        observation_type="listed_enquiry" if not is_server else "listed_enquiry_server",
        sold_vs_listed="listed", model=default["model"], form_factor=default.get("form_factor"),
        vram_gb=default.get("vram_gb"), condition=default.get("condition", "refurbished"),
        currency="GBP", price=price, unit=default.get("unit", "per_card"),
        notes=default.get("notes", "未税英镑；站点声明询价不是订单。"),
        include_in_market=0 if is_server else 1,
        raw_label=default.get("raw_label") or f"Creative Compute {default['model']} £{price:,.2f}",
    )
    if is_server:
        row["notes"] = "8 卡整机询价，未扣除主机部件，不进入官方单卡市场法中位数。"
    return [attach_fx(assign_model_key(row), fx)]


def parse_spd(rec, fx):
    raw = rec["body"].decode("utf-8", "replace")
    price = None
    available = None
    block = re.search(r'"variants":\[\{(.*?)\}\]', raw, re.S)
    if block and "XE9680" in raw[max(0, block.start() - 800): block.end()]:
        b = block.group(1)
        pm = re.search(r'"price":(\d+)', b)
        am = re.search(r'"available":(true|false)', b)
        if pm:
            cents = int(pm.group(1))
            price = cents / 100.0 if cents > 10000 else float(cents)
        if am:
            available = am.group(1) == "true"
    if price is None and re.search(r"\$180,000\.00", raw):
        price = 180000.0
        am = re.search(r'"available":(true|false)', raw)
        if am:
            available = am.group(1) == "true"
    if price is None:
        return []
    return [attach_fx(assign_model_key(obs(
        source_id="serverpartdeals", source_name="ServerPartsDeals", source_url=rec["url"],
        observation_type="listed_unavailable" if available is False else "listed_server",
        sold_vs_listed="listed", model="H100", form_factor="SXM", vram_gb=80,
        condition="seller_refurbished_server", currency="USD", price=price, unit="per_8gpu_server",
        notes=f"8×H100 SXM5 整机挂牌，USD。available={available}。未扣除主机部件，不进入官方单卡市场法中位数。",
        include_in_market=0,
        raw_label=f"SPD XE9680 8xH100 ${price:,.2f} available={available}",
    )), fx)]


def parse_clustermelt(rec, fx):
    txt = textify(rec["body"])
    out = []
    specs = [
        ("H100", "NVL", 94, r"H100 NVL.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
        ("H100", "PCIe", 80, r"H100 PCIe.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
        ("H100", "SXM", 80, r"H100 SXM5.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
        ("A100", "PCIe", 80, r"A100 PCIe.{0,80}?80GB.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
        ("A100", "PCIe", 40, r"A100 PCIe.{0,80}?40GB.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
        ("A100", "SXM", 80, r"A100 SXM4.{0,250}?Market range \$([0-9,]+) to \$([0-9,]+)"),
    ]
    for model, form, vram, pat in specs:
        m = re.search(pat, txt)
        if not m:
            continue
        lo, hi = money_to_float(m.group(1)), money_to_float(m.group(2))
        if not lo or not hi:
            continue
        mid = (lo + hi) / 2.0
        out.append(attach_fx(assign_model_key(obs(
            source_id="clustermelt", source_name="Clustermelt monthly ranges", source_url=rec["url"],
            observation_type="monthly_market_range", sold_vs_listed="listed_secondary",
            model=model, form_factor=form, vram_gb=vram, condition="secondary_range",
            currency="USD", price=mid, unit="per_card",
            notes=f"月度区间 ${lo:,.0f}–${hi:,.0f} 的中点仅作记录；正式市场法权重为 0。页面自述 indicative、非报价。",
            include_in_market=0,
            raw_label=f"Clustermelt {model} {form} {vram}GB ${lo:,.0f}–${hi:,.0f}",
        )), fx))
    return out


def parse_mulligan(rec, fx):
    txt = textify(rec["body"])
    out = []
    specs = [
        ("H100", "SXM", 80, r"H100 SXM 80 GB[^$]{0,80}\$([0-9,]+)"),
        ("A100", None, 80, r"A100 80 GB \$([0-9,]+)"),
        ("A100", None, 40, r"A100 40 GB \$([0-9,]+)"),
    ]
    for model, form, vram, pat in specs:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if not price:
            continue
        out.append(attach_fx(assign_model_key(obs(
            source_id="mulligan", source_name="Mulligan eBay asking tracker", source_url=rec["url"],
            observation_type="ebay_asking_aggregator", sold_vs_listed="listed_secondary",
            model=model, form_factor=form or "unknown", vram_gb=vram, condition="used_asking",
            currency="USD", price=price, unit="per_card",
            notes="第三方转述的 eBay 最低要价，不是成交价；未经本管道在 eBay 上复验。正式市场法权重为 0。",
            include_in_market=0,
            raw_label=f"Mulligan {model} {vram}GB lowest ask ${price:,.0f}",
        )), fx))
    return out


def parse_vast_feed(rec, fx):
    data = json.loads(rec["body"].decode("utf-8"))
    out = []
    updated = data.get("updated")
    gpus = data.get("gpus") or {}
    mapping = {
        "h100 pcie": ("H100", "PCIe", 80, "H100_PCIe", True),
        "h100 sxm": ("H100", "SXM", 80, "H100_SXM", True),
        "h100 nvl": ("H100", "NVL", None, None, False),
        "a100 pcie": ("A100", "PCIe", 80, "A100_80GB", True),
        "a100 sxm4": ("A100", "SXM", None, None, False),
    }
    for key, g in gpus.items():
        if key not in mapping:
            continue
        model, form, vram, mkey, include = mapping[key]
        cur = g.get("current") or {}
        med = cur.get("median")
        if med is None:
            continue
        vram_use = vram if vram is not None else g.get("vram_gb")
        note = f"Vast 公共 feed 中位数，更新 {updated}；可租数量={cur.get('available') or g.get('available')}。用于指数须向 data@vast.ai 申请许可；许可完成前仅作观察，不进入官方收益法。"
        if key == "a100 sxm4":
            note += " feed 标 80GB，但 bundles 中部分 offer 为 40GB，故不纳入官方收益法中位数。"
            include = False
        if key == "h100 nvl":
            note += " NVL 不进入 v0 四只官方子指数。"
            include = False
        out.append(attach_fx(obs(
            source_id="vast_public_feed", source_name="Vast.ai public pricing feed", source_url=rec["url"],
            observation_type="rental_stats", sold_vs_listed="rental_list",
            model=model, form_factor=form, vram_gb=vram_use, currency="USD",
            price=float(med), unit="per_gpu_hour", notes=note,
            include_in_income=0, model_key=mkey,
            raw_label=f"Vast feed median {key} ${float(med):.4f}/hr n={cur.get('available')}",
        ), fx))
    return out


def parse_vast_bundles(rec, fx):
    if not rec.get("ok") or not rec.get("body"):
        return []
    data = json.loads(rec["body"].decode("utf-8"))
    offers = data.get("offers") or []
    out = []
    for o in offers:
        name = (o.get("gpu_name") or "").upper()
        ram_mb = o.get("gpu_ram") or 0
        vram = int(round(ram_mb / 1024.0)) if ram_mb else None
        if vram in {79, 81}:
            vram = 80
        if vram in {39, 41}:
            vram = 40
        dph = o.get("dph_total")
        n_gpu = o.get("num_gpus") or 1
        if not dph or not n_gpu:
            continue
        per = float(dph) / float(n_gpu)
        model = form = None
        if "H100" in name:
            model = "H100"
            if "NVL" in name:
                form = "NVL"
            elif "PCI" in name:
                form = "PCIe"
            elif "SXM" in name:
                form = "SXM"
        elif "A100" in name:
            model = "A100"
            if "PCI" in name:
                form = "PCIe"
            elif "SXM" in name:
                form = "SXM"
        if not model:
            continue
        loc = o.get("geolocation") or ""
        out.append(attach_fx(assign_model_key(obs(
            source_id="vast_bundles", source_name="Vast.ai bundles offers", source_url=rec["url"],
            observation_type="rental_offer", sold_vs_listed="rental_list",
            model=model, form_factor=form, vram_gb=vram, currency="USD",
            price=per, unit="per_gpu_hour",
            notes=f"offer_id={o.get('id')}; num_gpus={n_gpu}; dph_total={dph}; loc={loc}。用于指数须向 data@vast.ai 申请许可；许可完成前仅作观察，不进入官方收益法。",
            include_in_income=0,
            raw_label=f"Vast offer {name} {vram}GB ${per:.4f}/hr ({loc})",
        )), fx))
    return out


def parse_runpod(rec, fx):
    data = json.loads(rec["body"].decode("utf-8"))
    types = (data.get("data") or {}).get("gpuTypes") or []
    out = []
    for t in types:
        name = t.get("displayName") or t.get("id") or ""
        uname = name.upper()
        inst = detect_instinct_model(uname + " " + (t.get("id") or "") + " " + name)
        if "H100" not in uname and "A100" not in uname and not inst:
            continue
        if inst:
            model = inst
            form = None
            idu = (t.get("id") or "").upper()
            if "OAM" in uname or "OAM" in idu:
                form = "OAM"
            elif "PCI" in uname or "PCI" in idu:
                form = "PCIe"
            vram = t.get("memoryInGb")
            for field, label, include in (("communityPrice", "community", False), ("securePrice", "secure", False)):
                price = t.get(field)
                if not price or float(price) <= 0:
                    continue
                stock = ((t.get("lowestPrice") or {}).get("stockStatus"))
                bid = (t.get("lowestPrice") or {}).get("minimumBidPrice")
                unint = (t.get("lowestPrice") or {}).get("uninterruptablePrice")
                tenor = "spot" if (bid or (unint is not None and field == "communityPrice" and float(price) == float(unint or 0))) else "on-demand"
                official = (model == "MI300X" and label == "secure" and tenor == "on-demand")
                if official:
                    notes = (
                        f"{label} list; stock={stock}。"
                        "MI300X Secure 按需价目，进入独立子指数收益法中位数。"
                        "Community $0.50 等占位/疑似无库存价不进入官方中位数。"
                    )
                else:
                    notes = (
                        f"{label} list; stock={stock}。"
                        "Secure/Community 仅作观察。【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
                    )
                row = attach_fx(assign_model_key(obs(
                    source_id="runpod", source_name="RunPod GraphQL gpuTypes", source_url=rec["url"],
                    observation_type="rental_list", sold_vs_listed="rental_list",
                    model=model, form_factor=form or "OAM", vram_gb=vram, currency="USD",
                    price=float(price), unit="per_gpu_hour",
                    tenor=tenor,
                    availability=stock,
                    raw_price_text=f"${float(price):.2f}/hr",
                    notes=notes,
                    include_in_income=1 if official else 0,
                    vendor="AMD",
                    raw_label=f"RunPod {label} {name} {vram}GB ${float(price):.2f}/hr",
                )), fx)
                if official:
                    out.append(enrich_observation(row))
                else:
                    out.append(enrich_observation(mark_not_in_official_v0(row, "AMD 观察，仅作研究/校准，不进入 v0 官方计算。")))
            continue
        model = "H100" if "H100" in uname else "A100"
        form = None
        if "NVL" in uname:
            form = "NVL"
        elif "PCI" in uname:
            form = "PCIe"
        elif "SXM" in uname:
            form = "SXM"
        vram = t.get("memoryInGb")
        for field, label, include in (("communityPrice", "community", True), ("securePrice", "secure", False)):
            price = t.get(field)
            if not price or float(price) <= 0:
                continue
            stock = ((t.get("lowestPrice") or {}).get("stockStatus"))
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="runpod", source_name="RunPod GraphQL gpuTypes", source_url=rec["url"],
                observation_type="rental_list", sold_vs_listed="rental_list",
                model=model, form_factor=form, vram_gb=vram, currency="USD",
                price=float(price), unit="per_gpu_hour",
                tenor="on-demand",
                availability=stock,
                raw_price_text=f"${float(price):.2f}/hr",
                notes=f"{label} list; stock={stock}。Secure 不进入官方中位数，以免与 Community 重复且市场层级不同。",
                include_in_income=1 if include and form != "NVL" else 0,
                raw_label=f"RunPod {label} {name} {vram}GB ${float(price):.2f}/hr",
            )), fx)))
    return out


def _rental_row(fx, source_id, name, url, model, form, vram, price, unit="per_gpu_hour", notes="", include=True, label=None, **extra):
    observation_type = extra.pop("observation_type", "rental_list")
    sold = extra.pop("sold_vs_listed", "rental_list")
    row = obs(
        source_id=source_id, source_name=name, source_url=url,
        observation_type=observation_type, sold_vs_listed=sold,
        model=model, form_factor=form, vram_gb=vram, currency="USD",
        price=price, unit=unit, notes=notes,
        include_in_income=1 if include and form != "NVL" else 0,
        raw_label=label or f"{name} {model} {form} {vram}GB ${price}/hr",
        **extra,
    )
    return enrich_observation(attach_fx(assign_model_key(row), fx))


def parse_lambda(rec, fx):
    txt = textify(rec["body"])
    out = []
    pats = [
        ("H100", "SXM", 80, r"NVIDIA HGX H100 80 GB[^$]{0,80}\$([0-9.]+)", True, "Lambda HGX H100 instance"),
        ("H100", "SXM", 80, r"NVIDIA H100 SXM 80 GB[^$]{0,80}\$([0-9.]+)", True, "Lambda H100 SXM 1x"),
        ("A100", "SXM", 80, r"NVIDIA HGX A100 80 GB[^$]{0,80}\$([0-9.]+)", True, "Lambda HGX A100 80"),
        ("A100", "SXM", 40, r"NVIDIA HGX A100 40 GB[^$]{0,80}\$([0-9.]+)", True, "Lambda HGX A100 40"),
        ("A100", "PCIe", 40, r"NVIDIA A100 PCIe 40 GB[^$]{0,80}\$([0-9.]+)", True, "Lambda A100 PCIe 40"),
    ]
    for model, form, vram, pat, inc, lab in pats:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if not price:
            continue
        out.append(_rental_row(fx, "lambda", "Lambda public pricing", rec["url"], model, form, vram, price,
                               notes="Lambda 公开按需实例价目。", include=inc, label=f"{lab} ${price}/hr"))
    for n, pat in [
        (16, r"NVIDIA H100 2 weeks – 1 year 16 \$([0-9.]+)"),
        (64, r"NVIDIA H100 2 weeks – 1 year 64 \$([0-9.]+)"),
        (256, r"NVIDIA H100 2 weeks – 1 year 256 \$([0-9.]+)"),
    ]:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if price:
            out.append(_rental_row(fx, "lambda", "Lambda 1-Click Cluster", rec["url"], "H100", "SXM", 80, price,
                                   notes=f"预留集群（2 周–1 年，{n}×H100），不进入官方按需中位数。", include=False,
                                   label=f"Lambda cluster {n}x H100 ${price}/hr reserved"))
    return out


def parse_digitalocean(rec, fx):
    txt = textify(rec["body"])
    out = []
    m = re.search(r"NVIDIA HGX H100 \$([0-9.]+) /GPU/hour On-Demand", txt)
    if m:
        price = money_to_float(m.group(1))
        out.append(_rental_row(fx, "digitalocean", "DigitalOcean GPU droplets", rec["url"],
                              "H100", "SXM", 80, price, notes="按需价目。", include=True,
                              label=f"DO HGX H100 on-demand ${price}/hr",
                              tenor="on-demand", raw_price_text=f"${price}/GPU/hour",
                              quantity_config="1 or 8 GPUs per droplet"))
    m = re.search(r"NVIDIA HGX H100 \$([0-9.]+) /GPU/hour 12 Month Reserved", txt)
    if m:
        price = money_to_float(m.group(1))
        out.append(_rental_row(fx, "digitalocean", "DigitalOcean GPU droplets", rec["url"],
                              "H100", "SXM", 80, price, notes="12 个月预留，不进入官方按需中位数。", include=False,
                              label=f"DO HGX H100 12mo reserved ${price}/hr",
                              tenor="reserved", raw_price_text=f"${price}/GPU/hour"))
    # AMD Instinct published on the same page. Recorded as research observations only.
    for m in re.finditer(
        r"AMD Instinct[^\s]*\s+(MI[0-9]{3}X)\s+\$([0-9.]+)\s*/GPU/hour\s+(On-Demand Price|12 Month Reserved Price\*|Current Spot Price\*)",
        txt,
    ):
        model = m.group(1).upper()
        price = money_to_float(m.group(2))
        plan = m.group(3)
        if not price:
            continue
        if "Spot" in plan:
            tenor, tnote, lab = "spot", "DigitalOcean Current Spot Price，可中断。", "spot"
        elif "Reserved" in plan:
            tenor, tnote, lab = "reserved", "DigitalOcean 12 个月预留。", "12mo reserved"
        else:
            tenor, tnote, lab = "on-demand", "DigitalOcean 按需价目。", "on-demand"
        # Optional nearby memory, only if present after this match and before the next plan header.
        vram = AMD_VRAM_HINT.get(model)
        window = txt[m.end(): m.end() + 280]
        vm = re.search(r"GPU Memory\s+(\d+)\s*GB", window)
        if vm:
            vram = int(vm.group(1))
        qty = None
        qm = re.search(r"Choose between (\d+) or (\d+) GPU", window, re.I)
        if qm:
            qty = f"{qm.group(1)} or {qm.group(2)} GPUs per droplet"
        official = (model == "MI300X" and tenor == "on-demand")
        if official:
            notes = tnote + " MI300X 按需价目，进入独立子指数收益法中位数；预留与现货不进入。"
        else:
            notes = tnote + " 【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
        out.append(_rental_row(
            fx, "digitalocean", "DigitalOcean GPU droplets", rec["url"],
            model, "OAM", vram, price,
            notes=notes,
            include=official,
            label=f"DO {model} {lab} ${price}/hr",
            tenor=tenor, vendor="AMD", raw_price_text=f"${price}/GPU/hour",
            quantity_config=qty,
        ))
    return out


def parse_coreweave(rec, fx):
    txt = textify(rec["body"])
    out = []
    m = re.search(r"NVIDIA HGX H100 On-Demand Price: \$([0-9.]+) / Hour", txt)
    if m:
        node = money_to_float(m.group(1))
        out.append(attach_fx(assign_model_key(obs(
            source_id="coreweave", source_name="CoreWeave pricing", source_url=rec["url"],
            observation_type="rental_list", sold_vs_listed="rental_list",
            model="H100", form_factor="SXM", vram_gb=80, currency="USD",
            price=node, unit="per_8gpu_hour",
            notes="页面为 8×H100 节点按需价，已除以 8 折成单卡小时价。",
            include_in_income=1, raw_label=f"CoreWeave 8x HGX H100 ${node}/node-hr",
        )), fx))
    m = re.search(r"NVIDIA A100 On-Demand Price: \$([0-9.]+) / Hour", txt)
    if m:
        node = money_to_float(m.group(1))
        out.append(attach_fx(assign_model_key(obs(
            source_id="coreweave", source_name="CoreWeave pricing", source_url=rec["url"],
            observation_type="rental_list", sold_vs_listed="rental_list",
            model="A100", form_factor="unknown", vram_gb=80, currency="USD",
            price=node, unit="per_8gpu_hour",
            notes="页面为 8×A100 节点按需价，已除以 8。形态未在该行写明，按 80GB 归入 A100 80GB。",
            include_in_income=1, raw_label=f"CoreWeave 8x A100 ${node}/node-hr",
        )), fx))
    return out


def parse_crusoe(rec, fx):
    txt = textify(rec["body"])
    out = []
    m = re.search(r"NVIDIA H100 80GB HGX \$([0-9.]+) / GPU-hr \$([0-9.]+) / GPU-hr", txt)
    if m:
        a, b = money_to_float(m.group(1)), money_to_float(m.group(2))
        for price, tag in ((a, "figure-1"), (b, "figure-2")):
            if not price:
                continue
            out.append(_rental_row(fx, "crusoe", "Crusoe pricing", rec["url"], "H100", "SXM", 80, price,
                                   notes=f"页面并列两个单价（{tag}），期限（按需/预留）未在解析中确认，故不进入官方中位数。",
                                   include=False, label=f"Crusoe H100 HGX {tag} ${price}/hr (tenor unconfirmed)",
                                   tenor="unconfirmed"))
    raw_html = ""
    try:
        raw_html = rec["body"].decode("utf-8", "replace")
    except Exception:
        raw_html = txt
    out.extend(_parse_crusoe_amd(raw_html, txt, rec, fx))
    return out


def _parse_crusoe_amd(raw_html: str, txt: str, rec: dict, fx: "FX") -> list:
    """AMD rows only. Leaves the existing NVIDIA H100 parser untouched."""
    out = []
    a = raw_html.find("GPU instances pricing")
    b = raw_html.find("CPU instances pricing")
    if a >= 0 and b > a:
        chunk = raw_html[a:b]
        names = re.findall(r'<h4 class="pricing-item-heading">([^<]+)</h4>', chunk)
        vrams = re.findall(r'class="pricing-tag is-dark">([^<]*)</div>', chunk)
        price_rows = re.split(r'<div role="listitem" class="gpu-row w-dyn-item">', chunk)[1:]
        def _cell(html_frag: str) -> str:
            t = re.sub(r"<[^>]+>", " ", html_frag)
            t = re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()
            return t
        cells = [_cell(x) for x in price_rows]
        n = len(names)
        if n and len(cells) >= 2 * n and len(vrams) >= n:
            currents, news = cells[:n], cells[n:2 * n]
            for i, name in enumerate(names):
                inst = detect_instinct_model(name)
                if not inst:
                    continue
                vram = None
                vm = re.search(r"(\d+)", vrams[i] or "")
                if vm:
                    vram = int(vm.group(1))
                cur = currents[i]
                newp = news[i] if i < len(news) else ""
                if re.search(r"contact sales", cur, re.I):
                    out.append(enrich_observation(attach_fx(assign_model_key(obs(
                        source_id="crusoe", source_name="Crusoe pricing", source_url=rec["url"],
                        observation_type="rental_list", sold_vs_listed="rental_list",
                        model=inst, form_factor="OAM", vram_gb=vram, currency="USD",
                        price=None, unit="per_gpu_hour", tenor="on-demand",
                        availability="contact_sales", vendor="AMD",
                        raw_price_text="Contact sales",
                        notes="GPU instances 表：Current on-demand 为 Contact sales，无公开单价。新价列=" + newp + "。【不进入 v0 官方计算】AMD 观察，仅作研究/校准。",
                        include_in_income=0,
                        raw_label=f"Crusoe {inst} on-demand Contact sales (new={newp})",
                    )), fx)))
                    continue
                price = money_to_float(cur.split("/")[0])
                if not price:
                    continue
                official = (inst == "MI300X")
                if official:
                    notes = (
                        f"GPU instances 表：Current on-demand。New on-demand starting Dec 1, 2026 = {newp}。"
                        "期限按需。MI300X GPU 实例按需价目，进入独立子指数收益法中位数。"
                    )
                else:
                    notes = (
                        f"GPU instances 表：Current on-demand。New on-demand starting Dec 1, 2026 = {newp}。"
                        "期限按需。【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
                    )
                out.append(_rental_row(
                    fx, "crusoe", "Crusoe pricing", rec["url"], inst, "OAM", vram, price,
                    notes=notes,
                    include=official,
                    label=f"Crusoe {inst} on-demand ${price}/hr",
                    tenor="on-demand", vendor="AMD",
                    raw_price_text=cur.split()[0] if cur else f"${price}",
                    availability="listed",
                ))
    # Self-Serve Deployments / dedicated model endpoints — not GPU instance rental.
    ss = raw_html.find("Self-Serve Deployments pricing")
    se = raw_html.find('id="Self-Serve-Deployments"')
    if ss >= 0:
        chunk = raw_html[ss: se if se > ss else ss + 20000]
        for m in re.finditer(
            r'<h4 class="pricing-item-heading">(AMD [^<]+)</h4>[\s\S]{0,800}?<p>\$([0-9.]+)</p>\s*<p>/</p>\s*<p>GPU-hr</p>[\s\S]{0,400}?<p>\$([0-9.]+)</p>\s*<p>/</p>\s*<p>GPU-hr</p>',
            chunk, re.I,
        ):
            inst = detect_instinct_model(m.group(1))
            if not inst:
                continue
            cur, newp = money_to_float(m.group(2)), money_to_float(m.group(3))
            vram = AMD_VRAM_HINT.get(inst)
            tm = re.search(r'class="pricing-tag is-dark">([^<]*)</div>', m.group(0))
            if tm and re.search(r"\d+", tm.group(1)):
                vram = int(re.search(r"\d+", tm.group(1)).group(0))
            for price, tag, when in (
                (cur, "current", "Self-Serve Deployments 当前价（专用推理端点，不是 GPU 实例租金）"),
                (newp, "new-from-2026-12-01", "Self-Serve Deployments 新价 starting Dec 1, 2026（专用推理端点，不是 GPU 实例租金）"),
            ):
                if not price:
                    continue
                out.append(_rental_row(
                    fx, "crusoe", "Crusoe pricing", rec["url"], inst, "OAM", vram, price,
                    notes=when + "。【不进入 v0 官方计算】AMD 观察，仅作研究/校准；不得当作 GPU 实例按需租金进入收益法。",
                    include=False,
                    label=f"Crusoe {inst} self-serve endpoint {tag} ${price}/GPU-hr",
                    tenor="unconfirmed", vendor="AMD",
                    raw_price_text=f"${price}/GPU-hr",
                    observation_type="inference_endpoint_list",
                ))
    return out


def parse_nebius(rec, fx):
    txt = textify(rec["body"])
    m = re.search(r"NVIDIA HGX H100.{0,80}from \$([0-9.]+) \$([0-9.]+)", txt)
    out = []
    if m:
        low, listed = money_to_float(m.group(1)), money_to_float(m.group(2))
        if listed:
            out.append(_rental_row(fx, "nebius", "Nebius prices", rec["url"], "H100", "SXM", 80, listed,
                                   notes=f"页面同时出现 from ${low} 与 ${listed}；将较高者视为公开价目、纳入按需中位数，较低者可能为预留/现货，仅记录。",
                                   include=True, label=f"Nebius HGX H100 ${listed}/hr"))
        if low:
            out.append(_rental_row(fx, "nebius", "Nebius prices", rec["url"], "H100", "SXM", 80, low,
                                   notes="「from」较低价，期限未确认，不进入官方中位数。",
                                   include=False, label=f"Nebius HGX H100 from ${low}/hr"))
    return out


def parse_together(rec, fx):
    txt = textify(rec["body"])
    m = re.search(r"NVIDIA HGX H100 \$([0-9.]+) Contact sales", txt)
    if not m:
        return []
    price = money_to_float(m.group(1))
    return [_rental_row(fx, "together", "Together AI pricing", rec["url"], "H100", "SXM", 80, price,
                        notes="公开按需价目。", include=True, label=f"Together HGX H100 on-demand ${price}/hr")]


def parse_hyperstack(rec, fx):
    txt = textify(rec["body"])
    out = []
    pats = [
        ("H100", "SXM", 80, r"NVIDIA H100 SXM 80 24 240 \$([0-9.]+)", True, "on-demand"),
        ("H100", "SXM", 80, r"NVIDIA H100 NVLink 80 31 180 \$([0-9.]+)", True, "on-demand NVLink"),
        ("H100", "PCIe", 80, r"NVIDIA H100 80 28 180 \$([0-9.]+)", True, "on-demand"),
        ("A100", "SXM", 80, r"NVIDIA A100 SXM 80 24 120 \$([0-9.]+)", True, "on-demand"),
        ("A100", "SXM", 80, r"NVIDIA A100 NVLink 80 31 240 \$([0-9.]+)", True, "on-demand NVLink"),
        ("A100", "PCIe", 80, r"NVIDIA A100 80 28 120 \$([0-9.]+)", True, "on-demand"),
    ]
    for model, form, vram, pat, inc, tag in pats:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if price:
            out.append(_rental_row(fx, "hyperstack", "Hyperstack pricing", rec["url"], model, form, vram, price,
                                   notes=f"Hyperstack {tag}。", include=inc,
                                   label=f"Hyperstack {model} {form} ${price}/hr {tag}"))
    m = re.search(r"NVIDIA H100 PCIe \$([0-9.]+)", txt)
    if m:
        price = money_to_float(m.group(1))
        out.append(_rental_row(fx, "hyperstack", "Hyperstack pricing", rec["url"], "H100", "PCIe", 80, price,
                               notes="页面标注 Spot VM，不进入官方按需中位数。", include=False,
                               label=f"Hyperstack H100 PCIe spot ${price}/hr"))
    return out


def parse_modal(rec, fx):
    txt = textify(rec["body"])
    out = []
    pats = [
        ("H100", "SXM", 80, r"Nvidia H100 SXM5 \$ ([0-9.]+)", True),
        ("A100", "unknown", 80, r"Nvidia A100, 80 GB \$ ([0-9.]+)", True),
        ("A100", "unknown", 40, r"Nvidia A100, 40 GB \$ ([0-9.]+)", True),
    ]
    for model, form, vram, pat, inc in pats:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if not price:
            continue
        out.append(attach_fx(assign_model_key(obs(
            source_id="modal", source_name="Modal pricing", source_url=rec["url"],
            observation_type="rental_list", sold_vs_listed="rental_list",
            model=model, form_factor=form, vram_gb=vram, currency="USD",
            price=price, unit="per_second",
            notes=f"页面以美元/秒标价，已×3600 折成小时价（约 ${price*3600:.2f}/hr）。",
            include_in_income=1 if inc else 0,
            raw_label=f"Modal {model} {vram}GB ${price}/sec",
        )), fx))
    return out


def parse_voltagepark(rec, fx):
    txt = textify(rec["body"])
    m = re.search(r"rent one H100 GPU for 1 hour starting at \$([0-9.]+)", txt)
    if not m:
        return []
    price = money_to_float(m.group(1))
    return [attach_fx(assign_model_key(obs(
        source_id="voltagepark", source_name="Voltage Park", source_url=rec["url"],
        observation_type="rental_list", sold_vs_listed="rental_list",
        model="H100", form_factor="unknown", vram_gb=80, currency="USD",
        price=price, unit="per_gpu_hour",
        notes="FAQ 起价，形态未标明，不进入官方子指数中位数。",
        include_in_income=0, raw_label=f"Voltage Park H100 starting ${price}/hr",
    )), fx)]


def parse_massed(rec, fx):
    txt = textify(rec["body"])
    out = []
    pats = [
        ("H100", "SXM", 80, r"H100 SXM5 \(80GB\) × 1 \$([0-9.]+) /hr", True),
        ("H100", "NVL", 94, r"H100 NVL × 1 \$([0-9.]+) /hr", False),
        ("H100", "PCIe", 80, r"H100 \(80GB\) × 1 \$([0-9.]+) /hr", True),
        ("A100", "SXM", 80, r"A100 SXM4 \(80GB\) × 1 \$([0-9.]+) /hr", True),
    ]
    for model, form, vram, pat, inc in pats:
        m = re.search(pat, txt)
        if not m:
            continue
        price = money_to_float(m.group(1))
        if price:
            out.append(_rental_row(fx, "massedcompute", "Massed Compute pricing", rec["url"],
                                   model, form, vram, price,
                                   notes="公开按需价目。H100 (80GB) 按非 SXM 行归入 PCIe。",
                                   include=inc, label=f"Massed {model} {form} {vram}GB ${price}/hr"))
    return out



def _blocked_or_challenge(rec: dict) -> bool:
    """True when the fetch failed or the body is a challenge/error page, not a real listing."""
    if not rec.get("ok") or not rec.get("body"):
        return True
    status = rec.get("status")
    if status and int(status) >= 400:
        return True
    txt = textify(rec["body"][:8000])
    if re.search(r"Just a moment|cf-browser-verification|attention required|access denied|Error Page \| eBay", txt, re.I):
        return True
    return False


def parse_genius_instinct_catalog(rec, fx):
    """Genius HPC public CSV catalogue. Official MI300X cost uses the single-module From (low) price only."""
    if not rec.get("ok") or not rec.get("body"):
        return []
    raw = rec["body"].decode("utf-8-sig", "replace")
    # Some responses may be HTML if the CSV URL is redirected.
    if "<html" in raw[:200].lower():
        return []
    import csv as _csv
    import io
    out = []
    reader = _csv.DictReader(io.StringIO(raw))
    for row in reader:
        model_txt = " ".join(str(row.get(k) or "") for k in ("Manufacturer", "Model", "Priced configuration", "Reference"))
        inst = detect_instinct_model(model_txt)
        if not inst:
            continue
        ref = (row.get("Reference") or "").strip()
        cfg = (row.get("Priced configuration") or "").strip()
        low = money_to_float(row.get("Low EUR ex VAT") or "")
        high = money_to_float(row.get("High EUR ex VAT") or "")
        is_platform = bool(re.search(r"8\s*×|8\s*x|UBB|platform|excludes CPU", cfg, re.I)) or inst in cfg and "8" in cfg and "UBB" in cfg.upper()
        # Platform / 8-GPU + UBB lines are not a single-card cost anchor (UBB not deducted).
        if is_platform or "UBB" in cfg.upper() or re.search(r"8\s*[×x]", cfg):
            for price, bound in ((low, "low"), (high, "high")):
                if not price:
                    continue
                out.append(enrich_observation(attach_fx(assign_model_key(obs(
                    source_id="genius_instinct_catalog", source_name="Genius HPC Instinct catalogue",
                    source_url=rec["url"],
                    observation_type="new_from_list_server", sold_vs_listed="new_list",
                    model=inst, form_factor="OAM", vram_gb=AMD_VRAM_HINT.get(inst),
                    condition="new", currency="EUR", price=price, unit="per_8gpu_server",
                    vendor="AMD", raw_price_text=f"€{price:,.2f}",
                    notes=(
                        f"公开目录 8 卡+UBB {bound} 价，未税欧元，配置：{cfg}。"
                        "未扣除 UBB/底板，不进入官方单卡成本法锚点。"
                    ),
                    include_in_cost_new=0,
                    quantity_config=cfg or "8-gpu + UBB",
                    raw_label=f"Genius catalogue {ref or inst} 8x {bound} €{price:,.2f}",
                )), fx)))
            continue
        form = "PCIe" if inst == "MI210" else "OAM"
        official = (inst == "MI300X" and not is_platform)
        if low:
            notes = (
                f"公开目录单模块 From（区间下限）€{low:,.2f} ex-VAT；配置：{cfg}。"
                "经销商起价，不是 AMD MSRP，也不是美国渠道价。"
            )
            if official:
                notes += " 作为 MI300X 独立子指数成本法临时锚点（取 From，不用区间中位）。"
            else:
                notes += "【不进入 v0 官方计算】AMD 观察，仅作研究/校准。"
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="genius_instinct_catalog", source_name="Genius HPC Instinct catalogue",
                source_url=rec["url"],
                observation_type="new_from_list", sold_vs_listed="new_list",
                model=inst, form_factor=form, vram_gb=AMD_VRAM_HINT.get(inst),
                condition="new", currency="EUR", price=low, unit="per_card",
                vendor="AMD", raw_price_text=f"€{low:,.2f}",
                notes=notes,
                include_in_cost_new=1 if official else 0,
                quantity_config=cfg or "1-gpu module",
                raw_label=f"Genius catalogue {ref or inst} From €{low:,.2f}",
            )), fx)))
        if high and high != low:
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="genius_instinct_catalog", source_name="Genius HPC Instinct catalogue",
                source_url=rec["url"],
                observation_type="new_from_list_high", sold_vs_listed="new_list",
                model=inst, form_factor=form, vram_gb=AMD_VRAM_HINT.get(inst),
                condition="new", currency="EUR", price=high, unit="per_card",
                vendor="AMD", raw_price_text=f"€{high:,.2f}",
                notes=(
                    f"公开目录单模块区间上限 €{high:,.2f} ex-VAT；配置：{cfg}。"
                    "只作观察，不进入成本法锚点，以免把区间当成两个独立经销商报价。"
                ),
                include_in_cost_new=0,
                quantity_config=cfg or "1-gpu module",
                raw_label=f"Genius catalogue {ref or inst} High €{high:,.2f}",
            )), fx)))
    return out


def parse_tensorwave(rec, fx):
    """TensorWave product-page 'Starting at' GPU-hour prices. Tenor is unconfirmed; not official income."""
    if _blocked_or_challenge(rec):
        return []
    txt = rec["body"].decode("utf-8", "replace")
    out = []
    seen = set()
    for m in re.finditer(
        r'"modelId":"(MI[0-9]+X)","title":"([^"]+)"[\s\S]{0,500}?"coolingLabel":"([^"]+)","price":"\$([0-9.]+)","priceUnit":"([^"]+)"',
        txt,
    ):
        inst = detect_instinct_model(m.group(1))
        if not inst:
            continue
        cooling = m.group(3)
        price = money_to_float(m.group(4))
        unit_txt = m.group(5)
        key = (inst, cooling, price)
        if not price or key in seen:
            continue
        seen.add(key)
        vram = AMD_VRAM_HINT.get(inst)
        fm = re.search(r"Memory:\s*(\d+)\s*GB", m.group(0))
        if fm:
            vram = int(fm.group(1))
        out.append(_rental_row(
            fx, "tensorwave", "TensorWave accelerators", rec["url"],
            inst, "OAM", vram, price,
            notes=(
                f"页面标 Starting at，制冷={cooling}，单位={unit_txt}。"
                "未写明按需/预留期限，故不进入官方收益法中位数。"
                + ("" if inst == "MI300X" else "【不进入 v0 官方计算】AMD 观察，仅作研究/校准。")
            ),
            include=False,
            label=f"TensorWave {inst} {cooling} starting at ${price}/GPU-hr",
            tenor="unconfirmed", vendor="AMD",
            raw_price_text=f"${price}{unit_txt}",
            quantity_config=cooling,
        ))
    return out


def parse_hotaisle(rec, fx):
    """Hot Aisle usage-based on-demand list prices for MI300X VMs and 8x bare metal."""
    if _blocked_or_challenge(rec):
        return []
    txt = textify(rec["body"])
    out = []
    specs = [
        (r"\$2\.99/GPU/hr.{0,80}?1,\s*2,\s*and\s*4x MI300X VMs", 2.99, "on-demand",
         "1, 2, and 4x MI300X VMs", "Hot Aisle MI300X VM $2.99/GPU/hr"),
        (r"8x bare metal.{0,80}?\$3\.39/GPU/hr", 3.39, "on-demand",
         "8x MI300X bare metal", "Hot Aisle MI300X 8x bare metal $3.39/GPU/hr"),
    ]
    # Fallback if punctuation/spacing differs but both prices and MI300X are visible.
    if not any(re.search(pat, txt, re.I | re.S) for pat, *_ in specs):
        if re.search(r"\$2\.99/GPU/hr", txt) and re.search(r"MI300X VMs", txt, re.I):
            specs[0] = (r"\$2\.99/GPU/hr",) + specs[0][1:]
        if re.search(r"\$3\.39/GPU/hr", txt) and re.search(r"bare metal", txt, re.I):
            specs[1] = (r"\$3\.39/GPU/hr",) + specs[1][1:]
    for pat, price, tenor, qty, label in specs:
        if not re.search(pat, txt, re.I | re.S):
            continue
        out.append(_rental_row(
            fx, "hotaisle", "Hot Aisle pricing", rec["url"],
            "MI300X", "OAM", 192, price,
            notes=(
                "页面为 usage-based / on-demand 价目（按小时列出）。"
                f"配置：{qty}。进入 MI300X 独立子指数收益法中位数。"
            ),
            include=True,
            label=label,
            tenor=tenor, vendor="AMD",
            raw_price_text=f"${price}/GPU/hr",
            quantity_config=qty,
        ))
    return out


def parse_oracle_mi300x(rec, fx):
    """OCI public price list API, PAY_AS_YOU_GO GPU Per Hour. Hyperscaler; observation only."""
    if not rec.get("ok") or not rec.get("body"):
        return []
    try:
        data = json.loads(rec["body"].decode("utf-8"))
    except Exception:
        return []
    out = []
    last_upd = data.get("lastUpdated")
    for item in data.get("items") or []:
        if not re.search(r"MI300X", str(item.get("displayName") or "") + " " + str(item.get("partNumber") or ""), re.I):
            continue
        for loc in item.get("currencyCodeLocalizations") or []:
            if (loc.get("currencyCode") or "").upper() != "USD":
                continue
            for pr in loc.get("prices") or []:
                model_name = pr.get("model") or ""
                val = pr.get("value")
                if val is None:
                    continue
                price = float(val)
                tenor = "on-demand" if model_name.upper() == "PAY_AS_YOU_GO" else "unconfirmed"
                out.append(_rental_row(
                    fx, "oracle_mi300x", "Oracle Cloud MI300X price list", rec["url"],
                    "MI300X", "OAM", 192, price,
                    notes=(
                        f"官方价目 API part {item.get('partNumber')}，{item.get('displayName')}，"
                        f"metric={item.get('metricName')}，model={model_name}，lastUpdated={last_upd}。"
                        "超大规模云单价含主机/网络/平台，与 neocloud 单卡价口径不同，待校准前不进入官方收益法中位数。"
                    ),
                    include=False,
                    label=f"OCI {item.get('partNumber')} {model_name} ${price}/GPU-hr",
                    tenor=tenor, vendor="AMD",
                    raw_price_text=f"${price}/GPU-hr",
                    region="OCI list (currency USD)",
                ))
    return out


def parse_azure_mi300x(rec, fx):
    """Azure Retail Prices API, eastus2 ND96isr_MI300X_v5. Hyperscaler; observation only."""
    if not rec.get("ok") or not rec.get("body"):
        return []
    try:
        data = json.loads(rec["body"].decode("utf-8"))
    except Exception:
        return []
    out = []
    seen = set()
    for it in data.get("Items") or []:
        product = it.get("productName") or ""
        # Prefer Linux (product name without 'Windows').
        if re.search(r"Windows", product, re.I):
            continue
        sku = it.get("skuName") or it.get("armSkuName") or ""
        ptype = it.get("type") or it.get("priceType") or ""
        unit_price = it.get("unitPrice")
        if unit_price is None:
            continue
        region = it.get("armRegionName") or ""
        term = it.get("reservationTerm")
        key = (sku, ptype, term, region, unit_price)
        if key in seen:
            continue
        seen.add(key)
        node = float(unit_price)
        if "Spot" in sku:
            tenor = "spot"
            qty = "8-gpu node; Linux Spot eastus2"
            notes_prefix = "Azure Retail Prices API，Linux Spot Consumption，节点小时价已除以 8。"
        elif "Low Priority" in sku:
            tenor = "spot"
            qty = "8-gpu node; Linux Low Priority eastus2"
            notes_prefix = "Azure Retail Prices API，Linux Low Priority，节点小时价已除以 8。"
        elif ptype == "Reservation":
            tenor = "reserved"
            qty = f"8-gpu node; {term or 'reservation'} eastus2"
            notes_prefix = f"Azure Retail Prices API，Reservation {term}，单位是预留总价而非按需小时，仅作观察。"
            # Do not convert reservation totals into a fake hourly rate.
            out.append(enrich_observation(attach_fx(assign_model_key(obs(
                source_id="azure_mi300x", source_name="Azure Retail Prices MI300X",
                source_url=rec["url"],
                observation_type="rental_list", sold_vs_listed="rental_list",
                model="MI300X", form_factor="OAM", vram_gb=192, currency="USD",
                price=node, unit="reservation_total", vendor="AMD",
                tenor="reserved", region=region,
                raw_price_text=f"${node:,.2f} {term}",
                notes=notes_prefix + " 超大规模云口径待校准，不进入官方收益法中位数。",
                include_in_income=0,
                quantity_config=qty,
                raw_label=f"Azure {sku} {ptype} {term} ${node:,.2f}",
            )), fx)))
            continue
        elif ptype == "Consumption":
            tenor = "on-demand"
            qty = "8-gpu node; Linux Pay-as-you-go eastus2"
            notes_prefix = "Azure Retail Prices API，Linux Consumption（Pay as you go），节点小时价已除以 8。"
        else:
            tenor = "unconfirmed"
            qty = f"8-gpu node; {ptype} eastus2"
            notes_prefix = f"Azure Retail Prices API，type={ptype}，节点小时价已除以 8。"
        out.append(attach_fx(assign_model_key(obs(
            source_id="azure_mi300x", source_name="Azure Retail Prices MI300X",
            source_url=rec["url"],
            observation_type="rental_list", sold_vs_listed="rental_list",
            model="MI300X", form_factor="OAM", vram_gb=192, currency="USD",
            price=node, unit="per_8gpu_hour", vendor="AMD",
            tenor=tenor, region=region,
            raw_price_text=f"${node}/node-hr",
            notes=notes_prefix + " 超大规模云单价含主机/网络/平台，待校准前不进入官方收益法中位数。",
            include_in_income=0,
            quantity_config=qty,
            raw_label=f"Azure {sku} {ptype} ${node}/node-hr",
        )), fx))
        out[-1] = enrich_observation(out[-1])
    return out


def parse_serverbuyback_index(rec, fx):
    """ServerBuyback published used range (not a listing or sold tape). Observation only."""
    if _blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace")
    m = re.search(
        r"<tr><td>AMD Instinct</td><td>MI300X</td><td>[^<]*</td><td>\$([0-9,]+)[–-]([0-9,]+)</td>",
        raw,
    )
    if not m:
        return []
    low = money_to_float(m.group(1))
    high = money_to_float(m.group(2))
    upd = None
    um = re.search(r"Last updated:\s*([0-9]{1,2}\s+\w+\s+20\d{2})", textify(rec["body"]), re.I)
    if um:
        upd = um.group(1)
    else:
        dm = re.search(r'"dateModified":\s*"(20\d{2}-\d{2}-\d{2})"', raw)
        if dm:
            upd = dm.group(1)
    note_date = f"来源标注更新于 {upd}。" if upd else "来源页自称 June 2026 区间、按月刷新。"
    return [enrich_observation(attach_fx(assign_model_key(obs(
        source_id="serverbuyback_index", source_name="ServerBuyback GPU resale value index",
        source_url=rec["url"],
        observation_type="broker_range", sold_vs_listed="range_not_transaction",
        model="MI300X", form_factor="OAM", vram_gb=192, condition="used_range",
        currency="USD", price=None, unit="per_card", vendor="AMD",
        raw_price_text=f"${m.group(1)}–{m.group(2)}",
        notes=(
            f"经纪商公开二手区间 ${m.group(1)}–{m.group(2)}，不是成交，也不是可执行挂牌。"
            f"{note_date} 该机构称区间来自公开二手市场与自身成交观察的汇总。"
            "不得进入官方市场法中位数，也不用区间中位去填一个假市价。"
        ),
        include_in_market=0,
        raw_label=f"ServerBuyback MI300X used range ${m.group(1)}–{m.group(2)}",
    )), fx))]


def parse_evelltech_mi300x(rec, fx):
    """EvellTech used listing, if the public page is reachable. Do not bypass a 403/challenge."""
    if _blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace")
    txt = textify(raw)
    if not re.search(r"MI300X", txt, re.I):
        return []
    # Prefer a visible product price, not invent from prior research.
    price = None
    pm = re.search(r'["\']price["\']\s*:\s*["\']?([0-9]+(?:\.[0-9]+)?)', raw, re.I)
    if pm:
        price = money_to_float(pm.group(1))
    if price is None:
        pm = re.search(r"\$\s*([0-9,]+(?:\.[0-9]+)?)", txt)
        if pm:
            price = money_to_float(pm.group(1))
    if not price:
        return []
    cond = "used" if re.search(r"used|working", txt, re.I) else None
    form = "OAM" if re.search(r"\bOAM\b", txt, re.I) else None
    return [enrich_observation(attach_fx(assign_model_key(obs(
        source_id="evelltech_mi300x", source_name="EvellTech MI300X listing",
        source_url=rec["url"],
        observation_type="listed", sold_vs_listed="listed",
        model="MI300X", form_factor=form or "OAM", vram_gb=192,
        condition=cond, currency="USD", price=price, unit="per_card", vendor="AMD",
        raw_price_text=f"${price:,.2f}",
        notes="经销商二手挂牌，非成交。机龄未在页面明确写出则留空。",
        include_in_market=1,
        raw_label=f"EvellTech MI300X ${price:,.2f}",
    )), fx))]


def parse_elipic_mi300x(rec, fx):
    """Elipic used listing, if the page is healthy. Quality flags historically heavy; keep off official market until clean."""
    if _blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace")
    if re.search(r"internal_server_error|critical error", raw, re.I):
        return []
    txt = textify(raw)
    if not re.search(r"MI300X", txt, re.I):
        return []
    price = None
    pm = re.search(r"\$\s*([0-9,]+(?:\.[0-9]+)?)", txt)
    if pm:
        price = money_to_float(pm.group(1))
    if not price:
        return []
    quality = []
    if re.search(r"Sxm", txt, re.I) and not re.search(r"\bOAM\b", txt):
        quality.append("页面把形态写成 SXM，MI300X 应为 OAM")
    notes = "经销商二手挂牌，非成交。"
    if quality:
        notes += "质量旗标：" + "；".join(quality) + "。在核验前不进入官方市场法。"
    return [enrich_observation(attach_fx(assign_model_key(obs(
        source_id="elipic_mi300x", source_name="Elipic Partners MI300X listing",
        source_url=rec["url"],
        observation_type="listed", sold_vs_listed="listed",
        model="MI300X", form_factor="OAM", vram_gb=192,
        condition="used", currency="USD", price=price, unit="per_card", vendor="AMD",
        raw_price_text=f"${price:,.2f}",
        notes=notes,
        include_in_market=0,
        raw_label=f"Elipic MI300X ${price:,.2f}",
    )), fx))]


def parse_vultr_pricing(rec, fx):
    """Vultr public pricing. 'Starting at' / blocked pages are not official income. Do not bypass 403."""
    if _blocked_or_challenge(rec):
        return []
    txt = textify(rec["body"])
    out = []
    for m in re.finditer(
        r"(MI3[0-9]{2}X).{0,160}?\$([0-9]+\.[0-9]+)",
        txt, re.I | re.S,
    ):
        inst = detect_instinct_model(m.group(1))
        price = money_to_float(m.group(2))
        if not inst or not price:
            continue
        ctx = m.group(0)
        starting = bool(re.search(r"starting at|starting from", ctx, re.I))
        out.append(_rental_row(
            fx, "vultr_pricing", "Vultr pricing", rec["url"],
            inst, "OAM", AMD_VRAM_HINT.get(inst), price,
            notes=(
                "Vultr 公开页价格"
                + ("，页面为 Starting at / Starting from 起价，期限待确认。" if starting else "。")
                + " 不进入官方收益法中位数。"
            ),
            include=False,
            label=f"Vultr {inst} ${price}/hr" + (" starting at" if starting else ""),
            tenor="unconfirmed", vendor="AMD",
            raw_price_text=f"${price}",
        ))
    return out



SOURCE_PLAN = [
    {"id": "fx_usd", "url": "https://open.er-api.com/v6/latest/USD", "method": "GET", "parser": None},
    {"id": "anchorgpu", "url": "https://anchorgpu.com/used-gpus", "method": "GET", "parser": parse_anchorgpu},
    {"id": "geniushpc", "url": "https://geniushpc.com/secondary-market", "method": "GET", "parser": parse_genius},
    {"id": "creative_h100", "url": "https://creativecompute.co.uk/products/nvidia-h100-80gb-pcie-refurbished",
     "method": "GET",
     "parser": lambda rec, fx: parse_creative_product(rec, fx, {
         "model": "H100", "form_factor": "PCIe", "vram_gb": 80, "condition": "refurbished",
         "unit": "per_card", "notes": "未税英镑翻新品询价，站点声明询价不是订单。",
         "raw_label": "Creative Compute H100 80GB PCIe refurb",
     })},
    {"id": "creative_a100", "url": "https://creativecompute.co.uk/products/nvidia-a100-80gb-refurbished",
     "method": "GET",
     "parser": lambda rec, fx: parse_creative_product(rec, fx, {
         "model": "A100", "form_factor": "PCIe", "vram_gb": 80, "condition": "refurbished",
         "unit": "per_card", "notes": "未税英镑翻新品询价，站点声明询价不是订单。",
         "raw_label": "Creative Compute A100 80GB PCIe refurb",
     })},
    {"id": "creative_8xa100", "url": "https://creativecompute.co.uk/products/refurbished-8x-a100-gpu-server",
     "method": "GET",
     "parser": lambda rec, fx: parse_creative_product(rec, fx, {
         "model": "A100", "form_factor": "PCIe", "vram_gb": 80, "condition": "refurbished_server",
         "unit": "per_8gpu_server",
     })},
    {"id": "serverpartdeals",
     "url": "https://www.serverpartdeals.com/products/dell-poweredge-xe9680-xeon-platinum-8462y-8x-nvidia-hgx-h100-80gb-sxm5-nvlink-ai-powered-refurbished-gpu-server",
     "method": "GET", "parser": parse_spd},
    {"id": "clustermelt", "url": "https://clustermelt.com/pricing", "method": "GET", "parser": parse_clustermelt},
    {"id": "mulligan", "url": "https://mulligancompute.com/used-gpus/", "method": "GET", "parser": parse_mulligan},
    {"id": "vast_public_feed", "url": "https://storage.googleapis.com/vast-public-gpu-pricing/gpu-pricing-public.json", "method": "GET", "parser": parse_vast_feed},
    {"id": "vast_bundles", "url": "https://console.vast.ai/api/v0/bundles/", "method": "POST",
     "headers": {"Content-Type": "application/json", "Accept": "application/json"},
     "data": json.dumps({"limit": 200}).encode("utf-8"), "parser": parse_vast_bundles},
    {"id": "runpod", "url": "https://api.runpod.io/graphql", "method": "POST",
     "headers": {"Content-Type": "application/json", "Accept": "application/json"},
     "data": json.dumps({"query": "query { gpuTypes { id displayName memoryInGb communityPrice securePrice lowestPrice { minimumBidPrice uninterruptablePrice stockStatus } } }"}).encode("utf-8"),
     "parser": parse_runpod},
    {"id": "lambda", "url": "https://lambda.ai/pricing", "method": "GET", "parser": parse_lambda},
    {"id": "digitalocean", "url": "https://www.digitalocean.com/pricing/gpu-droplets", "method": "GET", "parser": parse_digitalocean},
    {"id": "coreweave", "url": "https://www.coreweave.com/pricing", "method": "GET", "parser": parse_coreweave},
    {"id": "crusoe", "url": "https://www.crusoe.ai/pricing", "method": "GET", "parser": parse_crusoe},
    {"id": "nebius", "url": "https://nebius.com/prices", "method": "GET", "parser": parse_nebius},
    {"id": "together", "url": "https://www.together.ai/pricing", "method": "GET", "parser": parse_together},
    {"id": "hyperstack", "url": "https://www.hyperstack.cloud/pricing", "method": "GET", "parser": parse_hyperstack},
    {"id": "modal", "url": "https://modal.com/pricing", "method": "GET", "parser": parse_modal},
    {"id": "voltagepark", "url": "https://www.voltagepark.com/pricing", "method": "GET", "parser": parse_voltagepark},
    {"id": "massedcompute", "url": "https://massedcompute.com/pricing/", "method": "GET", "parser": parse_massed},
    {"id": "ebay_sold",
     "url": "https://www.ebay.com/sch/i.html?_nkw=NVIDIA+H100&LH_Sold=1&LH_Complete=1&_ipg=60",
     "method": "GET", "parser": None, "expect_block": True},
    {"id": "ebay_active",
     "url": "https://www.ebay.com/sch/i.html?_nkw=NVIDIA+H100&_ipg=60",
     "method": "GET", "parser": None, "expect_block": True},
    {"id": "genius_instinct_catalog",
     "url": "https://geniushpc.com/pricing/GeniusHPC-price-catalogue.csv",
     "method": "GET", "parser": parse_genius_instinct_catalog},
    {"id": "tensorwave",
     "url": "https://tensorwave.com/products/accelerators/amd-mi300x",
     "method": "GET", "parser": parse_tensorwave},
    {"id": "hotaisle",
     "url": "https://hotaisle.xyz/pricing",
     "method": "GET", "parser": parse_hotaisle},
    {"id": "oracle_mi300x",
     "url": "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/?partNumber=B109485&currencyCode=USD",
     "method": "GET", "parser": parse_oracle_mi300x},
    {"id": "azure_mi300x",
     "url": "https://prices.azure.com/api/retail/prices?%24filter=armSkuName+eq+%27Standard_ND96isr_MI300X_v5%27+and+armRegionName+eq+%27eastus2%27",
     "method": "GET", "parser": parse_azure_mi300x},
    {"id": "serverbuyback_index",
     "url": "https://serverbuyback.com/resources/gpu-resale-value-index/",
     "method": "GET", "parser": parse_serverbuyback_index},
    {"id": "evelltech_mi300x",
     "url": "https://evelltech.com/product/mi300x/",
     "method": "GET", "parser": parse_evelltech_mi300x, "expect_block": True},
    {"id": "elipic_mi300x",
     "url": "https://www.elipicpartners.com/item/amd-instinct-mi300x-192gb-hbm3-gpu-graph",
     "method": "GET", "parser": parse_elipic_mi300x, "expect_block": True},
    {"id": "vultr_pricing",
     "url": "https://www.vultr.com/pricing/",
     "method": "GET", "parser": parse_vultr_pricing, "expect_block": True},
    {"id": "ebay_sold_mi300x",
     "url": "https://www.ebay.com/sch/i.html?_nkw=AMD+Instinct+MI300X&LH_Sold=1&LH_Complete=1&_ipg=60",
     "method": "GET", "parser": None, "expect_block": True},
    {"id": "ebay_active_mi300x",
     "url": "https://www.ebay.com/sch/i.html?_nkw=AMD+Instinct+MI300X&_ipg=60",
     "method": "GET", "parser": None, "expect_block": True},
]


def active_source_plan() -> list[dict]:
    """Official SOURCE_PLAN, plus SEA sources only when the SEA trial is active for this ROOT."""
    plan = list(SOURCE_PLAN)
    if sea_index.sea_active_for_root(ROOT):
        plan.extend(sea_index.SEA_SOURCE_PLAN)
    return plan


def compute_index(observations, asof_date_pt, computed_at_utc, computed_at_pt):
    observations = drop_competitor_observations(list(observations or []), context="compute_index")
    observations = sea_index.drop_entity_list_observations(observations, context="compute_index")
    rows = []
    per_model = {}
    for key in OFFICIAL_INDEX_KEYS:
        mkt = [o["usd_per_card"] for o in observations
               if is_official(o, "include_in_market") and o.get("model_key") == key and o.get("usd_per_card")]
        inc_r = [o["usd_per_gpu_hour"] for o in observations
                 if is_official(o, "include_in_income") and o.get("model_key") == key and o.get("usd_per_gpu_hour")]
        newp = [o["usd_per_card"] for o in observations
                if is_official(o, "include_in_cost_new") and o.get("model_key") == key and o.get("usd_per_card")]
        market = median(mkt)
        r_med = median(inc_r)
        income = income_dcf(r_med) if r_med is not None else None
        new_med = median(newp)
        frac = ASSUMPTIONS["cost"]["unknown_age_remaining_fraction"]
        cost = (new_med * frac) if new_med is not None else None
        legs = [v for v in (market, income, cost) if v is not None]
        nav = (sum(legs) / len(legs)) if legs else None
        flags = []
        if not mkt:
            flags.append("no_official_listings")
        flags.append("sold_n=0")
        if r_med is None:
            flags.append("no_official_rentals")
        if new_med is None:
            flags.append("no_new_card_anchor")
        flags.append("income_assumptions=to-be-calibrated")
        flags.append("cost_age_fraction=to-be-calibrated")
        breakdown = {
            "market": {"method": "unweighted_median_of_official_listings", "n": len(mkt),
                       "values_usd": [round(v, 2) for v in sorted(mkt)], "median_usd": None if market is None else round(market, 2)},
            "income": {"method": "dcf_of_official_rental_median", "n": len(inc_r),
                       "rental_median_usd_per_hour": None if r_med is None else round(r_med, 4),
                       "dcf_usd": None if income is None else round(income, 2),
                       "assumptions": ASSUMPTIONS["income"]},
            "cost": {"method": "new_from_median_times_unknown_age_remaining_fraction", "n": len(newp),
                     "new_card_usd": None if new_med is None else round(new_med, 2),
                     "remaining_fraction": frac,
                     "cost_usd": None if cost is None else round(cost, 2),
                     "assumptions": ASSUMPTIONS["cost"]},
            "indicative_nav": {"method": "equal_weight_of_available_legs", "legs_used": len(legs),
                               "value_usd": None if nav is None else round(nav, 2)},
        }
        rec = {
            "asof_date_pt": asof_date_pt, "computed_at_utc": computed_at_utc, "computed_at_pt": computed_at_pt,
            "model_key": key, "market_usd": market, "income_usd": income, "cost_usd": cost,
            "nav_indicative_usd": nav, "market_n": len(mkt), "income_n": len(inc_r), "cost_n": len(newp),
            "rental_median_usd_per_hour": r_med, "new_card_usd": new_med,
            "quality_flag": ";".join(flags), "assumptions_json": json.dumps(ASSUMPTIONS, ensure_ascii=False),
            "breakdown_json": json.dumps(breakdown, ensure_ascii=False),
        }
        per_model[key] = rec
        rows.append(rec)

    navs = [per_model[k]["nav_indicative_usd"] for k in COMPOSITE_KEYS if per_model[k]["nav_indicative_usd"] is not None]
    composite = (sum(navs) / len(navs)) if navs else None
    rows.append({
        "asof_date_pt": asof_date_pt, "computed_at_utc": computed_at_utc, "computed_at_pt": computed_at_pt,
        "model_key": "COMPOSITE", "market_usd": None, "income_usd": None, "cost_usd": None,
        "nav_indicative_usd": composite, "market_n": None, "income_n": None, "cost_n": None,
        "rental_median_usd_per_hour": None, "new_card_usd": None,
        "quality_flag": "equal_weight_of_four_nvidia_models;mi300x_excluded_from_composite;installed_base_weights=to-be-calibrated;sold_n=0",
        "assumptions_json": json.dumps(ASSUMPTIONS, ensure_ascii=False),
        "breakdown_json": json.dumps({
            "components": {k: None if per_model[k]["nav_indicative_usd"] is None else round(per_model[k]["nav_indicative_usd"], 2)
                           for k in COMPOSITE_KEYS},
            "n_models": len(navs),
            "composite_usd": None if composite is None else round(composite, 2),
            "composite_members": list(COMPOSITE_KEYS),
            "excluded_subindices": [k for k in OFFICIAL_INDEX_KEYS if k not in COMPOSITE_KEYS],
        }, ensure_ascii=False),
    })
    return rows


def official_contributor_ids() -> set[str]:
    """SOURCE_PLAN ids that can contribute to official M/I/C legs (not FX, not watch-only, not competitors)."""
    out = set()
    for src in SOURCE_PLAN:
        sid = src["id"]
        if sid == "fx_usd" or sid in OBSERVATION_ONLY_SOURCES:
            continue
        # Expected blocks (eBay, Cloudflare challenge pages, etc.) are not
        # required official contributors. A standing 403 must not trip the
        # jump guard on unrelated models. If a fetch later succeeds, the
        # parsed observations can still enter official legs via include flags.
        if src.get("expect_block"):
            continue
        if competitor_exclusion_reason(sid, src.get("name"), src.get("url")):
            continue
        if sea_index.is_sea_source_id(sid) or sea_index.entity_list_exclusion_reason(sid, src.get("name"), src.get("url")):
            continue
        out.add(sid)
    return out


def official_source_ok_n(health: list[dict] | None) -> int:
    official = official_contributor_ids()
    n = 0
    for h in health or []:
        if h.get("source_id") in official and h.get("ok") and not h.get("competitor_excluded"):
            n += 1
    return n


def failed_official_source_ids(health: list[dict] | None) -> list[str]:
    official = official_contributor_ids()
    failed = []
    for h in health or []:
        sid = h.get("source_id")
        if sid not in official or h.get("competitor_excluded"):
            continue
        if not h.get("ok"):
            failed.append(sid)
    return failed


def legs_present(row: dict) -> list[str]:
    """Valuation approaches that meet MIN_LEG_SAMPLE_N. Composite has no M/I/C legs."""
    if row.get("model_key") == "COMPOSITE":
        return []
    out = []
    for name, nkey, vkey in LEG_SPECS:
        n = row.get(nkey)
        v = row.get(vkey)
        try:
            n_int = int(n) if n is not None else 0
        except (TypeError, ValueError):
            n_int = 0
        if v is not None and n_int >= MIN_LEG_SAMPLE_N:
            out.append(name)
    return out


def _is_carried_row(row: dict) -> bool:
    v = row.get("carried_forward")
    if v in (1, True, "1", "true"):
        return True
    return False


def load_last_normal(conn: sqlite3.Connection, model_key: str, before_asof: str) -> Optional[dict]:
    """Most recent non-carried daily_index row for model_key strictly before asof."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_index)").fetchall()}
    if "carried_forward" in cols:
        q = (
            "SELECT * FROM daily_index WHERE model_key=? AND asof_date_pt < ? "
            "AND COALESCE(carried_forward, 0)=0 AND nav_indicative_usd IS NOT NULL "
            "ORDER BY asof_date_pt DESC LIMIT 1"
        )
    else:
        q = (
            "SELECT * FROM daily_index WHERE model_key=? AND asof_date_pt < ? "
            "AND nav_indicative_usd IS NOT NULL ORDER BY asof_date_pt DESC LIMIT 1"
        )
    row = conn.execute(q, (model_key, before_asof)).fetchone()
    return dict(row) if row else None


def _append_quality_flags(row: dict, *tags: str) -> None:
    flags = [f for f in (row.get("quality_flag") or "").split(";") if f]
    for tag in tags:
        if tag not in flags:
            flags.append(tag)
    row["quality_flag"] = ";".join(flags)


def _mark_carried(rec: dict, prior: dict, reasons: list[str], failed_ids: list[str]) -> None:
    rec["carried_forward"] = 1
    rec["stale"] = 1
    rec["carried_from_asof"] = prior["asof_date_pt"]
    rec["nav_indicative_usd"] = prior["nav_indicative_usd"]
    reason = "; ".join(reasons)
    if failed_ids:
        reason += f"; failed_official_sources={failed_ids}"
    rec["carry_reason"] = reason
    _append_quality_flags(rec, "carried_forward", "stale")
    try:
        bd = json.loads(rec["breakdown_json"]) if rec.get("breakdown_json") else {}
    except (TypeError, json.JSONDecodeError):
        bd = {}
    bd["publication_guard"] = {
        "carried_forward": True,
        "stale": True,
        "carried_from_asof": prior["asof_date_pt"],
        "carried_nav_usd": None if prior["nav_indicative_usd"] is None else round(float(prior["nav_indicative_usd"]), 2),
        "raw_nav_usd": None if rec.get("raw_nav_indicative_usd") is None else round(float(rec["raw_nav_indicative_usd"]), 2),
        "reason": rec["carry_reason"],
        "failed_official_sources": failed_ids,
    }
    rec["breakdown_json"] = json.dumps(bd, ensure_ascii=False)


def _init_guard_fields(rec: dict, official_ok: int, failed_ids: list[str], run_id: Optional[str]) -> None:
    rec["raw_nav_indicative_usd"] = rec.get("nav_indicative_usd")
    rec["official_source_ok_n"] = official_ok
    rec["failed_sources_json"] = json.dumps(failed_ids, ensure_ascii=False)
    rec["carried_forward"] = 0
    rec["stale"] = 0
    rec["carry_reason"] = None
    rec["carried_from_asof"] = None
    rec["index_run_id"] = run_id
    if rec.get("model_key") == "COMPOSITE":
        rec["legs_present"] = None
    else:
        rec["legs_present"] = len(legs_present(rec))


def apply_publication_guards(
    conn: sqlite3.Connection,
    index_rows: list[dict],
    health: list[dict] | None,
    asof_date_pt: str,
    run_id: Optional[str] = None,
) -> list[dict]:
    """Carry forward last normal NAV when today's print is degraded by missing legs or a jump with a source failure.

    Does not write to the database. Composite is recomputed from published (possibly carried) model NAVs
    so a degraded model value never enters the composite.
    """
    failed_ids = failed_official_source_ids(health)
    official_ok = official_source_ok_n(health)
    by_key = {r["model_key"]: dict(r) for r in index_rows}

    any_model_carried = False
    for key in OFFICIAL_INDEX_KEYS:
        rec = by_key.get(key)
        if rec is None:
            continue
        _init_guard_fields(rec, official_ok, failed_ids, run_id)
        raw_nav = rec["raw_nav_indicative_usd"]
        today_legs = legs_present(rec)
        prior = load_last_normal(conn, key, asof_date_pt)
        reasons = []
        if prior is not None:
            prior_legs = legs_present(prior)
            if len(today_legs) < len(prior_legs):
                reasons.append(
                    f"leg_loss: today={today_legs or ['none']} prior={prior_legs} "
                    f"(asof {prior['asof_date_pt']})"
                )
            prior_nav = prior.get("nav_indicative_usd")
            if (
                failed_ids
                and raw_nav is not None
                and prior_nav not in (None, 0)
                and abs(float(raw_nav) - float(prior_nav)) / abs(float(prior_nav)) > JUMP_GUARD_PCT
            ):
                chg = (float(raw_nav) - float(prior_nav)) / float(prior_nav)
                reasons.append(
                    f"jump_guard: dod={chg:.1%} exceeds {JUMP_GUARD_PCT:.0%} with official source failure"
                )
        if reasons:
            _mark_carried(rec, prior, reasons, failed_ids)
            any_model_carried = True
            log_carry_forward(
                key, raw_nav, rec["nav_indicative_usd"],
                rec["carried_from_asof"], rec["carry_reason"], index_rows,
            )
        by_key[key] = rec

    comp = by_key.get("COMPOSITE")
    if comp is not None:
        _init_guard_fields(comp, official_ok, failed_ids, run_id)
        raw_comp = comp["raw_nav_indicative_usd"]
        pub_navs = [
            by_key[k]["nav_indicative_usd"]
            for k in COMPOSITE_KEYS
            if k in by_key and by_key[k].get("nav_indicative_usd") is not None
        ]
        mixed = (sum(pub_navs) / len(pub_navs)) if pub_navs else None
        comp["nav_indicative_usd"] = mixed
        try:
            bd = json.loads(comp["breakdown_json"]) if comp.get("breakdown_json") else {}
        except (TypeError, json.JSONDecodeError):
            bd = {}
        bd["components"] = {
            k: None if by_key[k].get("nav_indicative_usd") is None else round(float(by_key[k]["nav_indicative_usd"]), 2)
            for k in COMPOSITE_KEYS
            if k in by_key
        }
        bd["composite_members"] = list(COMPOSITE_KEYS)
        bd["excluded_subindices"] = [k for k in OFFICIAL_INDEX_KEYS if k not in COMPOSITE_KEYS]
        bd["n_models"] = len(pub_navs)
        bd["composite_usd"] = None if mixed is None else round(float(mixed), 2)
        bd["raw_composite_usd"] = None if raw_comp is None else round(float(raw_comp), 2)
        reasons = []
        carried_models = [k for k in COMPOSITE_KEYS if k in by_key and _is_carried_row(by_key[k])]
        prior_c = load_last_normal(conn, "COMPOSITE", asof_date_pt)
        if carried_models:
            reasons.append("component_carried:" + ",".join(carried_models))
        if (
            failed_ids
            and mixed is not None
            and prior_c is not None
            and prior_c.get("nav_indicative_usd") not in (None, 0)
            and abs(float(mixed) - float(prior_c["nav_indicative_usd"])) / abs(float(prior_c["nav_indicative_usd"])) > JUMP_GUARD_PCT
        ):
            chg = (float(mixed) - float(prior_c["nav_indicative_usd"])) / float(prior_c["nav_indicative_usd"])
            reasons.append(
                f"jump_guard: dod={chg:.1%} exceeds {JUMP_GUARD_PCT:.0%} with official source failure"
            )
            # A genuine composite jump with a source outage: fall back to last normal composite.
            comp["nav_indicative_usd"] = prior_c["nav_indicative_usd"]
            bd["composite_usd"] = round(float(prior_c["nav_indicative_usd"]), 2)
        if reasons and prior_c is not None:
            comp["breakdown_json"] = json.dumps(bd, ensure_ascii=False)
            _mark_carried(comp, prior_c, reasons, failed_ids)
            log_carry_forward(
                "COMPOSITE", raw_comp, comp["nav_indicative_usd"],
                None, comp["carry_reason"], index_rows,
            )
        else:
            comp["breakdown_json"] = json.dumps(bd, ensure_ascii=False)
        by_key["COMPOSITE"] = comp

    return [by_key.get(r["model_key"], r) for r in index_rows]


def existing_official_source_ok_n(conn: sqlite3.Connection, existing: dict[str, dict]) -> Optional[int]:
    vals = [
        r.get("official_source_ok_n")
        for r in existing.values()
        if r.get("official_source_ok_n") is not None
    ]
    if vals:
        return int(max(vals))
    return None


def same_day_run_is_worse(
    existing: dict[str, dict],
    new_rows: list[dict],
    new_official_ok: int,
    old_official_ok: Optional[int],
) -> Optional[str]:
    """Return a reason if the later same-asof run must not overwrite the earlier result."""
    if old_official_ok is not None and new_official_ok < old_official_ok:
        return f"fewer official sources ({new_official_ok} < {old_official_ok})"
    worse = []
    new_by = {r["model_key"]: r for r in new_rows}
    for key in OFFICIAL_INDEX_KEYS:
        if key not in existing or key not in new_by:
            continue
        old_legs = legs_present(existing[key])
        new_legs = legs_present(new_by[key])
        if len(new_legs) < len(old_legs):
            worse.append(f"{key}: {new_legs or ['none']} < {old_legs}")
    if worse:
        return "fewer legs than earlier same-day run (" + "; ".join(worse) + ")"
    return None


def load_index_rows_for_date(conn: sqlite3.Connection, asof_date_pt: str) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_index WHERE asof_date_pt=? ORDER BY model_key",
        (asof_date_pt,),
    ).fetchall()]
    # Keep official sub-index order, then COMPOSITE, matching compute_index.
    order = {k: i for i, k in enumerate(OFFICIAL_INDEX_KEYS + ["COMPOSITE"])}
    rows.sort(key=lambda r: order.get(r["model_key"], 99))
    return rows


OBS_COLS = [
    "run_id", "snapshot_id", "snapshot_hash", "source_id", "source_name", "source_url",
    "observation_type", "sold_vs_listed", "model", "form_factor", "vram_gb", "condition",
    "currency", "price", "unit", "usd_price", "usd_per_card", "usd_per_gpu_hour",
    "fx_usd_per_unit", "fx_source", "fx_asof_utc", "observed_at_utc", "asof_date_pt",
    "notes", "include_in_market", "include_in_income", "include_in_cost_new", "model_key", "raw_label",
    "raw_price_text", "tenor", "region", "quantity_config", "availability",
    "age_months", "manufacture_date", "first_deploy_date", "age_source", "age_confidence",
    "vendor", "snapshot_path", "in_official_v0",
]

IDX_COLS = [
    "asof_date_pt", "computed_at_utc", "computed_at_pt", "model_key",
    "market_usd", "income_usd", "cost_usd", "nav_indicative_usd",
    "market_n", "income_n", "cost_n", "rental_median_usd_per_hour", "new_card_usd",
    "quality_flag", "assumptions_json", "breakdown_json",
    "carried_forward", "stale", "carry_reason", "carried_from_asof",
    "legs_present", "official_source_ok_n", "raw_nav_indicative_usd",
    "failed_sources_json", "index_run_id",
]


SEA_OBS_COLS = [
    "include_in_sea_income", "include_in_sea_market", "include_in_sea_cost",
    "sea_index_key", "index_family",
]


def migrate_sea_schema(conn: sqlite3.Connection) -> None:
    """Add SEA columns only on a SEA-active data tree. Official nav.db is not altered."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    col_sql = {
        "include_in_sea_income": "INTEGER NOT NULL DEFAULT 0",
        "include_in_sea_market": "INTEGER NOT NULL DEFAULT 0",
        "include_in_sea_cost": "INTEGER NOT NULL DEFAULT 0",
        "sea_index_key": "TEXT",
        "index_family": "TEXT",
    }
    changed = False
    for col, typ in col_sql.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE observations ADD COLUMN {col} {typ}")
            changed = True
    if changed:
        conn.commit()


def insert_observations(conn, rows):
    rows = drop_competitor_observations(rows, context="insert_observations")
    rows = sea_index.drop_entity_list_observations(rows, context="insert_observations")
    if not rows:
        return
    existing = {r[1] for r in conn.execute("PRAGMA table_info(observations)").fetchall()}
    cols = list(OBS_COLS) + [c for c in SEA_OBS_COLS if c in existing]
    q = "INSERT INTO observations (" + ",".join(cols) + ") VALUES (" + ",".join(["?"] * len(cols)) + ")"
    conn.executemany(q, [[r.get(c) for c in cols] for r in rows])


def upsert_index(conn, rows, health=None):
    """Write daily_index, refusing to overwrite a better same-asof result.

    A later run on the same asof_date_pt must not replace an earlier row when it
    has fewer official sources or fewer valid valuation legs. Returns True if
    rows were written, False if the earlier result was kept.
    """
    if not rows:
        return True
    asof = rows[0]["asof_date_pt"]
    existing_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_index WHERE asof_date_pt=?", (asof,)
    ).fetchall()]
    existing = {r["model_key"]: r for r in existing_rows}
    if existing:
        new_ok = official_source_ok_n(health)
        old_ok = existing_official_source_ok_n(conn, existing)
        reason = same_day_run_is_worse(existing, rows, new_ok, old_ok)
        if reason:
            log.warning(
                "SAME_DAY_OVERWRITE_BLOCKED asof=%s: %s; keeping earlier daily_index",
                asof, reason,
            )
            return False
    q = (
        "INSERT INTO daily_index (" + ",".join(IDX_COLS) + ") VALUES (" + ",".join(["?"] * len(IDX_COLS)) + ") "
        "ON CONFLICT(asof_date_pt, model_key) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in IDX_COLS if c not in ("asof_date_pt", "model_key"))
    )
    conn.executemany(q, [[r.get(c) for c in IDX_COLS] for r in rows])
    return True


def write_outputs(conn, run_id, asof_date_pt, computed_at_pt, computed_at_utc, health, observations, index_rows, fx):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    hist_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_index ORDER BY asof_date_pt, model_key"
    ).fetchall()]
    latest_obs = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE asof_date_pt=? ORDER BY run_id, source_id, id",
        (asof_date_pt,),
    ).fetchall()]
    sold_n = sum(1 for o in observations if o.get("sold_vs_listed") == "sold")

    def r2(v):
        return None if v is None else round(float(v), 2)

    def r4(v):
        return None if v is None else round(float(v), 4)

    latest = {
        "asof_date_pt": asof_date_pt,
        "computed_at_pt": computed_at_pt,
        "computed_at_utc": computed_at_utc,
        "run_id": run_id,
        "disclaimer_zh": "指示性数据，尚无成交样本。收益法与成本法参数均为待校准占位假设，不构成正式指数。",
        "sold_count_today": sold_n,
        "methodology_page": METHODOLOGY_PAGE,
        "fx": {
            "source": fx.source,
            "asof_utc": fx.asof_utc,
            "usd_per_eur": r4(fx.rate("EUR")),
            "usd_per_gbp": r4(fx.rate("GBP")),
            "eur_per_usd": r4(fx.raw_rates.get("EUR")),
            "gbp_per_usd": r4(fx.raw_rates.get("GBP")),
        },
        "assumptions": ASSUMPTIONS,
        "models": {},
        "composite": None,
        "source_health": health,
        "observation_count": len(observations),
        "official_observation_counts": {
            "market": sum(1 for o in observations if is_official(o, "include_in_market")),
            "income": sum(1 for o in observations if is_official(o, "include_in_income")),
            "cost_new": sum(1 for o in observations if is_official(o, "include_in_cost_new")),
        },
        "observation_only_sources": [
            {"source_id": sid, "reason": reason}
            for sid, reason in OBSERVATION_ONLY_SOURCES.items()
        ],
        "observation_only_count": sum(1 for o in observations if o.get("source_id") in OBSERVATION_ONLY_SOURCES),
    }
    for r in index_rows:
        item = {
            "model_key": r["model_key"],
            "label": MODEL_LABELS.get(r["model_key"], r["model_key"]),
            "market_usd": r2(r["market_usd"]),
            "income_usd": r2(r["income_usd"]),
            "cost_usd": r2(r["cost_usd"]),
            "nav_indicative_usd": r2(r["nav_indicative_usd"]),
            "market_n": r["market_n"],
            "income_n": r["income_n"],
            "cost_n": r["cost_n"],
            "rental_median_usd_per_hour": r4(r["rental_median_usd_per_hour"]),
            "new_card_usd": r2(r["new_card_usd"]),
            "quality_flag": r["quality_flag"],
            "breakdown": json.loads(r["breakdown_json"]) if r.get("breakdown_json") else None,
            "carried_forward": bool(r.get("carried_forward")),
            "stale": bool(r.get("stale")),
            "dod_excluded": bool(r.get("carried_forward") or r.get("stale")),
            "carry_reason": r.get("carry_reason"),
            "carried_from_asof": r.get("carried_from_asof"),
            "legs_present": r.get("legs_present"),
            "official_source_ok_n": r.get("official_source_ok_n"),
            "raw_nav_indicative_usd": r2(r.get("raw_nav_indicative_usd")),
            "failed_official_sources": (
                json.loads(r["failed_sources_json"])
                if r.get("failed_sources_json") else []
            ),
            "index_run_id": r.get("index_run_id"),
        }
        if r["model_key"] == "COMPOSITE":
            latest["composite"] = item
        else:
            latest["models"][r["model_key"]] = item
    latest["composite_members"] = list(COMPOSITE_KEYS)
    latest["separate_subindices"] = [k for k in OFFICIAL_INDEX_KEYS if k not in COMPOSITE_KEYS]

    history = {
        "generated_at_pt": computed_at_pt,
        "series": {},
        "dates": sorted({r["asof_date_pt"] for r in hist_rows}),
    }
    for key in OFFICIAL_INDEX_KEYS + ["COMPOSITE"]:
        pts = [r for r in hist_rows if r["model_key"] == key]
        history["series"][key] = {
            "label": MODEL_LABELS.get(key, key),
            "points": [
                {
                    "asof_date_pt": p["asof_date_pt"],
                    "computed_at_pt": p["computed_at_pt"],
                    "nav_indicative_usd": r2(p["nav_indicative_usd"]),
                    "market_usd": r2(p["market_usd"]),
                    "income_usd": r2(p["income_usd"]),
                    "cost_usd": r2(p["cost_usd"]),
                    "carried_forward": bool(p.get("carried_forward")),
                    "stale": bool(p.get("stale")),
                    "dod_excluded": bool(p.get("carried_forward") or p.get("stale")),
                    "carry_reason": p.get("carry_reason"),
                    "carried_from_asof": p.get("carried_from_asof"),
                    "legs_present": p.get("legs_present"),
                    "raw_nav_indicative_usd": r2(p.get("raw_nav_indicative_usd")),
                }
                for p in pts
            ],
        }

    dash_obs = []
    for o in latest_obs:
        dash_obs.append({
            "source_id": o["source_id"],
            "source_name": o["source_name"],
            "source_url": o["source_url"],
            "sold_vs_listed": o["sold_vs_listed"],
            "model": o["model"],
            "form_factor": o["form_factor"],
            "vram_gb": o["vram_gb"],
            "condition": o["condition"],
            "currency": o["currency"],
            "price": o["price"],
            "unit": o["unit"],
            "usd_price": r2(o["usd_price"]) if o["unit"] not in ("per_gpu_hour", "per_second") else (None if o["usd_price"] is None else round(float(o["usd_price"]), 4)),
            "usd_per_card": r2(o["usd_per_card"]),
            "usd_per_gpu_hour": r4(o["usd_per_gpu_hour"]),
            "fx_usd_per_unit": r4(o["fx_usd_per_unit"]),
            "fx_source": o["fx_source"],
            "include_in_market": bool(o["include_in_market"]),
            "include_in_income": bool(o["include_in_income"]),
            "include_in_cost_new": bool(o["include_in_cost_new"]),
            "model_key": o["model_key"],
            "raw_label": o["raw_label"],
            "notes": o["notes"],
            "snapshot_hash": o["snapshot_hash"],
        })

    dashboard = {
        "latest": latest,
        "history": history,
        "observations": dash_obs,
        "methodology_page": METHODOLOGY_PAGE,
        "disclaimer_zh": latest["disclaimer_zh"],
        "title_zh": "ComputeNAV 指数看板",
    }

    LATEST_PATH.write_text(json.dumps(latest, indent=2, ensure_ascii=False))
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    DASHBOARD_DATA_PATH.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
    return latest, history, dashboard



OFFICIAL_ROOT = Path("/workspace/computenav")
SEA_TRIAL_DEFAULT_DIR = Path("/workspace/computenav-sea-test")
SEA_FX_CURRENCIES = ("SGD", "MYR", "IDR", "VND")
SEA_TRIAL_FIXTURES = {
    "acc_sg": "/tmp/sea_fetches/rental/acc.body",
    "novagpu_my": "/tmp/sea_fetches/rental/novagpu.body",
    "teamcloud_my": "/tmp/sea_fetches/rental/teamcloud.body",
    "lintasarta_deka": "/tmp/sea_fetches/rental/lintasarta.body",
    "biznet_neo": "/tmp/sea_fetches/rental/biznet_gpu.body",
    "biznet_pricelist": "/tmp/sea_fetches/rental/biznet_price.body",
    "siam_th": "/tmp/sea_fetches/rental/siam.body",
    "syndrome_ag": "/tmp/sea_fetches/dealers/syndrome_h100.body",
    "syndrome_ag_pcie": "/tmp/sea_fetches/dealers/syndrome_h100new.body",
}


def rebind_root(root: Path) -> None:
    """Relocate all private outputs. Used by --sea-trial before any writes."""
    global ROOT, SNAPSHOT_ROOT, OUTPUT_DIR, LOG_DIR, DB_PATH, HISTORY_PATH, LATEST_PATH, DASHBOARD_DATA_PATH
    ROOT = Path(root)
    SNAPSHOT_ROOT = ROOT / "snapshots"
    OUTPUT_DIR = ROOT / "output"
    LOG_DIR = ROOT / "logs"
    DB_PATH = ROOT / "nav.db"
    HISTORY_PATH = OUTPUT_DIR / "history.json"
    LATEST_PATH = OUTPUT_DIR / "latest.json"
    DASHBOARD_DATA_PATH = OUTPUT_DIR / "dashboard_data.json"


def maybe_attach_sea_and_rewrite(latest, history, dashboard, observations, fx, asof_date_pt, computed_at_utc, computed_at_pt):
    """Attach the SEA sub-index and rewrite JSON only when SEA is active for this ROOT.

    Official /workspace/computenav output is never rewritten by this path unless
    COMPUTENAV_SEA_ALLOW_OFFICIAL=1 (not used in the internal trial).
    """
    if not sea_index.sea_active_for_root(ROOT):
        return latest, history, dashboard, None
    sea = sea_index.compute_sea_index(
        observations, asof_date_pt, computed_at_utc, computed_at_pt,
        us_models=latest.get("models") or {},
    )
    latest = sea_index.attach_sea_to_latest(latest, sea)

    def r4(v):
        return None if v is None else round(float(v), 4)

    fx_out = dict(latest.get("fx") or {})
    for ccy in SEA_FX_CURRENCIES:
        fx_out[f"usd_per_{ccy.lower()}"] = r4(fx.rate(ccy))
        raw = (fx.raw_rates or {}).get(ccy)
        fx_out[f"{ccy.lower()}_per_usd"] = r4(float(raw) if raw is not None else None)
    latest["fx"] = fx_out
    history = dict(history or {})
    history["sea"] = {
        "asof_date_pt": asof_date_pt,
        "models": sea.get("models"),
        "sample_counts": sea.get("sample_counts"),
        "ushq_cloud_apac_comparison": sea.get("ushq_cloud_apac_comparison"),
    }
    dashboard = dict(dashboard or {})
    dashboard["latest"] = latest
    dashboard["history"] = history
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(json.dumps(latest, indent=2, ensure_ascii=False))
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    DASHBOARD_DATA_PATH.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False))
    sea_index.log_sea_summary(sea)
    return latest, history, dashboard, sea


def _source_plan_fx_only_plus_sea() -> list[dict]:
    fx_src = [s for s in SOURCE_PLAN if s["id"] == "fx_usd"]
    return fx_src + list(sea_index.SEA_SOURCE_PLAN)


def _load_official_latest() -> dict:
    path = OFFICIAL_ROOT / "output" / "latest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_official_observations_readonly(run_id: str) -> list[dict]:
    db = OFFICIAL_ROOT / "nav.db"
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    conn.close()
    return rows


def _sha256_file(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def official_output_checksums() -> dict:
    out = {}
    for name in ("nav.db", "output/latest.json", "output/history.json", "output/dashboard_data.json"):
        path = OFFICIAL_ROOT / name
        st = path.stat() if path.exists() else None
        out[name] = {
            "sha256": _sha256_file(path),
            "size": None if st is None else st.st_size,
            "mtime": None if st is None else st.st_mtime,
        }
    return out


def _fixture_rec(sid: str, url: str, path: str) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    body = p.read_bytes()
    return {
        "source_id": sid, "url": url, "method": "GET",
        "ok": True, "status": 200, "error": "fixture: research fetch 2026-11-27 (live fetch failed or was not used)",
        "body": body, "sha256": hashlib.sha256(body).hexdigest(),
        "byte_count": len(body), "fetched_at_utc": iso(now_utc()),
        "final_url": url, "fixture": True,
    }


def run_sea_trial() -> int:
    """Internal SEA trial: fetch SEA+FX into a temp tree; official db is read-only.

    Does not refetch official US sources (listing pages move). Official NAV
    identity is checked by recomputing from the official run's observations
    with and without the SEA rows.
    """
    os.environ["COMPUTENAV_ENABLE_SEA"] = "1"
    if not os.environ.get("COMPUTENAV_DATA_DIR"):
        rebind_root(SEA_TRIAL_DEFAULT_DIR)
        os.environ["COMPUTENAV_DATA_DIR"] = str(SEA_TRIAL_DEFAULT_DIR)
    if sea_index.is_official_data_tree(ROOT) and not sea_index.sea_allow_official_tree():
        print("refusing SEA trial write into the official /workspace/computenav tree", file=sys.stderr)
        return 2
    if not sea_index.sea_active_for_root(ROOT):
        print("SEA trial is not active for ROOT=%s" % ROOT, file=sys.stderr)
        return 2

    baseline_before = official_output_checksums()

    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    start = now_utc()
    start_pt = to_pt(start)
    run_id = start_pt.strftime("%Y%m%dT%H%M%S") + "_PT_SEA_TRIAL"
    asof_date_pt = start_pt.strftime("%Y-%m-%d")
    run_dir = SNAPSHOT_ROOT / run_id
    log_path = LOG_DIR / f"{run_id}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    log.info("SEA trial run_id=%s asof_date_pt=%s data_dir=%s", run_id, asof_date_pt, ROOT)

    official_latest = _load_official_latest()
    official_run_id = official_latest["run_id"]
    official_asof = official_latest["asof_date_pt"]
    official_obs = _load_official_observations_readonly(official_run_id)
    log.info("loaded %s official observations from official nav.db run_id=%s asof=%s (read-only)",
             len(official_obs), official_run_id, official_asof)

    conn = connect_db()
    migrate_sea_schema(conn)
    sea_index.scrub_entity_list_rows_from_db(conn)
    scrub_competitor_rows_from_db(conn)

    fx = FX()
    observations = []
    health = []
    skip = {x.strip() for x in os.environ.get("COMPUTENAV_SKIP_SOURCES", "").split(",") if x.strip()}
    source_problems = []

    for src in _source_plan_fx_only_plus_sea():
        sid = src["id"]
        competitor_reason = competitor_exclusion_reason(sid, src.get("name"), src.get("url"))
        if competitor_reason:
            log.error("COMPETITOR_EXCLUDED source %s — skipped: %s", sid, competitor_reason)
            health.append({"source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                           "competitor_excluded": True, "http_status": None,
                           "error": "COMPETITOR_EXCLUDED: " + competitor_reason,
                           "observation_count": 0, "fetched_at_utc": None, "sha256": None})
            source_problems.append({"source_id": sid, "url": src["url"], "problem": "competitor_excluded",
                                    "detail": competitor_reason})
            continue
        el_reason = sea_index.entity_list_exclusion_reason(sid, src.get("name"), src.get("url"))
        if el_reason:
            log.error("ENTITY_LIST_EXCLUDED source %s — skipped: %s", sid, el_reason)
            health.append({"source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                           "entity_list_excluded": True, "http_status": None,
                           "error": "ENTITY_LIST_EXCLUDED: " + el_reason,
                           "observation_count": 0, "fetched_at_utc": None, "sha256": None})
            source_problems.append({"source_id": sid, "url": src["url"], "problem": "entity_list_excluded",
                                    "detail": el_reason})
            continue
        if sid in skip:
            log.info("%s skipped via COMPUTENAV_SKIP_SOURCES", sid)
            continue
        rec = {
            "source_id": sid, "url": src["url"], "method": src.get("method", "GET"),
            "ok": False, "status": None, "error": None, "body": b"", "sha256": None,
            "byte_count": 0, "fetched_at_utc": iso(now_utc()),
        }
        n_obs = 0
        used_fixture = False
        try:
            rec = fetch(
                sid, src["url"], method=src.get("method", "GET"),
                data=src.get("data"), headers=src.get("headers"),
            )
            live_ok = bool(rec.get("ok") and rec.get("body"))
            live_status, live_error = rec.get("status"), rec.get("error")
            if not live_ok and sid in SEA_TRIAL_FIXTURES:
                fxrec = _fixture_rec(sid, src["url"], SEA_TRIAL_FIXTURES[sid])
                if fxrec:
                    rec = fxrec
                    used_fixture = True
                    source_problems.append({
                        "source_id": sid, "url": src["url"],
                        "problem": "live_fetch_failed_used_research_fixture",
                        "http_status": live_status,
                        "detail": f"Live fetch failed ({live_status} {live_error}). Parser ran on the 2026-11-27 research snapshot. Not a live quote.",
                    })
                    log.warning("%s: live fetch failed (%s); using 2026-11-27 research fixture", sid, live_status)
            save_snapshot(run_dir, rec)
            conn.execute(
                "INSERT INTO snapshots (run_id,source_id,url,method,fetched_at_utc,http_status,ok,error,sha256,byte_count,path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, sid, rec.get("final_url") or rec["url"], rec["method"], rec["fetched_at_utc"],
                 rec.get("status"), 1 if rec.get("ok") else 0, rec.get("error"), rec.get("sha256"),
                 rec.get("byte_count"), rec.get("path")),
            )
            snap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if sid == "fx_usd" and rec.get("ok"):
                try:
                    fx.load(rec)
                    log.info(
                        "FX loaded: 1 EUR=%s USD, 1 GBP=%s USD, 1 SGD=%s USD, 1 MYR=%s USD, 1 IDR=%s USD, 1 VND=%s USD, asof=%s",
                        fx.rate("EUR"), fx.rate("GBP"), fx.rate("SGD"), fx.rate("MYR"),
                        fx.rate("IDR"), fx.rate("VND"), fx.asof_utc,
                    )
                except Exception:
                    log.exception("FX parse failed")
                    rec["ok"] = False
                    rec["error"] = (rec.get("error") or "") + " | FX parse failed"
                    source_problems.append({"source_id": sid, "problem": "fx_parse_failed", "detail": rec.get("error")})
            parser = src.get("parser")
            if parser and rec.get("ok") and rec.get("body"):
                try:
                    rows = parser(rec, fx) or []
                    for row in rows:
                        row["run_id"] = run_id
                        row["snapshot_id"] = snap_id
                        row["snapshot_hash"] = rec.get("sha256")
                        row["snapshot_path"] = rec.get("path")
                        row["observed_at_utc"] = rec["fetched_at_utc"]
                        row["asof_date_pt"] = asof_date_pt
                        if not row.get("source_id"):
                            row["source_id"] = sid
                        if used_fixture:
                            note = row.get("notes") or ""
                            tag = "Research fixture 2026-11-27 used because live fetch failed; not a live quote."
                            if tag not in note:
                                row["notes"] = (note + " " + tag).strip()
                        mark_observation_only(row)
                        enrich_observation(row, snapshot_path=rec.get("path"))
                    rows = drop_competitor_observations(rows, context=f"parse:{sid}")
                    rows = sea_index.drop_entity_list_observations(rows, context=f"parse:{sid}")
                    insert_observations(conn, rows)
                    observations.extend(rows)
                    n_obs = len(rows)
                    log.info("%s: %s observations%s", sid, n_obs, " (research fixture)" if used_fixture else "")
                    if n_obs == 0 and sid != "fx_usd":
                        source_problems.append({"source_id": sid, "url": src["url"], "problem": "parsed_zero_observations",
                                                "fixture": used_fixture})
                except Exception:
                    log.exception("parse failed for %s", sid)
                    rec["error"] = (rec.get("error") or "") + f" | parse failed: {traceback.format_exc()[-400:]}"
                    source_problems.append({"source_id": sid, "url": src["url"], "problem": "parse_failed",
                                            "detail": rec.get("error")})
            elif not rec.get("ok"):
                log.warning("%s failed: %s %s", sid, rec.get("status"), rec.get("error"))
                source_problems.append({"source_id": sid, "url": src["url"], "problem": "fetch_failed",
                                        "http_status": rec.get("status"), "detail": rec.get("error")})
            else:
                log.info("%s fetched ok, no parser or empty body", sid)
        except Exception:
            log.exception("source %s crashed (continuing)", sid)
            rec["error"] = traceback.format_exc()[-500:]
            source_problems.append({"source_id": sid, "url": src["url"], "problem": "source_crashed",
                                    "detail": rec.get("error")})
        health.append({
            "source_id": sid, "url": src["url"], "ok": bool(rec.get("ok")),
            "http_status": rec.get("status"), "error": rec.get("error"),
            "observation_count": n_obs, "fetched_at_utc": rec.get("fetched_at_utc"),
            "sha256": rec.get("sha256"), "fixture": used_fixture,
        })
        conn.execute(
            "INSERT INTO source_health (run_id,asof_date_pt,source_id,url,ok,http_status,error,observation_count,fetched_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, asof_date_pt, sid, src["url"], 1 if rec.get("ok") else 0, rec.get("status"),
             rec.get("error"), n_obs, rec.get("fetched_at_utc")),
        )
        conn.commit()
        time.sleep(0.15)

    computed = now_utc()
    computed_pt = to_pt(computed)
    observations = drop_competitor_observations(observations, context="pre-compute")
    observations = sea_index.drop_entity_list_observations(observations, context="pre-compute")

    # Identity: official NVIDIA + MI300X + COMPOSITE must not move when SEA rows are added.
    only_official = compute_index(official_obs, official_asof, iso(computed), iso(computed_pt))
    mixed = compute_index(official_obs + observations, official_asof, iso(computed), iso(computed_pt))
    t_only = sea_index.official_values_tuple(only_official)
    t_mixed = sea_index.official_values_tuple(mixed)
    official_unchanged_with_sea_rows = t_only == t_mixed
    if official_unchanged_with_sea_rows:
        log.info("IDENTITY OK: official NVIDIA/MI300X/COMPOSITE values are unchanged when SEA observations are added")
    else:
        log.error("IDENTITY FAIL: official values changed when SEA observations were added")

    def _round_tuple(d):
        out = {}
        for k, v in (d or {}).items():
            out[k] = {
                "nav": None if v.get("nav") is None else round(float(v["nav"]), 2),
                "market": None if v.get("market") is None else round(float(v["market"]), 2),
                "income": None if v.get("income") is None else round(float(v["income"]), 2),
                "cost": None if v.get("cost") is None else round(float(v["cost"]), 2),
                "market_n": v.get("market_n"),
                "income_n": v.get("income_n"),
                "cost_n": v.get("cost_n"),
                "rental_median": None if v.get("rental_median") is None else round(float(v["rental_median"]), 4),
            }
        return out

    # Compare recomputed official values to the published official latest (2-decimal NAV).
    published = {}
    for k, item in (official_latest.get("models") or {}).items():
        published[k] = {
            "nav": item.get("nav_indicative_usd"),
            "market": item.get("market_usd"),
            "income": item.get("income_usd"),
            "cost": item.get("cost_usd"),
            "market_n": item.get("market_n"),
            "income_n": item.get("income_n"),
            "cost_n": item.get("cost_n"),
            "rental_median": item.get("rental_median_usd_per_hour"),
        }
    comp = official_latest.get("composite") or {}
    published["COMPOSITE"] = {
        "nav": comp.get("nav_indicative_usd"),
        "market": None, "income": None, "cost": None,
        "market_n": None, "income_n": None, "cost_n": None, "rental_median": None,
    }
    vs_published = _round_tuple(t_only) == published

    # Trial JSON: official models copied from the published official latest (no new official print).
    latest = json.loads(json.dumps(official_latest))
    latest["run_id"] = run_id
    latest["computed_at_pt"] = iso(computed_pt)
    latest["computed_at_utc"] = iso(computed)
    latest["asof_date_pt"] = official_asof
    latest["sea_trial"] = {
        "status": "internal_trial",
        "official_run_id": official_run_id,
        "official_asof_date_pt": official_asof,
        "official_values_unchanged_with_sea_rows": official_unchanged_with_sea_rows,
        "recomputed_official_matches_published_latest": vs_published,
        "note": "Official model values below are copied from the published official latest.json. SEA is attached as a separate_subindices family.",
    }
    history = {"generated_at_pt": iso(computed_pt), "series": {}, "dates": [official_asof], "sea_trial": True}
    dashboard = {
        "latest": latest,
        "history": history,
        "observations": [],
        "methodology_page": METHODOLOGY_PAGE,
        "disclaimer_zh": latest.get("disclaimer_zh"),
        "title_zh": "ComputeNAV 指数看板（SEA 内部试算）",
    }
    latest, history, dashboard, sea = maybe_attach_sea_and_rewrite(
        latest, history, dashboard, observations, fx, official_asof, iso(computed), iso(computed_pt)
    )
    conn.close()

    baseline_after = official_output_checksums()
    official_files_untouched = baseline_before == baseline_after
    if official_files_untouched:
        log.info("Official nav.db and output JSON checksums are unchanged after the SEA trial")
    else:
        log.error("Official output checksums changed — trial should not have written the official tree")

    report = {
        "run_id": run_id,
        "asof_date_pt": official_asof,
        "computed_at_pt": iso(computed_pt),
        "data_dir": str(ROOT),
        "official_run_id": official_run_id,
        "official_values_unchanged_with_sea_rows": official_unchanged_with_sea_rows,
        "recomputed_official_matches_published_latest": vs_published,
        "official_files_untouched": official_files_untouched,
        "official_checksums": baseline_after,
        "recomputed_official": _round_tuple(t_only),
        "sea": sea,
        "source_health": health,
        "source_problems": source_problems,
        "sea_observation_count": len(observations),
        "fx": latest.get("fx") if latest else None,
    }
    report_path = OUTPUT_DIR / "sea_trial_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    log.info("wrote trial report %s", report_path)
    log.info("wrote %s %s %s", LATEST_PATH, HISTORY_PATH, DASHBOARD_DATA_PATH)
    return 0 if official_unchanged_with_sea_rows and official_files_untouched else 1


def run():
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    start = now_utc()
    start_pt = to_pt(start)
    run_id = start_pt.strftime("%Y%m%dT%H%M%S") + "_PT"
    asof_date_pt = start_pt.strftime("%Y-%m-%d")
    run_dir = SNAPSHOT_ROOT / run_id

    log_path = LOG_DIR / f"{run_id}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    log.info("run_id=%s asof_date_pt=%s", run_id, asof_date_pt)

    conn = connect_db()
    fx = FX()
    observations = []
    health = []

    # CI hook (no-op by default): COMPUTENAV_SKIP_SOURCES=id1,id2 skips those fetches.
    skip = {x.strip() for x in os.environ.get("COMPUTENAV_SKIP_SOURCES", "").split(",") if x.strip()}

    already_removed = scrub_competitor_rows_from_db(conn)
    if already_removed:
        log.error("COMPETITOR_EXCLUDED: removed %s pre-existing observation(s) from nav.db before this run",
                  len(already_removed))
    el_removed = sea_index.scrub_entity_list_rows_from_db(conn)
    if el_removed:
        log.error("ENTITY_LIST_EXCLUDED: removed %s pre-existing observation(s) from nav.db before this run",
                  len(el_removed))
    if sea_index.sea_active_for_root(ROOT):
        migrate_sea_schema(conn)
        log.info("SEA sub-index enabled for data dir %s", ROOT)

    for src in active_source_plan():
        sid = src["id"]
        competitor_reason = competitor_exclusion_reason(sid, src.get("name"), src.get("url"))
        if competitor_reason:
            log.error("COMPETITOR_EXCLUDED source %s url=%s — skipped (not fetched, not stored): %s",
                      sid, src.get("url"), competitor_reason)
            health.append({
                "source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                "competitor_excluded": True,
                "http_status": None, "error": "COMPETITOR_EXCLUDED: " + competitor_reason,
                "observation_count": 0, "fetched_at_utc": None, "sha256": None,
            })
            conn.execute(
                "INSERT INTO source_health (run_id,asof_date_pt,source_id,url,ok,http_status,error,observation_count,fetched_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, asof_date_pt, sid, src["url"], 0, None,
                 "COMPETITOR_EXCLUDED: " + competitor_reason, 0, None),
            )
            conn.commit()
            continue
        el_reason = sea_index.entity_list_exclusion_reason(sid, src.get("name"), src.get("url"))
        if el_reason:
            log.error("ENTITY_LIST_EXCLUDED source %s url=%s — skipped (not fetched, not stored): %s",
                      sid, src.get("url"), el_reason)
            health.append({
                "source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                "entity_list_excluded": True,
                "http_status": None, "error": "ENTITY_LIST_EXCLUDED: " + el_reason,
                "observation_count": 0, "fetched_at_utc": None, "sha256": None,
            })
            conn.execute(
                "INSERT INTO source_health (run_id,asof_date_pt,source_id,url,ok,http_status,error,observation_count,fetched_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, asof_date_pt, sid, src["url"], 0, None,
                 "ENTITY_LIST_EXCLUDED: " + el_reason, 0, None),
            )
            conn.commit()
            continue
        if sid in skip:
            log.info("%s skipped via COMPUTENAV_SKIP_SOURCES", sid)
            health.append({"source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                           "http_status": None, "error": "skipped (COMPUTENAV_SKIP_SOURCES)",
                           "observation_count": 0, "fetched_at_utc": None, "sha256": None})
            conn.execute(
                "INSERT INTO source_health (run_id,asof_date_pt,source_id,url,ok,http_status,error,observation_count,fetched_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, asof_date_pt, sid, src["url"], 0, None,
                 "skipped (COMPUTENAV_SKIP_SOURCES)", 0, None),
            )
            conn.commit()
            continue
        rec = {
            "source_id": sid, "url": src["url"], "method": src.get("method", "GET"),
            "ok": False, "status": None, "error": None, "body": b"", "sha256": None,
            "byte_count": 0, "fetched_at_utc": iso(now_utc()),
        }
        n_obs = 0
        try:
            rec = fetch(
                sid, src["url"], method=src.get("method", "GET"),
                data=src.get("data"), headers=src.get("headers"),
            )
            save_snapshot(run_dir, rec)
            conn.execute(
                "INSERT INTO snapshots (run_id,source_id,url,method,fetched_at_utc,http_status,ok,error,sha256,byte_count,path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, sid, rec.get("final_url") or rec["url"], rec["method"], rec["fetched_at_utc"],
                 rec.get("status"), 1 if rec.get("ok") else 0, rec.get("error"), rec.get("sha256"),
                 rec.get("byte_count"), rec.get("path")),
            )
            snap_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            if sid == "fx_usd" and rec.get("ok"):
                try:
                    fx.load(rec)
                    if sea_index.sea_active_for_root(ROOT):
                        log.info(
                            "FX loaded: 1 EUR=%s USD, 1 GBP=%s USD, 1 SGD=%s USD, 1 MYR=%s USD, 1 IDR=%s USD, 1 VND=%s USD, asof=%s",
                            fx.rate("EUR"), fx.rate("GBP"), fx.rate("SGD"), fx.rate("MYR"),
                            fx.rate("IDR"), fx.rate("VND"), fx.asof_utc,
                        )
                    else:
                        log.info("FX loaded: 1 EUR=%s USD, 1 GBP=%s USD, asof=%s", fx.rate("EUR"), fx.rate("GBP"), fx.asof_utc)
                except Exception:
                    log.exception("FX parse failed")
                    rec["ok"] = False
                    rec["error"] = (rec.get("error") or "") + " | FX parse failed"
            parser = src.get("parser")
            if parser and rec.get("ok") and rec.get("body"):
                try:
                    rows = parser(rec, fx) or []
                    for row in rows:
                        row["run_id"] = run_id
                        row["snapshot_id"] = snap_id
                        row["snapshot_hash"] = rec.get("sha256")
                        row["snapshot_path"] = rec.get("path")
                        row["observed_at_utc"] = rec["fetched_at_utc"]
                        row["asof_date_pt"] = asof_date_pt
                        if not row.get("source_id"):
                            row["source_id"] = sid
                        mark_observation_only(row)
                        enrich_observation(row, snapshot_path=rec.get("path"))
                    rows = drop_competitor_observations(rows, context=f"parse:{sid}")
                    rows = sea_index.drop_entity_list_observations(rows, context=f"parse:{sid}")
                    insert_observations(conn, rows)
                    observations.extend(rows)
                    n_obs = len(rows)
                    log.info("%s: %s observations", sid, n_obs)
                except Exception:
                    log.exception("parse failed for %s", sid)
                    rec["error"] = (rec.get("error") or "") + f" | parse failed: {traceback.format_exc()[-400:]}"
            elif not rec.get("ok"):
                log.warning("%s failed: %s %s", sid, rec.get("status"), rec.get("error"))
            else:
                log.info("%s fetched ok, no parser or empty body", sid)
        except Exception:
            log.exception("source %s crashed (continuing)", sid)
            rec["error"] = traceback.format_exc()[-500:]
        health.append({
            "source_id": sid, "url": src["url"], "ok": bool(rec.get("ok")),
            "http_status": rec.get("status"), "error": rec.get("error"),
            "observation_count": n_obs, "fetched_at_utc": rec.get("fetched_at_utc"),
            "sha256": rec.get("sha256"),
        })
        conn.execute(
            "INSERT INTO source_health (run_id,asof_date_pt,source_id,url,ok,http_status,error,observation_count,fetched_at_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, asof_date_pt, sid, src["url"], 1 if rec.get("ok") else 0, rec.get("status"),
             rec.get("error"), n_obs, rec.get("fetched_at_utc")),
        )
        conn.commit()
        time.sleep(0.15)

    computed = now_utc()
    computed_pt = to_pt(computed)
    observations = drop_competitor_observations(observations, context="pre-compute")
    observations = sea_index.drop_entity_list_observations(observations, context="pre-compute")
    index_rows = compute_index(observations, asof_date_pt, iso(computed), iso(computed_pt))
    index_rows = apply_publication_guards(conn, index_rows, health, asof_date_pt, run_id=run_id)
    wrote = upsert_index(conn, index_rows, health=health)
    conn.commit()
    # Always publish the rows actually stored for this asof (may be an earlier same-day run).
    stored = load_index_rows_for_date(conn, asof_date_pt)
    if stored:
        if not wrote:
            log.warning("publishing earlier same-day daily_index for asof=%s (later run not written)", asof_date_pt)
        index_rows = stored
    latest, history, dashboard = write_outputs(
        conn, run_id, asof_date_pt, iso(computed_pt), iso(computed), health, observations, index_rows, fx
    )
    latest, history, dashboard, _sea = maybe_attach_sea_and_rewrite(
        latest, history, dashboard, observations, fx, asof_date_pt, iso(computed), iso(computed_pt)
    )
    rq = export_raw_quotes(conn, asof_date_pt, run_id)
    ew = write_event_windows(conn, asof_date_pt)
    conn.close()

    log.info("sold_count=%s observations=%s", latest["sold_count_today"], latest["observation_count"])
    log_index_summary(latest)
    log.info("wrote %s %s %s", LATEST_PATH, HISTORY_PATH, DASHBOARD_DATA_PATH)
    return 0



def _fx_from_observations(observations: list[dict]) -> FX:
    fx = FX()
    for o in observations:
        if o.get("fx_source"):
            fx.source = o.get("fx_source")
            fx.asof_utc = o.get("fx_asof_utc")
            break
    # Recover USD-per-unit from stored observation FX columns.
    for o in observations:
        ccy = (o.get("currency") or "").upper()
        rate = o.get("fx_usd_per_unit")
        if ccy and rate:
            fx.usd_per[ccy] = float(rate)
    fx.usd_per["USD"] = 1.0
    if fx.usd_per.get("EUR"):
        fx.raw_rates["EUR"] = 1.0 / fx.usd_per["EUR"]
    if fx.usd_per.get("GBP"):
        fx.raw_rates["GBP"] = 1.0 / fx.usd_per["GBP"]
    return fx


def recompute_from_run(run_id: str) -> int:
    """Recompute official index from a stored run (no new fetch).

    Used when official-inclusion rules change: snapshots stay append-only,
    observation-only flags are corrected, daily_index is upserted for that
    as-of date, and JSON outputs are rewritten.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    computed = now_utc()
    computed_pt = to_pt(computed)
    log_path = LOG_DIR / f"{run_id}_recompute_{computed_pt.strftime('%Y%m%dT%H%M%S')}_PT.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    conn = connect_db()
    already_removed = scrub_competitor_rows_from_db(conn)
    if already_removed:
        log.error("COMPETITOR_EXCLUDED: removed %s pre-existing observation(s) from nav.db before recompute",
                  len(already_removed))
    el_removed = sea_index.scrub_entity_list_rows_from_db(conn)
    if el_removed:
        log.error("ENTITY_LIST_EXCLUDED: removed %s pre-existing observation(s) from nav.db before recompute",
                  len(el_removed))
    if sea_index.sea_active_for_root(ROOT):
        migrate_sea_schema(conn)

    raw_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    if not raw_rows:
        log.error("no observations for run_id=%s", run_id)
        return 2
    asof_date_pt = raw_rows[0]["asof_date_pt"]
    raw_rows = drop_competitor_observations(raw_rows, context=f"recompute:{run_id}")
    raw_rows = sea_index.drop_entity_list_observations(raw_rows, context=f"recompute:{run_id}")
    corrected = 0
    for row in raw_rows:
        before = (row.get("include_in_market"), row.get("include_in_income"), row.get("include_in_cost_new"))
        mark_observation_only(row)
        after = (row.get("include_in_market"), row.get("include_in_income"), row.get("include_in_cost_new"), row.get("notes"))
        if before != after[:3] or (row.get("source_id") in OBSERVATION_ONLY_SOURCES):
            conn.execute(
                "UPDATE observations SET include_in_market=?, include_in_income=?, "
                "include_in_cost_new=?, notes=? WHERE id=?",
                (row["include_in_market"], row["include_in_income"], row["include_in_cost_new"],
                 row.get("notes"), row["id"]),
            )
            if before != after[:3]:
                corrected += 1
    conn.commit()
    log.info("recompute run_id=%s asof=%s; corrected official flags on %s rows", run_id, asof_date_pt, corrected)

    fx_preview = _fx_from_observations(raw_rows)
    fx_snap_preview = SNAPSHOT_ROOT / run_id / "fx_usd.bin"
    if fx_snap_preview.exists():
        try:
            fx_preview.load({
                "ok": True, "body": fx_snap_preview.read_bytes(),
                "fetched_at_utc": fx_preview.asof_utc or iso(computed),
            })
        except Exception:
            log.exception("could not reload FX snapshot before backfill")
    bf = backfill_from_snapshots(conn, run_id, fx_preview)
    log.info("recompute backfill updated=%s inserted=%s", bf.get("updated"), bf.get("inserted"))
    raw_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    raw_rows = drop_competitor_observations(raw_rows, context=f"recompute-after-backfill:{run_id}")
    raw_rows = sea_index.drop_entity_list_observations(raw_rows, context=f"recompute-after-backfill:{run_id}")

    health_rows = [dict(r) for r in conn.execute(
        "SELECT source_id, url, ok, http_status, error, observation_count, fetched_at_utc "
        "FROM source_health WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    health = []
    for h in health_rows:
        item = {
            "source_id": h["source_id"], "url": h["url"], "ok": bool(h["ok"]),
            "http_status": h["http_status"], "error": h["error"],
            "observation_count": h["observation_count"], "fetched_at_utc": h["fetched_at_utc"],
        }
        if h["source_id"] in OBSERVATION_ONLY_SOURCES:
            item["observation_only"] = True
            item["observation_only_reason"] = OBSERVATION_ONLY_SOURCES[h["source_id"]]
        health.append(item)

    fx = _fx_from_observations(raw_rows)
    # Prefer the archived FX snapshot if present.
    fx_snap = SNAPSHOT_ROOT / run_id / "fx_usd.bin"
    if fx_snap.exists():
        try:
            rec = {
                "ok": True, "body": fx_snap.read_bytes(),
                "fetched_at_utc": fx.asof_utc or iso(computed),
            }
            fx.load(rec)
        except Exception:
            log.exception("could not reload FX snapshot; using rates stored on observations")

    index_rows = compute_index(raw_rows, asof_date_pt, iso(computed), iso(computed_pt))
    index_rows = apply_publication_guards(conn, index_rows, health, asof_date_pt, run_id=run_id)
    wrote = upsert_index(conn, index_rows, health=health)
    conn.commit()
    stored = load_index_rows_for_date(conn, asof_date_pt)
    if stored:
        if not wrote:
            log.warning("publishing earlier same-day daily_index for asof=%s (recompute not written)", asof_date_pt)
        index_rows = stored
    latest, history, dashboard = write_outputs(
        conn, run_id, asof_date_pt, iso(computed_pt), iso(computed), health, raw_rows, index_rows, fx
    )
    latest, history, dashboard, _sea = maybe_attach_sea_and_rewrite(
        latest, history, dashboard, raw_rows, fx, asof_date_pt, iso(computed), iso(computed_pt)
    )
    export_raw_quotes(conn, asof_date_pt, run_id)
    write_event_windows(conn, asof_date_pt)
    conn.close()
    log.info("sold_count=%s observations=%s official=%s", latest["sold_count_today"],
             latest["observation_count"], latest["official_observation_counts"])
    log_index_summary(latest)
    log.info("wrote %s %s %s", LATEST_PATH, HISTORY_PATH, DASHBOARD_DATA_PATH)
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ComputeNAV v0 daily pipeline")
    parser.add_argument(
        "--recompute-from-run",
        metavar="RUN_ID",
        help="Recompute official NAV from a stored run (no fetch). Snapshots stay append-only.",
    )
    parser.add_argument(
        "--sea-trial",
        action="store_true",
        help=(
            "Internal SEA trial: fetch SEA+FX into COMPUTENAV_DATA_DIR "
            "(default /workspace/computenav-sea-test). Does not write official nav.db/output."
        ),
    )
    args = parser.parse_args()
    if args.recompute_from_run:
        sys.exit(recompute_from_run(args.recompute_from_run))
    if args.sea_trial:
        sys.exit(run_sea_trial())
    sys.exit(run())
