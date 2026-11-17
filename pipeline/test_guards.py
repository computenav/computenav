#!/usr/bin/env python3
"""Offline guard tests (no network). Run: python3 -m unittest pipeline/test_guards.py"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("COMPUTENAV_DATA_DIR", tempfile.mkdtemp(prefix="computenav-test-"))

import check_public  # noqa: E402
import check_sources  # noqa: E402
import denylist  # noqa: E402
import export_public as ep  # noqa: E402
import run_daily as rd  # noqa: E402

DATA = HERE.parent / "docs" / "data"


class DenylistTests(unittest.TestCase):
    def test_pipeline_is_verbatim_upstream_with_guard(self):
        self.assertTrue(hasattr(rd, "competitor_exclusion_reason"))
        self.assertFalse(hasattr(rd, "COMPETITOR_DENYLIST"))

    def test_current_plan_clean(self):
        self.assertEqual(check_sources.check(rd.SOURCE_PLAN), [])
        self.assertEqual(check_sources.upstream_coverage()[0], [])

    def test_plan_refused(self):
        for src in ({"id": "silicondata_rv", "url": "https://example.com"},
                    {"id": "x", "url": "https://data.ornn.com/analytics"},
                    {"id": "x", "url": "https://www.ornnai.com/"},
                    {"id": "x", "url": "https://example.com", "name": "SemiAnalysis GPU index"},
                    {"id": "compute_exchange", "url": "https://example.com"},
                    {"id": "x", "url": "https://www.compute.exchange/"}):
            self.assertTrue(check_sources.check(rd.SOURCE_PLAN + [src]), src)

    def test_identity_fields_only(self):
        rec = {"source_health": [{"source_id": "nebius", "url": "https://nebius.com/prices", "ok": True,
                                  "error": "page cites SemiAnalysis ClusterMAX Gold award; Ornn; Silicon Data"}],
               "observations": [{"source_id": "nebius", "source_name": "Nebius",
                                 "source_url": "https://nebius.com/prices",
                                 "raw_text": "Rated Gold by SemiAnalysis", "notes": "see semianalysis.com"}]}
        self.assertEqual(denylist.source_hits(rec), [])
        for bad in ({"source_id": "semianalysis"}, {"source_name": "Silicon Data SDH100RT"},
                    {"url": "https://gpu-index.semianalysis.com/x"}, {"source_url": "https://compute.exchange/x"},
                    {"source_name": "Ornn OCPI"}):
            self.assertTrue(denylist.source_hits([bad]), bad)
        for ok in ("https://notsilicondata.com.example.org", "https://cornnet.com", "https://semianalysis.com.evil.io"):
            self.assertIsNone(denylist.match_url(ok), ok)
        self.assertFalse([n for n in ("Genius HPC", "Cornell", "semiconductor", "silicon", "Nebius") if denylist.match_name(n)])

    def test_export_refuses(self):
        import subprocess
        priv = json.loads(Path("/workspace/computenav/output/latest.json").read_text()) \
            if Path("/workspace/computenav/output/latest.json").exists() else None
        if priv is None:
            self.skipTest("no private latest.json available")
        for mutate, expect in ((lambda p: p["source_health"].append(
                {"source_id": "cx", "url": "https://www.compute.exchange/", "ok": False, "observation_count": 0}), 1),
                (lambda p: p["source_health"][0].update(error="mentions SemiAnalysis award"), 0)):
            p = copy.deepcopy(priv)
            mutate(p)
            d = Path(tempfile.mkdtemp())
            (d / "latest.json").write_text(json.dumps(p))
            r = subprocess.run([sys.executable, str(HERE / "export_public.py"), "--allow-observation-only",
                                "--private-latest", str(d / "latest.json"), "--public-dir", str(d / "pub")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, expect, r.stdout + r.stderr)


class PublicDataTests(unittest.TestCase):
    def setUp(self):
        self.L = json.loads((DATA / "latest.json").read_text())
        self.H = json.loads((DATA / "history.json").read_text())

    def test_published_data_passes_all_guards(self):
        ep.assert_small_sample(self.L, self.H)
        ep.assert_english(self.L)
        ep.assert_english(self.H)
        self.assertEqual(check_public.json_problems(self.L, "l") + check_public.json_problems(self.H, "h"), [])
        self.assertEqual(denylist.source_hits(self.L) + denylist.source_hits(self.H), [])
        self.assertEqual(check_public.check(), [])

    def test_small_sample_guard(self):
        for k, m in self.L["models"].items():
            for leg in ep.LEGS:
                if m["insufficient_sample"][leg]:
                    L = copy.deepcopy(self.L)
                    L["models"][k][f"{leg}_usd"] = 1.0
                    with self.assertRaises(SystemExit):
                        ep.assert_small_sample(L, self.H)

    def test_currency_and_language_guard(self):
        for bad in ("€10,348.80", "EUR 15,708", "15750 GBP", "¥1000"):
            self.assertTrue(check_public.text_problems(bad, "x"))
        self.assertTrue(check_public.json_problems({"usd_per_eur": 1.1}, "x"))
        with self.assertRaises(SystemExit):
            ep.assert_english({"label": "综合"})


if __name__ == "__main__":
    unittest.main()
