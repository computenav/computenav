#!/usr/bin/env python3
"""Seed docs/data/ from the box database using upstream's own recompute.

Copies nav.db and snapshots/<run_id>/ from the box data dir into a temp dir
(the source is never modified), runs
    COMPUTENAV_DATA_DIR=<tmp> python3 pipeline/run_daily.py --recompute-from-run <run_id>
(upstream logic: observation-only sources such as Vast.ai are excluded from the
official legs), then exports the public JSON with --allow-observation-only.

    python3 pipeline/seed_from_db.py --src /workspace/computenav --run-id 20261116T212104_PT
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="box data dir containing nav.db and snapshots/")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--public-dir", default=str(HERE.parent / "docs" / "data"))
    a = ap.parse_args(argv)
    src = Path(a.src)
    tmp = Path(tempfile.mkdtemp(prefix="computenav-seed-"))
    try:
        s = sqlite3.connect(f"file:{src / 'nav.db'}?mode=ro", uri=True)
        d = sqlite3.connect(tmp / "nav.db")
        s.backup(d)
        s.close()
        d.close()
        snap = src / "snapshots" / a.run_id
        if snap.is_dir():
            shutil.copytree(snap, tmp / "snapshots" / a.run_id)
        env = dict(os.environ, COMPUTENAV_DATA_DIR=str(tmp))
        subprocess.run([sys.executable, str(HERE / "run_daily.py"), "--recompute-from-run", a.run_id],
                       env=env, check=True)
        subprocess.run([sys.executable, str(HERE / "export_public.py"), "--allow-observation-only",
                        "--private-latest", str(tmp / "output" / "latest.json"),
                        "--public-dir", a.public_dir], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
