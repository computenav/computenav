#!/usr/bin/env python3
"""ComputeNAV v0 daily pipeline.

Fetches public sources that worked in the 2026-11-16 survey, archives raw
snapshots (append-only) with timestamp + SHA-256, normalizes observations into
SQLite, and computes *indicative* per-model NAV values.

Sold transactions are expected to be zero until licensed market-tape access
exists. Income and cost legs use clearly labeled to-be-calibrated placeholders.

Usage:
    python3 /workspace/computenav/run_daily.py
"""

from __future__ import annotations

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
        "new_price_source": "优先使用 Genius HPC 同页 New From（欧元未税经销商起价），不是 NVIDIA MSRP，也不是美国渠道价。",
    },
    "market": {
        "sold_weight": "n/a (sold=0)",
        "listing_rule": "仅使用可解析的经销商挂牌（Genius HPC 在售、Creative Compute 询价）。AnchorGPU（未开放下单）、第三方汇总（Clustermelt/Mulligan）、不可售整机、以及未扣除主机部件的整机简单除法，不进入官方市场法中位数。",
        "aggregator": "unweighted median (weights to-be-calibrated)",
    },
    "composite": {
        "rule": "四只子指数的等权平均（装机量权数待校准）。子指数指示性 NAV 为当日可得的市场法/收益法/成本法的等权平均（三法权重待校准）。",
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
}


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
    return bool(row.get(flag)) and row.get("source_id") not in OBSERVATION_ONLY_SOURCES


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


MODEL_KEYS = ["H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB"]
MODEL_LABELS = {
    "H100_SXM": "H100 SXM",
    "H100_PCIe": "H100 PCIe",
    "A100_80GB": "A100 80GB",
    "A100_40GB": "A100 40GB",
    "COMPOSITE": "综合（等权，待校准）",
}

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
            out.append(attach_fx(assign_model_key(obs(
                source_id="geniushpc", source_name="Genius HPC New From", source_url=rec["url"],
                observation_type="new_from_list_server", sold_vs_listed="new_list",
                model=model, form_factor=form, vram_gb=vram, condition="new_server",
                currency="EUR", price=new_price, unit="per_8gpu_server",
                notes="DGX H100 新机 New From，8 卡整机，未扣除主机部件，不进入官方单卡成本法锚点。",
                include_in_cost_new=0,
                raw_label=f"Genius HPC New From DGX H100 €{new_price:,.2f}",
            )), fx))
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
        if "H100" not in uname and "A100" not in uname:
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
            out.append(attach_fx(assign_model_key(obs(
                source_id="runpod", source_name="RunPod GraphQL gpuTypes", source_url=rec["url"],
                observation_type="rental_list", sold_vs_listed="rental_list",
                model=model, form_factor=form, vram_gb=vram, currency="USD",
                price=float(price), unit="per_gpu_hour",
                notes=f"{label} list; stock={stock}。Secure 不进入官方中位数，以免与 Community 重复且市场层级不同。",
                include_in_income=1 if include and form != "NVL" else 0,
                raw_label=f"RunPod {label} {name} {vram}GB ${float(price):.2f}/hr",
            )), fx))
    return out


def _rental_row(fx, source_id, name, url, model, form, vram, price, unit="per_gpu_hour", notes="", include=True, label=None):
    return attach_fx(assign_model_key(obs(
        source_id=source_id, source_name=name, source_url=url,
        observation_type="rental_list", sold_vs_listed="rental_list",
        model=model, form_factor=form, vram_gb=vram, currency="USD",
        price=price, unit=unit, notes=notes,
        include_in_income=1 if include and form != "NVL" else 0,
        raw_label=label or f"{name} {model} {form} {vram}GB ${price}/hr",
    )), fx)


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
                              label=f"DO HGX H100 on-demand ${price}/hr"))
    m = re.search(r"NVIDIA HGX H100 \$([0-9.]+) /GPU/hour 12 Month Reserved", txt)
    if m:
        price = money_to_float(m.group(1))
        out.append(_rental_row(fx, "digitalocean", "DigitalOcean GPU droplets", rec["url"],
                              "H100", "SXM", 80, price, notes="12 个月预留，不进入官方按需中位数。", include=False,
                              label=f"DO HGX H100 12mo reserved ${price}/hr"))
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
                                   include=False, label=f"Crusoe H100 HGX {tag} ${price}/hr (tenor unconfirmed)"))
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
]


def compute_index(observations, asof_date_pt, computed_at_utc, computed_at_pt):
    observations = drop_competitor_observations(list(observations or []), context="compute_index")
    rows = []
    per_model = {}
    for key in MODEL_KEYS:
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

    navs = [per_model[k]["nav_indicative_usd"] for k in MODEL_KEYS if per_model[k]["nav_indicative_usd"] is not None]
    composite = (sum(navs) / len(navs)) if navs else None
    rows.append({
        "asof_date_pt": asof_date_pt, "computed_at_utc": computed_at_utc, "computed_at_pt": computed_at_pt,
        "model_key": "COMPOSITE", "market_usd": None, "income_usd": None, "cost_usd": None,
        "nav_indicative_usd": composite, "market_n": None, "income_n": None, "cost_n": None,
        "rental_median_usd_per_hour": None, "new_card_usd": None,
        "quality_flag": "equal_weight_of_four_models;installed_base_weights=to-be-calibrated;sold_n=0",
        "assumptions_json": json.dumps(ASSUMPTIONS, ensure_ascii=False),
        "breakdown_json": json.dumps({
            "components": {k: None if per_model[k]["nav_indicative_usd"] is None else round(per_model[k]["nav_indicative_usd"], 2)
                           for k in MODEL_KEYS},
            "n_models": len(navs),
            "composite_usd": None if composite is None else round(composite, 2),
        }, ensure_ascii=False),
    })
    return rows


