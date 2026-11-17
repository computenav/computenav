#!/usr/bin/env python3
"""Build the PUBLIC, aggregated JSON for the static dashboard.

Input  (private, never committed): <data-dir>/output/latest.json written by run_daily.py
Output (public, committed):        docs/data/latest.json, docs/data/history.json

Rules (see README "数据许可 / Data licensing"):
  * Only per-model aggregated NAV values, leg values, counts, medians and the
    placeholder assumptions are published.
  * NO listing-level observations, NO per-source prices, NO raw_label, NO
    snapshot hashes/paths, NO breakdown.values_usd (those are per-listing prices).
  * Sources listed in LICENCE_RESTRICTED must not contribute to a published
    value. If latest.json shows they contributed observations (i.e. they were
    not skipped), the export aborts instead of publishing. The only exception
    is --allow-observation-only (used for seeding from the box DB, where these
    sources were fetched): then each such source must be marked
    observation_only=true by run_daily.py (methodology §10, excluded from the
    official legs), otherwise the export still aborts.
  * history.json is the long-term public record: it is read from the repo,
    today's point is upserted (keyed by asof_date_pt), and it is written back.
    nav.db is NOT needed for continuity.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1
MODEL_KEYS = ["H100_SXM", "H100_PCIe", "A100_80GB", "A100_40GB"]
SERIES_KEYS = MODEL_KEYS + ["COMPOSITE"]
LABELS_EN = {
    "H100_SXM": "H100 SXM", "H100_PCIe": "H100 PCIe",
    "A100_80GB": "A100 80GB", "A100_40GB": "A100 40GB",
    "COMPOSITE": "Composite (equal-weight, to be calibrated)",
}

# Sources whose terms forbid index use / redistribution without a licence.
LICENCE_RESTRICTED = {
    "vast_public_feed": "Vast.ai: 'use in any index, benchmark, or derivative product requires a data license' (data@vast.ai)",
    "vast_bundles": "Vast.ai: same data-licence requirement as the public feed",
    "ebay_sold": "eBay: no licensed API access; ToS restricts scraping / reuse of sold comps",
    "ebay_active": "eBay: no licensed API access; ToS restricts scraping",
}
# Sources that are collected for cross-check only (weight 0) and whose raw
# values are never published. Listed here for transparency in the public JSON.
CROSS_CHECK_ONLY = {
    "clustermelt": "third-party compilation; redistribution/index use may need permission; weight 0",
    "mulligan": "derived from eBay listings; inherits eBay terms; weight 0",
    "anchorgpu": "orders not open; reference only; weight 0",
    "serverpartdeals": "unavailable 8-GPU server listing; weight 0",
}

DISCLAIMER = {
    "zh": "高中生研究项目。指示性数据，尚无成交样本；收益法与成本法参数均为待校准占位假设，不构成正式指数，不构成投资建议。",
    "en": "High-school student research project. Indicative data only, no sold transactions yet; income/cost parameters are placeholders to be calibrated. Not an official index. Not investment advice.",
}


def _short(s, n=160):
    if s is None:
        return None
    s = str(s).strip().splitlines()[0] if str(s).strip() else ""
    return s[:n]


def build_latest(priv: dict, allow_observation_only: bool = False) -> dict:
    health = priv.get("source_health") or []
    # Guard: restricted sources must not have contributed observations.
    for h in health:
        sid = h.get("source_id")
        if sid in LICENCE_RESTRICTED and (h.get("observation_count") or 0) > 0:
            if allow_observation_only and h.get("observation_only") is True:
                continue
            raise SystemExit(
                f"REFUSING TO PUBLISH: licence-restricted source '{sid}' contributed "
                f"{h.get('observation_count')} observations. Run with COMPUTENAV_SKIP_SOURCES "
                f"including it (see .github/workflows/daily.yml)."
            )

    models = {}
    for key in MODEL_KEYS:
        m = (priv.get("models") or {}).get(key)
        if m is None:
            models[key] = None
            continue
        legs = ((m.get("breakdown") or {}).get("indicative_nav") or {}).get("legs_used")
        models[key] = {
            "model_key": key,
            "label": m.get("label") or key,
            "label_en": LABELS_EN[key],
            "nav_indicative_usd": m.get("nav_indicative_usd"),
            "market_usd": m.get("market_usd"),
            "income_usd": m.get("income_usd"),
            "cost_usd": m.get("cost_usd"),
            "market_n": m.get("market_n"),
            "income_n": m.get("income_n"),
            "cost_n": m.get("cost_n"),
            "legs_used": legs,
            "rental_median_usd_per_hour": m.get("rental_median_usd_per_hour"),
            "new_card_usd": m.get("new_card_usd"),
            "quality_flag": m.get("quality_flag"),
        }
    comp = priv.get("composite") or {}
    comp_bd = comp.get("breakdown") or {}

    pub_health = []
    for h in health:
        sid = h.get("source_id")
        role = ("licence_restricted" if sid in LICENCE_RESTRICTED
                else "cross_check_only" if sid in CROSS_CHECK_ONLY else "collected")
        pub_health.append({
            "source_id": sid,
            "ok": bool(h.get("ok")),
            "skipped": bool(h.get("skipped")),
            "http_status": h.get("http_status"),
            "observation_count": h.get("observation_count") or 0,
            "role": role,
            "observation_only": bool(h.get("observation_only")),
            "error": _short(h.get("error")),
        })

    fx = priv.get("fx") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "asof_date_pt": priv.get("asof_date_pt"),
        "computed_at_pt": priv.get("computed_at_pt"),
        "computed_at_utc": priv.get("computed_at_utc"),
        "run_id": priv.get("run_id"),
        "disclaimer": DISCLAIMER,
        "sold_count_today": priv.get("sold_count_today"),
        "observation_count": priv.get("observation_count"),
        "official_observation_counts": priv.get("official_observation_counts"),
        "fx": {
            "source": fx.get("source"),
            "asof_utc": fx.get("asof_utc"),
            "usd_per_eur": fx.get("usd_per_eur"),
            "usd_per_gbp": fx.get("usd_per_gbp"),
        },
        "assumptions": priv.get("assumptions"),
        "composite": {
            "label": comp.get("label") or "综合（等权，待校准）",
            "label_en": LABELS_EN["COMPOSITE"],
            "nav_indicative_usd": comp.get("nav_indicative_usd"),
            "n_models": comp_bd.get("n_models"),
            "quality_flag": comp.get("quality_flag"),
        },
        "models": models,
        "source_health": pub_health,
        "licensing": {
            "excluded_from_index": [
                {"source_id": k, "reason": v} for k, v in LICENCE_RESTRICTED.items()
            ],
            "cross_check_only_not_published": [
                {"source_id": k, "reason": v} for k, v in CROSS_CHECK_ONLY.items()
            ],
            "note_zh": "公开数据仅含按型号汇总的指示性 NAV、三法结果、样本数与中位数；不含任何单条挂牌/租金观测或分来源原始价格。按方法论第 10 节，Vast.ai、eBay、Clustermelt、Mulligan 不进入官方计算；每日 CI 不抓取 Vast.ai 与 eBay。",
            "note_en": "Public data contains only per-model aggregates (indicative NAV, method legs, counts, medians). No listing-level observations or per-source raw prices. Per methodology section 10, Vast.ai, eBay, Clustermelt and Mulligan are excluded from the official calculation; the daily CI does not fetch Vast.ai or eBay.",
        },
    }


def upsert_history(hist: dict | None, latest_pub: dict) -> dict:
    if not hist:
        hist = {"schema_version": SCHEMA_VERSION, "series": {}, "dates": []}
    hist.setdefault("series", {})
    date = latest_pub["asof_date_pt"]
    if not date:
        raise SystemExit("latest has no asof_date_pt")
    for key in SERIES_KEYS:
        s = hist["series"].setdefault(key, {"label": None, "label_en": LABELS_EN[key], "points": []})
        if key == "COMPOSITE":
            src = latest_pub["composite"]
            pt = {"asof_date_pt": date, "computed_at_pt": latest_pub["computed_at_pt"],
                  "nav_indicative_usd": src.get("nav_indicative_usd"),
                  "market_usd": None, "income_usd": None, "cost_usd": None}
            s["label"] = src.get("label")
        else:
            src = latest_pub["models"].get(key) or {}
            pt = {"asof_date_pt": date, "computed_at_pt": latest_pub["computed_at_pt"],
                  "nav_indicative_usd": src.get("nav_indicative_usd"),
                  "market_usd": src.get("market_usd"), "income_usd": src.get("income_usd"),
                  "cost_usd": src.get("cost_usd")}
            s["label"] = src.get("label") or key
        s["label_en"] = LABELS_EN[key]
        pts = [p for p in s.get("points", []) if p.get("asof_date_pt") != date]
        pts.append(pt)
        s["points"] = sorted(pts, key=lambda p: p["asof_date_pt"])
    hist["dates"] = sorted({p["asof_date_pt"] for s in hist["series"].values() for p in s["points"]})
    hist["generated_at_pt"] = latest_pub["computed_at_pt"]
    hist["schema_version"] = SCHEMA_VERSION
    return hist


FORBIDDEN_KEYS = {"observations", "raw_label", "snapshot_hash", "values_usd", "source_url", "sha256", "path"}


def assert_clean(obj, where="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_KEYS:
                raise SystemExit(f"forbidden key '{k}' at {where}")
            assert_clean(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_clean(v, f"{where}[{i}]")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--private-latest", required=True, help="path to private output/latest.json")
    ap.add_argument("--allow-observation-only", action="store_true",
                    help="seeding only: accept restricted sources that run_daily.py marked observation_only")
    ap.add_argument("--public-dir", default=str(Path(__file__).resolve().parent.parent / "docs" / "data"))
    a = ap.parse_args(argv)

    priv = json.loads(Path(a.private_latest).read_text())
    pub = build_latest(priv, a.allow_observation_only)
    if all(pub["models"][k] is None or pub["models"][k]["nav_indicative_usd"] is None for k in MODEL_KEYS):
        raise SystemExit("no model produced an indicative NAV today; not publishing")
    out = Path(a.public_dir)
    out.mkdir(parents=True, exist_ok=True)
    hp = out / "history.json"
    hist = json.loads(hp.read_text()) if hp.exists() else None
    hist = upsert_history(hist, pub)
    assert_clean(pub)
    assert_clean(hist)
    (out / "latest.json").write_text(json.dumps(pub, indent=2, ensure_ascii=False) + "\n")
    hp.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out/'latest.json'} and {hp} (asof {pub['asof_date_pt']}, {len(hist['dates'])} history dates)")


if __name__ == "__main__":
    sys.exit(main())
