"""Collectors, preprocessing, shared AI and independent delivery entry points.

No API calls on import. Runtime configuration is required explicitly.
"""
import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ai_pipeline import Queue
from ai_worker import tick, deliver_one, retry_seconds, DeliveryHold
from dashboard_sender import exclusive_lock
from dashboard_outbox import enqueue as enqueue_dashboard
from news_scan import Scanner, universe
from news_monitor import ai_rules
from news_monitor.naver_news import parse_stock_news
from news_monitor.news_quality import (KST, article_publication_time,
    choose_registration_time, load_company_names,
    resolve_article_company)
from dart_monitor import disclosure_rules
from dart_monitor.disclosure_targets import matches_group, company_identity

ROOT = Path(__file__).resolve().parent
MODEL = 'nvidia/nemotron-3-ultra-550b-a55b:free'


def read_list(path):
    return [x.strip() for x in Path(path).read_text(encoding='utf-8').splitlines() if x.strip()]


def active_news(now):
    local = datetime.fromtimestamp(now, KST)
    return local.hour == 13 if local.weekday() >= 5 else 6 <= local.hour < 22


class Runtime:
    def __init__(self, queue, settings, root=ROOT, session=None):
        self.q, self.settings, self.root = queue, settings, Path(root)
        self.http = session or requests.Session()
        self.companies = universe(self.root)
        self.by_code = {x['stock_code']: x for x in self.companies}
        self.names = load_company_names()
        self.monitored = {company_identity(x['name']): x['name'] for x in self.companies}
        self.chem = {company_identity(x) for x in read_list(self.root/'news_monitor/Companies_ChemHold_news.txt')}
        start=datetime.fromisoformat(settings['news_start_at'])
        if start.tzinfo is None:
            raise ValueError('news_start_at requires an offset')
        previous=self.q.get('expansion_start')
        if previous and previous!=start.isoformat():
            raise ValueError('expansion start cannot change for an existing queue')
        if previous is None:
            self.q.put('expansion_start',start.isoformat())

    def credentials(self, source):
        path = self.root / ('dart_monitor/config.json' if source == 'dart' else 'news_monitor/config_news.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def discover(self, source, identifier, company, payload):
        with self.q.db() as db:
            db.execute('INSERT OR IGNORE INTO discoveries(id,company,payload) VALUES (?,?,?)',
                       (source+':'+identifier, company, json.dumps(payload,ensure_ascii=False)))

    def dart_step(self, rss=None):
        now = self.q.clock()
        if now < self.q.get('dart_due', 0):
            return 'waiting'
        local = datetime.fromtimestamp(now,KST)
        interval = 30 if local.weekday()<5 and 8<=local.hour<19 else 3600
        self.q.put('dart_due', now+interval)
        if rss is None:
            response = self.http.get('https://dart.fss.or.kr/api/todayRSS.xml',timeout=15)
            response.raise_for_status()
            rss = response.content
        entries = ET.fromstring(rss).findall('.//item')
        if not entries:
            return 'empty'
        last = self.q.get('dart_last_link')
        if last is None:
            # Preserve startup skip policy; establish a durable boundary.
            self.q.put('dart_last_link',entries[0].findtext('link'))
            return 'initialized'
        groups = {group:read_list(self.root/'dart_monitor'/file) for group,file in
                  [('BAMK_BL','companies_BAMK_BL.txt'),('JM','companies_JM.txt'),('ChemHold','companies_ChemHold.txt')]}
        excludes = read_list(self.root/'dart_monitor/exclude_keywords.txt')
        new = []
        for entry in entries:
            if entry.findtext('link') == last:
                break
            new.append(entry)
        if new and len(new) == len(entries):
            self.q.put('dart_rss_gap',dict(at=now,reason='previous_link_not_in_feed'))
        for entry in reversed(new):
            title = re.sub(r'^\([^)]+\)\s*','',entry.findtext('title') or '').strip()
            if not title or any(x in title for x in excludes):
                continue
            if '증권' in title and any(x in title for x in ['증권발행','투자설명','일괄신고']):
                continue
            company = title.split(' - ')[0].strip() if ' - ' in title else title.split(' ')[0].strip()
            submitter = entry.findtext('{http://purl.org/dc/elements/1.1/}creator') or ''
            market = '유가증권시장' in submitter or '코스닥시장' in submitter
            selected = [g for g,names in groups.items() if matches_group(names,company,submitter.strip(),market)]
            if not ('JM' in selected or 'BAMK_BL' in selected):
                continue
            link = entry.findtext('link') or ''
            match = re.search(r'rcpNo=(\d+)',link)
            if match:
                self.discover('dart',match[1],company,dict(source='dart',source_id=match[1],receipt_no=match[1],
                    company=company,title=title,url=link,published_at=None,
                    published_at_original=entry.findtext('pubDate'),monitor_groups=selected,sector=None,
                    disclosure_type=title.split(' - ',1)[-1] if ' - ' in title else None))
        self.q.put('dart_last_link',entries[0].findtext('link'))
        return 'collected'

    def fetch_news_page(self, code, page):
        response = self.http.get('https://stock.naver.com/api/domestic/detail/news',
            params=dict(itemCode=code,page=page,pageSize=self.settings.get('page_size',15)),
            headers={'User-Agent':'invest-monitor-news/1.0','Accept':'application/json',
                     'Referer':f'https://stock.naver.com/domestic/stock/{code}/news'},timeout=15)
        if response.status_code in (403,429):
            delay=max(300,retry_seconds(response.headers,self.q.clock()))
            self.q.put('scan_provider_pause',self.q.clock()+delay)
            self.q.put('scan_provider_error',dict(status=response.status_code,at=self.q.clock()))
        response.raise_for_status()
        data = response.json()
        # Parse without a moving 24h cutoff: durable checkpoint/start bounds it.
        rows = parse_stock_news(data,datetime(1970,1,1))
        raw_count = sum(len(c.get('items',[])) for c in data['clusters'])
        raw_ids={(str(x.get('officeId')),str(x.get('articleId'))) for c in data['clusters'] for x in c.get('items',[])}
        parsed_ids={(x['office_id'],x['article_id']) for x in rows}
        if raw_ids!=parsed_ids:
            raise ValueError('news_page_dates_unparseable')
        for row in rows:
            row.pop('date',None)
            row['source'] = 'news'
        return rows, raw_count == 0

    def news_step(self):
        if self.q.clock() < self.q.get('scan_provider_pause',0):
            return 'provider_pause'
        scanner = Scanner(self.q,self.companies,self.settings['news_start_at'],
                          self.settings.get('news_interval',45),self.settings.get('max_pages',5))
        return scanner.step(self.fetch_news_page,active_news(self.q.clock()))

    def _legacy_ids(self):
        path=self.root/'news_monitor/sent_articles.json'
        return set(json.loads(path.read_text(encoding='utf-8'))) if path.exists() else set()

    def _sent_cache(self):
        path=self.root/'news_monitor/daily_sent_cache.json'
        cache=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        with self.q.db() as db:
            for row in db.execute("SELECT j.payload,o.due FROM outputs o JOIN jobs j ON j.id=o.job WHERE o.state='delivered' AND o.destination='telegram:news:chem'"):
                payload=json.loads(row['payload'])
                cache.append(dict(company=payload['event']['company'],title=payload['event']['title'],
                                  body=payload['body'],sent_at=datetime.fromtimestamp(row['due'],KST).isoformat()))
        recent=[]
        for row in cache:
            try:
                stamp=datetime.fromisoformat(row['sent_at'])
                if stamp.tzinfo is None:
                    stamp=stamp.replace(tzinfo=KST)
                if 0 <= self.q.clock()-stamp.timestamp() <= 86400:
                    recent.append(row)
            except (KeyError,ValueError,TypeError):
                pass
        return recent

    def ai_preflight(self):
        # Recheck once the serial worker reaches the job: another article may
        # have completed while this one waited. No API attempt is charged.
        cache=self._sent_cache()
        with self.q.db() as db:
            for row in db.execute("SELECT payload,result,created FROM jobs WHERE source='news' AND state='complete' AND created>=?",(self.q.clock()-86400,)).fetchall():
                result=json.loads(row['result'] or '{}')
                if result.get('decision')=='SEND':
                    p=json.loads(row['payload'])
                    cache.append(dict(company=p['event']['company'],title=p['event']['title'],body=p['body']))
            for row in db.execute("SELECT id,payload FROM jobs WHERE source='news' AND state='pending' AND attempts=0").fetchall():
                p=json.loads(row['payload'])
                if ai_rules.find_similar_sent_article(p['event']['title'],p['body'],cache,p['event']['company']):
                    db.execute("UPDATE jobs SET state='excluded',reason='similar_completed_issue' WHERE id=?",(row['id'],))

    def preprocess_one(self):
        # A separate process, never invoked by the list collectors.
        with exclusive_lock(self.q.path+'.preprocess'):
            with self.q.db() as db:
                db.execute("UPDATE discoveries SET state='pending',reason='interrupted' WHERE state='running'")
                rows=db.execute("SELECT rowid,* FROM discoveries WHERE state='pending' ORDER BY rowid").fetchall()
            last_source=self.q.get('preprocess_last_source','news')
            def ordering(row):
                try:
                    source=json.loads(row['payload']).get('source','news')
                except (ValueError,TypeError,AttributeError):
                    source='invalid'
                return source==last_source,row['rowid']
            rows=sorted(rows,key=ordering)
            for row in rows:
                try:
                    payload=json.loads(row['payload'])
                    source=payload.get('source','news')
                    identifier=payload['source_id'] if source=='dart' else payload['id']
                except (ValueError,KeyError,TypeError,AttributeError):
                    self._discovery_state(row['rowid'],'hold','malformed_discovery')
                    continue
                if payload.get('_retry_at',0)>self.q.clock():
                    continue
                state,reason='complete','prepared'
                with self.q.db() as db:
                    existing=db.execute('SELECT 1 FROM jobs WHERE id=?',(source+':'+identifier,)).fetchone()
                if existing:
                    self._discovery_state(row['rowid'],'complete','duplicate_job')
                    return 'duplicate'
                with self.q.db() as db:
                    db.execute("UPDATE discoveries SET state='running' WHERE rowid=?",(row['rowid'],))
                try:
                    prepared=self.prepare_dart(payload) if source=='dart' else self.prepare_news(payload,row['company'])
                    if prepared is not None:
                        self.q.enqueue(source,identifier,prepared)
                    else:
                        reason='filtered'
                except Deferred as exc:
                    state,reason='pending',exc.reason
                except Exception as exc:
                    state,reason='pending',type(exc).__name__
                if state=='pending':
                    payload['_failures']=payload.get('_failures',0)+1
                    payload['_retry_at']=self.q.clock()+min(3600,60*2**min(payload['_failures'],6))
                    if payload['_failures']>=5:
                        state='hold'
                    with self.q.db() as db:
                        db.execute('UPDATE discoveries SET payload=? WHERE rowid=?',(json.dumps(payload,ensure_ascii=False),row['rowid']))
                self._discovery_state(row['rowid'],state,reason)
                self.q.put('preprocess_last_source',source)
                return state
            return 'waiting'

    def _discovery_state(self,rowid,state,reason):
        with self.q.db() as db:
            db.execute('UPDATE discoveries SET state=?,reason=? WHERE rowid=?',(state,reason,rowid))

    def prepare_news(self,item,code):
        if item['id'] in self._legacy_ids():
            return None
        for field,filename in [('title','exclude_keywords.txt'),('press','exclude_press.txt')]:
            if any(word in item[field] for word in read_list(self.root/'news_monitor'/filename)):
                return None
        lookup=self.by_code[code]['name']
        url=f"https://n.news.naver.com/mnews/article/{item['office_id']}/{item['article_id']}"
        # Cache extraction once for all discovery contexts of the article.
        cached=self.q.get('body:'+item['id'])
        if cached is None:
            response=self.http.get(url,headers={'User-Agent':'invest-monitor-news/1.0'},timeout=15)
            response.raise_for_status()
            soup=BeautifulSoup(response.text,'html.parser')
            tag=soup.select_one('#newsct_article') or soup.select_one('#articleBodyContents') or soup.select_one('.article_body')
            if tag is None:
                raise Deferred('body_missing')
            pub,raw,modified=article_publication_time(soup)
            cached=dict(body=tag.get_text('\n',strip=True),published_at=pub,
                        article_published_at_original=raw,article_modified_at_original=modified)
            self.q.put('body:'+item['id'],cached)
        sectors={company_identity(c['name']):c.get('sector') for c in self.companies if c.get('stock_code')}
        company,reason=resolve_article_company(lookup,item['title'],cached['body'],self.monitored,self.names,sectors)
        if not company:
            return None
        if ai_rules.find_similar_sent_article(item['title'],cached['body'],self._sent_cache(),company):
            return None
        pub,raw=choose_registration_time(cached,item)
        if pub and datetime.fromisoformat(pub)<datetime.fromisoformat(self.settings['news_start_at']):
            return None
        groups=list(self.by_code[code]['disclosure_groups'])
        event=dict(source='news',source_id=item['id'],article_id=item['id'],company=company,
            stock_code=code,title=item['title'],press=item['press'],url=url,published_at=pub,
            published_at_original=raw,npay_datetime_original=item.get('npay_datetime_raw'),
            article_published_at_original=cached.get('article_published_at_original'),
            article_modified_at_original=cached.get('article_modified_at_original'),monitor_groups=groups,sector=None)
        if not re.fullmatch(r'\d{6}',code):
            # Existing receiver accepts digits only. Preserve the actual code
            # separately and let its confirmed-name catalog matching work.
            event['stock_code']=None
            event['stock_code_original']=code
        return dict(event=event,body=cached['body'],request=ai_rules.build_request(company,item['title'],cached['body']),
                    chem=company_identity(company) in self.chem)

    def prepare_dart(self,event):
        raw=disclosure_rules.get_raw_text(self.credentials('dart')['DART_API_KEY'],event['source_id'])
        if not raw:
            # Existing failure notification, once; body extraction itself does not
            # consume AI budget or stop the list collector.
            message=f"{event['title']}\n\n내용: 원문을 가져오지 못해 AI 분석을 수행하지 못했습니다.\n\n🔗 {event['url']}"
            self.add_output('dart:'+event['source_id'],'telegram:dart:bl',dict(message=message))
            return None
        _,prompt=disclosure_rules.generate_custom_prompt(raw,event['title'])
        if not prompt:
            raise Deferred('dart_body_short')
        priorities=read_list(self.root/'dart_monitor/priority_keywords.txt')
        priority=(any(x in event['title'] or x in raw for x in priorities) and
                  not any(x in event['title'] for x in ['현물배당','매출액또는손익구조','주요경영사항']))
        return dict(event=event,priority=priority,request=dict(model=MODEL,messages=[dict(role='user',content=prompt)]))

    def add_output(self,job,destination,payload):
        with self.q.db() as db:
            db.execute('INSERT OR IGNORE INTO outputs(job,destination,payload) VALUES (?,?,?)',
                       (job,destination,json.dumps(payload,ensure_ascii=False)))

    def transport(self,job):
        key=self.credentials(job['source'])['OPENROUTER_API_KEY']
        return self.http.post('https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},
            json=job['payload']['request'],timeout=60)

    def parse(self,job,content):
        payload=job['payload'];event=dict(payload['event']);outputs=[]
        if job['source']=='news':
            decision,summary=ai_rules.parse_response(content)
            if decision=='SEND':
                event['summary']=summary
                outputs.append(('dashboard',event))
                if payload['chem']:
                    message=f"<b>[{event['company']}]</b>\n{event['title']}\n\n{summary.replace('**','')}\n\n🔗 <a href='{event['url']}'>뉴스 원문 보기</a>"
                    outputs.append(('telegram:news:chem',dict(message=message,parse_mode='HTML')))
            return dict(content=content,decision=decision,summary=summary),outputs
        if 'AI 분석 지연' in content or (content.strip()!='NO_SEND' and len(re.findall('[a-zA-Z]',content))>len(content)*.5):
            raise ValueError('invalid dart response')
        text=content.replace('**','').replace('*','')
        match=re.search(r'SCORE:\s*(\d+)',text,re.I)
        score=int(match[1]) if match else 0
        summary=re.sub(r'SCORE:\s*\d+','',text,flags=re.I).strip()
        no_send='NO_SEND' in summary
        message=f"{event['title']}\n\n🤖 AI 분석({score}):\n{summary}\n\n🔗 {event['url']}"
        if not no_send:
            event.update(analysis=summary,score=score)
            outputs.extend([('dashboard',event),('telegram:dart:bl',dict(message=message))])
            if 'ChemHold' in event['monitor_groups']:
                outputs.append(('telegram:dart:chem',dict(message=message)))
        if payload['priority']:
            if no_send:
                message=f"❗ [중요 키워드 포착]\n{event['title']}\n\n알림: AI가 중요도가 낮다고 판단했으나 설정된 키워드가 발견되어 전송합니다.\n\n🔗 {event['url']}"
            outputs.append(('telegram:dart:caution',dict(message=message)))
        # Same configured Telegram room gets one copy, like disclosure_rooms.
        config=self.credentials('dart');seen=set();unique=[]
        for destination,value in outputs:
            if destination.startswith('telegram') and destination!='telegram:dart:caution':
                room=config[{'bl':'CHAT_ID_BL','chem':'CHAT_ID_CHEM','caution':'CHAT_ID_BL_Caution'}[destination.split(':')[-1]]]
                if room in seen:
                    continue
                seen.add(room)
            unique.append((destination,value))
        return dict(content=content,score=score,decision='NO_SEND' if no_send else 'SEND'),unique

    def send(self,destination,payload):
        if destination=='dashboard':
            enqueue_dashboard(self.settings['dashboard_db'],payload)
            return True  # handoff to existing sender, not an external delivery ACK
        _,source,room=destination.split(':')
        config=self.credentials(source)
        key={'bl':'CHAT_ID_BL','chem':'CHAT_ID_CHEM','caution':'CHAT_ID_BL_Caution'}[room]
        data=dict(chat_id=config[key],text=payload['message'])
        if payload.get('parse_mode'):
            data['parse_mode']=payload['parse_mode']
        response=self.http.post(f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage",json=data,timeout=15)
        if response.status_code in (400,401,403,404):
            raise DeliveryHold()
        return response.status_code==200 and response.json().get('ok') is True


class Deferred(Exception):
    def __init__(self,reason):
        self.reason=reason


def main(default_role=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('role',nargs='?',default=default_role,choices=['dart','news','preprocess','ai','deliver','status'])
    parser.add_argument('--settings',default=os.environ.get('MONITOR_PIPELINE_SETTINGS'))
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    if args.role is None:
        parser.error('role is required')
    if not args.settings:
        parser.error('MONITOR_PIPELINE_SETTINGS or --settings is required; legacy direct AI path is disabled')
    settings=json.loads(Path(args.settings).read_text(encoding='utf-8'))
    if not settings.get('enabled') and args.role!='status':
        parser.error('pipeline is disabled in settings')
    if Path(settings['ai_db']).resolve()==Path(settings['dashboard_db']).resolve():
        parser.error('AI and dashboard databases must be different')
    q=Queue(settings['ai_db'],limits=settings.get('limits'))
    runtime=Runtime(q,settings)
    with exclusive_lock(q.path+'.role-'+str(args.role)):
        while True:
            try:
                if args.role=='dart': result=runtime.dart_step()
                elif args.role=='news': result=runtime.news_step()
                elif args.role=='preprocess': result=runtime.preprocess_one()
                elif args.role=='ai': result=tick(q,runtime.transport,runtime.parse,runtime.ai_preflight)
                elif args.role=='deliver': result=deliver_one(q,runtime.send)
                else:
                    print(json.dumps(q.status(),ensure_ascii=False));return
                print(json.dumps(dict(role=args.role,state=result)),flush=True)
            except Exception as exc:
                print(json.dumps(dict(role=args.role,error=type(exc).__name__)),flush=True)
            if args.once:return
            time.sleep(2)


if __name__=='__main__':
    main()
