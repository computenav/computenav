#!/usr/bin/env python3
"""One-off: seed the public history from an existing private nav.db.

Used once on 2026-11-16 to bootstrap docs/data/ from the box database
(/workspace/computenav/nav.db) WITHOUT re-scraping. For the latest run_id in
the DB it reloads the stored observations, drops every observation from the
licence-restricted sources (export_public.LICENCE_RESTRICTED), recomputes the
index with the unchanged run_daily.compute_index(), and writes a private
latest.json into a temp dir, which export_public.py then turns into public JSON.

The source DB is opened read-only and is never modified.

    python3 pipeline/seed_from_db.py --db /workspace/computenav/nav.db \
        --fx-from /workspace/computenav/output/latest.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--fx-from", required=True, help="private latest.json of the same run (for FX metadata)")
    ap.add_argument("--public-dir", default=str(HERE.parent / "docs" / "data"))
    a = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="computenav-seed-"))
    os.environ["COMPUTENAV_DATA_DIR"] = str(tmp)
    sys.path.insert(0, str(HERE))
    import run_daily as rd  # noqa: E402
    import export_public as ep  # noqa: E402

    # work on a copy so the source DB is untouched
    src = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    dst_path = tmp / "nav.db"
    dst = sqlite3.connect(dst_path)
    src.backup(dst)
    src.close()
    dst.row_factory = sqlite3.Row

    run_id = dst.execute("SELECT run_id FROM observations ORDER BY id DESC LIMIT 1").fetchone()[0]
    restricted = set(ep.LICENCE_RESTRICTED)
    obs = [dict(r) for r in dst.execute("SELECT * FROM observations WHERE run_id=?", (run_id,))]
    kept = [o for o in obs if o["source_id"] not in restricted]
    dropped = len(obs) - len(kept)
    # remove restricted rows from the temp copy so counts/health reflect the public basis
    dst.execute("DELETE FROM observations WHERE run_id=? AND source_id IN (%s)" % ",".join("?" * len(restricted)),
                (run_id, *sorted(restricted)))
    asof = kept[0]["asof_date_pt"]
    prev = dst.execute("SELECT computed_at_utc, computed_at_pt FROM daily_index WHERE asof_date_pt=? LIMIT 1", (asof,)).fetchone()
    computed_utc, computed_pt = prev["computed_at_utc"], prev["computed_at_pt"]
    # replace only the temp copy's daily_index with the restricted-free recompute
    dst.execute("DELETE FROM daily_index")
    rows = rd.compute_index(kept, asof, computed_utc, computed_pt)
    rd.upsert_index(dst, rows)
    dst.commit()

    health = []
    for h in dst.execute("SELECT * FROM source_health WHERE run_id=? ORDER BY id", (run_id,)):
        h = dict(h)
        if h["source_id"] in restricted:
            health.append({"source_id": h["source_id"], "url": h["url"], "ok": False, "skipped": True,
                           "http_status": None, "observation_count": 0, "fetched_at_utc": None,
                           "error": "excluded from public basis (licence pending / no licensed access)"})
        else:
            health.append({"source_id": h["source_id"], "url": h["url"], "ok": bool(h["ok"]),
                           "http_status": h["http_status"], "error": h["error"],
                           "observation_count": h["observation_count"], "fetched_at_utc": h["fetched_at_utc"]})

    fxj = json.loads(Path(a.fx_from).read_text())["fx"]
    fx = rd.FX()
    fx.source, fx.asof_utc = fxj["source"], fxj["asof_utc"]
    fx.usd_per.update({"EUR": fxj["usd_per_eur"], "GBP": fxj["usd_per_gbp"]})
    fx.raw_rates = {"EUR": fxj["eur_per_usd"], "GBP": fxj["gbp_per_usd"]}

    rd.write_outputs(dst, run_id, asof, computed_pt, computed_utc, health, kept, rows, fx)
    dst.close()
    print(f"seed run_id={run_id} asof={asof}: kept {len(kept)} obs, dropped {dropped} licence-restricted obs")
    ep.main(["--private-latest", str(rd.LATEST_PATH), "--public-dir", a.public_dir])
    shutil.rmtree(tmp)


if __name__ == "__main__":
    sys.exit(main())
