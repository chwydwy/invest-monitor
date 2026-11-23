import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from ai_pipeline import Queue, DEFAULTS
from news_scan import Scanner, universe
from ai_worker import tick, deliver_one


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1795478400.0
        self.path = Path(self.temp.name) / 'analysis.sqlite3'
        self.q = Queue(self.path, lambda: self.now)

    def test_serial_reservation_spacing_and_fairness(self):
        self.q.enqueue('dart', '1', {})
        self.q.enqueue('news', '1', {})
        first = self.q.reserve()
        self.assertEqual(first['source'], 'dart')
        self.assertIsNone(self.q.reserve())
        self.now+=60
        self.assertIsNone(Queue(self.path,lambda:self.now).reserve())
        self.q.finish(first, 200, {'text': 'result'})
        self.now += 60
        self.assertEqual(self.q.reserve()['source'], 'news')

    def test_worker_fake_api_and_independent_outputs(self):
        self.q.enqueue('news', '1', {})
        class Response:
            status_code = 200
            headers = {}
            def json(self):
                return {'choices':[{'message':{'content':'SEND\n가상 요약'}}]}
        self.assertEqual(tick(self.q, lambda job: Response(), lambda j,c:
                              (c,[('dashboard',{}),('telegram:chem',{})])), 'complete')
        self.assertEqual(deliver_one(self.q, lambda dest,p: dest == 'dashboard'), 'delivered')
        self.assertEqual(deliver_one(self.q, lambda dest,p: False), 'pending')

    def test_budget_isolation_reset_and_retry_pause(self):
        self.q.limits.update(news=1)
        self.q.enqueue('news', '1', {})
        job = self.q.reserve()
        self.q.finish(job, 429, retry_after=120)
        self.q.enqueue('dart', '1', {})
        self.now += 60
        self.assertIsNone(self.q.reserve())
        self.now += 60
        dart = self.q.reserve()
        self.assertEqual(dart['source'], 'dart')
        self.q.finish(dart, 200)
        self.now += 60
        retry = self.q.reserve()
        self.assertEqual(retry['source'], 'news')
        self.q.finish(retry, 200)
        self.q.enqueue('news', '2', {})
        self.now += 60
        self.assertIsNone(self.q.reserve())
        self.now = (int(self.now // 86400) + 1) * 86400
        self.assertIsNotNone(self.q.reserve())

    def test_recovery_and_no_duplicate_job_or_output(self):
        self.assertEqual(self.q.enqueue('news', '1', {'body': 'kept'}), 1)
        self.assertEqual(self.q.enqueue('news', '1', {'body': 'different'}), 0)
        job = self.q.reserve()
        reopened = Queue(self.path, lambda: self.now)
        reopened.recover()
        self.now += 60
        retry = reopened.reserve()
        self.assertEqual(retry['payload']['body'], 'kept')
        reopened.finish(retry, 200, 'saved', [('dashboard', {'id': 'news:1'}), ('telegram:chem', {})])
        self.assertEqual(len(reopened.status()['outputs']), 2)

    def test_auth_and_permanent_errors_hold(self):
        self.q.enqueue('dart', '1', {})
        job = self.q.reserve()
        self.q.finish(job, 401)
        self.now += 600
        self.q.enqueue('news', '2', {})
        self.assertIsNone(self.q.reserve())
        self.assertEqual(self.q.status()['jobs'][0]['state'], 'hold')

    def test_transition_baseline_consumes_today_only(self):
        day=datetime.fromtimestamp(self.now,timezone.utc).date().isoformat()
        self.q.put('budget_baseline',dict(day=day,news=600,dart=200,retry=100,reason='unknown_prior_usage'))
        self.q.enqueue('news','1',{})
        self.assertIsNone(self.q.reserve())
        self.assertEqual(self.q.status()['remaining']['total'],0)
        self.now=(int(self.now//86400)+1)*86400
        self.assertIsNotNone(self.q.reserve())

    def test_default_budgets_and_failed_retries_are_bounded(self):
        self.assertEqual([DEFAULTS[x] for x in ('total','news','dart','retry')],[900,600,200,100])
        self.q.enqueue('news','broken',{})
        for attempt in range(3):
            job=self.q.reserve()
            self.assertIsNotNone(job)
            self.q.finish(job,503)
            self.now+=600
        self.assertIsNone(self.q.reserve())
        status=self.q.status()
        self.assertEqual(status['usage'],{'news':1,'retry':2})
        self.assertEqual(status['failed_attempts'],3)
        self.assertEqual(status['jobs'][0]['state'],'hold')

    def test_format_failure_and_permanent_job_do_not_block_other_source(self):
        class Response:
            status_code=200
            def json(self):return {'choices':[{'message':{'content':None}}]}
        self.q.enqueue('news','1',{})
        self.assertEqual(tick(self.q,lambda j:Response(),lambda j,c:(c,[])),'format_retry')
        self.now+=60
        self.q.enqueue('dart','2',{})
        job=self.q.reserve();self.assertEqual(job['source'],'dart')
        self.q.finish(job,404)
        self.now+=60
        self.assertEqual(self.q.reserve()['source'],'news')

    def test_full_universe_restart_inactive_and_cycle(self):
        companies = universe()
        self.assertEqual(len(companies), 349)
        scanner = Scanner(self.q, companies, '2026-11-23T00:00:00+00:00')
        fetch = lambda code, page: ([], True)
        self.assertEqual(scanner.step(fetch, active=False), 'inactive')
        for _ in range(349):
            self.assertEqual(scanner.step(fetch), 'success')
            self.now += 45
            scanner = Scanner(Queue(self.path, lambda: self.now), companies,
                              '2026-11-23T00:00:00+00:00')
        self.assertEqual(self.q.get('cursor'), 0)
        self.assertGreaterEqual(self.q.get('cycle_seconds'), 348 * 45)

    def test_pagination_failure_gap_and_checkpoint(self):
        scanner = Scanner(self.q, universe()[:2], '2026-11-23T00:00:00+00:00', pages=2)
        item = dict(id='1_1',published_at='2026-11-23T01:00:00+00:00')
        self.assertEqual(scanner.step(lambda c,p: ([item], False)), 'more_pages')
        self.now += 45
        self.assertEqual(scanner.step(lambda c,p: ([item], False)), 'page_cap')
        status = self.q.status()
        self.assertIsNone(status['scans'][0]['last_success'])
        self.assertEqual(status['discoveries'][0]['count'], 1)
        self.now += 300
        def fail(c,p):
            raise TimeoutError
        self.assertEqual(scanner.step(fail), 'TimeoutError')
        self.assertEqual(self.q.get('cursor'), 0)


if __name__ == '__main__':
    unittest.main()
