import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ai_pipeline import Queue
from ai_worker import deliver_one
from operations_report import KST, snapshot, run_report, load_json
from dashboard_outbox import enqueue


class Response:
    def __init__(self,payload,status=200,headers=None):
        self.payload=payload;self.status_code=status;self.headers=headers or {}
    def json(self):return self.payload


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,11,25,8,59,tzinfo=KST).timestamp()
        self.q=Queue(self.root/'ai.sqlite3',lambda:self.now)
        self.token=self.root/'report.token';self.token.write_text('x'*40);self.token.chmod(0o600)
        self.cfg=dict(ai_db=self.q.path,dashboard_db=str(self.root/'out.sqlite3'),endpoint='https://example.invalid/report',
                      token_file=str(self.token),state_file=str(self.root/'report-state.json'),limits=dict(total=900,news=600,dart=200,retry=100))
        enqueue(self.cfg['dashboard_db'],dict(source='news',source_id='old',summary='SENSITIVE_AI_TEXT',published_at=None))

    def full_day_coverage(self):
        self.q.put('operations_metrics_started',datetime(2026,11,24,0,0,tzinfo=KST).timestamp())

    def test_old_records_never_invent_today_or_completion_time(self):
        self.q.enqueue('news','old',{})
        with self.q.db() as db:db.execute("UPDATE jobs SET state='complete',result='{}'")
        report=snapshot(self.cfg,self.now)
        self.assertIsNone(report['today_kst']['sources']['news']['ai_completed'])
        self.assertIsNone(report['last_success']['ai'])
        self.assertIsNone(report['last_success']['telegram'])
        self.assertNotIn('SENSITIVE_AI_TEXT',json.dumps(report))

    def test_dedup_daily_counters_and_real_confirmation_points(self):
        self.full_day_coverage()
        with self.q.db() as db:
            self.q.insert_discovery(db,'news','same','A',{'id':'same'})
            self.q.insert_discovery(db,'news','same','B',{'id':'same'})
        self.q.enqueue('news','same',{})
        job=self.q.reserve();self.now+=2
        self.q.finish(job,200,result={'decision':'NO_SEND'})
        report=snapshot(self.cfg,self.now)
        self.assertEqual(report['today_kst']['sources']['news'],dict(collected=1,ai_completed=1,excluded=1,failed=0,held=0))
        self.assertEqual(report['last_success']['ai'],datetime.fromtimestamp(self.now,timezone.utc).isoformat(timespec='milliseconds'))
        self.assertEqual(report['ai_budget']['used'],1)
        self.assertEqual(report['queue']['oldest_wait_seconds'],0)

    def test_kst_activity_and_utc_budget_roll_at_different_times(self):
        self.full_day_coverage()
        self.q.enqueue('dart','1',{});job=self.q.reserve();self.q.finish(job,599)
        before=snapshot(self.cfg,self.now)
        self.assertEqual(before['today_kst']['date'],'2026-11-25')
        self.assertEqual(before['ai_budget']['utc_date'],'2026-11-24')
        self.now=datetime(2026,11,25,9,0,tzinfo=KST).timestamp()
        after=snapshot(self.cfg,self.now)
        self.assertEqual(after['ai_budget']['used'],0)
        self.assertEqual(after['today_kst']['sources']['dart']['failed'],1)
        self.now=datetime(2026,11,26,0,0,tzinfo=KST).timestamp()
        next_day=snapshot(self.cfg,self.now)
        self.assertEqual(next_day['today_kst']['sources']['dart']['failed'],0)
        self.assertEqual(next_day['ai_budget']['utc_date'],'2026-11-25')

    def test_handoff_ack_and_telegram_are_separate(self):
        self.q.enqueue('news','1',{})
        job=self.q.reserve();self.q.finish(job,200,{},[('dashboard',{}),('telegram:news:chem',{})])
        deliver_one(self.q,lambda destination,p: True)
        report=snapshot(self.cfg,self.now)
        self.assertEqual(report['delivery']['dashboard_enqueue']['delivered'],1)
        self.assertEqual(report['delivery']['dashboard_stored']['delivered'],0)
        self.assertEqual(report['delivery']['telegram']['delivered'],0)
        self.assertIsNotNone(report['last_success']['dashboard_enqueue'])
        self.assertIsNone(report['last_success']['dashboard_stored'])
        deliver_one(self.q,lambda destination,p: True)
        self.assertEqual(snapshot(self.cfg,self.now)['delivery']['telegram']['delivered'],1)

    def test_one_sample_per_slot_and_identical_duplicate_verification(self):
        sent=[]
        def post(endpoint,**kw):
            sent.append(kw['data']);payload=json.loads(kw['data'])
            self.assertNotIn('Cookie',kw['headers']);self.assertNotIn('Origin',kw['headers'])
            return Response(dict(status='stored' if len(sent)==1 else 'duplicate',report_id=payload['report_id'],received_at='2026-11-24T23:59:00Z'))
        first=run_report(self.cfg,now=self.now,post=post)
        self.assertEqual(first['status'],'stored')
        self.assertEqual(run_report(self.cfg,now=self.now+10,post=post)['status'],'waiting')
        self.assertEqual(run_report(self.cfg,True,now=self.now+10,post=post)['status'],'duplicate')
        self.assertEqual(sent[0],sent[1])
        run_report(self.cfg,now=self.now+1800,post=post)
        self.assertNotEqual(json.loads(sent[-1])['report_id'],first['report_id'])
        self.assertEqual(len(list(self.root.glob('*.snapshot.json'))),1)

    def test_failure_does_not_call_pipeline_or_build_backlog(self):
        post=lambda *a,**k:Response({},503,{'Retry-After':'3600'})
        with patch('pipeline_runtime.Runtime.news_step',side_effect=AssertionError),patch('pipeline_runtime.Runtime.transport',side_effect=AssertionError):
            self.assertEqual(run_report(self.cfg,now=self.now,post=post)['status'],'rejected')
            self.assertEqual(run_report(self.cfg,now=self.now+1800,post=post)['status'],'waiting')
            run_report(self.cfg,now=self.now+3600,post=post)
        self.assertEqual(len(list(self.root.glob('*.snapshot.json'))),1)
        self.assertEqual(self.q.status()['usage'],{})

    def test_auth_and_invalid_payload_pause_without_repeated_requests(self):
        for code in (401,422):
            state=Path(self.cfg['state_file'])
            if state.exists():state.unlink()
            called=[]
            def post(*a,**k):called.append(1);return Response({},code)
            run_report(self.cfg,now=self.now,post=post)
            self.assertIn(run_report(self.cfg,now=self.now+1800,post=post)['status'],('auth_hold','configuration_hold'))
            self.assertEqual(len(called),1)

    def test_contract_validator_on_dashboard_source(self):
        module=Path('C:/Projects/MarketResearchHub/src/modules/pi-report.js')
        if not module.exists():self.skipTest('receiver source unavailable')
        code="import {validatePiReport} from '"+module.as_uri()+"';let s='';for await(const c of process.stdin)s+=c;const p=JSON.parse(s);validatePiReport(p,new Date(p.reported_at));console.log('contract OK');"
        result=subprocess.run(['node','--input-type=module','-e',code],input=json.dumps(snapshot(self.cfg,self.now)),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
