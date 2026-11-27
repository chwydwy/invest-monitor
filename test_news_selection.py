import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_pipeline import Queue
from ai_worker import tick
from news_cutover import retire
from news_scan import Scanner
from news_selection import (policy,add_candidate,create_batch,parse_selection,stage_status)
from test_pipeline_integration import IntegrationTest,Response


class SelectionTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/'a.sqlite3';self.now=1795824000.0
        self.q=Queue(self.path,lambda:self.now)
        self.q.news_policy=policy(dict(news_selection=dict(enabled=True)))

    def candidate(self,n,title=None):
        event=dict(company='LG화학',title=title or f'LG화학 공급계약 {n}',press='검증언론',
                   published_at='2026-11-28T13:00:00+09:00',url=f'https://example.invalid/{n}',source='news',source_id=str(n))
        p=dict(event=event,body='원문은 선별에 전송하지 않는다',request=dict(messages=[dict(content='원문은 선별에 전송하지 않는다')]))
        add_candidate(self.q,str(n),p)

    def complete(self,answer):
        def transport(j):return Response(dict(choices=[dict(message=dict(content=answer),finish_reason='stop')]))
        return tick(self.q,transport,lambda j,c:parse_selection(j,c))

    def test_size_and_exact_wait_boundary_title_only(self):
        for n in range(29):self.candidate(n)
        self.now+=1799;self.assertIsNone(create_batch(self.q,'free'))
        self.now+=1;bid=create_batch(self.q,'free');self.assertIsNotNone(bid)
        with self.q.db() as db:
            p=json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])
        self.assertNotIn('원문은 선별',json.dumps(p['request'],ensure_ascii=False))
        self.assertNotIn('body',json.dumps(p['request']))
        self.assertEqual(len(p['article_ids']),29)
        self.assertIsNone(create_batch(self.q,'free'))
        for n in range(30,60):self.candidate(n)
        self.assertIsNotNone(create_batch(self.q,'free'))

    def test_zero_and_numbers_strictly_validated(self):
        self.candidate(1);self.now+=1800;create_batch(self.q,'free')
        job=self.q.reserve()
        for bad in ('','{}','[0]','[2]','[1,1]','[true]','[1.0]','[1','[] explanation'):
            with self.assertRaises((ValueError,TypeError)):parse_selection(job,bad)
        self.q.finish(job,200,dict(kind='title_selection',selected=[]))
        with self.q.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT state FROM news_candidates').fetchone()[0],'not_selected')

    def test_restart_atomic_selected_and_summary_priority(self):
        for n in range(30):self.candidate(n)
        bid=create_batch(self.q,'free')
        self.assertEqual(self.complete('[1,3]'),'complete')
        old=self.q
        self.q=Queue(self.path,lambda:self.now);self.q.news_policy=old.news_policy
        self.assertIsNone(create_batch(self.q,'free'))
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM news_candidates WHERE state='not_selected'").fetchone()[0],28)
        for n in range(30,60):self.candidate(n)
        create_batch(self.q,'free');self.now+=60
        self.assertEqual(self.q.reserve()['payload']['kind'],'news_summary')

    def test_fake_same_event_rewrite_and_material_followup(self):
        titles=['LG화학 2조원 공급계약 체결','LG화학, 2조 원 공급 계약 체결',
                'LG화학 기존 계약 3조원으로 증액 확정','LG화학 직원 봉사활동']
        for n,title in enumerate(titles):self.candidate(n,title)
        self.now+=1800;create_batch(self.q,'free')
        self.assertEqual(self.complete('[1,3]'),'complete')
        with self.q.db() as db:
            states=dict(db.execute('SELECT id,state FROM news_candidates'))
        self.assertEqual(list(states.values()),['selected','not_selected','selected','not_selected'])
        # Fake model proves data-path behavior only, not real model judgment.

    def test_429_global_wait_and_dart_opportunity(self):
        self.candidate(1);self.now+=1800;create_batch(self.q,'free')
        answer=Response(dict(error=dict(message='rate limit')),status=429,headers={'Retry-After':'180'})
        self.assertEqual(tick(self.q,lambda j:answer,parse_selection),'http_429')
        self.q.enqueue('dart','x',{})
        self.now+=179;self.assertIsNone(self.q.reserve())
        self.now+=1;job=self.q.reserve();self.assertEqual(job['source'],'dart')
        self.q.finish(job,200,{})
        self.now+=60;retry=self.q.reserve();self.assertEqual(retry['attempts'],1)
        self.assertEqual(self.q.status()['usage']['retry'],1)

    def test_stage_cap_and_preserved_summary_budget(self):
        self.q.news_policy.update(selection_daily_limit=1,summary_daily_limit=1)
        for n in range(30):self.candidate(n)
        create_batch(self.q,'free');self.complete('[1]')
        for n in range(30,60):self.candidate(n)
        create_batch(self.q,'free');self.now+=60
        summary=self.q.reserve();self.assertEqual(summary['payload']['kind'],'news_summary')
        self.q.finish(summary,200,dict(decision='NO_SEND'))
        self.now+=60;self.assertIsNone(self.q.reserve())
        self.q.enqueue('dart','z',{});self.assertEqual(self.q.reserve()['source'],'dart')

    def test_truncated_response_is_not_zero(self):
        self.candidate(1);self.now+=1800;create_batch(self.q,'free')
        response=Response(dict(choices=[dict(message=dict(content='[]'),finish_reason='length')]))
        self.assertEqual(tick(self.q,lambda j:response,parse_selection),'format_retry')
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT state FROM news_candidates').fetchone()[0],'batched')
            self.assertIsNone(db.execute('SELECT completed FROM news_batches').fetchone()[0])

    def test_not_selected_body_expiry_and_id_dedup(self):
        self.candidate(1);self.q.put('body:1',dict(body='cache'))
        self.now+=1800;create_batch(self.q,'free');self.complete('[]')
        self.now+=7*86400;create_batch(self.q,'free')
        with self.q.db() as db:
            row=db.execute('SELECT * FROM news_candidates').fetchone()
            self.assertIsNone(row['prepared']);self.assertIsNotNone(row['title'])
        self.candidate(1)
        self.assertIsNone(self.q.get('body:1'))
        with self.q.db() as db:self.assertEqual(self.q.insert_discovery(db,'news','1','new_context',dict(id='1')),0)
        with self.q.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM news_candidates').fetchone()[0],1)

    def test_retire_incomplete_only_preserves_dart_complete_outputs_and_cursor(self):
        self.q.put('cursor',123);self.q.put('scan_progress',dict(code='001',page=2))
        for n in ('pending','hold','running'):
            self.q.enqueue('news',n,dict(body='scrub',event=dict(url='https://example.invalid/'+n)))
            with self.q.db() as db:db.execute('UPDATE jobs SET state=? WHERE id=?',(n,'news:'+n))
        self.q.enqueue('news','done',dict(body='keep'))
        with self.q.db() as db:
            db.execute("UPDATE jobs SET state='complete' WHERE id='news:done'")
            db.execute("INSERT INTO outputs(job,destination,payload) VALUES ('news:done','dashboard','{}')")
        self.q.enqueue('dart','preserve',dict(body='dart'))
        with self.q.db() as db:
            self.q.insert_discovery(db,'news','bodyhold','001',dict(id='bodyhold'),prefixed=False)
            db.execute("UPDATE discoveries SET state='hold' WHERE id='bodyhold'")
        result=retire(self.q,self.now)
        self.assertEqual(result['retired_ids'],4)
        for identifier in ('pending','hold','running','bodyhold'):
            self.assertEqual(self.q.enqueue('news',identifier,{}),0)
            with self.q.db() as db:self.assertEqual(self.q.insert_discovery(db,'news',identifier,'different',dict(id=identifier)),0)
        self.assertEqual(self.q.get('cursor'),123);self.assertEqual(self.q.get('scan_progress')['page'],2)
        with self.q.db() as db:
            self.assertEqual(json.loads(db.execute("SELECT payload FROM jobs WHERE id='news:done'").fetchone()[0])['body'],'keep')
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id='dart:preserve'").fetchone()[0],'pending')
        with self.assertRaises(ValueError):retire(self.q,self.now)

    def test_stage_budget_resets_utc_without_refunding_old_attempts(self):
        self.q.news_policy.update(selection_daily_limit=1,summary_daily_limit=1)
        for n in range(30):self.candidate(n)
        create_batch(self.q,'free');self.complete('[1]')
        self.now+=60;job=self.q.reserve();self.q.finish(job,200,dict(decision='NO_SEND'))
        for n in range(30,60):self.candidate(n)
        create_batch(self.q,'free');self.now+=60;self.assertIsNone(self.q.reserve())
        self.now=(int(self.now//86400)+1)*86400
        self.assertEqual(self.q.reserve()['payload']['kind'],'title_selection')

    def test_cutover_keeps_cursor_and_ignores_pre_cutover_publications(self):
        from datetime import datetime, timezone
        self.q.put('cursor',1);retire(self.q,self.now)
        stamp=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
        scanner=Scanner(self.q,[{'stock_code':'1'},{'stock_code':'2'}],stamp(self.now-86400))
        def fetch(code,page):
            self.assertEqual((code,page),('2',1))
            return ([dict(id='old',published_at=stamp(self.now-1)),dict(id='new',published_at=stamp(self.now+1))],True)
        self.assertEqual(scanner.step(fetch),'success')
        with self.q.db() as db:self.assertEqual([r[0] for r in db.execute('SELECT id FROM discoveries')],['new'])


class ConnectedSelectionTest(IntegrationTest):
    def setUp(self):
        super().setUp()
        self.runtime.settings['news_selection']=dict(enabled=True,batch_size=1)
        self.q.news_policy=policy(self.runtime.settings)

    # Do not inherit the legacy direct-news integration expectations here.
    def test_connected_selection_to_existing_summary_and_destinations(self):
        self.runtime.discover('news','999_1','051910',self.news())
        self.assertEqual(self.runtime.preprocess_one(),'complete')
        self.http.answer='[1]'
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse,self.runtime.ai_preflight),'complete')
        self.now+=60;self.http.answer='SEND\n공급계약을 체결했다고 밝혔다.\n계약 규모는 2조원이다.'
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse,self.runtime.ai_preflight),'complete')
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],2)
        requests=[x[2]['json'] for x in self.http.calls if 'openrouter' in x[1]]
        self.assertNotIn('2조원',requests[0]['messages'][-1]['content'])
        self.assertEqual(requests[1]['messages'],self.runtime.prepare_news(self.news(),'051910')['request']['messages'])


# Reuse the fixture without rerunning legacy expectations under a different mode.
for name in list(IntegrationTest.__dict__):
    if name.startswith('test_'):
        setattr(ConnectedSelectionTest,name,None)
del IntegrationTest

if __name__=='__main__':unittest.main()
