"""One-shot Pi outbox sender. Intended for a systemd timer after validation."""

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path
from urllib import request

from dashboard_outbox import counts, deliver_pending, inspect_items, release_auth_holds, release_item


@contextmanager
def exclusive_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another dashboard sender is running") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("another dashboard sender is running") from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_token(path):
    token_path = Path(path)
    mode = token_path.stat().st_mode
    if os.name == "posix" and mode & 0o077:
        raise ValueError("token file permissions must be owner-only")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("token file is missing or invalid")
    return token


def verify_duplicate(db_path, event_id, endpoint, token):
    """Explicitly resend one already delivered real event and check duplicate ack."""
    with closing(sqlite3.connect(db_path)) as db:
        row = db.execute("SELECT payload FROM outbox WHERE event_id=? AND state='delivered'", (event_id,)).fetchone()
    if not row:
        raise ValueError("delivered event not found")
    body = row[0].encode("utf-8")
    req = request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "Idempotency-Key": event_id,
    })
    with request.urlopen(req, timeout=10) as response:
        ack = json.load(response)
        if response.status != 200 or ack.get("event_id") != event_id or ack.get("status") != "duplicate":
            raise RuntimeError("duplicate acknowledgement mismatch")
    return "duplicate"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Dashboard outbox management")
    parser.add_argument("--db", default=os.environ.get("DASHBOARD_OUTBOX_DB", str(Path(__file__).with_name("dashboard_outbox.sqlite3"))))
    parser.add_argument("--endpoint", default=os.environ.get("DASHBOARD_INGEST_URL"))
    parser.add_argument("--token-file", default=os.environ.get("DASHBOARD_TOKEN_FILE"))
    parser.add_argument("--limit", type=int, default=5)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--once", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--holds", action="store_true")
    action.add_argument("--release", metavar="EVENT_ID")
    action.add_argument("--release-auth", action="store_true")
    action.add_argument("--verify-duplicate", metavar="EVENT_ID")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 50:
        parser.error("limit must be between 1 and 50")
    with exclusive_lock(args.db):
        if args.status:
            print(counts(args.db))
        elif args.holds:
            for item in inspect_items(args.db, limit=100):
                if item["state"] in ("hold_size", "hold_payload", "hold_auth", "conflict"):
                    print(item)
        elif args.release:
            print("released" if release_item(args.db, args.release) else "not released")
        elif args.release_auth:
            print(f"released {release_auth_holds(args.db)} authentication holds")
        else:
            if not args.endpoint or not args.endpoint.startswith("https://") and not args.endpoint.startswith("http://127.0.0.1:"):
                parser.error("HTTPS endpoint is required")
            if not args.token_file:
                parser.error("token file is required")
            token = read_token(args.token_file)
            if args.once:
                print(deliver_pending(args.db, args.endpoint, token, limit=args.limit))
            else:
                print(verify_duplicate(args.db, args.verify_duplicate, args.endpoint, token))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"dashboard sender error: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
