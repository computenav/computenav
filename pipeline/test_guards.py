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

class PublicationGuardExportTests(unittest.TestCase):
    def _latest_shell(self):
        L = json.loads((DATA / "latest.json").read_text())
        # Minimal public-shaped latest used as the upsert source.
        return L

    def test_guard_fields_present_and_raw_nav_absent(self):
        L = json.loads((DATA / "latest.json").read_text())
        H = json.loads((DATA / "history.json").read_text())
        keys = ("carried_forward", "stale", "dod_excluded", "carry_reason", "carried_from_asof", "legs_present")
        for k, m in L["models"].items():
            for f in keys:
                self.assertIn(f, m, msg=k)
            self.assertNotIn("raw_nav_indicative_usd", m)
            self.assertNotIn("raw_nav_usd", m)
            if m.get("stale") or m.get("carried_forward"):
                self.assertTrue(m["dod_excluded"])
            if k != "COMPOSITE":
                self.assertIsInstance(m["legs_present"], int)
        for f in keys:
            self.assertIn(f, L["composite"])
        self.assertIsNone(L["composite"]["legs_present"])
        self.assertNotIn("raw_nav_indicative_usd", L["composite"])
        for key, s in H["series"].items():
            for pt in s["points"]:
                for f in keys:
                    self.assertIn(f, pt, msg=f"{key} {pt.get('asof_date_pt')}")
                self.assertNotIn("raw_nav_indicative_usd", pt)
                self.assertNotIn("raw_nav_usd", pt)

    def test_build_latest_strips_raw_nav(self):
        priv = {
            "asof_date_pt": "2026-11-24",
            "computed_at_pt": "2026-11-24T06:58:04-08:00",
            "computed_at_utc": "2026-11-24T14:58:04+00:00",
            "run_id": "test",
            "sold_count_today": 0,
            "observation_count": 0,
            "official_observation_counts": {"market": 0, "income": 0, "cost_new": 0},
            "fx": {"source": "test", "asof_utc": "2026-11-24T00:00:00+00:00"},
            "assumptions": {},
            "source_health": [],
            "models": {},
            "composite": {
                "nav_indicative_usd": 100.0,
                "quality_flag": "",
                "breakdown": {"n_models": 4},
                "raw_nav_indicative_usd": 999.0,
                "stale": False,
                "carried_forward": False,
            },
        }
        # Use a real published latest as a template for model shapes/n counts.
        published = json.loads((DATA / "latest.json").read_text())
        for k, m in published["models"].items():
            priv["models"][k] = {
                "nav_indicative_usd": 1.0,
                "market_usd": None,
                "income_usd": None,
                "cost_usd": None,
                "market_n": m["market_n"],
                "income_n": m["income_n"],
                "cost_n": m["cost_n"],
                "rental_median_usd_per_hour": None,
                "new_card_usd": None,
                "quality_flag": "",
                "breakdown": {"indicative_nav": {"legs_used": 1}},
                "raw_nav_indicative_usd": 12345.67,
                "stale": False,
                "carried_forward": False,
                "dod_excluded": False,
                "carry_reason": None,
                "carried_from_asof": None,
                "legs_present": m.get("legs_present") or 0,
            }
        pub = ep.build_latest(priv)
        blob = json.dumps(pub)
        self.assertNotIn("raw_nav_indicative_usd", blob)
        self.assertNotIn("12345.67", blob)
        self.assertIn("carried_forward", pub["models"]["H100_SXM"])
        self.assertFalse(pub["models"]["H100_SXM"]["stale"])
        with self.assertRaises(SystemExit):
            ep.assert_clean({"raw_nav_indicative_usd": 1})

    def test_upsert_refuses_stale_or_fewer_legs(self):
        L = json.loads((DATA / "latest.json").read_text())
        H = json.loads((DATA / "history.json").read_text())
        date = L["asof_date_pt"]
        before = copy.deepcopy(H)
        # Stale incoming point must not replace the stored same-day point.
        stale = copy.deepcopy(L)
        stale["models"]["H100_SXM"]["stale"] = True
        stale["models"]["H100_SXM"]["carried_forward"] = True
        stale["models"]["H100_SXM"]["dod_excluded"] = True
        stale["models"]["H100_SXM"]["nav_indicative_usd"] = 1.0
        out = ep.upsert_history(copy.deepcopy(H), stale)
        stored = next(p for p in out["series"]["H100_SXM"]["points"] if p["asof_date_pt"] == date)
        self.assertEqual(stored["nav_indicative_usd"], L["models"]["H100_SXM"]["nav_indicative_usd"])
        self.assertFalse(stored.get("stale"))
        # Fewer legs must not replace a more complete same-day point.
        fewer = copy.deepcopy(L)
        fewer["models"]["H100_SXM"]["market_n"] = 0
        fewer["models"]["H100_SXM"]["cost_n"] = 0
        fewer["models"]["H100_SXM"]["legs_present"] = 1
        fewer["models"]["H100_SXM"]["nav_indicative_usd"] = 2.0
        out2 = ep.upsert_history(copy.deepcopy(H), fewer)
        stored2 = next(p for p in out2["series"]["H100_SXM"]["points"] if p["asof_date_pt"] == date)
        self.assertEqual(stored2["nav_indicative_usd"], L["models"]["H100_SXM"]["nav_indicative_usd"])
        self.assertGreaterEqual(stored2["legs_present"], 3)
        # A non-stale same-leg (or better) point is allowed.
        better = copy.deepcopy(L)
        better["computed_at_pt"] = "2026-11-24T12:00:00-08:00"
        out3 = ep.upsert_history(copy.deepcopy(H), better)
        stored3 = next(p for p in out3["series"]["H100_SXM"]["points"] if p["asof_date_pt"] == date)
        self.assertEqual(stored3["computed_at_pt"], "2026-11-24T12:00:00-08:00")
        self.assertEqual(before["series"]["H100_SXM"]["points"][-1]["asof_date_pt"], date)

    def test_nov23_history_restored_to_morning(self):
        H = json.loads((DATA / "history.json").read_text())
        def pt(key):
            return next(p for p in H["series"][key]["points"] if p["asof_date_pt"] == "2026-11-23")
        sxm = pt("H100_SXM")
        self.assertEqual(sxm["nav_indicative_usd"], 19743.73)
        self.assertEqual(sxm["income_usd"], 41123.05)
        self.assertIsNone(sxm["market_usd"])  # n=1, hidden
        self.assertIsNone(sxm["cost_usd"])    # n=1, hidden
        self.assertEqual((sxm["market_n"], sxm["income_n"], sxm["cost_n"]), (1, 11, 1))
        self.assertFalse(sxm.get("stale") or sxm.get("carried_forward") or sxm.get("dod_excluded"))
        self.assertEqual(sxm["computed_at_pt"], "2026-11-23T06:59:14.115579-08:00")
        self.assertEqual(pt("H100_PCIe")["nav_indicative_usd"], 16658.03)
        self.assertTrue(pt("A100_80GB")["nav_hidden"])
        self.assertIsNone(pt("A100_80GB")["nav_indicative_usd"])
        self.assertIsNone(pt("A100_80GB")["cost_usd"])  # n=2, hidden
        self.assertEqual(pt("A100_40GB")["nav_indicative_usd"], 20721.89)
        self.assertTrue(pt("COMPOSITE")["nav_hidden"])
        self.assertIsNone(pt("COMPOSITE")["nav_indicative_usd"])
        blob = json.dumps(H)
        self.assertNotIn("8639.79", blob)
        self.assertNotIn("16440.86", blob)
        self.assertNotIn("10563.08", blob)
        self.assertNotIn("7545.06", blob)
        self.assertNotIn("raw_nav", blob)


