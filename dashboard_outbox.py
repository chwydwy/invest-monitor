"""Durable local handoff. Sending occurs only when deliver_pending is called."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib import error, request

MAX_BYTES = 65536
MAX_AI_CHARS = 40000
DASHBOARD_USER_AGENT = "invest-monitor-dashboard-sender/1.0"
HOLD_STATES = {"hold_size", "hold_payload", "hold_auth", "conflict"}


def _now():
    return datetime.now(timezone.utc)


def _connect(db_path):
    db = sqlite3.connect(db_path, timeout=10)
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS outbox (
        event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, delivered_at TEXT, last_status INTEGER
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
    for name in ("next_attempt_at", "last_error"):
        if name not in columns:
            db.execute(f"ALTER TABLE outbox ADD COLUMN {name} TEXT")
    db.commit()
    return db


def _size_error(payload, encoded):
    field = "analysis" if payload.get("source") == "dart" else "summary"
    value = payload.get(field)
    if not isinstance(value, str):
        return "missing_ai_text"
    # The Worker counts JavaScript UTF-16 code units for string.length.
    if len(value.encode("utf-16-le")) // 2 > MAX_AI_CHARS:
        return "ai_text_too_long"
    if len(encoded) > MAX_BYTES:
        return "payload_too_large"
    return None


def enqueue(db_path, payload):
    key = f"{payload['source']}:{payload['source_id']}"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    reason = _size_error(payload, encoded.encode("utf-8"))
    with closing(_connect(db_path)) as db, db:
        db.execute("""INSERT OR IGNORE INTO outbox
            (event_id,payload,state,created_at,last_error) VALUES(?,?,?,?,?)""",
            (key, encoded, "hold_size" if reason else "pending", _now().isoformat(), reason))
    return key


def counts(db_path):
    with closing(_connect(db_path)) as db:
        return dict(db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state"))


def inspect_items(db_path, state=None, limit=100):
    """Metadata only: never return payload, token, or AI text."""
    with closing(_connect(db_path)) as db:
        if state:
            rows = db.execute("""SELECT event_id,state,attempts,last_status,last_error,next_attempt_at
                                 FROM outbox WHERE state=? ORDER BY created_at LIMIT ?""", (state, limit))
        else:
            rows = db.execute("""SELECT event_id,state,attempts,last_status,last_error,next_attempt_at
                                 FROM outbox ORDER BY created_at LIMIT ?""", (limit,))
        return [dict(zip(("event_id", "state", "attempts", "last_status", "last_error", "next_attempt_at"), row)) for row in rows]


def release_item(db_path, event_id):
    """Explicit operator retry after fixing the underlying issue; never rewrite payload."""
    with closing(_connect(db_path)) as db, db:
        row = db.execute("SELECT state,payload FROM outbox WHERE event_id=?", (event_id,)).fetchone()
        if not row or row[0] not in HOLD_STATES:
            return False
        payload = json.loads(row[1])
        if _size_error(payload, row[1].encode("utf-8")):
            return False
        db.execute("UPDATE outbox SET state='pending',next_attempt_at=NULL,last_error=NULL WHERE event_id=?", (event_id,))
        return True


def release_auth_holds(db_path):
    """Resume authentication holds only after the shared credential is corrected."""
    with closing(_connect(db_path)) as db, db:
        cursor = db.execute("""UPDATE outbox SET state='pending',next_attempt_at=NULL,last_error=NULL
                               WHERE state='hold_auth'""")
        return cursor.rowcount


def _retry_time(attempts, retry_after=None):
    now = _now()
    delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
    if retry_after:
        try:
            delay = max(delay, int(retry_after))
        except ValueError:
            try:
                delay = max(delay, int((parsedate_to_datetime(retry_after) - now).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return (now + timedelta(seconds=max(1, delay))).isoformat()


def deliver_pending(db_path, endpoint, token, limit=50, timeout=10):
    if not endpoint or not token:
        raise ValueError("dashboard endpoint and token are required")
    result = {"delivered": 0, "retry": 0, "held": 0}
    with closing(_connect(db_path)) as db, db:
        if db.execute("SELECT 1 FROM outbox WHERE state='hold_auth' LIMIT 1").fetchone():
            return result
        rows = db.execute("""SELECT event_id,payload,attempts FROM outbox
            WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
            ORDER BY created_at,event_id LIMIT ?""", (_now().isoformat(), limit)).fetchall()
        for event_id, payload_text, attempts in rows:
            attempts += 1
            payload = json.loads(payload_text)
            reason = _size_error(payload, payload_text.encode("utf-8"))
            if reason:
                state, status, next_at = "hold_size", None, None
            else:
                req = request.Request(endpoint, data=payload_text.encode("utf-8"),
                                      headers={"Authorization": f"Bearer {token}",
                                               "Content-Type": "application/json",
                                               "Idempotency-Key": event_id,
                                               "User-Agent": DASHBOARD_USER_AGENT}, method="POST")
                status, next_at, reason, state = None, None, None, "pending"
                retry_after = None
                try:
                    with request.urlopen(req, timeout=timeout) as response:
                        status = response.status
                        ack = json.load(response)
                    if status in (200, 201) and ack.get("event_id") == event_id and ack.get("status") in ("stored", "duplicate"):
                        state = "delivered"
                    else:
                        reason = "invalid_ack"
                except error.HTTPError as exc:
                    status = exc.code
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if status == 409:
                        state, reason = "conflict", "event_id_conflict"
                    elif status == 413:
                        state, reason = "hold_size", "receiver_size_limit"
                    elif status in (401, 403):
                        state, reason = "hold_auth", "authentication_failed"
                    elif status in (400, 405, 415, 422):
                        state, reason = "hold_payload", f"http_{status}"
                    else:
                        reason = f"http_{status}"
                except (error.URLError, TimeoutError, OSError, ValueError):
                    reason = "network_or_response_error"
                if state == "pending":
                    next_at = _retry_time(attempts, retry_after)
            if state == "delivered":
                db.execute("""UPDATE outbox SET state=?,attempts=?,delivered_at=?,last_status=?,last_error=NULL,next_attempt_at=NULL
                              WHERE event_id=?""", (state, attempts, _now().isoformat(), status, event_id))
                result["delivered"] += 1
            else:
                db.execute("""UPDATE outbox SET state=?,attempts=?,last_status=?,last_error=?,next_attempt_at=?
                              WHERE event_id=?""", (state, attempts, status, reason, next_at, event_id))
                result["retry" if state == "pending" else "held"] += 1
            db.commit()
            if state == "hold_auth":
                # The same credential is used for the whole queue. Stop this batch
                # and hold the remaining pending entries until it is corrected.
                db.execute("""UPDATE outbox SET state='hold_auth',last_error='authentication_failed',next_attempt_at=NULL
                              WHERE state='pending'""")
                db.commit()
                result["held"] += db.execute("SELECT changes()").fetchone()[0]
                break
    return result
