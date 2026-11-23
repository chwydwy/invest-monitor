import os
import io
import json
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from news_monitor.naver_news import load_or_create_cutover, parse_stock_news


class NpayNewsTest(unittest.TestCase):
    @staticmethod
    def news_module():
        """Load production functions without reading a real credential file."""
        source = Path(__file__).with_name("news.py")
        actual_open = open

        def safe_open(file, *args, **kwargs):
            if str(file).endswith("config_news.json"):
                return io.StringIO(json.dumps({
                    "TELEGRAM_TOKEN": "unused", "CHAT_ID_CHEM": "unused",
                    "OPENROUTER_API_KEY": "unused",
                }))
            return actual_open(file, *args, **kwargs)

        sys.path.insert(0, str(source.parent))
        try:
            spec = importlib.util.spec_from_file_location("_news_source_test", source)
            module = importlib.util.module_from_spec(spec)
            with patch("builtins.open", side_effect=safe_open):
                spec.loader.exec_module(module)
            return module
        finally:
            sys.path.remove(str(source.parent))

    def test_preserves_old_article_ids_and_orders_newest_first(self):
        payload = {"clusters": [
            {"items": [
                {"officeId": "015", "articleId": "0005357186", "title": "새 기사", "officeName": "언론사", "datetime": "202611231727"},
                {"officeId": "008", "articleId": "0005445162", "title": "이전 기사", "officeName": "언론사", "datetime": "202611231702"},
            ]},
            {"items": [
                {"officeId": "015", "articleId": "0005357186", "title": "중복 클러스터", "datetime": "202611231727"},
                {"officeId": "001", "articleId": "0000000001", "title": "오래된 기사", "datetime": "202611221600"},
                {"officeId": "002", "articleId": "0000000002", "title": "잘못된 시각", "datetime": "unknown"},
            ]},
        ]}
        rows = parse_stock_news(payload, datetime(2026, 11, 23, 17, 0))
        self.assertEqual([row["id"] for row in rows], ["015_0005357186", "008_0005445162"])
        self.assertEqual(rows[0]["date_raw"], "2026.11.23 17:27")
        self.assertEqual(rows[0]["press"], "언론사")
        self.assertEqual(rows[0]["office_id"], "015")
        self.assertEqual(rows[0]["article_id"], "0005357186")

    def test_missing_clusters_and_cutover_persistence(self):
        with self.assertRaises(ValueError):
            parse_stock_news({}, datetime(2026, 11, 23))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state" / "cutover.txt"
            first = datetime(2026, 11, 23, 17, 30)
            self.assertEqual(load_or_create_cutover(path, first), first)
            self.assertEqual(load_or_create_cutover(path, datetime(2026, 11, 24)), first)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_production_fetch_uses_json_and_preserves_record_shape(self):
        module = self.news_module()

        class Response:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"clusters": [{"items": [{
                    "officeId": "015", "articleId": "0005357186",
                    "title": "기사", "officeName": "언론사", "datetime": "202611231727",
                }]}]}

        with patch.object(module.requests, "get", return_value=Response()) as get:
            rows = module.get_latest_news_by_date("051910", datetime(2026, 11, 23, 17, 0))
        self.assertEqual(rows[0]["id"], "015_0005357186")
        self.assertEqual(get.call_args.kwargs["params"]["itemCode"], "051910")
        self.assertEqual(get.call_args.args[0], "https://stock.naver.com/api/domestic/detail/news")

    def test_ai_permanent_error_stops_without_marking_an_article(self):
        module = self.news_module()

        class Response:
            status_code = 404

        with patch.object(module.requests, "post", return_value=Response()) as post, patch.object(module.time, "sleep") as sleep:
            decision, summary = module.get_ai_summary("회사", "제목", "본문")
        self.assertEqual((decision, summary), (None, ""))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_ai_rate_limit_is_bounded(self):
        module = self.news_module()

        class Response:
            status_code = 429

        with patch.object(module.requests, "post", return_value=Response()) as post, patch.object(module.time, "sleep") as sleep:
            self.assertEqual(module.get_ai_summary("회사", "제목", "본문"), (None, ""))
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
