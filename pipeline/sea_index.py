#!/usr/bin/env python3
"""Southeast Asia GPU sub-index helpers for the ComputeNAV daily pipeline.

Loaded by run_daily.py. Official NVIDIA composite and MI300X are unchanged:
SEA observations never set official v0 inclusion, and is_official() rejects
SEA source ids. SEA is enabled by default on non-official data trees (CI uses a
temporary directory). Writes into the official /workspace/computenav tree are
still refused unless COMPUTENAV_SEA_ALLOW_OFFICIAL=1. Set COMPUTENAV_ENABLE_SEA=0
to disable. The public SEA card is a regional basket for Singapore, Malaysia,
Indonesia and Vietnam only (China and Thailand are not in the public basket).
"""

from __future__ import annotations

import html as htmlmod
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

OFFICIAL_TREE = Path("/workspace/computenav")

SEA_BATCH1_REGIONS = ("SG", "MY", "ID", "VN")  # public basket countries; TH is not included
SEA_PUBLIC_REGIONS = SEA_BATCH1_REGIONS
# Philippines remains monitor-only and is not computed.
SEA_MODEL_KEYS = (
    "H100_SXM",
    "H100_PCIe",
    "A100_80GB",
    "A100_40GB",
    "H200_NVL",
    "H200_SXM",
    "H200",
)
US_COMPARE_MAP = {
    "H100_SXM": "H100_SXM",
    "H100_PCIe": "H100_PCIe",
    "A100_80GB": "A100_80GB",
    "A100_40GB": "A100_40GB",
}

# Public dashboard basket: one regional sub-index, not in the NVIDIA composite.
# H200 form factors are pooled (the official NVIDIA composite does not include H200).
SEA_BASKET_KEY = "SEA"
SEA_BASKET_MEMBERS = ("H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB", "H200")
SEA_BASKET_MAP = {
    "H100_SXM": "H100_SXM",
    "H100_PCIe": "H100_PCIe",
    "A100_80GB": "A100_80GB",
    "A100_40GB": "A100_40GB",
    "H200": "H200",
    "H200_NVL": "H200",
    "H200_SXM": "H200",
}
SEA_COUNTRY_LABELS = {
    "SG": "Singapore",
    "MY": "Malaysia",
    "ID": "Indonesia",
    "VN": "Vietnam",
}
SEA_PUBLIC_LABEL = (
    "Southeast Asia GPU (Singapore, Malaysia, Indonesia, Vietnam) "
    "— separate sub-index, not in composite"
)

def basket_member_key(model_key: Optional[str]) -> Optional[str]:
    return SEA_BASKET_MAP.get(model_key or "")

# Official valuation uses identity factors (P=R=1). The indicative grid is
# computed only as an internal comparison and is not the official SEA NAV.
OFFICIAL_P = 1.0
OFFICIAL_R = 1.0

SOURCE_CLASS_BY_ID = {
    "acc_sg": "local_cloud_official_list",
    "novagpu_my": "local_cloud_official_list",
    "teamcloud_my": "local_cloud_official_list",
    "lintasarta_deka": "local_cloud_official_list",
    "biznet_neo": "local_cloud_official_list",
    "biznet_pricelist": "local_cloud_official_list",
    "fpt_vn": "local_cloud_official_list",
    "siam_th": "starting_at_list",
    "syndrome_ag": "wholesale_fob_mixed_hub",
}

INDICATIVE_P = {
    "us_hq_cloud_official_list": 1.00,
    "local_cloud_official_list": 1.00,
    "authorized_dealer_invoiceable": 0.90,
    "wholesale_fob_mixed_hub": 0.75,
    "starting_at_list": 0.00,  # not used in official income; haircut path also drops
    "c2c_marketplace": 0.50,
    "unprovenanced": 0.00,
}
INDICATIVE_R = {
    "SG": 0.95,
    "MY": 0.90,
    "TH": 0.90,
    "ID": 0.90,
    "VN": 0.95,
    "PH": 1.00,
}

# Entity List, Federal Register Document 2026-23999, effective 2026-11-23.
# Matching is by source id, display name, and URL/domain only — never page body.
ENTITY_LIST_EXCLUDED_SOURCES = {
    "msfusion": "Entity List (2026-11-23): MSFusion Sdn. Bhd. — excluded outright.",
    "msfusion_sdn_bhd": "Entity List (2026-11-23): MSFusion Sdn. Bhd. — excluded outright.",
    "pt_megaspeed_international": "Entity List (2026-11-23): PT Megaspeed International — excluded outright.",
    "speedmatrix": "Entity List (2026-11-23): Speedmatrix Sdn Bhd — excluded outright.",
    "speedmatrix_sdn_bhd": "Entity List (2026-11-23): Speedmatrix Sdn Bhd — excluded outright.",
    "apex_enterprise_solutions": "Entity List (2026-11-23): Apex Enterprise Solutions (Singapore) Private Limited — excluded outright.",
    "megaspeed": "Entity List (2026-11-23): Megaspeed — excluded outright.",
    "megaspeed_international": "Entity List (2026-11-23): Megaspeed International Pte. Ltd. — excluded outright.",
    "megaspeed_international_pte": "Entity List (2026-11-23): Megaspeed International Pte. Ltd. — excluded outright.",
    "megaspeed_thailand": "Entity List (2026-11-23): Megaspeed (Thailand) Co., Ltd. — excluded outright.",
    "swiftdata": "Entity List (2026-11-23): Swiftdata Pte. Ltd. — excluded outright.",
    "swiftdata_pte": "Entity List (2026-11-23): Swiftdata Pte. Ltd. — excluded outright.",
}

ENTITY_LIST_EXCLUDED_DOMAINS = {
    "megaspeed.com",
    "megaspeed.sg",
    "megaspeed.international",
    "msfusion.com",
    "speedmatrix.com",
    "swiftdata.com",
    "swiftdata.sg",
}

# Tight patterns on id + name + url only. Do not use short tokens such as "apex".
ENTITY_LIST_EXCLUDED_PATTERNS = (
    (re.compile(r"\bmegaspeed\b", re.I),
     "Entity List (2026-11-23): Megaspeed-linked entity — excluded outright."),
    (re.compile(r"\bmsfusion\b", re.I),
     "Entity List (2026-11-23): MSFusion Sdn. Bhd. — excluded outright."),
    (re.compile(r"\bspeedmatrix\b", re.I),
     "Entity List (2026-11-23): Speedmatrix Sdn Bhd — excluded outright."),
    (re.compile(r"\bswiftdata\b", re.I),
     "Entity List (2026-11-23): Swiftdata Pte. Ltd. — excluded outright."),
    (re.compile(r"apex\s+enterprise\s+solutions", re.I),
     "Entity List (2026-11-23): Apex Enterprise Solutions (Singapore) Private Limited — excluded outright."),
    (re.compile(r"pt\s+megaspeed\s+international", re.I),
     "Entity List (2026-11-23): PT Megaspeed International — excluded outright."),
)


def sea_enabled() -> bool:
    """True unless COMPUTENAV_ENABLE_SEA is an explicit off value.

    Historical trial runs set the flag to 1. Public CI and temp trees now
    default to on; official-tree writes are still blocked in sea_active_for_root.
    """
    v = (os.environ.get("COMPUTENAV_ENABLE_SEA") or "").strip().lower()
    if v in {"0", "false", "no", "off"}:
        return False
    return True


