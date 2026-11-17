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
  * Small-sample rule (MIN_SAMPLE_N = 3): a method leg (market/income/cost)
    with n < 3 is published as null with insufficient_sample[leg]=true and its
    n. Values derived from that leg are hidden too (rent median <- income,
    new-card anchor <- cost). The formulas are NOT changed: indicative NAV and
    the composite are still computed upstream from all legs. A NAV/composite
    is published only if, together with everything else published, it does not
    let anyone back out a hidden leg (exact linear-algebra check, see
    _identifiable()). Otherwise it is null with nav_hidden=true + reason.
    The same masking is stored in history.json, and assert_small_sample()
    re-checks latest + every history point before anything is written.
  * history.json is the long-term public record: it is read from the repo,
    today's point is upserted (keyed by asof_date_pt), and it is written back.
    nav.db is NOT needed for continuity.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_VERSION = 2
MIN_SAMPLE_N = 3          # Wilson 2026-11-17: legs with n < 3 are not published
LEGS = ("market", "income", "cost")
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


# ---------------------------------------------------------------------------
# Small-sample masking
# ---------------------------------------------------------------------------
from fractions import Fraction  # noqa: E402


def _present(n) -> bool:
    return isinstance(n, int) and n > 0


def _hidden(n) -> bool:
    """Leg exists but has too few observations to publish."""
    return _present(n) and n < MIN_SAMPLE_N


def _rank(rows):
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


def _identifiable(equations, n_unknowns):
    """Indices of hidden legs uniquely determined by `equations` (rows of
    coefficients over the hidden legs; published legs act as constants)."""
    if not equations:
        return set()
    base = _rank(equations)
    out = set()
    for u in range(n_unknowns):
        e = [Fraction(int(i == u)) for i in range(n_unknowns)]
        if _rank(equations + [e]) == base:
            out.add(u)
    return out


def plan_masking(ns: dict) -> dict:
    """ns: {model_key: {"market": n, "income": n, "cost": n}}.
    Decides from sample counts only which legs are hidden and which NAVs /
    composite can be published without revealing a hidden leg.
    Greedy order: model NAVs (MODEL_KEYS order) first, then the composite."""
    unknowns = [(k, leg) for k in MODEL_KEYS for leg in LEGS if _hidden(ns[k][leg])]
    idx = {u: i for i, u in enumerate(unknowns)}
    nu = len(unknowns)

    def nav_row(k):
        legs = [leg for leg in LEGS if _present(ns[k][leg])]
        if not legs:
            return None
        row = [Fraction(0)] * nu
        for leg in legs:
            if (k, leg) in idx:
                row[idx[(k, leg)]] += Fraction(1, len(legs))
        return row

    rows = {k: nav_row(k) for k in MODEL_KEYS}
    nav_models = [k for k in MODEL_KEYS if rows[k] is not None]
    comp_row = None
    if nav_models:
        comp_row = [Fraction(0)] * nu
        for k in nav_models:
            comp_row = [a + b / len(nav_models) for a, b in zip(comp_row, rows[k])]

    published, plan = [], {"nav": {}, "composite": None}
    for k in MODEL_KEYS:
        r = rows[k]
        if r is None:
            plan["nav"][k] = "absent"
        elif nu == 0 or not _identifiable(published + [r], nu):
            published.append(r)
            plan["nav"][k] = "published"
        else:
            plan["nav"][k] = "hidden"
    if comp_row is None:
        plan["composite"] = "absent"
    elif nu == 0 or not _identifiable(published + [comp_row], nu):
        published.append(comp_row)
        plan["composite"] = "published"
    else:
        plan["composite"] = "hidden"
    plan["hidden_legs"] = unknowns
    return plan


NAV_HIDDEN_REASON = {
    "zh": "已隐藏：结合已公开的其他数值，可反推出某个样本不足（n<3）的分项。公式未改，上游仍按全部分项计算。",
    "en": "Hidden: together with other published values it would reveal a method leg with n<3. Formula unchanged; computed upstream from all legs.",
}
COMPOSITE_HIDDEN_REASON = {
    "zh": "已隐藏：综合为四个型号 NAV 的等权平均，公开它可反推出被隐藏的型号 NAV，进而反推出样本不足（n<3）的分项。",
    "en": "Hidden: the composite is the equal-weight mean of model NAVs; publishing it would reveal a hidden model NAV and thus a leg with n<3.",
}


def mask_model(m: dict, nav_status: str) -> dict:
    """Apply the small-sample rule to one public model dict (in place)."""
    ins = {}
    for leg in LEGS:
        ins[leg] = _hidden(m.get(f"{leg}_n"))
        if ins[leg]:
            m[f"{leg}_usd"] = None
    if ins["income"]:
        m["rental_median_usd_per_hour"] = None
    if ins["cost"]:
        m["new_card_usd"] = None
    m["insufficient_sample"] = ins
    m["nav_hidden"] = nav_status == "hidden"
    m["nav_hidden_reason"] = NAV_HIDDEN_REASON if m["nav_hidden"] else None
    if m["nav_hidden"]:
        m["nav_indicative_usd"] = None
    return m


