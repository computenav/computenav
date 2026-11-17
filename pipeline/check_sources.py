#!/usr/bin/env python3
"""CI pre-compute check: refuse to run if the pipeline registers a competitor
source (Silicon Data / Ornn / SemiAnalysis / Compute Exchange).

Imports run_daily without executing it and checks every SOURCE_PLAN entry by
id, URL domain and registered name against denylist.py. It also verifies that
upstream's own guard (competitor_exclusion_reason) is present and covers each
denied id/domain; gaps are reported as warnings (this check and the export
guard still refuse them)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import denylist  # noqa: E402
import run_daily  # noqa: E402


def check(plan) -> list[str]:
    problems = []
    for src in plan:
        hit = denylist.match_source(src.get("id"), src.get("name"), src.get("url"))
        if hit:
            problems.append(f"SOURCE_PLAN entry {src.get('id')!r} ({src.get('url')}) is a denied competitor source (matched {hit!r})")
    return problems


def upstream_coverage() -> tuple[list[str], list[str]]:
    fn = getattr(run_daily, "competitor_exclusion_reason", None)
    if fn is None:
        return ["run_daily.competitor_exclusion_reason missing (upstream guard not present)"], []
    warnings = [f"upstream guard does not reject id {i!r} (still refused here and at export)"
                for i in sorted(denylist.DENY_IDS) if not fn(i, None, None)]
    warnings += [f"upstream guard does not reject domain {d!r} (still blocked by export guard)"
                for d in sorted(denylist.DENY_DOMAINS) if not fn(None, None, f"https://{d}/")]
    return [], warnings


def main():
    probs = check(run_daily.SOURCE_PLAN)
    errs, warns = upstream_coverage()
    for w in warns:
        print("DENYLIST WARNING:", w)
    for p in probs + errs:
        print("DENYLIST CHECK FAILED:", p)
    if probs or errs:
        return 1
    print(f"denylist check OK: {len(run_daily.SOURCE_PLAN)} sources, none from Silicon Data / Ornn / SemiAnalysis / Compute Exchange")
    return 0


if __name__ == "__main__":
    sys.exit(main())
