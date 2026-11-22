import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dashboard_outbox import counts, deliver_pending, enqueue, inspect_items, release_item, release_auth_holds
from dashboard_sender import verify_duplicate, exclusive_lock


class Receiver(BaseHTTPRequestHandler):
    status = 200
    calls = 0
    retry_after = None
    seen = set()

    def do_POST(self):
        Receiver.calls += 1
        event_id = self.headers.get("Idempotency-Key")
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(Receiver.status)
        if Receiver.retry_after:
            self.send_header("Retry-After", Receiver.retry_after)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if Receiver.status == 200:
            result = "duplicate" if event_id in Receiver.seen else "stored"
            Receiver.seen.add(event_id)
            self.wfile.write(json.dumps({"event_id": event_id, "status": result}).encode())

    def log_message(self, *_args):
        pass


class OutboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "outbox.sqlite3")
        Receiver.status, Receiver.calls, Receiver.retry_after, Receiver.seen = 200, 0, None, set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/events"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def payload(self, source_id="sample", source="dart", text="가상 분석"):
        return {"source": source, "source_id": source_id, "company": "가상기업",
                "published_at": None, "published_at_original": "원본 시각",
                "analysis" if source == "dart" else "summary": text}

    def row(self, key):
        with closing(sqlite3.connect(self.db)) as db:
            return db.execute("SELECT state,attempts,last_status,last_error,next_attempt_at,payload FROM outbox WHERE event_id=?", (key,)).fetchone()

    def test_store_duplicate_and_raw_times(self):
        dart = self.payload("001")
        dart["published_at_original"] = "Sun, 22 Nov 2026 09:00:00 GMT"
        news = self.payload("002", "news")
        news["published_at_original"] = "2026.11.22 09:00"
        enqueue(self.db, dart)
        enqueue(self.db, dart)
        enqueue(self.db, news)
        self.assertEqual(counts(self.db), {"pending": 2})
        self.assertEqual(deliver_pending(self.db, self.url, "local-test"), {"delivered": 2, "retry": 0, "held": 0})
        self.assertEqual(Receiver.calls, 2)
        self.assertEqual(counts(self.db), {"delivered": 2})
        self.assertEqual(verify_duplicate(self.db, "dart:001", self.url, "local-test"), "duplicate")
        self.assertEqual(counts(self.db), {"delivered": 2})
        self.assertIsNone(json.loads(self.row("dart:001")[5])["published_at"])
        self.assertEqual(json.loads(self.row("news:002")[5])["published_at_original"], "2026.11.22 09:00")

    def test_size_boundaries(self):
        for name, value, held in (("at", "a" * 40000, False), ("over", "a" * 40001, True), ("emoji", "😀" * 20001, True)):
            key = enqueue(self.db, self.payload(name, text=value))
            self.assertEqual(self.row(key)[0] == "hold_size", held)
        base = self.payload("bytes")
        base["padding"] = "가" * 10000
        encoded = json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        base["padding"] += "a" * (65536 - len(encoded))
        key = enqueue(self.db, base)
        self.assertEqual(len(self.row(key)[5].encode("utf-8")), 65536)
        self.assertEqual(self.row(key)[0], "pending")
        base["source_id"] = "bytes-over"
        base["padding"] += "a"
        key = enqueue(self.db, base)
        self.assertEqual(self.row(key)[0], "hold_size")
        self.assertFalse(release_item(self.db, key))

    def test_permanent_statuses(self):
        for status, expected in ((409, "conflict"), (413, "hold_size"), (400, "hold_payload"), (415, "hold_payload")):
            key = enqueue(self.db, self.payload(str(status)))
            Receiver.status = status
            self.assertEqual(deliver_pending(self.db, self.url, "local-test")["held"], 1)
            self.assertEqual(self.row(key)[0], expected)
            before = Receiver.calls
            deliver_pending(self.db, self.url, "local-test")
            self.assertEqual(Receiver.calls, before)
            if status == 409:
                self.assertTrue(release_item(self.db, key))
                Receiver.status = 200
                self.assertEqual(deliver_pending(self.db, self.url, "local-test")["delivered"], 1)
        self.assertTrue(all("payload" not in item for item in inspect_items(self.db)))

    def test_auth_pause_and_release(self):
        enqueue(self.db, self.payload("auth1"))
        enqueue(self.db, self.payload("auth2"))
        Receiver.status = 401
        self.assertEqual(deliver_pending(self.db, self.url, "bad-local-token"), {"delivered": 0, "retry": 0, "held": 2})
        self.assertEqual(Receiver.calls, 1)
        self.assertEqual(counts(self.db), {"hold_auth": 2})
        self.assertEqual(release_auth_holds(self.db), 2)
        Receiver.status = 200
        self.assertEqual(deliver_pending(self.db, self.url, "corrected-local-token")["delivered"], 2)

    def test_rate_limit_transient_and_retry(self):
        key = enqueue(self.db, self.payload("retry"))
        Receiver.status, Receiver.retry_after = 429, "120"
        self.assertEqual(deliver_pending(self.db, self.url, "local-test")["retry"], 1)
        self.assertGreaterEqual((datetime.fromisoformat(self.row(key)[4]) - datetime.now(timezone.utc)).total_seconds(), 115)
        Receiver.status, Receiver.retry_after = 200, None
        self.assertEqual(deliver_pending(self.db, self.url, "local-test")["delivered"], 0)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE outbox SET next_attempt_at=NULL WHERE event_id=?", (key,))
        Receiver.status = 503
        self.assertEqual(deliver_pending(self.db, self.url, "local-test")["retry"], 1)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE outbox SET next_attempt_at=NULL WHERE event_id=?", (key,))
        Receiver.status = 200
        self.assertEqual(deliver_pending(self.db, self.url, "local-test")["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