def assert_small_sample(latest_pub: dict, hist: dict) -> None:
    """Guard: refuse to write if any value with n < MIN_SAMPLE_N (or anything
    that would reveal one) is about to be published, in latest or history."""
    def check_point(where, p):
        for leg in LEGS:
            if f"{leg}_n" not in p:
                raise SystemExit(f"GUARD: {where} has no {leg}_n; cannot verify small-sample rule")
            n = p.get(f"{leg}_n")
            if p.get(f"{leg}_usd") is not None and not (isinstance(n, int) and n >= MIN_SAMPLE_N):
                raise SystemExit(f"GUARD: {where}.{leg}_usd published with n={n} < {MIN_SAMPLE_N}")
        if p.get("rental_median_usd_per_hour") is not None and not (isinstance(p.get("income_n"), int) and p["income_n"] >= MIN_SAMPLE_N):
            raise SystemExit(f"GUARD: {where}.rental_median published with income_n={p.get('income_n')}")
        if p.get("new_card_usd") is not None and not (isinstance(p.get("cost_n"), int) and p["cost_n"] >= MIN_SAMPLE_N):
            raise SystemExit(f"GUARD: {where}.new_card_usd published with cost_n={p.get('cost_n')}")

    def check_set(where, model_points, comp_val):
        for k in MODEL_KEYS:
            if model_points[k] is not None:
                check_point(f"{where}.{k}", model_points[k])
        ns = {k: {leg: (model_points[k] or {}).get(f"{leg}_n") for leg in LEGS} for k in MODEL_KEYS}
        plan = plan_masking(ns)
        for k in MODEL_KEYS:
            p = model_points[k]
            if p is not None and p.get("nav_indicative_usd") is not None and plan["nav"][k] != "published":
                raise SystemExit(f"GUARD: {where}.{k} NAV would reveal a leg with n<{MIN_SAMPLE_N}")
        if comp_val is not None and plan["composite"] != "published":
            raise SystemExit(f"GUARD: {where} composite would reveal a leg with n<{MIN_SAMPLE_N}")

    check_set("latest", latest_pub["models"], (latest_pub.get("composite") or {}).get("nav_indicative_usd"))
    for d in hist.get("dates", []):
        pts = {k: next((p for p in hist["series"].get(k, {}).get("points", []) if p["asof_date_pt"] == d), None)
               for k in MODEL_KEYS}
        cp = next((p for p in hist["series"].get("COMPOSITE", {}).get("points", []) if p["asof_date_pt"] == d), None)
        check_set(f"history[{d}]", pts, cp.get("nav_indicative_usd") if cp else None)


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
    ns = {k: {leg: (models[k] or {}).get(f"{leg}_n") for leg in LEGS} for k in MODEL_KEYS}
    plan = plan_masking(ns)
    for k in MODEL_KEYS:
        if models[k] is not None:
            mask_model(models[k], plan["nav"][k])
    comp = priv.get("composite") or {}
    comp_bd = comp.get("breakdown") or {}
    comp_hidden = plan["composite"] == "hidden"

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
            "nav_indicative_usd": None if comp_hidden else comp.get("nav_indicative_usd"),
            "nav_hidden": comp_hidden,
            "nav_hidden_reason": COMPOSITE_HIDDEN_REASON if comp_hidden else None,
            "n_models": comp_bd.get("n_models"),
            "quality_flag": comp.get("quality_flag"),
        },
        "models": models,
        "small_sample_rule": {
            "min_n": MIN_SAMPLE_N,
            "zh": "任一方法（市场/收益/成本）样本数 n<3 时，不公开该方法数值及由其直接得出的数值（租金中位数、新卡锚点），只公开 n 并标记 insufficient_sample。指示性 NAV 与综合指数仍按上游方法论用全部分项计算（公式不变）；若公开后可反推出被隐藏的分项，则该 NAV/综合也隐藏（nav_hidden=true）。",
            "en": "Any method leg with n<3 is published as null with its n and insufficient_sample=true; derived values (rent median, new-card anchor) are hidden too. NAV and composite are computed upstream from all legs (formula unchanged) but are hidden (nav_hidden=true) if publishing them would let a hidden leg be backed out.",
        },
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
                  "nav_hidden": bool(src.get("nav_hidden"))}
            s["label"] = src.get("label")
        else:
            src = latest_pub["models"].get(key) or {}
            pt = {"asof_date_pt": date, "computed_at_pt": latest_pub["computed_at_pt"],
                  "nav_indicative_usd": src.get("nav_indicative_usd"),
                  "market_usd": src.get("market_usd"), "income_usd": src.get("income_usd"),
                  "cost_usd": src.get("cost_usd"),
                  "market_n": src.get("market_n"), "income_n": src.get("income_n"), "cost_n": src.get("cost_n"),
                  "insufficient_sample": src.get("insufficient_sample"),
                  "nav_hidden": bool(src.get("nav_hidden"))}
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
    assert_small_sample(pub, hist)
    (out / "latest.json").write_text(json.dumps(pub, indent=2, ensure_ascii=False) + "\n")
    hp.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out/'latest.json'} and {hp} (asof {pub['asof_date_pt']}, {len(hist['dates'])} history dates)")


if __name__ == "__main__":
    sys.exit(main())
