import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import requests
from ai_pipeline import Queue
from ai_worker import tick, deliver_one
from pipeline_runtime import Runtime
from news_monitor import ai_rules
from news_scan import universe

ROOT=Path(__file__).parent


class Response:
    def __init__(self,data=None,text='',status=200,headers=None):
        self.data=data;self.text=text;self.content=text.encode();self.status_code=status;self.headers=headers or {}
    def json(self):return self.data
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError(response=self)


class FakeHTTP:
    def __init__(self):
        self.calls=[];self.answer='SEND\n공급계약을 체결했다고 밝혔다.\n계약 규모는 2조원이다.'
        self.status=200
    def get(self,url,**kwargs):
        self.calls.append(('get',url,kwargs))
        return Response(text='<span data-date-time="2026-11-23 18:04:17"></span><div id="newsct_article">LG화학은 2조원 공급계약을 체결했다고 밝혔다.</div>')
    def post(self,url,**kwargs):
        self.calls.append(('post',url,kwargs))
        if 'openrouter' in url:
            return Response({'choices':[{'message':{'content':self.answer}}]},status=self.status,headers={'Retry-After':'120'})
        return Response({'ok':True})


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for file in ['DASHBOARD_COMPANIES.json','DASHBOARD_COMPANY_ALIASES.json',
                     'dart_monitor/companies_BAMK_BL.txt','dart_monitor/companies_JM.txt',
                     'dart_monitor/companies_ChemHold.txt','dart_monitor/exclude_keywords.txt',
                     'dart_monitor/priority_keywords.txt','news_monitor/Companies_ChemHold_news.txt',
                     'news_monitor/exclude_keywords.txt','news_monitor/exclude_press.txt']:
            target=self.root/file;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/file,target)
        for file in ['dart_monitor/config.json','news_monitor/config_news.json']:
            (self.root/file).write_text(json.dumps(dict(OPENROUTER_API_KEY='unused',DART_API_KEY='unused',
                TELEGRAM_TOKEN='unused',CHAT_ID_BL='bl',CHAT_ID_CHEM='chem',CHAT_ID_BL_Caution='caution')),encoding='utf8')
        self.now=datetime(2026,11,23,18,5,tzinfo=timezone.utc).timestamp()
        self.q=Queue(self.root/'ai.sqlite3',lambda:self.now)
        self.http=FakeHTTP()
        self.runtime=Runtime(self.q,dict(ai_db=str(self.root/'ai.sqlite3'),dashboard_db=str(self.root/'out.sqlite3'),
            news_start_at='2026-11-23T00:00:00+09:00'),self.root,self.http)

    def news(self,code='051910',identifier='999_1',title='LG화학 신규 공급계약'):
        return dict(id=identifier,source='news',office_id='999',article_id='1',title=title,press='가상언론',
                    published_at='2026-11-23T18:04:00+09:00',npay_datetime_raw='202611231804')

    def test_news_importance_after_title_selection_and_invalid_retry(self):
        self.runtime.settings['classify_importance']=True
        prepared=self.runtime.prepare_news(self.news(),'051910')
        self.assertEqual(prepared['importance_version'],1)
        self.assertIn('IMPORTANCE: exclude',prepared['request']['messages'][0]['content'])
        for label in ('high','normal','exclude'):
            content=f'IMPORTANCE: {label}'
            if label!='exclude':content+='\n공급계약을 체결했다.\n계약금액은 2조원이다.'
            result,outputs=self.runtime.parse(dict(source='news',payload=prepared),content)
            self.assertEqual(result['importance'],label)
            self.assertEqual([d for d,_ in outputs],[] if label=='exclude' else ['dashboard','telegram:news:chem'])
            if outputs:
                self.assertEqual(outputs[0][1]['importance_version'],1)
                self.assertEqual(outputs[0][1]['importance'],label)
        for invalid in ('NO_SEND','IMPORTANCE: exclude\n요약','IMPORTANCE: unknown\n한국어\n요약','IMPORTANCE: normal\n요약'):
            with self.assertRaises(ValueError):self.runtime.parse(dict(source='news',payload=prepared),invalid)
        self.q.enqueue('news','999_1',prepared)
        self.http.answer='SEND\n분류가 없다.\n따라서 실패한다.'
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse),'format_retry')
        with self.q.db() as db:
            self.assertEqual(tuple(db.execute('SELECT state,reason FROM jobs').fetchone()),('pending','http_598'))
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],0)

    def test_dart_importance_keeps_priority_no_send_and_rejects_missing_label(self):
        self.runtime.settings['classify_importance']=True
        event=self.dart(title='LG화학 - [첨부정정]공급계약')
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value='거래 조건이 달라졌다. '*30):
            payload=self.runtime.prepare_dart(event)
        prompt=payload['request']['messages'][0]['content']
        self.assertNotIn('SCORE:',prompt)
        self.assertIn('정정 전후의 실제 차이',prompt)
        payload['priority']=True
        job=dict(source='dart',payload=payload)
        for label in ('high','normal'):
            result,outputs=self.runtime.parse(job,'계약 조건을 정정했다.\nIMPORTANCE: '+label)
            self.assertEqual(outputs[0][1]['importance'],label)
            self.assertNotIn('score',result)
            self.assertNotIn('score',outputs[0][1])
        result,outputs=self.runtime.parse(job,'NO_SEND')
        self.assertEqual(result['decision'],'NO_SEND')
        self.assertEqual([destination for destination,_ in outputs],['telegram:dart:caution'])
        for invalid in ('본문만 있다.','본문\nSCORE: 8','본문\nIMPORTANCE: exclude','본문\nIMPORTANCE: high\nIMPORTANCE: normal','NO_SEND\nIMPORTANCE: normal'):
            with self.assertRaises(ValueError):self.runtime.parse(job,invalid)

    def dart(self,identifier='20261123001',title='LG화학 - 공급계약'):
        return dict(source='dart',source_id=identifier,receipt_no=identifier,company='LG화학',title=title,
                    url='https://example.invalid/dart',published_at=None,published_at_original=None,
                    monitor_groups=['BAMK_BL','ChemHold'],sector=None,disclosure_type='공급계약')

    def test_news_end_to_end_and_destination_independence(self):
        self.runtime.discover('news','999_1','051910',self.news())
        self.assertEqual(self.runtime.preprocess_one(),'complete')
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse),'complete')
        with self.q.db() as db:
            result=json.loads(db.execute('SELECT result FROM jobs').fetchone()[0])
            self.assertEqual(result['decision'],'SEND')
            payload=json.loads(db.execute("SELECT payload FROM outputs WHERE destination='dashboard'").fetchone()[0])
            self.assertEqual(payload['published_at'],'2026-11-23T18:04:17+09:00')
        with patch('pipeline_runtime.enqueue_dashboard',side_effect=OSError):
            self.assertEqual(deliver_one(self.q,self.runtime.send),'pending')
        self.assertEqual(deliver_one(self.q,self.runtime.send),'delivered') # Telegram can succeed despite dashboard retry
        self.now+=60
        self.assertEqual(deliver_one(self.q,self.runtime.send),'delivered')
        with self.q.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outputs WHERE state='delivered'").fetchone()[0],2)
        self.assertEqual(sum('openrouter' in c[1] for c in self.http.calls),1)
        self.runtime.discover('news','999_1','051910',self.news())
        self.assertEqual(self.runtime.preprocess_one(),'waiting')

    def test_nonchem_news_dashboard_only_and_no_send(self):
        prepared=self.runtime.prepare_news(self.news(), '051910')
        prepared['chem']=False
        self.q.enqueue('news','1',prepared)
        tick(self.q,self.runtime.transport,self.runtime.parse)
        with self.q.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT destination FROM outputs')],['dashboard'])
        self.now+=60;prepared=json.loads(json.dumps(prepared));prepared['event']['url']+='?second'
        self.q.enqueue('news','2',prepared);self.http.answer='NO_SEND'
        tick(self.q,self.runtime.transport,self.runtime.parse)
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],1)

    def test_dart_rss_body_prompt_score_and_caution(self):
        xml='''<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>LG화학 - 합병</title><link>https://example.invalid/?rcpNo=20261123001</link><dc:creator>LG화학</dc:creator></item></channel></rss>'''
        self.q.put('dart_last_link','prior')
        self.assertEqual(self.runtime.dart_step(xml),'collected')
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value='LG화학 합병 검토 중 결정된 사항 없음. '*30):
            self.runtime.preprocess_one()
        self.http.answer='검토 중이며 결정된 사항은 없다고 밝혔다.\nSCORE: 7'
        tick(self.q,self.runtime.transport,self.runtime.parse)
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],4)
            request=json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])['request']
            self.assertIn('공시 원문에 명시된 사실만',request['messages'][0]['content'])

    def test_dart_no_send_priority_and_extraction_failure_exceptions(self):
        event=self.dart(title='LG화학 - 합병')
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value='합병 검토 중 '*100):
            prepared=self.runtime.prepare_dart(event)
        self.q.enqueue('dart',event['source_id'],prepared);self.http.answer='NO_SEND'
        tick(self.q,self.runtime.transport,self.runtime.parse)
        with self.q.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT destination FROM outputs')],['telegram:dart:caution'])
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value=None):
            self.runtime.prepare_dart(self.dart('2'))
        with self.q.db() as db:
            self.assertEqual(db.execute("SELECT destination FROM outputs WHERE job='dart:2'").fetchone()[0],'telegram:dart:bl')

    def test_error_does_not_block_preprocessing_other_item(self):
        self.runtime.discover('news','bad','051910',self.news(identifier='bad'))
        self.runtime.discover('news','good','051910',self.news(identifier='good'))
        with patch.object(self.http,'get',side_effect=requests.Timeout):
            self.assertEqual(self.runtime.preprocess_one(),'pending')
        self.assertEqual(self.runtime.preprocess_one(),'complete')
        with self.q.db() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM jobs WHERE id='news:good'").fetchone())

    def test_legacy_history_read_only_and_multiple_discovery_contexts(self):
        path=self.root/'news_monitor/sent_articles.json';path.write_text('["old"]')
        self.runtime.discover('news','old','051910',self.news(identifier='old'))
        self.runtime.preprocess_one()
        self.assertEqual(path.read_text(),'["old"]')
        self.runtime.discover('news','new','034730',self.news(identifier='new'))
        self.runtime.discover('news','new','051910',self.news(identifier='new'))
        self.runtime.preprocess_one();self.runtime.preprocess_one()
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_common_worker_429_pause_and_later_dart(self):
        self.q.enqueue('news','1',self.runtime.prepare_news(self.news(),'051910'))
        self.http.status=429
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse),'http_429')
        self.q.enqueue('dart','1',dict(event=self.dart(),priority=False,request={}))
        self.now+=60
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse),'waiting')
        self.now+=60;self.http.status=200;self.http.answer='한국어 공시 요약\nSCORE: 1'
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse),'complete')

    def test_dart_actual_extractor_uses_fake_opendart_response(self):
        raw='<html><body>'+('LG화학은 공급계약을 체결했다. '*50)+'</body></html>'
        with patch('dart_monitor.disclosure_rules.requests.get',return_value=Response(text=raw)) as get:
            prepared=self.runtime.prepare_dart(self.dart())
        self.assertIn('2-3줄',prepared['request']['messages'][0]['content'])
        self.assertIn('opendart.fss.or.kr/api/document.xml',get.call_args.args[0])

    def test_actual_news_page_adapter_and_restart_pagination(self):
        from news_scan import Scanner
        page_data={'clusters':[{'items':[dict(officeId='999',articleId='100',officeName='가상언론',
                          title='LG화학 공급계약',datetime='202611231804')]}]}
        scanner=Scanner(self.q,self.runtime.companies,'2026-11-23T00:00:00+09:00',pages=2)
        with patch.object(self.http,'get',return_value=Response(page_data)) as get:
            self.assertEqual(scanner.step(self.runtime.fetch_news_page),'more_pages')
            self.assertEqual(get.call_args.kwargs['params']['page'],1)
        self.now+=45
        reopened=Queue(self.q.path,lambda:self.now)
        scanner=Scanner(reopened,self.runtime.companies,'2026-11-23T00:00:00+09:00',pages=2)
        with patch.object(self.http,'get',return_value=Response({'clusters':[]})) as get:
            self.assertEqual(scanner.step(self.runtime.fetch_news_page),'success')
            self.assertEqual(get.call_args.kwargs['params']['page'],2)
        self.assertEqual(reopened.get('cursor'),1)
        self.now+=45
        with patch.object(self.http,'get',return_value=Response(status=429,headers={'Retry-After':'900'})):
            self.assertEqual(scanner.step(self.runtime.fetch_news_page),'HTTPError')
        self.assertEqual(self.runtime.news_step(),'provider_pause')

    def test_uncertain_telegram_is_not_automatically_resent(self):
        self.runtime.add_output('news:test','telegram:news:chem',{'message':'가상'})
        with patch.object(self.http,'post',side_effect=requests.Timeout):
            self.assertEqual(deliver_one(self.q,self.runtime.send),'uncertain')
        self.now+=3600
        self.assertEqual(deliver_one(self.q,self.runtime.send),'waiting')

    def test_second_same_issue_skips_before_ai_attempt(self):
        prepared=self.runtime.prepare_news(self.news(),'051910')
        self.q.enqueue('news','first',prepared)
        tick(self.q,self.runtime.transport,self.runtime.parse,self.runtime.ai_preflight)
        prepared=json.loads(json.dumps(prepared));prepared['event']['url']+='?second'
        self.q.enqueue('news','second',prepared);self.now+=60
        self.assertEqual(tick(self.q,self.runtime.transport,self.runtime.parse,self.runtime.ai_preflight),'waiting')
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id='news:second'").fetchone()[0],'excluded')

    def test_alphanumeric_code_preserved_without_receiver_contract_violation(self):
        company=self.runtime.by_code['0126Z0']['name']
        item=self.news('0126Z0',title=company+' 신규 계약')
        self.q.put('body:'+item['id'],dict(body=company+'은 신규 계약을 체결했다.',published_at=None,
                   article_published_at_original=None,article_modified_at_original=None))
        prepared=self.runtime.prepare_news(item,'0126Z0')
        self.assertIsNone(prepared['event']['stock_code'])
        self.assertEqual(prepared['event']['stock_code_original'],'0126Z0')
        self.assertEqual(prepared['event']['company'],company)

    def test_holding_filter_routes_without_listing_request(self):
        item=self.news('034730','other-subject','SK하이닉스 신규 계약')
        self.q.put('body:other-subject',dict(body='SK하이닉스는 신규 계약을 체결했다.',published_at=None,
                   article_published_at_original=None,article_modified_at_original=None))
        self.runtime.discover('news','other-subject','034730',item)
        self.assertEqual(self.runtime.preprocess_one(),'complete')
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        outside=self.news('267250','outside','HD현대케미칼 신규 계약')
        self.q.put('body:outside',dict(body='HD현대케미칼은 신규 계약을 체결했다.',published_at=None,
                   article_published_at_original=None,article_modified_at_original=None))
        self.runtime.discover('news','outside','267250',outside)
        self.assertEqual(self.runtime.preprocess_one(),'complete')
        self.http.answer='NO_SEND'
        tick(self.q,self.runtime.transport,self.runtime.parse)
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],0)
            result=json.loads(db.execute("SELECT result FROM jobs WHERE id='news:outside'").fetchone()[0])
            self.assertEqual(result['decision'],'NO_SEND')
        self.assertFalse(any('kind.krx' in call[1] for call in self.http.calls))

    def test_dart_excluded_title_persisted_without_body_ai_or_delivery(self):
        xml='''<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><title>LG화학 - [기재정정] 특수관계인과의 보험 거래</title><link>https://example.invalid/?rcpNo=20261125001</link><dc:creator>LG화학</dc:creator></item></channel></rss>'''
        self.q.put('dart_last_link','prior')
        with patch('pipeline_runtime.disclosure_rules.get_raw_text') as body:
            self.runtime.dart_step(xml)
            self.assertEqual(self.runtime.preprocess_one(),'waiting')
            self.now+=3600
            self.runtime.dart_step(xml)
            body.assert_not_called()
        with self.q.db() as db:
            row=db.execute('SELECT state,reason FROM discoveries').fetchone()
            self.assertEqual(tuple(row),('excluded','title_type:특수관계인과의보험거래'))
            self.assertEqual(db.execute('SELECT count(*) FROM discoveries').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],0)

    def test_dart_subsidiary_exclusion_blocks_priority_and_retry(self):
        event=self.dart(title='LG화학 - 합병 (자회사의 주요경영사항)')
        # A monitored reporting company discloses another monitored subsidiary.
        self.runtime.discover('dart',event['source_id'],'LG화학',event)
        raw='자회사인 | SK하이닉스 | 의 주요경영사항신고 합병 검토 중. '*10
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value=raw) as body:
            self.assertEqual(self.runtime.preprocess_one(),'excluded')
            self.assertEqual(self.runtime.preprocess_one(),'waiting')
            self.assertEqual(body.call_count,1)
        with self.q.db() as db:
            row=db.execute('SELECT state,reason,payload FROM discoveries').fetchone()
            self.assertEqual(row['reason'],'subsidiary_in_universe')
            self.assertEqual(json.loads(row['payload'])['_filter_decision']['target_codes'],['000660'])
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],0)

    def test_pending_discovery_and_ai_job_filtered_without_reanalysis(self):
        event=self.dart(title='LG화학 - 의결권대리행사권유참고서류')
        self.runtime.discover('dart',event['source_id'],'LG화학',event)
        with patch('pipeline_runtime.disclosure_rules.get_raw_text') as body:
            self.assertEqual(self.runtime.preprocess_one(),'excluded')
            body.assert_not_called()
        event2=self.dart('20261125002',title='LG화학 - 주주명부폐쇄기간또는기준일설정')
        prepared=dict(event=event2,priority=False,request={'model':'existing','messages':[]})
        self.q.enqueue('dart',event2['source_id'],prepared)
        self.runtime.discover('dart',event2['source_id'],'LG화학',event2)
        self.assertEqual(self.runtime.preprocess_one(),'duplicate')
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs').fetchone()[0],'pending')
            self.assertEqual(json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0]),prepared)
        with patch.object(self.runtime,'transport') as transport:
            self.assertEqual(tick(self.q,transport,self.runtime.parse,self.runtime.ai_preflight),'waiting')
            transport.assert_not_called()
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs').fetchone()[0],'excluded')
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)

    def test_pending_subsidiary_cached_prompt_filtered_but_completed_output_untouched(self):
        from dart_monitor.disclosure_rules import generate_custom_prompt
        event=self.dart(title='LG화학 - 합병 (자회사의 주요경영사항)')
        raw='자회사인 | SK하이닉스 | 의 주요경영사항신고 합병 검토 중. '*10
        _,prompt=generate_custom_prompt(raw,event['title'])
        prepared=dict(event=event,priority=True,request=dict(model='existing',messages=[dict(role='user',content=prompt)]))
        self.q.enqueue('dart',event['source_id'],prepared)
        with self.q.db() as db:
            db.execute("INSERT INTO jobs(id,source,payload,state,created,due,result) VALUES (?,?,?,?,?,?,?)",
                       ('dart:completed','dart',json.dumps(prepared),'complete',self.now,0,'"original result"'))
        self.runtime.add_output('dart:completed','dashboard',dict(original='preserve'))
        self.runtime.ai_preflight()
        with self.q.db() as db:
            self.assertEqual(db.execute('SELECT state FROM jobs WHERE id=?',('dart:'+event['source_id'],)).fetchone()[0],'excluded')
            self.assertEqual(db.execute("SELECT state,result FROM jobs WHERE id='dart:completed'").fetchone()[0],'complete')
            self.assertEqual(db.execute('SELECT count(*) FROM outputs').fetchone()[0],1)

    def test_dart_unidentified_target_keeps_ai_path_and_records_decision(self):
        event=self.dart(title='LG화학 - 공급계약 (종속회사의 주요경영사항)')
        self.runtime.discover('dart',event['source_id'],'LG화학',event)
        with patch('pipeline_runtime.disclosure_rules.get_raw_text',return_value='다른 회사는 SK하이닉스다. '*20):
            self.assertEqual(self.runtime.preprocess_one(),'complete')
        with self.q.db() as db:
            row=db.execute('SELECT reason FROM discoveries').fetchone()
            self.assertEqual(row[0],'subsidiary_target_unidentified')
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)


if __name__=='__main__':unittest.main()
