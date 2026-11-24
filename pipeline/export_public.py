#!/usr/bin/env python3
"""Build the PUBLIC, aggregated JSON for the static dashboard.

Input  (private, never committed): <data-dir>/output/latest.json written by run_daily.py
Output (public, committed):        docs/data/latest.json, docs/data/history.json

Rules (see README "Data licensing"):
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
  * Competitor denylist (pipeline/denylist.py): if the id, URL domain or
    registered name of any source / observation of the run, or of any public
    record, matches Silicon Data / Ornn / SemiAnalysis / Compute Exchange, the
    export aborts. Free text (errors, notes) is not scanned.
  * Publication-guard fields from the private latest (carried_forward, stale,
    dod_excluded, carry_reason, carried_from_asof, legs_present) are copied into
    public latest.json and history.json. raw_nav_indicative_usd is never
    published (it is the pre-carry indicative value and is forbidden in public
    files). legs_present is the count of internally complete legs (n >= 1), not
    the public n<3 hide rule.
  * history.json is the long-term public record: it is read from the repo,
    today's point is upserted (keyed by asof_date_pt), and it is written back.
    A same-asof_date upsert is refused when the new point is stale / carried
    forward or has fewer internally complete legs than the point already stored.
    nav.db is NOT needed for continuity.
"""
from __future__ import annotations

import argparse
import hashlib
import re
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

# English versions of the upstream (Chinese) assumption texts. Keyed by path;
# each entry carries a short SHA-256 of the upstream text it translates. If the
# upstream text changes, the hash no longer matches and the text is replaced by
# UNTRANSLATED (with a warning) instead of publishing a stale translation.
UNTRANSLATED = "[upstream text changed; English translation pending]"
ASSUMPTION_TEXT_EN = {
    ("disclaimer_zh",): ("1a3e076f0afa", "All parameters below are placeholder assumptions. They have not been calibrated by an index committee and are not official methodology parameters."),
    ("income", "remaining_life_note"): ("e69c46b55325", "Placeholder: remaining economic life of 4 years. CoreWeave's 6-year accounting life and IREN's 5-year life for pledged GPUs are reference points only and are not this parameter."),
    ("income", "utilization_note"): ("81d2afc37dcd", "Placeholder: 50% utilization. Must not be read as a calibrated market utilization."),
    ("income", "opex_note"): ("671f55aba0da", "Placeholder: operating costs (power, rack space, operations) are deducted at 25% of gross rental revenue; not based on empirical power or colocation costs."),
    ("income", "discount_note"): ("33679942762f", "Placeholder: 10% annual discount rate."),
    ("income", "terminal_note"): ("abaf16a80b22", "Placeholder: terminal value of 0; no additional terminal value at the end of the life."),
    ("income", "rental_rate_rule"): ("45a309c883bc", "Median of the day's on-demand / list rents (USD per GPU-hour) admitted to the income approach. Reserved, spot and unclear-term observations, and observations whose form factor cannot be assigned, are excluded from the median."),
    ("cost", "book_life_note"): ("190b61bbd4db", "Placeholder: the straight-line life uses IREN's 5-year accounting life for pledged GPUs as a scenario anchor (CoreWeave uses 6 years for compute equipment). The official life is to be calibrated."),
    ("cost", "age_note"): ("3e87306a5ad5", "Placeholder: used observations generally lack age, so the cost approach uses a 50% remaining-value factor (\"5-year straight line, midpoint for unknown age\"). Not a calibrated residual curve."),
    ("cost", "new_price_source"): ("1de9ef7f365f", "Genius HPC \"New From\" prices on the same page (ex-VAT dealer starting prices in a non-USD currency, converted to USD at the daily FX rate) are preferred. These are not NVIDIA MSRP and not US channel prices."),
    ("market", "listing_rule"): ("692ece886998", "Only parseable dealer listings are used (Genius HPC in stock, Creative Compute on enquiry). AnchorGPU (ordering not open), third-party compilations (Clustermelt/Mulligan), unavailable systems, and systems simply divided by card count without deducting host components do not enter the official market-approach median."),
    ("composite", "rule"): ("008851e89fec", "Equal-weight average of the four sub-indices (installed-base weights to be calibrated). Each sub-index's indicative NAV is the equal-weight average of the market/income/cost values available that day (method weights to be calibrated)."),
}
CJK_RE = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


