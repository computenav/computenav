#!/usr/bin/env python3
"""CI pre-compute check: refuse to run if the pipeline registers a
denylisted competitor source (see denylist.py). Imports run_daily without
executing it and inspects SOURCE_PLAN (ids, URLs, names, parser names)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import denylist  # noqa: E402
import run_daily  # noqa: E402


def check(plan) -> list[str]:
    problems = []
    for src in plan:
        fields = {k: src.get(k) for k in ("id", "url", "name")}
        parser = src.get("parser")
        fields["parser"] = getattr(parser, "__name__", None)
        for k, v in fields.items():
            hit = denylist.matches(v)
            if hit:
                problems.append(f"SOURCE_PLAN entry {src.get('id')!r}: {k}={v!r} matches denylist term {hit!r}")
    # the pipeline's own hook must cover every denylist term
    own = getattr(run_daily, "COMPETITOR_DENYLIST", None)
    if own is None:
        problems.append("run_daily.COMPETITOR_DENYLIST missing (pipeline-side guard not installed)")
    else:
        missing = [t for t in denylist.DENY_TERMS if t not in own]
        if missing:
            problems.append(f"run_daily.COMPETITOR_DENYLIST lacks {missing}")
    return problems


def main():
    probs = check(run_daily.SOURCE_PLAN)
    for p in probs:
        print("DENYLIST CHECK FAILED:", p)
    if probs:
        return 1
    print(f"denylist check OK: {len(run_daily.SOURCE_PLAN)} sources, none from Silicon Data / Ornn / SemiAnalysis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
