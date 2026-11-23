"""Slow durable universe scan, injected fetcher; no article/AI processing here."""
import json
from datetime import datetime
from pathlib import Path


def universe(root=None):
    root = Path(root or Path(__file__).parent)
    aliases = json.loads((root / 'DASHBOARD_COMPANY_ALIASES.json').read_text(encoding='utf-8'))
    canonical = {name: c['name'] for c in aliases['companies'] for name in [c['name'], *c['aliases']]}
    catalog = json.loads((root / 'DASHBOARD_COMPANIES.json').read_text(encoding='utf-8'))['companies']
    by_name = {c['name']: c for c in catalog}
    rows = {}
    for filename in ('companies_BAMK_BL.txt', 'companies_JM.txt'):
        for name in (root / 'dart_monitor' / filename).read_text(encoding='utf-8').splitlines():
            if not name.strip():
                continue
            name = canonical.get(name.strip(), name.strip())
            row = by_name[name]
            code = row.get('stock_code')
            if not code:
                raise ValueError('universe code missing')
            rows.setdefault(code, row)
    return list(rows.values())


class Scanner:
    def __init__(self, queue, companies, start_at, interval=45, pages=5):
        self.q, self.companies = queue, companies
        self.start = datetime.fromisoformat(start_at)
        if self.start.tzinfo is None:
            raise ValueError('expansion start requires an explicit timezone')
        if not companies or pages<1:
            raise ValueError('companies and a positive page limit are required')
        self.interval, self.pages = max(45, interval), pages

    def step(self, fetch, active=True):
        if not active:
            return 'inactive'
        now = self.q.clock()
        if now < self.start.timestamp():
            return 'before_start'
        if now < self.q.get('scan_due', 0):
            return 'waiting'
        cursor = self.q.get('cursor', 0) % len(self.companies)
        company = self.companies[cursor]
        code = company['stock_code']
        with self.q.db() as db:
            prior = db.execute('SELECT * FROM scans WHERE company=?', (code,)).fetchone()
        boundary = datetime.fromisoformat(prior['checkpoint']) if prior and prior['checkpoint'] else self.start
        newest = boundary
        complete, gap = False, 'page_cap'
        last_start = now
        progress = self.q.get('scan_progress', {})
        page = progress.get('page', 1) if progress.get('code') == code else 1
        if progress.get('code') == code:
            newest = datetime.fromisoformat(progress['newest'])
        try:
            for _ in range(1):
                last_start = self.q.clock()
                batch, exhausted = fetch(code, page)
                for item in batch:
                    date = datetime.fromisoformat(item['published_at'])
                    if date < boundary:
                        complete = True
                        continue
                    newest = max(newest, date)
                    if date < self.start:
                        continue
                    with self.q.db() as db:
                        db.execute('INSERT OR IGNORE INTO discoveries(id,company,payload) VALUES (?,?,?)',
                                   (item['id'], code, json.dumps(item, ensure_ascii=False)))
                complete = complete or exhausted
                if complete:
                    gap = None
                    break
                if page < self.pages:
                    self.q.put('scan_progress', dict(code=code,page=page+1,newest=newest.isoformat()))
                    self.q.put('scan_due', last_start + self.interval)
                    return 'more_pages'
        except Exception as exc:
            gap = type(exc).__name__
        with self.q.db() as db:
            db.execute('INSERT OR REPLACE INTO scans VALUES (?,?,?,?)',
                       (code, self.q.clock() if gap is None else (prior['last_success'] if prior else None),
                        newest.isoformat() if gap is None else boundary.isoformat(), gap))
            if gap:
                db.execute('INSERT INTO gaps(company,at,boundary,reason) VALUES (?,?,?,?)',
                           (code,self.q.clock(),boundary.isoformat(),gap))
        self.q.put('scan_due', last_start + (max(300, self.interval * 2) if gap else self.interval))
        self.q.put('cursor', (cursor + 1) % len(self.companies))
        self.q.put('scan_progress', {})
        started = self.q.get('cycle_started', now)
        if cursor + 1 == len(self.companies):
            self.q.put('cycle_seconds', self.q.clock() - started)
            started = self.q.clock()
        self.q.put('cycle_started', started)
        return gap or 'success'