OBS_COLS = [
    "run_id", "snapshot_id", "snapshot_hash", "source_id", "source_name", "source_url",
    "observation_type", "sold_vs_listed", "model", "form_factor", "vram_gb", "condition",
    "currency", "price", "unit", "usd_price", "usd_per_card", "usd_per_gpu_hour",
    "fx_usd_per_unit", "fx_source", "fx_asof_utc", "observed_at_utc", "asof_date_pt",
    "notes", "include_in_market", "include_in_income", "include_in_cost_new", "model_key", "raw_label",
]

IDX_COLS = [
    "asof_date_pt", "computed_at_utc", "computed_at_pt", "model_key",
    "market_usd", "income_usd", "cost_usd", "nav_indicative_usd",
    "market_n", "income_n", "cost_n", "rental_median_usd_per_hour", "new_card_usd",
    "quality_flag", "assumptions_json", "breakdown_json",
]


def insert_observations(conn, rows):
    rows = drop_competitor_observations(rows, context="insert_observations")
    if not rows:
        return
    q = "INSERT INTO observations (" + ",".join(OBS_COLS) + ") VALUES (" + ",".join(["?"] * len(OBS_COLS)) + ")"
    conn.executemany(q, [[r.get(c) for c in OBS_COLS] for r in rows])


def upsert_index(conn, rows):
    q = (
        "INSERT INTO daily_index (" + ",".join(IDX_COLS) + ") VALUES (" + ",".join(["?"] * len(IDX_COLS)) + ") "
        "ON CONFLICT(asof_date_pt, model_key) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in IDX_COLS if c not in ("asof_date_pt", "model_key"))
    )
    conn.executemany(q, [[r.get(c) for c in IDX_COLS] for r in rows])


def write_outputs(conn, run_id, asof_date_pt, computed_at_pt, computed_at_utc, health, observations, index_rows, fx):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    hist_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_index ORDER BY asof_date_pt, model_key"
    ).fetchall()]
    latest_obs = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY source_id, id", (run_id,)
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
        }
        if r["model_key"] == "COMPOSITE":
            latest["composite"] = item
        else:
            latest["models"][r["model_key"]] = item

    history = {
        "generated_at_pt": computed_at_pt,
        "series": {},
        "dates": sorted({r["asof_date_pt"] for r in hist_rows}),
    }
    for key in MODEL_KEYS + ["COMPOSITE"]:
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

    for src in SOURCE_PLAN:
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
        if sid in skip:
            log.info("%s skipped via COMPUTENAV_SKIP_SOURCES", sid)
            health.append({"source_id": sid, "url": src["url"], "ok": False, "skipped": True,
                           "http_status": None, "error": "skipped (COMPUTENAV_SKIP_SOURCES)",
                           "observation_count": 0, "fetched_at_utc": None, "sha256": None})
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
                        row["observed_at_utc"] = rec["fetched_at_utc"]
                        row["asof_date_pt"] = asof_date_pt
                        if not row.get("source_id"):
                            row["source_id"] = sid
                        mark_observation_only(row)
                    rows = drop_competitor_observations(rows, context=f"parse:{sid}")
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
    index_rows = compute_index(observations, asof_date_pt, iso(computed), iso(computed_pt))
    upsert_index(conn, index_rows)
    conn.commit()
    latest, history, dashboard = write_outputs(
        conn, run_id, asof_date_pt, iso(computed_pt), iso(computed), health, observations, index_rows, fx
    )
    conn.close()

    log.info("sold_count=%s observations=%s", latest["sold_count_today"], latest["observation_count"])
    for k, v in latest["models"].items():
        log.info("NAV %s: indicative=%s market=%s income=%s cost=%s", k,
                 v["nav_indicative_usd"], v["market_usd"], v["income_usd"], v["cost_usd"])
    if latest["composite"]:
        log.info("COMPOSITE=%s", latest["composite"]["nav_indicative_usd"])
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

    raw_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM observations WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()]
    if not raw_rows:
        log.error("no observations for run_id=%s", run_id)
        return 2
    asof_date_pt = raw_rows[0]["asof_date_pt"]
    raw_rows = drop_competitor_observations(raw_rows, context=f"recompute:{run_id}")
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
    upsert_index(conn, index_rows)
    conn.commit()
    latest, history, dashboard = write_outputs(
        conn, run_id, asof_date_pt, iso(computed_pt), iso(computed), health, raw_rows, index_rows, fx
    )
    conn.close()
    log.info("sold_count=%s observations=%s official=%s", latest["sold_count_today"],
             latest["observation_count"], latest["official_observation_counts"])
    for k, v in latest["models"].items():
        log.info("NAV %s: indicative=%s market=%s income=%s cost=%s", k,
                 v["nav_indicative_usd"], v["market_usd"], v["income_usd"], v["cost_usd"])
    if latest["composite"]:
        log.info("COMPOSITE=%s", latest["composite"]["nav_indicative_usd"])
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
    args = parser.parse_args()
    if args.recompute_from_run:
        sys.exit(recompute_from_run(args.recompute_from_run))
    sys.exit(run())