# ---------------------------------------------------------------------------
# AMD Instinct MI300X: separate sub-index (methodology section 16)
# ---------------------------------------------------------------------------
MI300X_HIDDEN_1125 = ("7063.29", "19099.09", "14126.58")  # cost n=1, NAV, new-card anchor (2026-11-25)


def _priv_fixture():
    """Private-shaped latest mirroring 2026-11-25: NVIDIA legs all publishable
    (n>=3) so the composite is publishable; MI300X market n=0, income n=5,
    cost n=1 (hidden) -> NAV hidden."""
    def model(nav, mk, inc, cost, n, legs, rent=None, new=None):
        return {"nav_indicative_usd": nav, "market_usd": mk, "income_usd": inc, "cost_usd": cost,
                "market_n": n[0], "income_n": n[1], "cost_n": n[2],
                "rental_median_usd_per_hour": rent, "new_card_usd": new, "quality_flag": "sold_n=0",
                "breakdown": {"indicative_nav": {"legs_used": legs}},
                "raw_nav_indicative_usd": nav, "stale": False, "carried_forward": False,
                "dod_excluded": False, "carry_reason": None, "carried_from_asof": None, "legs_present": legs}
    return {
        "asof_date_pt": "2026-11-25", "computed_at_pt": "2026-11-25T13:10:02-08:00",
        "computed_at_utc": "2026-11-25T21:10:02+00:00", "run_id": "test", "sold_count_today": 0,
        "observation_count": 0, "official_observation_counts": {"market": 0, "income": 0, "cost_new": 0},
        "fx": {"source": "test", "asof_utc": "2026-11-25T00:00:00+00:00"}, "assumptions": {}, "source_health": [],
        "models": {
            "H100_SXM": model(19735.02, None, 19735.02, None, (0, 11, 0), 1, 2.49),
            "H100_PCIe": model(16652.86, None, 16652.86, None, (0, 3, 0), 1, 1.99),
            "A100_80GB": model(8634.34, None, 8634.34, None, (0, 9, 0), 1, 1.19),
            "A100_40GB": model(20721.89, None, 20721.89, None, (0, 4, 0), 1, 1.29),
            "MI300X": model(19099.09, None, 31134.89, 7063.29, (0, 5, 1), 2, 2.99, 14126.58),
        },
        "composite": {"nav_indicative_usd": 16436.03, "quality_flag": "mi300x_excluded_from_composite",
                      "breakdown": {"n_models": 4, "composite_members": ep.MODEL_KEYS,
                                    "excluded_subindices": ["MI300X"]},
                      "raw_nav_indicative_usd": 16436.03, "stale": False, "carried_forward": False},
    }


