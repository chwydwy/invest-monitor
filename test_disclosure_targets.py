import unittest
import json
from pathlib import Path

from dart_monitor.disclosure_targets import (
    company_identity,
    disclosure_rooms,
    load_list,
    matches_group,
    review_bl_update,
)


ROOT = Path(__file__).parent


def load_names(path):
    return load_list(ROOT / path)


class DisclosureTargetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bl = load_names("dart_monitor/companies_BAMK_BL.txt")
        cls.jm = load_names("dart_monitor/companies_JM.txt")
        cls.chem = load_names("dart_monitor/companies_ChemHold.txt")

    def test_lists_form_disjoint_universe(self):
        bl_ids = {company_identity(name) for name in self.bl}
        jm_ids = {company_identity(name) for name in self.jm}
        self.assertFalse(bl_ids & jm_ids)
        self.assertTrue({company_identity(name) for name in self.chem} <= bl_ids | jm_ids)

    def test_fake_sender_routes_once_per_room(self):
        config = {"CHAT_ID_BL": "fake-bl", "CHAT_ID_CHEM": "fake-chem"}
        sent = []

        def fake_send(chat_id, message):
            sent.append((chat_id, message))

        for company, expected in (
            ("삼성전자", ["fake-bl"]),
            ("SK케미칼", ["fake-bl", "fake-chem"]),
            ("LG화학", ["fake-bl", "fake-chem"]),
            ("케이씨씨", ["fake-bl"]),
            ("케이티앤지", ["fake-bl"]),
        ):
            with self.subTest(company=company):
                sent.clear()
                in_bl = matches_group(self.bl, company, company, False)
                in_jm = matches_group(self.jm, company, company, False)
                in_chem = matches_group(self.chem, company, company, False)
                for room in disclosure_rooms(config, in_bl or in_jm, in_chem):
                    fake_send(room, "fake disclosure")
                self.assertEqual([room for room, _ in sent], expected)
                self.assertEqual(len(sent), len(set(sent)))

        self.assertEqual(disclosure_rooms({"CHAT_ID_BL": "same", "CHAT_ID_CHEM": "same"}, True, True), ["same"])

    def test_future_bl_review_does_not_move_removed_names_to_jm(self):
        proposed = [name for name in self.bl if name != "삼성전자"] + ["SK케미칼", "가상신규기업"]
        result = review_bl_update(self.bl, self.jm, proposed)
        self.assertEqual(result["bl_additions"], ["가상신규기업"])
        self.assertIn("삼성전자", result["bl_removals"])
        self.assertEqual(result["move_from_jm_to_bl"], ["SK케미칼"])
        self.assertNotIn("삼성전자", result["jm_retained"])

    def test_verified_aliases_and_exclusions(self):
        pairs = [
            ("LSELECTRIC", "엘에스일렉트릭"),
            ("LS일렉트릭", "엘에스일렉트릭"),
            ("CJENM", "CJ ENM"),
            ("JYPEnt.", "JYP Ent."),
            ("NHNKCP", "NHN KCP"),
            ("KCC", "케이씨씨"),
            ("KT&G", "케이티앤지"),
            ("TKG휴켐스", "티케이지휴켐스"),
            ("케이카", "KG모빌리티플랫폼"),
            ("티웨이항공", "트리니티항공"),
        ]
        for alias, representative in pairs:
            with self.subTest(alias=alias):
                self.assertEqual(company_identity(alias), representative)
                self.assertTrue(matches_group(self.bl + self.jm, alias, alias, False))
        for excluded in ("현대홈쇼핑", "에코마케팅", "HD현대미포", "동원F&B"):
            with self.subTest(excluded=excluded):
                self.assertFalse(matches_group(self.bl + self.jm + self.chem, excluded, excluded, False))

    def test_catalog_matches_source_lists(self):
        catalog = json.loads((ROOT / "DASHBOARD_COMPANIES.json").read_text(encoding="utf-8"))
        draft = json.loads((ROOT / "DASHBOARD_SECTOR_DRAFT.json").read_text(encoding="utf-8"))
        news = load_names("news_monitor/Companies_ChemHold_news.txt")
        self.assertEqual(len(catalog["companies"]), len(self.bl) + len(self.jm))
        self.assertEqual([r["name"] for r in catalog["companies"]], self.bl + self.jm)
        self.assertEqual([r["name"] for r in draft["companies"]], self.bl + self.jm)
        self.assertEqual({company_identity(n) for n in news}, {company_identity(n) for n in self.chem})
        by_name = {row["name"]: row for row in catalog["companies"]}
        for row in catalog["companies"]:
            self.assertEqual(row["news_monitored"], company_identity(row["name"]) in {company_identity(n) for n in news})
        for name, sector in {"삼성물산": "지주", "POSCO홀딩스": "철강금속", "HD한국조선해양": "조선", "메리츠금융지주": "보험", "SNT다이내믹스": "자동차", "HDC랩스": "건설건자재", "코웨이": "기타소비재"}.items():
            self.assertEqual(by_name[name]["sector"], sector)

    def test_requested_bl_jm_move_and_new_company(self):
        for name in ("레인보우로보틱스", "삼성에피스홀딩스", "현대그린푸드"):
            self.assertIn(name, self.bl)
            self.assertNotIn(name, self.jm)
            self.assertTrue(matches_group(self.bl, name, name, False))
        self.assertIn("이수화학", self.jm)
        self.assertNotIn("이수화학", self.bl)
        self.assertTrue(matches_group(self.jm, "이수화학", "이수화학", False))
        self.assertEqual(disclosure_rooms({"CHAT_ID_BL": "fake-bl", "CHAT_ID_CHEM": "fake-chem"}, True, False), ["fake-bl"])
        catalog = json.loads((ROOT / "DASHBOARD_COMPANIES.json").read_text(encoding="utf-8"))
        row = next(row for row in catalog["companies"] if row["name"] == "이수화학")
        self.assertEqual((row["stock_code"], row["sector"], row["universe_list"], row["news_monitored"]), ("005950", "화학", "JM", False))


if __name__ == "__main__":
    unittest.main()
