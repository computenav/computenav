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
    def test_current_plan_clean(self):
        self.assertEqual(check_sources.check(rd.SOURCE_PLAN), [])

    def test_plan_refused(self):
        for src in ({"id": "silicondata_rv", "url": "https://x"},
                    {"id": "x", "url": "https://data.ornn.com/analytics"},
                    {"id": "x", "url": "https://x", "name": "SemiAnalysis index"},
                    {"id": "x", "url": "https://www.ornnai.com"}):
            self.assertTrue(check_sources.check(rd.SOURCE_PLAN + [src]))
            with self.assertRaises(SystemExit):
                rd.assert_no_competitor_sources(rd.SOURCE_PLAN + [src])

    def test_rows_refused(self):
        with self.assertRaises(SystemExit):
            rd.assert_no_competitor_rows([{"source_id": "a", "source_url": "https://www.silicondata.com/x"}])
        rd.assert_no_competitor_rows([{"source_id": "geniushpc", "source_name": "Genius HPC", "source_url": "https://geniushpc.com"}])

    def test_export_source_hits(self):
        self.assertTrue(denylist.source_hits({"source_health": [{"source_id": "x", "url": "https://semianalysis.com"}]}))
        self.assertFalse([t for t in ("Genius HPC", "Cornell", "semiconductor", "silicon") if denylist.matches(t)])

    def test_pipeline_list_covers_denylist(self):
        self.assertTrue(set(denylist.DENY_TERMS) <= set(rd.COMPETITOR_DENYLIST))


class PublicDataTests(unittest.TestCase):
    def setUp(self):
        self.L = json.loads((DATA / "latest.json").read_text())
        self.H = json.loads((DATA / "history.json").read_text())

    def test_published_data_passes_all_guards(self):
        ep.assert_small_sample(self.L, self.H)
        ep.assert_english(self.L)
        ep.assert_english(self.H)
        self.assertEqual(check_public.json_problems(self.L, "l") + check_public.json_problems(self.H, "h"), [])
        self.assertEqual(denylist.text_hits(self.L) + denylist.text_hits(self.H), [])
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
