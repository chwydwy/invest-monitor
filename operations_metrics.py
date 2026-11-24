"""Bounded, transactional activity counters. No network; never log payloads."""
import sqlite3
import json
from datetime import datetime, timedelta, timezone

KST=timezone(timedelta(hours=9))


def initialize(db, now):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS operations_daily(day TEXT,source TEXT,metric TEXT,n INTEGER NOT NULL,
        PRIMARY KEY(day,source,metric));
      CREATE TABLE IF NOT EXISTS operations_seen(day TEXT,source TEXT,metric TEXT,item TEXT,
        PRIMARY KEY(day,source,metric,item));
      CREATE TABLE IF NOT EXISTS operations_success(stage TEXT PRIMARY KEY,at REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS operations_errors(stage TEXT PRIMARY KEY,source TEXT,at REAL,code TEXT);
    ''')
    db.execute("INSERT OR IGNORE INTO meta VALUES ('operations_metrics_started',?)",(str(now),))


def mark(db, now, source=None, metric=None, item=None, success=None):
    """Use caller's transaction; a metrics error must not reject business work."""
    try:
        day=datetime.fromtimestamp(now,KST).date().isoformat()
        if metric:
            added=db.execute('INSERT OR IGNORE INTO operations_seen VALUES (?,?,?,?)',
                             (day,source,metric,item)).rowcount
            if added:
                db.execute('INSERT INTO operations_daily VALUES (?,?,?,1) ON CONFLICT(day,source,metric) DO UPDATE SET n=n+1',
                           (day,source,metric))
        if success:
            db.execute('INSERT INTO operations_success VALUES (?,?) ON CONFLICT(stage) DO UPDATE SET at=max(at,excluded.at)',(success,now))
        cutoff=(datetime.fromtimestamp(now,KST).date()-timedelta(days=1)).isoformat()
        # Only dedup metadata for today/yesterday, counters for seven days.
        previous=db.execute("SELECT value FROM meta WHERE key='operations_pruned_day'").fetchone()
        if not previous or json.loads(previous[0])!=day:
            db.execute('DELETE FROM operations_seen WHERE day<?',(cutoff,))
            db.execute('DELETE FROM operations_daily WHERE day<?',((datetime.fromtimestamp(now,KST).date()-timedelta(days=6)).isoformat(),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('operations_pruned_day',?)",(json.dumps(day),))
    except (sqlite3.Error,ValueError,OverflowError):
        try:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('operations_metrics_fault',?)",(str(now),))
        except sqlite3.Error:
            pass