class MI300XSubindexTests(unittest.TestCase):
    def setUp(self):
        self.pub = ep.build_latest(_priv_fixture())

    def test_mi300x_masked_and_separate(self):
        m = self.pub["models"]["MI300X"]
        self.assertEqual(m["income_usd"], 31134.89)
        self.assertEqual(m["rental_median_usd_per_hour"], 2.99)
        self.assertIsNone(m["cost_usd"])
        self.assertIsNone(m["new_card_usd"])
        self.assertIsNone(m["market_usd"])
        self.assertIsNone(m["nav_indicative_usd"])
        self.assertTrue(m["nav_hidden"])
        self.assertTrue(m["insufficient_sample"]["cost"])
        self.assertFalse(m["insufficient_sample"]["market"])  # n=0: absent, not "small"
        self.assertIn("Insufficient samples", m["market_note"])
        self.assertIn("Parameters to be calibrated", m["income_note"])
        self.assertFalse(m["in_composite"])
        self.assertTrue(m["separate_subindex"])
        self.assertEqual(self.pub["composite_members"], ep.MODEL_KEYS)
        self.assertNotIn("MI300X", self.pub["composite"]["members"])
        self.assertEqual(self.pub["composite"]["excluded_subindices"], ["MI300X"])
        blob = json.dumps(self.pub)
        for v in MI300X_HIDDEN_1125:
            self.assertNotIn(v, blob)

    def test_composite_unaffected_by_mi300x(self):
        # MI300X's hidden cost leg must not hide (or enter) the composite.
        self.assertEqual(self.pub["composite"]["nav_indicative_usd"], 16436.03)
        self.assertFalse(self.pub["composite"]["nav_hidden"])
        plan = ep.plan_masking({k: {l: self.pub["models"][k][f"{l}_n"] for l in ep.LEGS} for k in ep.MODEL_KEYS})
        self.assertNotIn("MI300X", plan["nav"])
        self.assertEqual(plan["composite"], "published")

    def test_guard_rejects_mi300x_leaks(self):
        H = ep.upsert_history(None, self.pub)
        ep.assert_small_sample(self.pub, H)
        for field, val in (("cost_usd", 7063.29), ("nav_indicative_usd", 19099.09), ("new_card_usd", 14126.58)):
            L = copy.deepcopy(self.pub)
            L["models"]["MI300X"][field] = val
            with self.assertRaises(SystemExit, msg=field):
                ep.assert_small_sample(L, H)
            H2 = copy.deepcopy(H)
            H2["series"]["MI300X"]["points"][-1][field] = val
            if field != "new_card_usd":  # history points do not carry the anchor
                with self.assertRaises(SystemExit, msg="history " + field):
                    ep.assert_small_sample(self.pub, H2)

    def test_check_public_leak_detection(self):
        priv = _priv_fixture()
        root = Path(tempfile.mkdtemp())
        (root / "docs" / "data").mkdir(parents=True)
        (root / "README.md").write_text("ComputeNAV\n")
        (root / "docs" / "data" / "latest.json").write_text(json.dumps(self.pub))
        (root / "docs" / "index.html").write_text("<p>ok</p>")
        self.assertEqual(check_public.leak_problems(priv, root), [])
        for leak in ("$19,099.09", "7063.29", "14126.58"):
            (root / "docs" / "index.html").write_text(f"<p>{leak}</p>")
            self.assertTrue(check_public.leak_problems(priv, root), leak)
        (root / "docs" / "index.html").write_text("<p>ok</p>")
        L = copy.deepcopy(self.pub)
        L["models"]["MI300X"]["debug"] = 7063.29
        (root / "docs" / "data" / "latest.json").write_text(json.dumps(L))
        self.assertTrue(check_public.leak_problems(priv, root))

    def test_live_public_files_have_no_mi300x_hidden_values(self):
        for p in check_public.public_files():
            text = p.read_text(encoding="utf-8")
            for v in MI300X_HIDDEN_1125:
                self.assertNotIn(v, text, str(p))

    def test_subindex_refusal_does_not_block_composite(self):
        H = ep.upsert_history(None, self.pub)
        later = copy.deepcopy(self.pub)
        later["computed_at_pt"] = "2026-11-25T16:00:00-08:00"
        later["models"]["MI300X"].update(stale=True, carried_forward=True, dod_excluded=True)
        later["models"]["H100_SXM"]["nav_indicative_usd"] = 19800.0
        later["models"]["H100_SXM"]["income_usd"] = 19800.0
        out = ep.upsert_history(copy.deepcopy(H), later)
        sxm = out["series"]["H100_SXM"]["points"][-1]
        mi = out["series"]["MI300X"]["points"][-1]
        self.assertEqual(sxm["nav_indicative_usd"], 19800.0)
        self.assertFalse(mi["stale"])  # earlier same-day MI300X point kept
        self.assertEqual(mi["income_usd"], 31134.89)

    def test_no_mi300x_in_private_means_no_card(self):
        priv = _priv_fixture()
        del priv["models"]["MI300X"]
        pub = ep.build_latest(priv)
        self.assertNotIn("MI300X", pub["models"])
        self.assertEqual(pub["separate_subindices"], [])
        H = ep.upsert_history(None, pub)
        self.assertNotIn("MI300X", H["series"])