def sea_allow_official_tree() -> bool:
    v = (os.environ.get("COMPUTENAV_SEA_ALLOW_OFFICIAL") or "").strip().lower()
    return v in {"1", "true", "yes", "on"}


def is_official_data_tree(root: Path) -> bool:
    try:
        return Path(root).resolve() == OFFICIAL_TREE.resolve()
    except Exception:
        return str(root) == str(OFFICIAL_TREE)


def sea_active_for_root(root: Path) -> bool:
    """True when SEA should run for this data directory.

    Never write SEA into the official /workspace/computenav tree unless
    COMPUTENAV_SEA_ALLOW_OFFICIAL=1. On any other tree (CI temp dir, trial
    dir), SEA is on unless COMPUTENAV_ENABLE_SEA is explicitly off.
    """
    if not sea_enabled():
        return False
    if is_official_data_tree(root) and not sea_allow_official_tree():
        log.info(
            "SEA is not written into the official tree (%s). Set "
            "COMPUTENAV_DATA_DIR to a temp directory, or "
            "COMPUTENAV_SEA_ALLOW_OFFICIAL=1, to compute the SEA sub-index.",
            root,
        )
        return False
    return True


def _registrable_domain(host: str) -> str:
    host = (host or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def entity_list_exclusion_reason(
    source_id: Optional[str] = None,
    source_name: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Optional[str]:
    sid = (source_id or "").strip()
    if sid in ENTITY_LIST_EXCLUDED_SOURCES:
        return ENTITY_LIST_EXCLUDED_SOURCES[sid]
    url = (source_url or "").strip()
    if url:
        try:
            host = _registrable_domain(urllib.parse.urlparse(url).hostname or "")
        except Exception:
            host = ""
        if host:
            for blocked in ENTITY_LIST_EXCLUDED_DOMAINS:
                if host == blocked or host.endswith("." + blocked):
                    return f"Entity List domain {host} — excluded outright."
    hay = " ".join(x for x in (sid, source_name or "", url) if x)
    if hay:
        for pat, reason in ENTITY_LIST_EXCLUDED_PATTERNS:
            if pat.search(hay):
                return reason
    return None


def drop_entity_list_observations(observations: list[dict], *, context: str = "") -> list[dict]:
    kept: list[dict] = []
    for o in observations:
        reason = entity_list_exclusion_reason(
            o.get("source_id"), o.get("source_name"), o.get("source_url"),
        )
        if reason:
            log.error(
                "ENTITY_LIST_EXCLUDED %s source_id=%s name=%s url=%s — dropped, not stored or used: %s",
                context or "observation",
                o.get("source_id"), o.get("source_name"), o.get("source_url"), reason,
            )
            continue
        kept.append(o)
    return kept


def scrub_entity_list_rows_from_db(conn: sqlite3.Connection) -> list[dict]:
    """Delete Entity List observations if any were ever stored. Raw files stay."""
    removed: list[dict] = []
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM observations").fetchall()]
    except sqlite3.Error:
        return removed
    for o in rows:
        reason = entity_list_exclusion_reason(o.get("source_id"), o.get("source_name"), o.get("source_url"))
        if not reason:
            continue
        conn.execute("DELETE FROM observations WHERE id=?", (o["id"],))
        removed.append(o)
        log.error(
            "ENTITY_LIST_EXCLUDED observation already in nav.db id=%s run_id=%s source_id=%s "
            "url=%s — deleted from observations (raw files kept): %s",
            o.get("id"), o.get("run_id"), o.get("source_id"), o.get("source_url"), reason,
        )
    if removed:
        conn.commit()
    return removed


def is_sea_source_id(source_id: Optional[str]) -> bool:
    return (source_id or "") in SEA_SOURCE_IDS


def sea_index_key(region: str, model_key: str) -> str:
    return f"SEA_{region}_{model_key}"


def parse_sea_key(key: str) -> Optional[tuple[str, str]]:
    if not key or not key.startswith("SEA_"):
        return None
    parts = key.split("_", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def parse_sea_money(text: str, currency: str) -> Optional[float]:
    """Parse a public price string in USD, MYR, IDR, THB, SGD or VND. Do not invent a number."""
    if text is None:
        return None
    raw = htmlmod.unescape(str(text))
    raw = re.sub(r"[\xa0\u202f]", " ", raw)
    # TeamCloud-style "RM 1 .96"
    raw = re.sub(r"(\d)\s+([.,])\s*(\d)", r"\1\2\3", raw)
    cur = (currency or "").upper()
    t = raw.strip()
    if cur == "IDR":
        t = re.sub(r"(?i)(?:rp|idr)\s*", "", t).strip()
        t = t.replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", t):
            t = t.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d+,\d+", t):
            t = t.replace(",", ".")
        elif re.fullmatch(r"\d+(\.\d+)?", t):
            pass
        else:
            return None
        try:
            val = float(t)
        except ValueError:
            return None
        return val if val > 0 else None
    if cur in {"MYR", "RM"}:
        t = re.sub(r"(?i)(?:myr|rm)\s*", "", t).strip()
        t = t.replace(" ", "")
        t = t.replace(",", "")
        try:
            val = float(t)
        except ValueError:
            return None
        return val if val > 0 else None
    if cur in {"VND", "DONG", "Đ"}:
        t = re.sub(r"(?i)(?:vnd|đ|₫|dong|vnđ)\s*", "", t).strip()
        t = t.replace(" ", "")
        if re.fullmatch(r"\d{1,3}(\.(?:\d{3}))+(,\d+)?", t):
            t = t.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(,(?:\d{3}))+(\.\d+)?", t):
            t = t.replace(",", "")
        elif re.fullmatch(r"\d+(?:[.,]\d+)?", t):
            t = t.replace(",", ".")
        else:
            return None
        try:
            val = float(t)
        except ValueError:
            return None
        return val if val > 0 else None
    if cur in {"USD", "SGD", "THB"}:
        t = re.sub(r"(?i)(?:usd|sgd|thb|s\$|\$|฿)\s*", "", t).strip()
        t = t.replace(" ", "").replace(",", "")
        try:
            val = float(t)
        except ValueError:
            return None
        return val if val > 0 else None
    t = re.sub(r"[^\d.,]", "", t)
    t = t.replace(",", "")
    try:
        val = float(t)
    except ValueError:
        return None
    return val if val > 0 else None


def _helpers():
    import run_daily as rd
    return rd


def _visible_text(raw: str) -> str:
    raw = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = htmlmod.unescape(raw)
    raw = re.sub(r"[\r\t]+", " ", raw)
    raw = re.sub(r" *\n *", "\n", raw)
    return raw


def _sea_row(fx, *, source_id, source_name, source_url, model, form, vram, price, currency,
             unit, region, tenor, quantity_config, notes, raw_label, raw_price_text=None,
             include_sea_income=0, include_sea_market=0, include_sea_cost=0,
             condition=None, observation_type="rental_list", sold_vs_listed="rental_list",
             availability=None):
    """Build a SEA observation. Official v0 include flags stay 0."""
    rd = _helpers()
    row = rd.obs(
        source_id=source_id, source_name=source_name, source_url=source_url,
        observation_type=observation_type, sold_vs_listed=sold_vs_listed,
        model=model, form_factor=form, vram_gb=vram, condition=condition,
        currency=currency, price=price, unit=unit,
        notes=notes, raw_label=raw_label, raw_price_text=raw_price_text,
        tenor=tenor, region=region, quantity_config=quantity_config,
        availability=availability, vendor="NVIDIA",
        include_in_market=0, include_in_income=0, include_in_cost_new=0,
        in_official_v0=0,
    )
    row = rd.attach_fx(rd.assign_model_key(row), fx)
    row = rd.enrich_observation(row)
    # Re-assert after enrich: SEA must never enter official v0 legs.
    row["include_in_market"] = 0
    row["include_in_income"] = 0
    row["include_in_cost_new"] = 0
    row["in_official_v0"] = 0
    row["index_family"] = "sea"
    row["include_in_sea_income"] = 1 if include_sea_income else 0
    row["include_in_sea_market"] = 1 if include_sea_market else 0
    row["include_in_sea_cost"] = 1 if include_sea_cost else 0
    if not row.get("model_key") and (model or "").upper() == "H100":
        # Form not stated; do not invent SXM vs PCIe.
        row["model_key"] = "H100"
    qc = (quantity_config or "")
    if "MIG" in qc.upper():
        row["model_key"] = "H100_MIG_20GB"
        row["include_in_sea_income"] = 0
        row["include_in_sea_market"] = 0
        row["include_in_sea_cost"] = 0
        row["sea_index_key"] = None
        return row
    key = None
    if region in SEA_PUBLIC_REGIONS and row.get("model_key"):
        key = sea_index_key(region, row["model_key"])
    row["sea_index_key"] = key
    return row


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_acc_sg(rec, fx):
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    out = []
    specs = [
        (r"NVIDIA HGX H100", "H100", "HGX", 80, True),
        (r"NVIDIA H100 PCIe", "H100", "PCIe", 80, True),
        (r"A100 80GB NVLINK", "A100", "NVLink", 80, True),
        (r"A100 80GB PCIe", "A100", "PCIe", 80, True),
        (r"A100 40GB NVLINK", "A100", "NVLink", 40, True),
        (r"A100 40GB PCIe", "A100", "PCIe", 40, True),
        ("A40", "A40", "PCIe", 48, False),  # observed, not an official SEA bucket
    ]
    for title, model, form, vram, official_income in specs:
        pat = re.compile(
            title + r".{0,220}?<div class=\"ftit\">\s*\$([0-9]+(?:\.[0-9]+)?)\s*</div>"
            r".{0,160}?GPU Component Cost Per Hour",
            re.I | re.S,
        )
        m = pat.search(raw)
        if not m:
            continue
        price = parse_sea_money(m.group(1), "USD")
        if not price:
            continue
        include = official_income and model in {"H100", "A100"}
        out.append(_sea_row(
            fx, source_id="acc_sg", source_name="ACC Singapore GPU leasing",
            source_url=rec.get("url"), model=model, form=form, vram=vram,
            price=price, currency="USD", unit="per_gpu_hour", region="SG",
            tenor="on_demand", quantity_config="1 gpu component",
            notes="Singapore public list; GPU component cost per hour only (vCPU/RAM extra, not added). Official v0 flags off.",
            raw_label=f"ACC {title} ${price}/hr GPU component",
            raw_price_text=f"${price:.2f}",
            include_sea_income=1 if include else 0,
            availability="list",
        ))
    return out


def parse_novagpu_my(rec, fx):
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    # Official SEA income: 1-GPU H200 NVL hourly only.
    h200 = re.search(
        r"H200 NVL.*?1 GPU.*?1 x 141 GB.*?MYR\s*([0-9]+(?:\.[0-9]+)?).*?MYR\s*([0-9,]+(?:\.[0-9]+)?)",
        text, re.I | re.S,
    )
    # Fallback: first Price/Hour after an H200 NVL heading, 1 GPU row.
    if not h200:
        h200 = re.search(
            r"H200 NVL[\s\S]{0,400}?1 GPU[\s\S]{0,200}?MYR\s*([0-9]+(?:\.[0-9]+)?)",
            text, re.I,
        )
    if h200:
        hourly = parse_sea_money(h200.group(1), "MYR")
        if hourly:
            out.append(_sea_row(
                fx, source_id="novagpu_my", source_name="IP ServerOne NovaGPU Malaysia",
                source_url=rec.get("url"), model="H200", form="NVL", vram=141,
                price=hourly, currency="MYR", unit="per_gpu_hour", region="MY",
                tenor="on_demand", quantity_config="1 gpu",
                notes="Malaysia public list, 1× H200 NVL 141GB hourly. 2-GPU row not double-counted. Official v0 flags off.",
                raw_label=f"NovaGPU H200 NVL 1x MYR {hourly}/hr",
                raw_price_text=f"MYR {hourly}",
                include_sea_income=1,
                availability="list",
            ))
    # Consumer RTX rows are on the same page but are not official SEA
    # data-center buckets. They are not parsed here: a loose regex can pick
    # up the first MYR amount (RTX 3090) for every card.
    return out


def parse_teamcloud_my(rec, fx):
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    m = re.search(
        r"H200 NVL[\s\S]{0,500}?1 GPU[\s\S]{0,200}?1 x 141 GB[\s\S]{0,200}?RM\s*([0-9]+(?:\s*[.,]\s*[0-9]+)?)",
        text, re.I,
    )
    if m:
        hourly = parse_sea_money(m.group(1), "MYR")
        if hourly:
            out.append(_sea_row(
                fx, source_id="teamcloud_my", source_name="TeamCloud GPU-Now Malaysia",
                source_url=rec.get("url"), model="H200", form="NVL", vram=141,
                price=hourly, currency="MYR", unit="per_gpu_hour", region="MY",
                tenor="on_demand", quantity_config="1 gpu",
                notes="Malaysia public list, 1× H200 NVL 141GB hourly. Monthly field on the page may be garbled and is not used. Official v0 flags off.",
                raw_label=f"TeamCloud H200 NVL 1x RM {hourly}/hr",
                raw_price_text=f"RM {hourly}",
                include_sea_income=1,
                availability="list",
            ))
    return out


def parse_lintasarta_deka(rec, fx):
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    # Full-card H100 80GB. 10-day minimum: stored, but not official on-demand income.
    m80 = re.search(
        r"NVIDIA H100.{0,80}?Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+).{0,80}?per hour.{0,80}?80GB\s*\(\s*1 GPU H100\s*\)",
        text, re.I | re.S,
    )
    if not m80:
        m80 = re.search(
            r"80GB\s*\(\s*1 GPU H100\s*\).{0,80}?Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)",
            text, re.I | re.S,
        )
    if not m80:
        # Visible-text order seen in 2026-11-27 fetch: label, Rp79.000, / per hour, 80GB (1 GPU H100)
        m80 = re.search(
            r"NVIDIA H100\s+Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)\s*/\s*per hour\s*80GB\s*\(\s*1 GPU H100\s*\)",
            text, re.I,
        )
    if m80:
        price = parse_sea_money(m80.group(1), "IDR")
        if price:
            out.append(_sea_row(
                fx, source_id="lintasarta_deka", source_name="Lintasarta Deka GPU Indonesia",
                source_url=rec.get("url"), model="H100", form="SXM5", vram=80,
                price=price, currency="IDR", unit="per_gpu_hour", region="ID",
                tenor="min_10_day", quantity_config="1 gpu",
                notes="Jakarta public list, 1× H100 80GB SXM5. Page requires a minimum 10-day commitment, so this is not official SEA on-demand income. Official v0 flags off.",
                raw_label=f"Lintasarta H100 80GB SXM5 Rp {m80.group(1)}/hr min 10 days",
                raw_price_text=f"Rp{m80.group(1)}",
                include_sea_income=0,
                availability="list",
            ))
    # MIG 20GB: never mix with a full card.
    mmig = re.search(
        r"NVIDIA H100\s+Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)\s*/\s*per hour\s*20GB\s*\(\s*MIG H100\s*\)",
        text, re.I,
    )
    if mmig:
        price = parse_sea_money(mmig.group(1), "IDR")
        if price:
            out.append(_sea_row(
                fx, source_id="lintasarta_deka", source_name="Lintasarta Deka GPU Indonesia",
                source_url=rec.get("url"), model="H100", form="SXM5", vram=20,
                price=price, currency="IDR", unit="per_gpu_hour", region="ID",
                tenor="min_10_day", quantity_config="MIG 20GB",
                notes="MIG 20GB slice, not a full H100. Observation only; not mixed into the full-card income median.",
                raw_label=f"Lintasarta H100 MIG 20GB Rp {mmig.group(1)}/hr",
                raw_price_text=f"Rp{mmig.group(1)}",
                include_sea_income=0,
                availability="list",
            ))
    return out


def parse_biznet_neo(rec, fx):
    """Biznet product or pricelist page. 1× H200 hourly enters official SEA income."""
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    # Pricelist shape: 1 GPU H200 / 141 GB / Rp119.000 / Rp80.000.000
    m = re.search(
        r"1 GPU H200.{0,80}?141\s*GB.{0,80}?Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)\s*Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)",
        text, re.I | re.S,
    )
    hourly = monthly = None
    if m:
        hourly = parse_sea_money(m.group(1), "IDR")
        monthly = parse_sea_money(m.group(2), "IDR")
    if hourly is None:
        m2 = re.search(
            r"1x\s*NVIDIA H200\s+1 GPU\s+141\s*GB.{0,240}?Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)\s*/\s*jam",
            text, re.I | re.S,
        )
        if m2:
            hourly = parse_sea_money(m2.group(1), "IDR")
    if hourly is None:
        m3 = re.search(
            r"1x\s*NVIDIA H200.{0,200}?141\s*GB.{0,200}?Rp\s*([0-9]{1,3}(?:\.[0-9]{3})+)",
            text, re.I | re.S,
        )
        if m3:
            hourly = parse_sea_money(m3.group(1), "IDR")
    if hourly is None:
        return out
    # Consistency check with monthly column when both present (672 hours ≈ month).
    notes = "Indonesia public list, 1× NVIDIA H200 141GB. Form factor is not stated on the page, so model_key is H200 (not invented as NVL or SXM). Official v0 flags off."
    if monthly and hourly:
        implied = hourly * 672
        if abs(implied - monthly) / monthly < 0.05:
            notes += f" Monthly Rp column is consistent with ~672 × hourly."
    out.append(_sea_row(
        fx, source_id=rec.get("source_id") or "biznet_neo",
        source_name="Biznet Gio NEO GPU Indonesia",
        source_url=rec.get("url"), model="H200", form=None, vram=141,
        price=hourly, currency="IDR", unit="per_gpu_hour", region="ID",
        tenor="on_demand", quantity_config="1 gpu",
        notes=notes,
        raw_label=f"Biznet NEO 1x H200 Rp {hourly}/hr",
        raw_price_text=f"Rp{hourly:,.0f}".replace(",", "."),
        include_sea_income=1,
        availability="list",
    ))
    return out


def parse_siam_th(rec, fx):
    """Starting-at on-demand figures: observation only, not official SEA income."""
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    m = re.search(
        r"On-demand pricing is available from.{0,40}?H200 at\s*\$([0-9]+(?:\.[0-9]+)?)/Hour.{0,40}?H100 at\s*\$([0-9]+(?:\.[0-9]+)?)/Hour",
        text, re.I | re.S,
    )
    if not m:
        return out
    h200 = parse_sea_money(m.group(1), "USD")
    h100 = parse_sea_money(m.group(2), "USD")
    if h100:
        out.append(_sea_row(
            fx, source_id="siam_th", source_name="SIAM.AI CLOUD Bangkok",
            source_url=rec.get("url"), model="H100", form=None, vram=80,
            price=h100, currency="USD", unit="per_gpu_hour", region="TH",
            tenor="starting_at", quantity_config="unspecified",
            notes="Bangkok page states on-demand pricing 'from' this figure. Starting-at is observation only and does not enter official SEA income.",
            raw_label=f"SIAM.AI H100 from ${h100}/hr",
            raw_price_text=f"from ${h100}/Hour",
            include_sea_income=0,
            availability="starting_at",
        ))
    if h200:
        out.append(_sea_row(
            fx, source_id="siam_th", source_name="SIAM.AI CLOUD Bangkok",
            source_url=rec.get("url"), model="H200", form=None, vram=141,
            price=h200, currency="USD", unit="per_gpu_hour", region="TH",
            tenor="starting_at", quantity_config="unspecified",
            notes="Bangkok page states on-demand pricing 'from' this figure. Starting-at is observation only and does not enter official SEA income.",
            raw_label=f"SIAM.AI H200 from ${h200}/hr",
            raw_price_text=f"from ${h200}/Hour",
            include_sea_income=0,
            availability="starting_at",
        ))
    return out


def parse_syndrome_ag(rec, fx):
    """FOB Singapore/Dubai wholesale asks. Observation only until the hub can be split."""
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    text = _visible_text(raw)
    out = []
    # Primary product on the page (title / wholesale price).
    specs = [
        (r"NVIDIA H100 80GB PCIe \(Refurbished\).*?wholesale price\s*\$([0-9,]+)",
         "H100", "PCIe", 80, "refurbished"),
        (r"NVIDIA H100 80GB PCIe — wholesale price\s*\$([0-9,]+)",
         "H100", "PCIe", 80, "wholesale_ask"),
        (r"NVIDIA H100 80GB SXM5\s*\$([0-9,]+)",
         "H100", "SXM5", 80, "ask"),
        (r"NVIDIA H200 NVL 141GB\s*\$([0-9,]+)",
         "H200", "NVL", 141, "ask"),
        (r"NVIDIA A100 80GB PCIe \(Used\)\s*\$([0-9,]+)",
         "A100", "PCIe", 80, "used"),
    ]
    seen = set()
    for pat, model, form, vram, cond in specs:
        m = re.search(pat, text, re.I | re.S)
        if not m:
            continue
        price = parse_sea_money(m.group(1), "USD")
        if not price:
            continue
        key = (model, form, vram, price, cond)
        if key in seen:
            continue
        seen.add(key)
        out.append(_sea_row(
            fx, source_id="syndrome_ag", source_name="SYNDROME AG (FOB Singapore / Dubai)",
            source_url=rec.get("url"), model=model, form=form, vram=vram,
            price=price, currency="USD", unit="per_card", region="SG",
            tenor=None, quantity_config="1 card", condition=cond,
            observation_type="listed", sold_vs_listed="listed",
            notes="Wholesale FOB Singapore / Dubai ask, not a sold print. Delivery hub is mixed, so this is not official SEA market for a single country bucket.",
            raw_label=f"SYNDROME {model} {form} {cond} ${price:,.0f} FOB SG/DXB",
            raw_price_text=f"${price:,.0f}",
            include_sea_market=0,
            include_sea_cost=0,
            availability="wholesale_ask",
        ))
    return out



def parse_fpt_vn(rec, fx):
    """FPT AI Factory public USD list (factory.fpt.ai/pricing).

    Admit only the 1x H100 SXM5 on-demand GPU Container / GPU VM rate in the
    page's "Southeast Asia" region. FPT is Vietnam-headquartered (Hanoi).
    Japan-labelled H200 rows, Dedicated Inference, Serverless Inference,
    fractional GPU slices, and multi-GPU SKUs are not official SEA income.
    Container and VM rows at the same 1x rate are counted once.
    """
    if _helpers()._blocked_or_challenge(rec):
        return []
    raw = rec["body"].decode("utf-8", "replace") if isinstance(rec.get("body"), (bytes, bytearray)) else str(rec.get("body") or "")
    chunks = re.split(r'<div class="api-pricing-wrapper"', raw)
    prices = []
    for ch in chunks[1:]:
        sm = re.match(r'[^>]*data-service="([^"]+)"', ch)
        service = (sm.group(1) if sm else "").strip()
        if service not in {"gpu-container", "gpu-vm"}:
            continue
        text = _visible_text(ch)
        # Each H100 heading is followed by a location, then 1x / 2x rows.
        for m in re.finditer(
            r"(?:NVIDIA\s+)?H100\s+SXM5[\s\S]{0,500}?1x\s+GPU\s+H100\s+SXM5[\s\S]{0,280}?\$([0-9]+(?:\.[0-9]+)?)",
            text, re.I,
        ):
            window = m.group(0)
            if re.search(r"\bJapan\b", window) and not re.search(r"Southeast Asia", window, re.I):
                continue
            if not re.search(r"Southeast Asia", window, re.I):
                continue
            if re.search(r"\b(?:1/2|1/4|1/8)\s*x\s*GPU\s*H100", window, re.I):
                continue
            price = parse_sea_money(m.group(1), "USD")
            if price:
                prices.append(price)
    if not prices:
        log.warning("fpt_vn: fetched the pricing page but found no 1x H100 SXM5 Southeast Asia list rate")
        return []
    uniq = sorted({round(float(p), 4) for p in prices})
    out = []
    for price in uniq:
        out.append(_sea_row(
            fx, source_id="fpt_vn", source_name="FPT AI Factory Vietnam",
            source_url=rec.get("url"), model="H100", form="SXM5", vram=80,
            price=price, currency="USD", unit="per_gpu_hour", region="VN",
            tenor="on_demand", quantity_config="1x GPU H100 SXM5",
            notes=(
                "Vietnam-headquartered FPT AI Factory public USD list for 1x H100 SXM5 "
                "in the page's Southeast Asia region (GPU Container / GPU VM, deduplicated). "
                "Japan H200, Dedicated Inference and fractional slices excluded. "
                "Official v0 flags off."
            ),
            raw_label=f"FPT AI Factory 1x H100 SXM5 ${price}/hr SEA",
            raw_price_text=f"${price:.2f}",
            include_sea_income=1,
            availability="list",
        ))
    return out


SEA_SOURCE_PLAN = [
    {"id": "acc_sg", "name": "ACC Singapore GPU leasing",
     "url": "https://www.acc-ai.com/GPULeasing",
     "method": "GET", "parser": parse_acc_sg, "family": "sea", "region": "SG"},
    {"id": "novagpu_my", "name": "IP ServerOne NovaGPU Malaysia",
     "url": "https://www.ipserverone.com/novagpu/",
     "method": "GET", "parser": parse_novagpu_my, "family": "sea", "region": "MY"},
    {"id": "teamcloud_my", "name": "TeamCloud GPU-Now Malaysia",
     "url": "https://www.teamcloud.my/gpu-cloud-hosting/",
     "method": "GET", "parser": parse_teamcloud_my, "family": "sea", "region": "MY"},
    {"id": "lintasarta_deka", "name": "Lintasarta Deka GPU Indonesia",
     "url": "https://www.lintasarta.net/en/dekagpu/",
     "method": "GET", "parser": parse_lintasarta_deka, "family": "sea", "region": "ID"},
    {"id": "biznet_neo", "name": "Biznet Gio NEO GPU Indonesia",
     "url": "https://www.biznetgio.com/product/neo-gpu",
     "method": "GET", "parser": parse_biznet_neo, "family": "sea", "region": "ID"},
    {"id": "biznet_pricelist", "name": "Biznet Gio pricelist",
     "url": "https://www.biznetgio.com/en/pricelist",
     "method": "GET", "parser": parse_biznet_neo, "family": "sea", "region": "ID"},
    {"id": "fpt_vn", "name": "FPT AI Factory Vietnam",
     "url": "https://factory.fpt.ai/pricing",
     "method": "GET", "parser": parse_fpt_vn, "family": "sea", "region": "VN"},
]
# Not fetched for the public basket: Thailand (outside the approved country
# list), wholesale FOB mixed-hub catalogues, and unlicensed C2C marketplaces.
SEA_SOURCE_PLAN_NOT_FETCHED = [
    {"id": "siam_th", "name": "SIAM.AI CLOUD Bangkok",
     "url": "https://siam.ai/nvidia-hseries/",
     "method": "GET", "parser": parse_siam_th, "family": "sea", "region": "TH",
     "skip_reason": "Thailand is not in the public SEA basket (SG, MY, ID, VN)."},
    {"id": "syndrome_ag", "name": "SYNDROME AG wholesale catalog",
     "url": "https://syndromeag.com/catalog/ai-accelerators/ai-nvidia-h100-80gb-refurb/",
     "method": "GET", "parser": parse_syndrome_ag, "family": "sea", "region": "SG",
     "skip_reason": "Wholesale FOB Singapore/Dubai mixed hub; observation-only, not fetched for the public basket."},
    {"id": "syndrome_ag_pcie", "name": "SYNDROME AG H100 PCIe catalog",
     "url": "https://syndromeag.com/catalog/ai-accelerators/ai-nvidia-h100-80gb-pcie/",
     "method": "GET", "parser": parse_syndrome_ag, "family": "sea", "region": "SG",
     "skip_reason": "Wholesale FOB Singapore/Dubai mixed hub; observation-only, not fetched for the public basket."},
]

SEA_SOURCE_IDS = {s["id"] for s in SEA_SOURCE_PLAN}
# Alias the second Syndrome catalog onto the same logical source class.
SOURCE_CLASS_BY_ID["syndrome_ag_pcie"] = "wholesale_fob_mixed_hub"
SOURCE_CLASS_BY_ID["biznet_pricelist"] = "local_cloud_official_list"


def indicative_factors(row: dict) -> tuple[float, float, str]:
    sid = row.get("source_id") or ""
    klass = SOURCE_CLASS_BY_ID.get(sid, "unprovenanced")
    region = (row.get("region") or "").upper()
    p = INDICATIVE_P.get(klass, 0.0)
    r = INDICATIVE_R.get(region, 1.0)
    return p, r, klass


def _leg_values(observations, key, flag, field, apply_indicative: bool):
    vals = []
    for o in observations:
        if o.get("sea_index_key") != key:
            continue
        if not o.get(flag):
            continue
        # Full-card on-demand income only; already encoded in include flags.
        v = o.get(field)
        if v is None or float(v) <= 0:
            continue
        v = float(v)
        if apply_indicative:
            p, r, klass = indicative_factors(o)
            if p <= 0:
                continue
            v = v * p * r
        vals.append(v)
    return vals


def _one_bucket(observations, key, asof, computed_at_utc, computed_at_pt, apply_indicative: bool):
    rd = _helpers()
    mkt = _leg_values(observations, key, "include_in_sea_market", "usd_per_card", apply_indicative)
    inc_r = _leg_values(observations, key, "include_in_sea_income", "usd_per_gpu_hour", apply_indicative)
    newp = _leg_values(observations, key, "include_in_sea_cost", "usd_per_card", apply_indicative)
    market = rd.median(mkt)
    r_med = rd.median(inc_r)
    income = rd.income_dcf(r_med) if r_med is not None else None
    new_med = rd.median(newp)
    frac = rd.ASSUMPTIONS["cost"]["unknown_age_remaining_fraction"]
    cost = (new_med * frac) if new_med is not None else None
    legs = [v for v in (market, income, cost) if v is not None]
    nav = (sum(legs) / len(legs)) if legs else None
    flags = ["sold_n=0", "income_assumptions=to-be-calibrated", "cost_age_fraction=to-be-calibrated",
             "not_in_nvidia_composite", "not_mixed_with_mi300x"]
    if apply_indicative:
        flags.append("indicative_discount_grid")
    else:
        flags.append("official_P=R=1")
    if not mkt:
        flags.append("no_official_listings")
    if r_med is None:
        flags.append("no_official_rentals")
    if new_med is None:
        flags.append("no_new_card_anchor")
    parsed = parse_sea_key(key) or ("", "")
    return {
        "asof_date_pt": asof,
        "computed_at_utc": computed_at_utc,
        "computed_at_pt": computed_at_pt,
        "model_key": key,
        "region": parsed[0],
        "base_model_key": parsed[1],
        "label": f"SEA {parsed[0]} {parsed[1].replace('_', ' ')}".strip(),
        "market_usd": market,
        "income_usd": income,
        "cost_usd": cost,
        "nav_indicative_usd": nav,
        "market_n": len(mkt),
        "income_n": len(inc_r),
        "cost_n": len(newp),
        "rental_median_usd_per_hour": r_med,
        "new_card_usd": new_med,
        "quality_flag": ";".join(flags),
        "legs_used": len(legs),
    }


def _public_status_for_bucket(row: dict) -> str:
    rd = _helpers()
    ns = {row["model_key"]: {
        "market": rd._public_n(row.get("market_n")),
        "income": rd._public_n(row.get("income_n")),
        "cost": rd._public_n(row.get("cost_n")),
    }}
    plan = rd.plan_public_log_masking(ns, keys=[row["model_key"]], with_composite=False)
    return plan["nav"].get(row["model_key"], "absent")


def _r2(v):
    return None if v is None else round(float(v), 2)


def _r4(v):
    return None if v is None else round(float(v), 4)



_VENDOR_ALIASES = {
    "biznet_neo": "biznet",
    "biznet_pricelist": "biznet",
    "syndrome_ag": "syndrome_ag",
    "syndrome_ag_pcie": "syndrome_ag",
}


def _dedupe_official_sea_legs(rows: list[dict]) -> list[dict]:
    """Keep one official income/market/cost point per vendor × bucket × price.

    Biznet's product page and pricelist can yield the same 1× H200 hourly rate.
    """
    seen = set()
    out = []
    for o in rows:
        official = o.get("include_in_sea_income") or o.get("include_in_sea_market") or o.get("include_in_sea_cost")
        if not official:
            out.append(o)
            continue
        vendor = _VENDOR_ALIASES.get(o.get("source_id") or "", o.get("source_id") or "")
        price = o.get("usd_per_gpu_hour") if o.get("include_in_sea_income") else o.get("usd_per_card")
        price_k = round(float(price), 6) if price is not None else None
        # Include form so two same-price SKUs (e.g. ACC A100 80GB NVLINK vs PCIe)
        # are not collapsed. Do not include URL, or Biznet product+pricelist would
        # double-count the same 1x H200 hourly rate.
        key = (vendor, o.get("sea_index_key"), o.get("unit"), o.get("form_factor"), price_k,
               int(bool(o.get("include_in_sea_income"))),
               int(bool(o.get("include_in_sea_market"))),
               int(bool(o.get("include_in_sea_cost"))))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    return out



def _member_bucket(observations, member_key, asof, computed_at_utc, computed_at_pt):
    """Pool official SEA legs for one basket member across SG/MY/ID/VN."""
    rd = _helpers()
    mkt, inc_r, newp, regions, sources = [], [], [], [], []
    for o in observations:
        if (o.get("region") or "").upper() not in SEA_PUBLIC_REGIONS:
            continue
        if basket_member_key(o.get("model_key")) != member_key:
            continue
        region = (o.get("region") or "").upper()
        sid = o.get("source_id")
        if o.get("include_in_sea_market") and o.get("usd_per_card"):
            v = float(o["usd_per_card"])
            if v > 0:
                mkt.append(v)
                if region not in regions:
                    regions.append(region)
                if sid and sid not in sources:
                    sources.append(sid)
        if o.get("include_in_sea_income") and o.get("usd_per_gpu_hour"):
            v = float(o["usd_per_gpu_hour"])
            if v > 0:
                inc_r.append(v)
                if region not in regions:
                    regions.append(region)
                if sid and sid not in sources:
                    sources.append(sid)
        if o.get("include_in_sea_cost") and o.get("usd_per_card"):
            v = float(o["usd_per_card"])
            if v > 0:
                newp.append(v)
                if region not in regions:
                    regions.append(region)
                if sid and sid not in sources:
                    sources.append(sid)
    market = rd.median(mkt)
    r_med = rd.median(inc_r)
    income = rd.income_dcf(r_med) if r_med is not None else None
    new_med = rd.median(newp)
    frac = rd.ASSUMPTIONS["cost"]["unknown_age_remaining_fraction"]
    cost = (new_med * frac) if new_med is not None else None
    legs = [v for v in (market, income, cost) if v is not None]
    nav = (sum(legs) / len(legs)) if legs else None
    flags = [
        "sold_n=0", "income_assumptions=to-be-calibrated", "cost_age_fraction=to-be-calibrated",
        "not_in_nvidia_composite", "not_mixed_with_mi300x", "official_P=R=1",
        "sea_basket_member",
    ]
    if not mkt:
        flags.append("no_official_listings")
    if r_med is None:
        flags.append("no_official_rentals")
    if new_med is None:
        flags.append("no_new_card_anchor")
    return {
        "model_key": member_key,
        "label": {"H100_SXM": "H100 SXM", "H100_PCIe": "H100 PCIe", "A100_80GB": "A100 80GB",
                  "A100_40GB": "A100 40GB", "H200": "H200"}.get(member_key, member_key.replace("_", " ")),
        "regions": regions,
        "sources": sources,
        "market_usd": market,
        "income_usd": income,
        "cost_usd": cost,
        "nav_indicative_usd": nav,
        "market_n": len(mkt),
        "income_n": len(inc_r),
        "cost_n": len(newp),
        "rental_median_usd_per_hour": r_med,
        "new_card_usd": new_med,
        "legs_used": len(legs),
        "quality_flag": ";".join(flags),
        "asof_date_pt": asof,
        "computed_at_utc": computed_at_utc,
        "computed_at_pt": computed_at_pt,
    }


def build_sea_public_model(observations, asof_date_pt, computed_at_utc, computed_at_pt) -> dict:
    """Equal-weight basket of SEA model NAVs for SG/MY/ID/VN. Not in the NVIDIA composite."""
    rd = _helpers()
    observations = rd.drop_competitor_observations(list(observations or []), context="build_sea_public_model")
    observations = drop_entity_list_observations(observations, context="build_sea_public_model")
    sea_obs = [o for o in observations if is_sea_source_id(o.get("source_id")) or o.get("index_family") == "sea"]
    sea_obs = _dedupe_official_sea_legs(sea_obs)
    members = {}
    for mk in SEA_BASKET_MEMBERS:
        row = _member_bucket(sea_obs, mk, asof_date_pt, computed_at_utc, computed_at_pt)
        status = _public_status_for_bucket(row)
        row["public_status"] = status
        row["n_lt_3_hidden"] = status != "published"
        members[mk] = row
    navs = [m["nav_indicative_usd"] for m in members.values() if m["nav_indicative_usd"] is not None]
    nav = (sum(navs) / len(navs)) if navs else None
    flags = [
        "sold_n=0",
        "income_assumptions=to-be-calibrated",
        "cost_age_fraction=to-be-calibrated",
        "not_in_nvidia_composite",
        "not_mixed_with_mi300x",
        "official_P=R=1",
        "sea_regional_basket=SG_MY_ID_VN",
        "equal_weight_of_sea_model_navs",
        "h200_included_in_sea_basket_only",
        "china_excluded",
        "thailand_excluded",
    ]
    if not navs:
        flags.append("no_member_nav")
    used_regions, used_sources = [], []
    for m in members.values():
        for r in m.get("regions") or []:
            if r not in used_regions:
                used_regions.append(r)
        for s in m.get("sources") or []:
            if s not in used_sources:
                used_sources.append(s)
    official_income_n = sum(int(m["income_n"] or 0) for m in members.values())
    official_market_n = sum(int(m["market_n"] or 0) for m in members.values())
    official_cost_n = sum(int(m["cost_n"] or 0) for m in members.values())
    # Method legs live on the members. The basket itself has no single-SKU
    # market/income/cost value, so n is published as 0 on the parent card.
    return {
        "model_key": SEA_BASKET_KEY,
        "label": SEA_PUBLIC_LABEL,
        "nav_indicative_usd": _r2(nav),
        "market_usd": None,
        "income_usd": None,
        "cost_usd": None,
        "market_n": 0,
        "income_n": 0,
        "cost_n": 0,
        "legs_used": 0,
        "rental_median_usd_per_hour": None,
        "new_card_usd": None,
        "quality_flag": ";".join(flags),
        "in_composite": False,
        "separate_subindex": True,
        "sea_countries": [SEA_COUNTRY_LABELS[c] for c in SEA_PUBLIC_REGIONS],
        "sea_country_codes": list(SEA_PUBLIC_REGIONS),
        "sea_member_keys": list(SEA_BASKET_MEMBERS),
        "sea_official_observation_counts": {
            "market": official_market_n,
            "income": official_income_n,
            "cost": official_cost_n,
        },
        "sea_regions_with_official_quotes": used_regions,
        "sea_source_ids": used_sources,
        "sea_members": {
            k: {
                "model_key": k,
                "label": m["label"],
                "regions": m["regions"],
                "sources": m["sources"],
                "market_usd": _r2(m["market_usd"]),
                "income_usd": _r2(m["income_usd"]),
                "cost_usd": _r2(m["cost_usd"]),
                "nav_indicative_usd": _r2(m["nav_indicative_usd"]),
                "market_n": m["market_n"],
                "income_n": m["income_n"],
                "cost_n": m["cost_n"],
                "rental_median_usd_per_hour": _r4(m["rental_median_usd_per_hour"]),
                "new_card_usd": _r2(m["new_card_usd"]),
                "legs_used": m["legs_used"],
                "quality_flag": m["quality_flag"],
                "public_status": m["public_status"],
            }
            for k, m in members.items()
        },
        "carried_forward": False,
        "stale": False,
        "dod_excluded": False,
        "carry_reason": None,
        "carried_from_asof": None,
        "legs_present": len(navs),
        "raw_nav_indicative_usd": _r2(nav),
    }


def compute_sea_index(observations, asof_date_pt, computed_at_utc, computed_at_pt, us_models: Optional[dict] = None):
    """Return official (P=R=1) SEA buckets plus an internal indicative-grid comparison."""
    rd = _helpers()
    observations = rd.drop_competitor_observations(list(observations or []), context="compute_sea_index")
    observations = drop_entity_list_observations(observations, context="compute_sea_index")
    sea_obs = [o for o in observations if is_sea_source_id(o.get("source_id")) or o.get("index_family") == "sea"]
    sea_obs = _dedupe_official_sea_legs(sea_obs)
    keys = sorted({o.get("sea_index_key") for o in sea_obs if o.get("sea_index_key")})
    official_rows = [
        _one_bucket(sea_obs, k, asof_date_pt, computed_at_utc, computed_at_pt, apply_indicative=False)
        for k in keys
    ]
    haircut_rows = [
        _one_bucket(sea_obs, k, asof_date_pt, computed_at_utc, computed_at_pt, apply_indicative=True)
        for k in keys
    ]
    haircut_by = {r["model_key"]: r for r in haircut_rows}

    # Sample counts by region × model, including observation-only rows.
    counts = {}
    for o in sea_obs:
        region = o.get("region") or "?"
        mk = o.get("model_key") or "?"
        rec = counts.setdefault(
            f"{region}:{mk}",
            {"region": region, "model_key": mk, "n_obs": 0, "income_official_n": 0,
             "market_official_n": 0, "cost_official_n": 0, "observation_only_n": 0,
             "sources": []},
        )
        rec["n_obs"] += 1
        if o.get("include_in_sea_income"):
            rec["income_official_n"] += 1
        elif o.get("include_in_sea_market"):
            rec["market_official_n"] += 1
        elif o.get("include_in_sea_cost"):
            rec["cost_official_n"] += 1
        else:
            rec["observation_only_n"] += 1
        sid = o.get("source_id")
        if sid and sid not in rec["sources"]:
            rec["sources"].append(sid)

    models = {}
    premiums = []
    for row in official_rows:
        status = _public_status_for_bucket(row)
        hc = haircut_by.get(row["model_key"]) or {}
        us_key = US_COMPARE_MAP.get(row.get("base_model_key"))
        us = (us_models or {}).get(us_key) if us_key else None
        premium = None
        premium_status = "not_comparable"
        if us and row.get("nav_indicative_usd") is not None and us.get("nav_indicative_usd") is not None:
            us_status = _public_status_for_bucket({
                "model_key": us_key,
                "market_n": us.get("market_n"),
                "income_n": us.get("income_n"),
                "cost_n": us.get("cost_n"),
                "market_usd": us.get("market_usd"),
                "income_usd": us.get("income_usd"),
                "cost_usd": us.get("cost_usd"),
                "nav_indicative_usd": us.get("nav_indicative_usd"),
            }) if us_key else "hidden"
            # _public_status_for_bucket uses n counts; reuse plan on a synthetic row.
            if status == "published" and us_status == "published" and float(us["nav_indicative_usd"]) != 0:
                premium = float(row["nav_indicative_usd"]) / float(us["nav_indicative_usd"]) - 1.0
                premium_status = "published"
            else:
                premium_status = "hidden_n_or_backout"
        elif row.get("base_model_key") not in US_COMPARE_MAP:
            premium_status = "no_us_official_bucket"
        item = {
            "model_key": row["model_key"],
            "region": row["region"],
            "base_model_key": row["base_model_key"],
            "label": row["label"],
            "market_usd": _r2(row["market_usd"]),
            "income_usd": _r2(row["income_usd"]),
            "cost_usd": _r2(row["cost_usd"]),
            "nav_indicative_usd": _r2(row["nav_indicative_usd"]),
            "market_n": row["market_n"],
            "income_n": row["income_n"],
            "cost_n": row["cost_n"],
            "rental_median_usd_per_hour": _r4(row["rental_median_usd_per_hour"]),
            "new_card_usd": _r2(row["new_card_usd"]),
            "quality_flag": row["quality_flag"],
            "public_status": status,
            "n_lt_3_hidden": status != "published",
            "us_compare_key": us_key,
            "premium_vs_us": None if premium is None else round(premium, 4),
            "premium_status": premium_status,
            "indicative_discount": {
                "nav_indicative_usd": _r2(hc.get("nav_indicative_usd")),
                "market_usd": _r2(hc.get("market_usd")),
                "income_usd": _r2(hc.get("income_usd")),
                "cost_usd": _r2(hc.get("cost_usd")),
                "rental_median_usd_per_hour": _r4(hc.get("rental_median_usd_per_hour")),
                "market_n": hc.get("market_n"),
                "income_n": hc.get("income_n"),
                "cost_n": hc.get("cost_n"),
                "note": "Internal comparison only. Official SEA NAV uses P=R=1.",
            },
        }
        models[row["model_key"]] = item
        if premium is not None:
            premiums.append({"sea_key": row["model_key"], "us_key": us_key, "premium": round(premium, 4)})

    public_model = build_sea_public_model(
        sea_obs, asof_date_pt, computed_at_utc, computed_at_pt,
    )
    return {
        "asof_date_pt": asof_date_pt,
        "computed_at_utc": computed_at_utc,
        "computed_at_pt": computed_at_pt,
        "disclaimer": (
            "Southeast Asia GPU sub-index (Singapore, Malaysia, Indonesia, Vietnam). "
            "Official SEA NAV uses P=R=1. The indicative discount grid is an internal "
            "comparison only and is not published. Not a member of the NVIDIA composite "
            "or the MI300X sub-index. High-school research project; indicative data; "
            "not investment advice."
        ),
        "official_factors": {"P": OFFICIAL_P, "R": OFFICIAL_R},
        "indicative_grid": {"P_by_source_class": INDICATIVE_P, "R_by_region": INDICATIVE_R},
        "regions": list(SEA_PUBLIC_REGIONS),
        "vietnam": "in_public_basket",
        "thailand": "excluded_from_public_basket",
        "china": "excluded",
        "philippines": "monitor_only_not_computed",
        "models": models,
        "public_model": public_model,
        "sample_counts": counts,
        "premiums_published": premiums,
        "ushq_cloud_apac_comparison": {
            "status": "empty",
            "note": (
                "US-headquartered cloud APAC list prices are a separate comparison line "
                "and are not mixed with local-cloud SEA income. This trial did not obtain "
                "region-tagged on-demand prices from official AWS/GCP/Azure static pages "
                "(prices are not in the static HTML). DigitalOcean documents no Singapore "
                "GPU Droplet region. RunPod's public pricing page did not name a Singapore "
                "or Jakarta region."
            ),
            "rows": [],
        },
        "observation_count": len(sea_obs),
        "separate_subindices": list(models.keys()),
    }


def log_sea_summary(sea: dict) -> None:
    """INFO lines for SEA buckets. Hidden n<3 values are never printed as numbers."""
    rd = _helpers()
    models = sea.get("models") or {}
    pub = sea.get("public_model") or {}
    if pub:
        status = "published"
        # Member-level publication is decided later by export_public; log only n counts.
        log.info(
            "SEA public basket countries=%s member_navs_present=%s official_n market=%s income=%s cost=%s sources=%s",
            ",".join(pub.get("sea_country_codes") or []),
            pub.get("legs_present"),
            (pub.get("sea_official_observation_counts") or {}).get("market"),
            (pub.get("sea_official_observation_counts") or {}).get("income"),
            (pub.get("sea_official_observation_counts") or {}).get("cost"),
            ",".join(pub.get("sea_source_ids") or []),
        )
        for k, m in (pub.get("sea_members") or {}).items():
            log.info(
                "SEA member %s: public_status=%s market_n=%s income_n=%s cost_n=%s regions=%s",
                k, m.get("public_status"), m.get("market_n"), m.get("income_n"), m.get("cost_n"),
                ",".join(m.get("regions") or []),
            )
    if not models:
        log.info("SEA sub-index: no region×model buckets this run")
        return
    log.info("SEA sub-index official P=R=1; indicative discount grid is internal comparison only")
    for k, v in models.items():
        status = v.get("public_status") or "hidden"
        log.info(
            "SEA NAV %s: indicative=%s market=%s income=%s cost=%s premium_vs_us=%s",
            k,
            rd._fmt_nav_for_log(v.get("nav_indicative_usd"), status if status == "published" else "hidden"),
            rd._fmt_leg_for_log(v.get("market_usd"), v.get("market_n")),
            rd._fmt_leg_for_log(v.get("income_usd"), v.get("income_n")),
            rd._fmt_leg_for_log(v.get("cost_usd"), v.get("cost_n")),
            "published" if v.get("premium_status") == "published" else v.get("premium_status"),
        )
        # Indicative-grid NAV uses the same n; still mask if official public status is hidden.
        hc = v.get("indicative_discount") or {}
        log.info(
            "SEA indicative-grid %s: nav=%s income=%s",
            k,
            rd._fmt_nav_for_log(hc.get("nav_indicative_usd"), status if status == "published" else "hidden"),
            rd._fmt_leg_for_log(hc.get("income_usd"), hc.get("income_n")),
        )


def attach_sea_to_latest(latest: dict, sea: dict) -> dict:
    latest = dict(latest)
    latest["sea"] = sea
    official_subs = list(latest.get("separate_subindices") or [])
    # Public dashboard publishes a single SEA basket card, not region×model keys.
    if sea.get("public_model") and SEA_BASKET_KEY not in official_subs:
        official_subs.append(SEA_BASKET_KEY)
    latest["separate_subindices"] = official_subs
    models = dict(latest.get("models") or {})
    if sea.get("public_model"):
        models[SEA_BASKET_KEY] = sea["public_model"]
    latest["models"] = models
    comp = dict(latest.get("composite") or {})
    bd = dict(comp.get("breakdown") or {})
    excluded = list(bd.get("excluded_subindices") or comp.get("excluded_subindices") or [])
    for k in ("MI300X", SEA_BASKET_KEY):
        if k not in excluded:
            excluded.append(k)
    bd["excluded_subindices"] = excluded
    flags = [f for f in str(comp.get("quality_flag") or "").split(";") if f]
    if "sea_excluded_from_composite" not in flags:
        flags.append("sea_excluded_from_composite")
    comp["quality_flag"] = ";".join(flags)
    comp["breakdown"] = bd
    comp["excluded_subindices"] = excluded
    latest["composite"] = comp
    fx = dict(latest.get("fx") or {})
    latest["fx"] = fx
    return latest


def official_values_tuple(index_rows: list[dict]) -> dict:
    out = {}
    for r in index_rows or []:
        k = r.get("model_key")
        if k in {"H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB", "MI300X", "COMPOSITE"}:
            out[k] = {
                "nav": r.get("nav_indicative_usd"),
                "market": r.get("market_usd"),
                "income": r.get("income_usd"),
                "cost": r.get("cost_usd"),
                "market_n": r.get("market_n"),
                "income_n": r.get("income_n"),
                "cost_n": r.get("cost_n"),
                "rental_median": r.get("rental_median_usd_per_hour"),
            }
    return out
