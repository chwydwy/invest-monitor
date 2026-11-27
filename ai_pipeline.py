"""Durable analysis scheduling. No network or operational side effects on import."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from operations_metrics import initialize as initialize_metrics, mark
import news_selection

DEFAULTS = dict(interval=60, total=900, news=600, dart=200, retry=100, max_attempts=3)


class Queue:
    def __init__(self, path, clock=None, limits=None):
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        self.limits = dict(DEFAULTS, **(limits or {}))
        self.news_policy = dict(news_selection.DEFAULTS)
        if self.limits['interval'] < 60 or any(self.limits[k]<0 for k in ('total','news','dart','retry')):
            raise ValueError('invalid AI limits')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, source TEXT, payload TEXT,
                    state TEXT, created REAL, due REAL, attempts INTEGER DEFAULT 0,
                    result TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY, job TEXT,
                    day TEXT, bucket TEXT, started REAL, outcome TEXT);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS outputs(job TEXT, destination TEXT, payload TEXT,
                    state TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, due REAL DEFAULT 0,
                    reason TEXT, PRIMARY KEY(job,destination));
                CREATE TABLE IF NOT EXISTS discoveries(id TEXT, company TEXT, payload TEXT,
                    state TEXT DEFAULT 'pending', reason TEXT, PRIMARY KEY(id,company));
                CREATE TABLE IF NOT EXISTS scans(company TEXT PRIMARY KEY, last_success REAL,
                    checkpoint TEXT, gap TEXT);
                CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY,company TEXT,at REAL,boundary TEXT,reason TEXT);
            ''')
            initialize_metrics(db,self.clock())
            news_selection.initialize(db)

    def record(self,source=None,metric=None,item=None,success=None):
        try:
            with self.db() as db:
                mark(db,self.clock(),source,metric,item,success)
        except sqlite3.Error:
            pass

    def record_error(self,stage,source,code):
        try:
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO operations_errors VALUES (?,?,?,?)',
                           (stage,source,self.clock(),code))
        except sqlite3.Error:
            pass

    def insert_discovery(self,db,source,identifier,company,payload,exclusion=None,prefixed=True):
        key=source+':'+identifier
        if source=='news' and db.execute('SELECT 1 FROM news_tombstones WHERE id=?',(identifier,)).fetchone():
            return 0
        if source=='news' and db.execute('SELECT 1 FROM news_candidates WHERE id=?',(identifier,)).fetchone():
            return 0
        seen=db.execute("SELECT 1 FROM discoveries WHERE id IN (?,?) AND coalesce(json_extract(payload,'$.source'),'news')=? LIMIT 1",
                        (identifier,key,source)).fetchone()
        added=db.execute('INSERT OR IGNORE INTO discoveries(id,company,payload,state,reason,first_seen) VALUES (?,?,?,?,?,?)',
                         (key if prefixed else identifier,company,json.dumps(payload,ensure_ascii=False),
                          'excluded' if exclusion else 'pending',exclusion,self.clock())).rowcount
        if added and not seen:
            mark(db,self.clock(),source,'collected',key)
        if added and exclusion:
            mark(db,self.clock(),source,'excluded',key)
        return added

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def enqueue(self, source, source_id, payload):
        if source not in ('dart', 'news'):
            raise ValueError('invalid source')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if source=='news' and db.execute('SELECT 1 FROM news_tombstones WHERE id=?',(source_id,)).fetchone():return 0
            url=payload.get('event',{}).get('url')
            if url and db.execute("SELECT 1 FROM jobs WHERE source=? AND json_extract(payload,'$.event.url')=? LIMIT 1",(source,url)).fetchone():
                return 0
            return db.execute('INSERT OR IGNORE INTO jobs(id,source,payload,state,created,due) '
                              'VALUES (?,?,?,?,?,?)', (source + ':' + source_id, source,
                              json.dumps(payload, ensure_ascii=False), 'pending', self.clock(), 0)).rowcount

    def recover(self):
        # Only call while holding the process-wide worker lock. An interrupted
        # HTTP request has uncertain provider usage: retain its charged attempt.
        with self.db() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN attempts>=? THEN 'hold' ELSE 'pending' END,reason='interrupted_request' WHERE state='running'",(self.limits['max_attempts'],))
            db.execute("UPDATE attempts SET outcome='unknown_after_restart' WHERE outcome='running'")

    def reserve(self):
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                return None
            meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
            if meta.get('auth_hold') or now < meta.get('paused_until', 0):
                return None
            last = db.execute('SELECT max(started) FROM attempts').fetchone()[0]
            if last is not None and now - last < self.limits['interval']:
                return None
            used = dict(db.execute('SELECT bucket,count(*) FROM attempts WHERE day=? GROUP BY bucket', (day,)).fetchall())
            baseline=meta.get('budget_baseline',{})
            if baseline.get('day')==day:
                for bucket in ('news','dart','retry'):
                    used[bucket]=used.get(bucket,0)+baseline.get(bucket,0)
            if sum(used.values()) >= self.limits['total']:
                return None
            last_source = meta.get('last_source', 'news')
            sources = ['dart', 'news'] if last_source == 'news' else ['news', 'dart']
            for source in sources:
                stage_used={r[0]:r[1] for r in db.execute("SELECT CASE WHEN json_extract(j.payload,'$.kind')='title_selection' THEN 'selection' ELSE 'summary' END,count(*) FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.source='news' AND a.day=? AND a.bucket='news' GROUP BY 1",(day,))}
                summary_pending=db.execute("SELECT count(*) FROM jobs WHERE source='news' AND state='pending' AND coalesce(json_extract(payload,'$.kind'),'')!='title_selection' AND attempts=0").fetchone()[0]
                for row in db.execute("SELECT * FROM jobs WHERE source=? AND state='pending' AND due<=? ORDER BY CASE WHEN json_extract(payload,'$.kind')='title_selection' THEN 1 ELSE 0 END,created,id", (source, now)):
                    bucket = 'retry' if row['attempts'] else source
                    if used.get(bucket, 0) >= self.limits[bucket]:
                        continue
                    if source=='news' and self.news_policy['enabled']:
                        selection=json.loads(row['payload']).get('kind')=='title_selection'
                        summary_left=self.news_policy['summary_daily_limit']-stage_used.get('summary',0)
                        if selection and (summary_left<=summary_pending or
                            (not row['attempts'] and stage_used.get('selection',0)>=self.news_policy['selection_daily_limit'])):
                            continue
                        if not selection and not row['attempts'] and summary_left<=0:continue
                    db.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (row['id'],))
                    attempt = db.execute('INSERT INTO attempts(job,day,bucket,started,outcome) VALUES (?,?,?,?,?)',
                                         (row['id'], day, bucket, now, 'running')).lastrowid
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('last_source', json.dumps(source)))
                    return dict(row, attempt_id=attempt, payload=json.loads(row['payload']))
        return None

    def finish(self, job, status, result=None, outputs=(), retry_after=0, daily=False):
        now = self.clock()
        attempts = job['attempts'] + 1
        reason = 'http_' + str(status)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if status == 200:
                state, due = 'complete', 0
                if job['payload'].get('kind')=='title_selection':
                    news_selection.complete_batch(db,job,result,now)
                for destination, payload in outputs:
                    db.execute('INSERT OR IGNORE INTO outputs(job,destination,payload) VALUES (?,?,?)',
                               (job['id'], destination, json.dumps(payload, ensure_ascii=False)))
            elif status in (400, 401, 402, 403, 404):
                state, due = 'hold', 0
                if status in (401, 402, 403):
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('auth_hold', json.dumps(reason)))
            else:
                state = 'hold' if attempts >= self.limits['max_attempts'] else 'pending'
                due = now + max(retry_after, min(3600, 60 * 2 ** (attempts - 1)))
                if status == 429:
                    if daily:
                        due = (int(now // 86400) + 1) * 86400
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('paused_until', json.dumps(due)))
                    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('pause_reason',json.dumps('daily_limit' if daily else 'rate_limit')))
            db.execute('UPDATE jobs SET state=?,due=?,result=?,reason=? WHERE id=?',
                       (state, due, json.dumps(result, ensure_ascii=False), reason, job['id']))
            db.execute('UPDATE attempts SET outcome=? WHERE id=?',
                       ('success' if status == 200 else reason, job['attempt_id']))
            selection=job['payload'].get('kind')=='title_selection'
            if selection:
                db.execute('UPDATE news_batches SET state=? WHERE id=?',(state,job['payload']['batch_id']))
            mark(db,now,job['source'],('title_completed' if selection else 'ai_completed') if status==200 else 'failed',job['id'],
                 'ai' if status==200 else None)
            if status==200 and isinstance(result,dict) and result.get('decision')=='NO_SEND':
                mark(db,now,job['source'],'excluded',job['id'])
            if state=='hold':
                mark(db,now,job['source'],'held',job['id'])

    def status(self):
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.db() as db:
            jobs = [dict(r) for r in db.execute('SELECT source,state,count(*) AS count,min(created) AS oldest FROM jobs GROUP BY source,state')]
            for row in jobs:
                row['oldest_wait_seconds'] = max(0, now - row.pop('oldest'))
            usage = dict(db.execute('SELECT bucket,count(*) FROM attempts WHERE day=? GROUP BY bucket', (day,)).fetchall())
            local_usage=dict(usage)
            row=db.execute("SELECT value FROM meta WHERE key='budget_baseline'").fetchone()
            baseline=json.loads(row[0]) if row else {}
            if baseline.get('day')==day:
                for bucket in ('news','dart','retry'):
                    usage[bucket]=usage.get(bucket,0)+baseline.get(bucket,0)
            return dict(news_stages=news_selection.stage_status(db,now),jobs=jobs, usage=usage, remaining={k: max(0, v - (sum(usage.values()) if k == 'total' else usage.get(k, 0))) for k,v in self.limits.items() if k in ('total','news','dart','retry')},
                local_usage=local_usage,budget_baseline=baseline if baseline.get('day')==day else None,
                failed_attempts=db.execute("SELECT count(*) FROM attempts WHERE day=? AND outcome NOT IN ('success','running')", (day,)).fetchone()[0],
                outputs=[dict(r) for r in db.execute('SELECT destination,state,count(*) AS count FROM outputs GROUP BY destination,state')],
                discoveries=[dict(r) for r in db.execute('SELECT state,count(*) AS count FROM discoveries GROUP BY state')],
                exclusions=[dict(r) for r in db.execute('SELECT reason,count(*) AS count FROM discoveries GROUP BY reason')],
                collection_gaps=[dict(r) for r in db.execute('SELECT company,at,boundary,reason FROM gaps ORDER BY id DESC LIMIT 100')],
                backlog_over_daily_capacity=db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0]>self.limits['news']+self.limits['dart'],
                scans=[dict(r) for r in db.execute('SELECT * FROM scans')],
                metadata={r['key']:json.loads(r['value']) for r in db.execute('SELECT * FROM meta') if r['key'] in ('cursor','cycle_started','cycle_seconds','paused_until','pause_reason','auth_hold','account_usage','scan_provider_pause','scan_provider_error','expansion_start','dart_rss_gap')})