def translate_assumptions(a, path=()):
    """Copy upstream assumptions; numbers unchanged, Chinese texts -> English."""
    if isinstance(a, dict):
        out = {}
        for k, v in a.items():
            key = "disclaimer" if path + (k,) == ("disclaimer_zh",) else k
            out[key] = translate_assumptions(v, path + (k,))
        return out
    if isinstance(a, str) and CJK_RE.search(a):
        entry = ASSUMPTION_TEXT_EN.get(path)
        h = hashlib.sha256(a.encode()).hexdigest()[:12]
        if entry and entry[0] == h:
            return entry[1]
        print(f"WARNING: no current English translation for assumptions.{'.'.join(path)} (sha {h})", file=sys.stderr)
        return UNTRANSLATED
    return a


def englishify(obj, where="root"):
    """Last line of defence: replace any remaining CJK string (e.g. an
    upstream error message) with a placeholder, and warn."""
    if isinstance(obj, dict):
        return {k: englishify(v, f"{where}.{k}") for k, v in obj.items()}
    if isinstance(obj, list):
        return [englishify(v, f"{where}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str) and CJK_RE.search(obj):
        print(f"WARNING: non-English text at {where} replaced", file=sys.stderr)
        return "[non-English text omitted]"
    return obj


def assert_english(obj, where="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_english(v, f"{where}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_english(v, f"{where}[{i}]")
    elif isinstance(obj, str) and CJK_RE.search(obj):
        raise SystemExit(f"GUARD: non-English text at {where}")

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

DISCLAIMER = ("High-school student research project. Indicative data only, no sold transactions yet; "
              "income/cost parameters are placeholders to be calibrated. Not an official index. Not investment advice.")


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


NAV_HIDDEN_REASON = ("Hidden: together with other published values it would reveal a method value with n<3. "
                     "The formula is unchanged; the NAV is still computed upstream from all methods.")
COMPOSITE_HIDDEN_REASON = ("Hidden: the composite is the equal-weight mean of the model NAVs; publishing it would "
                           "reveal a hidden model NAV and therefore a method value with n<3.")


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



def _as_bool(v) -> bool:
    if v in (None, False, 0, "0", ""):
        return False
    return bool(v)


def _leg_count(src: dict) -> int:
    """Count internally complete legs (n >= 1). Uses legs_present when present."""
    lp = src.get("legs_present")
    if lp is not None:
        try:
            return int(lp)
        except (TypeError, ValueError):
            pass
    n = 0
    for leg in LEGS:
        nv = src.get(f"{leg}_n")
        try:
            n_int = int(nv) if nv is not None else 0
        except (TypeError, ValueError):
            n_int = 0
        if n_int >= 1:
            n += 1
    return n


def extract_guard_fields(src: dict, *, composite: bool = False) -> dict:
    """Public publication-guard fields. Never includes raw_nav_indicative_usd."""
    stale = _as_bool(src.get("stale"))
    carried = _as_bool(src.get("carried_forward"))
    dod = _as_bool(src.get("dod_excluded")) or stale or carried
    return {
        "carried_forward": carried,
        "stale": stale,
        "dod_excluded": dod,
        "carry_reason": src.get("carry_reason"),
        "carried_from_asof": src.get("carried_from_asof"),
        "legs_present": None if composite else _leg_count(src),
    }


def point_is_stale(pt: dict) -> bool:
    return bool(pt.get("stale") or pt.get("carried_forward") or pt.get("dod_excluded"))


def should_refuse_overwrite(existing: dict | None, new: dict) -> bool:
    """Refuse to replace a same-asof point with a stale or fewer-legs point."""
    if not existing:
        return False
    if point_is_stale(new):
        return True
    # Composite public points have no M/I/C legs; stale is the only comparison.
    new_has_legs = new.get("legs_present") is not None or any(f"{leg}_n" in new for leg in LEGS)
    old_has_legs = existing.get("legs_present") is not None or any(f"{leg}_n" in existing for leg in LEGS)
    if new_has_legs or old_has_legs:
        if _leg_count(new) < _leg_count(existing):
            return True
    return False


def build_latest(priv: dict, allow_observation_only: bool = False) -> dict:
    health = priv.get("source_health") or []
    obs_only = {o.get("source_id") for o in (priv.get("observation_only_sources") or []) if isinstance(o, dict)}
    # Guard: restricted sources must not have contributed observations.
    for h in health:
        sid = h.get("source_id")
        if sid in LICENCE_RESTRICTED and (h.get("observation_count") or 0) > 0:
            if allow_observation_only and (h.get("observation_only") is True or sid in obs_only):
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
            "label": LABELS_EN[key],
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
        models[key].update(extract_guard_fields(m, composite=False))
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
            "note": "All published values are in USD. Non-USD source prices are converted to USD internally at this daily FX rate; the rates themselves are not published.",
        },
        "assumptions": translate_assumptions(priv.get("assumptions") or {}),
        "composite": {
            "label": LABELS_EN["COMPOSITE"],
            "nav_indicative_usd": None if comp_hidden else comp.get("nav_indicative_usd"),
            "nav_hidden": comp_hidden,
            "nav_hidden_reason": COMPOSITE_HIDDEN_REASON if comp_hidden else None,
            "n_models": comp_bd.get("n_models"),
            "quality_flag": comp.get("quality_flag"),
            **extract_guard_fields(comp, composite=True),
        },
        "models": models,
        "small_sample_rule": {
            "min_n": MIN_SAMPLE_N,
            "text": "Any method leg with n<3 is published as null with its n and insufficient_sample=true; derived values (rent median, new-card anchor) are hidden too. NAV and composite are computed upstream from all legs (formula unchanged) but are hidden (nav_hidden=true) if publishing them would let a hidden leg be backed out.",
        },
        "source_health": pub_health,
        "licensing": {
            "excluded_from_index": [
                {"source_id": k, "reason": v} for k, v in LICENCE_RESTRICTED.items()
            ],
            "cross_check_only_not_published": [
                {"source_id": k, "reason": v} for k, v in CROSS_CHECK_ONLY.items()
            ],
            "note": "Public data contains only per-model aggregates (indicative NAV, method legs, counts, medians). No listing-level observations or per-source raw prices. Per methodology section 10, Vast.ai, eBay, Clustermelt and Mulligan are excluded from the official calculation; the daily CI does not fetch Vast.ai or eBay.",
        },
    }


def _history_point(key: str, latest_pub: dict) -> dict:
    date = latest_pub["asof_date_pt"]
    if key == "COMPOSITE":
        src = latest_pub["composite"]
        pt = {"asof_date_pt": date, "computed_at_pt": latest_pub["computed_at_pt"],
              "nav_indicative_usd": src.get("nav_indicative_usd"),
              "nav_hidden": bool(src.get("nav_hidden"))}
    else:
        src = latest_pub["models"].get(key) or {}
        pt = {"asof_date_pt": date, "computed_at_pt": latest_pub["computed_at_pt"],
              "nav_indicative_usd": src.get("nav_indicative_usd"),
              "market_usd": src.get("market_usd"), "income_usd": src.get("income_usd"),
              "cost_usd": src.get("cost_usd"),
              "market_n": src.get("market_n"), "income_n": src.get("income_n"), "cost_n": src.get("cost_n"),
              "insufficient_sample": src.get("insufficient_sample"),
              "nav_hidden": bool(src.get("nav_hidden"))}
    pt.update(extract_guard_fields(src, composite=(key == "COMPOSITE")))
    return pt


def upsert_history(hist: dict | None, latest_pub: dict) -> dict:
    if not hist:
        hist = {"schema_version": SCHEMA_VERSION, "series": {}, "dates": []}
    hist.setdefault("series", {})
    date = latest_pub["asof_date_pt"]
    if not date:
        raise SystemExit("latest has no asof_date_pt")
    new_pts = {key: _history_point(key, latest_pub) for key in SERIES_KEYS}
    existing = {}
    for key in SERIES_KEYS:
        s = hist["series"].setdefault(key, {"label": LABELS_EN[key], "points": []})
        existing[key] = next((p for p in s.get("points", []) if p.get("asof_date_pt") == date), None)
    refused = [key for key in SERIES_KEYS if should_refuse_overwrite(existing[key], new_pts[key])]
    if refused and any(existing[k] is not None for k in SERIES_KEYS):
        reasons = []
        for key in refused:
            new, old = new_pts[key], existing[key]
            if point_is_stale(new):
                reasons.append(f"{key}: new point is stale/carried_forward")
            elif _leg_count(new) < _leg_count(old or {}):
                reasons.append(f"{key}: fewer legs ({_leg_count(new)} < {_leg_count(old)})")
        print("history upsert refused for " + date + " (" + "; ".join(reasons)
              + "); keeping the earlier same-asof_date points", file=sys.stderr)
        return hist
    for key in SERIES_KEYS:
        s = hist["series"].setdefault(key, {"label": LABELS_EN[key], "points": []})
        s.pop("label_en", None)
        s["label"] = LABELS_EN[key]
        pts = [p for p in s.get("points", []) if p.get("asof_date_pt") != date]
        pts.append(new_pts[key])
        s["points"] = sorted(pts, key=lambda p: p["asof_date_pt"])
    hist["dates"] = sorted({p["asof_date_pt"] for s in hist["series"].values() for p in s["points"]})
    hist["generated_at_pt"] = latest_pub["computed_at_pt"]
    hist["schema_version"] = SCHEMA_VERSION
    return hist


def annotate_history_guards(hist: dict) -> dict:
    """Fill missing publication-guard fields on existing history points (defaults: not stale)."""
    for key, s in (hist.get("series") or {}).items():
        for p in s.get("points") or []:
            fields = extract_guard_fields(p, composite=(key == "COMPOSITE"))
            for k, v in fields.items():
                if k not in p:
                    p[k] = v
    return hist



FORBIDDEN_KEYS = {"observations", "raw_label", "snapshot_hash", "values_usd", "source_url", "sha256", "path", "raw_nav_indicative_usd", "raw_nav_usd", "publication_guard"}


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
    # Competitor denylist guard (Silicon Data / Ornn / SemiAnalysis / Compute
    # Exchange): refuse if any source or observation of the run is theirs,
    # matched by id / URL domain / registered name only.
    import denylist
    hits = denylist.source_hits(priv, "latest")
    dash = Path(a.private_latest).with_name("dashboard_data.json")
    if dash.exists():
        hits += denylist.source_hits(json.loads(dash.read_text()).get("observations") or [], "observations")
    if hits:
        raise SystemExit("GUARD: denylisted competitor source in run: " + "; ".join(hits[:5]))
    pub = englishify(build_latest(priv, a.allow_observation_only))
    if all(pub["models"][k] is None or pub["models"][k]["nav_indicative_usd"] is None for k in MODEL_KEYS):
        raise SystemExit("no model produced an indicative NAV today; not publishing")
    out = Path(a.public_dir)
    out.mkdir(parents=True, exist_ok=True)
    hp = out / "history.json"
    hist = json.loads(hp.read_text()) if hp.exists() else None
    hist = upsert_history(hist, pub)
    hist = annotate_history_guards(hist)
    assert_clean(pub)
    assert_clean(hist)
    assert_small_sample(pub, hist)
    assert_english(pub)
    assert_english(hist)
    # USD-only guard (shared with pipeline/check_public.py)
    from check_public import json_problems
    probs = json_problems(pub, "latest") + json_problems(hist, "history")
    probs += denylist.source_hits(pub, "latest") + denylist.source_hits(hist, "history")
    if probs:
        raise SystemExit("GUARD: " + "; ".join(probs[:5]))
    (out / "latest.json").write_text(json.dumps(pub, indent=2, ensure_ascii=False) + "\n")
    hp.write_text(json.dumps(hist, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out/'latest.json'} and {hp} (asof {pub['asof_date_pt']}, {len(hist['dates'])} history dates)")


if __name__ == "__main__":
    sys.exit(main())