# ---------------------------------------------------------------------------
# Southeast Asia basket: separate sub-index (methodology section 17)
# ---------------------------------------------------------------------------

class SEABasketTests(unittest.TestCase):
    def _priv(self):
        priv = _priv_fixture()
        priv["models"]["SEA"] = {
            "nav_indicative_usd": 20000.0, "market_usd": None, "income_usd": None, "cost_usd": None,
            "market_n": 0, "income_n": 0, "cost_n": 0, "rental_median_usd_per_hour": None, "new_card_usd": None,
            "quality_flag": "not_in_nvidia_composite;sea_regional_basket=SG_MY_ID_VN",
            "breakdown": {"indicative_nav": {"legs_used": 0}},
            "raw_nav_indicative_usd": 20000.0, "stale": False, "carried_forward": False,
            "dod_excluded": False, "carry_reason": None, "carried_from_asof": None, "legs_present": 2,
            "sea_countries": ["Singapore", "Malaysia", "Indonesia", "Vietnam"],
            "sea_country_codes": ["SG", "MY", "ID", "VN"],
            "sea_member_keys": list(ep.SEA_MEMBER_KEYS),
            "sea_official_observation_counts": {"market": 0, "income": 5, "cost": 0},
            "sea_source_ids": ["acc_sg", "fpt_vn"],
            "sea_members": {
                "H100_SXM": {"label": "H100 SXM", "regions": ["SG"], "sources": ["acc_sg"],
                             "market_usd": None, "income_usd": 10000.0, "cost_usd": None,
                             "nav_indicative_usd": 10000.0, "market_n": 0, "income_n": 2, "cost_n": 0,
                             "rental_median_usd_per_hour": 1.11, "new_card_usd": None},
                "H100_PCIe": {"label": "H100 PCIe", "regions": ["SG"], "sources": ["acc_sg"],
                              "market_usd": None, "income_usd": None, "cost_usd": None,
                              "nav_indicative_usd": None, "market_n": 0, "income_n": 0, "cost_n": 0,
                              "rental_median_usd_per_hour": None, "new_card_usd": None},
                "A100_80GB": {"label": "A100 80GB", "regions": [], "sources": [],
                              "market_usd": None, "income_usd": None, "cost_usd": None,
                              "nav_indicative_usd": None, "market_n": 0, "income_n": 0, "cost_n": 0,
                              "rental_median_usd_per_hour": None, "new_card_usd": None},
                "A100_40GB": {"label": "A100 40GB", "regions": [], "sources": [],
                              "market_usd": None, "income_usd": None, "cost_usd": None,
                              "nav_indicative_usd": None, "market_n": 0, "income_n": 0, "cost_n": 0,
                              "rental_median_usd_per_hour": None, "new_card_usd": None},
                "H200": {"label": "H200", "regions": ["MY", "ID"], "sources": ["novagpu_my", "teamcloud_my", "biznet_neo"],
                         "market_usd": None, "income_usd": 30000.0, "cost_usd": None,
                         "nav_indicative_usd": 30000.0, "market_n": 0, "income_n": 3, "cost_n": 0,
                         "rental_median_usd_per_hour": 2.22, "new_card_usd": None},
            },
        }
        bd = priv["composite"].setdefault("breakdown", {})
        bd["excluded_subindices"] = ["MI300X", "SEA"]
        return priv

    def test_sea_basket_masked_and_separate(self):
        pub = ep.build_latest(self._priv())
        sea = pub["models"]["SEA"]
        self.assertFalse(sea["in_composite"])
        self.assertTrue(sea["separate_subindex"])
        self.assertIn("SEA", pub["separate_subindices"])
        self.assertNotIn("SEA", pub["composite"]["members"])
        self.assertIn("SEA", pub["composite"]["excluded_subindices"])
        # H100_SXM income n=2 is hidden; H200 n=3 is published; basket of members can be published
        # (it does not uniquely reveal the one hidden member? Wait: only one hidden member NAV
        # plus published H200 — publishing the basket would reveal the hidden H100_SXM NAV.)
        h100 = sea["sea_members"]["H100_SXM"]
        h200 = sea["sea_members"]["H200"]
        self.assertTrue(h100["nav_hidden"])
        self.assertIsNone(h100["income_usd"])
        self.assertIsNone(h100["rental_median_usd_per_hour"])
        self.assertFalse(h200["nav_hidden"])
        self.assertEqual(h200["income_usd"], 30000.0)
        self.assertTrue(sea["nav_hidden"])  # would reveal the single hidden member
        self.assertIsNone(sea["nav_indicative_usd"])
        blob = json.dumps(pub)
        self.assertNotIn("10000.0", blob)
        self.assertNotIn("1.11", blob)
        self.assertIn("30000", blob)

    def test_sea_does_not_enter_nvidia_composite(self):
        pub = ep.build_latest(self._priv())
        self.assertEqual(pub["composite"]["nav_indicative_usd"], 16436.03)
        self.assertFalse(pub["composite"]["nav_hidden"])
