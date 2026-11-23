import unittest
from datetime import datetime
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup

from news_monitor.news_quality import (article_publication_time, choose_registration_time,
                                       fetch_listed_companies,
                                       load_company_names, resolve_article_company)
from news_monitor import test_naver_news as naver_tests


class NewsQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.names = load_company_names()
        cls.listed = {'SK하이닉스': '000660', 'SK텔레콤': '017670',
                      '롯데케미칼': '011170', '롯데지주': '004990'}
        cls.monitored = {cls.names.get(name, name): name for name in (
            'SK', 'HD현대', '롯데케미칼', 'LG화학', 'TKG휴켐스')}

    def resolve(self, lookup, title, body):
        return resolve_article_company(lookup, title, body, self.monitored,
                                       self.names, self.listed)

    def test_affiliate_is_not_holding_company(self):
        self.assertEqual(self.resolve('SK', 'SK하이닉스 메모리 공급',
                                      'SK하이닉스는 신규 공급 계약을 체결했다')[0], None)
        self.assertEqual(self.resolve('SK', 'SK텔레콤 희망퇴직',
                                      'SK텔레콤은 희망퇴직을 추진한다')[0], None)
        self.assertEqual(self.resolve('SK', 'SK 관련 반도체 기사',
                                      'SK하이닉스는 메모리를 공급한다')[1],
                         'other_listed_company')
        self.assertEqual(self.resolve('SK', '[연합뉴스 이 시각 헤드라인] - 18:00',
                                      'SK가 언급됐다')[1], 'digest_title')

    def test_unlisted_subsidiary_and_normal_article(self):
        self.assertEqual(self.resolve('HD현대', 'HD현대케미칼 설비 투자',
                                      'HD현대케미칼은 설비 투자를 결정했다'),
                         ('HD현대', 'unlisted_subject'))
        self.assertEqual(self.resolve('HD현대', '석유화학 설비 투자',
                                      'HD현대케미칼은 설비 투자를 결정했다'),
                         ('HD현대', 'unlisted_subject_body'))
        self.assertEqual(resolve_article_company('HD현대', 'HD현대케미칼 설비 투자',
                         'HD현대케미칼은 설비 투자를 결정했다', self.monitored,
                         self.names, None)[1], 'listing_unverified')
        self.assertEqual(self.resolve('LG화학', 'LG화학 양극재 공급계약',
                                      'LG화학은 공급계약을 체결했다')[0], 'LG화학')
        self.assertEqual(self.resolve('HD현대', '롯데케미칼 8000억원 상환',
                                      '롯데케미칼은 회사채를 상환했다'),
                         (None, 'other_listed_company'))
        self.assertEqual(self.resolve('HD현대', '롯데월드타워 담보 해지',
                                      '롯데지주와 롯데케미칼이 각각 언급됐다')[0], None)

    def test_confirmed_alias_retains_news_list_name(self):
        self.assertEqual(self.resolve('TKG휴켐스', '티케이지휴켐스 신규 계약',
                                      '티케이지휴켐스는 계약을 체결했다')[0], 'TKG휴켐스')

    def test_registration_time_prefers_article_and_keeps_modification_separate(self):
        soup = BeautifulSoup('''<span data-date-time="2026-11-23 18:04:00"
            ></span>
            <span data-modify-date-time="2026-11-23 18:05:11"></span>''', 'html.parser')
        self.assertEqual(article_publication_time(soup),
                         ('2026-11-23T18:04:00+09:00', '2026-11-23 18:04:00',
                          '2026-11-23 18:05:11'))
        offset = BeautifulSoup('<span data-date-time="2026-11-23T09:04:00Z"></span>', 'html.parser')
        self.assertEqual(article_publication_time(offset)[0], '2026-11-23T09:04:00+00:00')
        missing = BeautifulSoup('<span data-modify-date-time="2026-11-23 18:05:11"></span>', 'html.parser')
        self.assertEqual(article_publication_time(missing), (None, None, '2026-11-23 18:05:11'))
        invalid = BeautifulSoup('<span data-date-time="not-a-time"></span>', 'html.parser')
        self.assertEqual(article_publication_time(invalid), (None, 'not-a-time', None))
        news_time = {'published_at': '2026-11-23T18:04:00+09:00',
                     'npay_datetime_raw': '202611231804'}
        self.assertEqual(choose_registration_time(
            {'published_at': '2026-11-23T18:04:17+09:00',
             'article_published_at_original': '2026-11-23 18:04:17'}, news_time),
            ('2026-11-23T18:04:17+09:00', '2026-11-23 18:04:17'))
        self.assertEqual(choose_registration_time(
            {'published_at': None, 'article_published_at_original': 'bad-time'}, news_time),
            ('2026-11-23T18:04:00+09:00', '202611231804'))
        self.assertEqual(choose_registration_time({}, {}), (None, None))

    def test_kind_listing_keeps_alphanumeric_stock_codes(self):
        rows = ''.join(f'<tr><td>가상기업{i}</td><td>코스피</td><td>{i:06d}</td></tr>'
                       for i in range(2700))
        rows += '<tr><td>SK하이닉스</td><td>유가</td><td>000660</td></tr>'
        rows += '<tr><td>가상영문코드</td><td>코스닥</td><td>0126Z0</td></tr>'
        class Response:
            content = ('<table>' + rows + '</table>').encode('euc-kr')
            def raise_for_status(self):
                pass
        with patch('news_monitor.news_quality.requests.get', return_value=Response()):
            listed = fetch_listed_companies()
        self.assertEqual(listed['가상영문코드'], '0126Z0')
        self.assertEqual(listed['SK하이닉스'], '000660')

    def test_rewritten_issue_and_new_facts(self):
        news = naver_tests.NpayNewsTest.news_module()
        cached = [{'company': '롯데케미칼', 'title': '롯데그룹, 롯데월드타워 담보 해지',
                   'body': '롯데월드타워의 담보가 해지됐다.'}]
        duplicate = news.find_similar_sent_article(
            "'롯데의 상징' 잠실 월드타워 담보 풀렸다… 케미칼 유동성 우려 완화",
            '롯데월드타워 담보를 해지했다.', cached, '롯데케미칼')
        self.assertIsNotNone(duplicate)
        self.assertEqual(duplicate['reason'], '동일 사건·제목 재작성')
        self.assertIsNone(news.find_similar_sent_article(
            '롯데케미칼 NCC 설비 매각', 'NCC 설비를 매각한다.', cached, '롯데케미칼'))
        self.assertIsNone(news.find_similar_sent_article(
            '롯데월드타워 담보 해지 후 9000억원 추가 상환',
            '추가 상환을 발표했다.', cached, '롯데케미칼'))
        self.assertIsNone(news.find_similar_sent_article(
            '롯데그룹, 롯데월드타워 담보 해지',
            '추가로 9000억원을 상환하기로 확정했다.', cached, '롯데케미칼'))
        self.assertIsNone(news.find_similar_sent_article(
            '롯데그룹, 롯데월드타워 담보 해지',
            '롯데월드타워 담보 해지에 관한 다른 회사 기사', cached, 'HD현대'))
        pending = [{'company': 'LG화학', 'title': 'LG화학 북미 공장 투자 검토',
                    'body': 'LG화학은 투자 여부를 검토 중이다.'}]
        self.assertIsNone(news.find_similar_sent_article(
            'LG화학 북미 공장 투자 확정', 'LG화학은 투자를 확정했다.',
            pending, 'LG화학'))
        self.assertIsNone(news.find_similar_sent_article(
            'LG화학 북미 공장 투자 검토', 'LG화학은 투자를 최종 확정했다.',
            pending, 'LG화학'))

    def test_cache_window_crosses_midnight(self):
        news = naver_tests.NpayNewsTest.news_module()
        now = datetime(2026, 11, 24, 0, 10)
        self.assertTrue(news.cache_item_recent({'sent_at': '2026-11-23T23:50:00'}, now))
        self.assertFalse(news.cache_item_recent({'sent_at': '2026-11-22T23:50:00'}, now))

    def test_ai_prompt_requires_new_direct_facts_without_api(self):
        news = naver_tests.NpayNewsTest.news_module()
        from unittest.mock import patch
        class Response:
            status_code = 200
            def json(self):
                return {'choices': [{'message': {'content': 'NO_SEND'}}]}
        with patch.object(news.requests, 'post', return_value=Response()) as post:
            self.assertEqual(news.get_ai_summary('LG화학', '과거 사업 소개', '과거 사실만 반복'),
                             ('NO_SEND', ''))
        system_prompt = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertIn('과거 사실의 반복', system_prompt)
        self.assertIn('지분·지배구조', system_prompt)

    def test_main_routes_verified_company_without_real_outputs(self):
        news = naver_tests.NpayNewsTest.news_module()
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'targets.txt'
            target.write_text('롯데케미칼\n', encoding='utf-8')
            mapping = root / 'mapping.json'
            mapping.write_text(json.dumps({'롯데케미칼': '011170'}), encoding='utf-8')
            fixture = {'id': '999_0000000001', 'title': '롯데케미칼 신규 공급계약',
                       'press': '가상 언론사', 'office_id': '999',
                       'article_id': '0000000001', 'npay_datetime_raw': '202611231804',
                       'published_at': '2026-11-23T18:04:00+09:00'}
            article_time = {'published_at': '2026-11-23T18:04:00+09:00',
                            'article_published_at_original': '2026-11-23 18:04:00',
                            'article_modified_at_original': None}
            def stop_after_company(seconds):
                if seconds == 90:
                    raise StopLoop
            with patch.object(news, 'TARGET_LIST_FILE', str(target)), \
                 patch.object(news, 'MAPPING_FILE', str(mapping)), \
                 patch.object(news, 'SENT_HISTORY_FILE', str(root / 'sent.json')), \
                 patch.object(news, 'DAILY_SENT_CACHE_FILE', str(root / 'cache.json')), \
                 patch.object(news, 'load_exclude_list', return_value=[]), \
                 patch.object(news, 'fetch_listed_companies', return_value=self.listed), \
                 patch.object(news, 'load_or_create_cutover', return_value=datetime(2026, 11, 23)), \
                 patch.object(news, 'is_active_time', return_value=True), \
                 patch.object(news, 'get_latest_news_by_date', return_value=[fixture]), \
                 patch.object(news, 'get_article_body', return_value=(
                     '롯데케미칼은 신규 공급계약을 체결했다.',
                     'https://n.news.naver.com/mnews/article/999/0000000001', article_time)), \
                 patch.object(news, 'get_ai_summary', return_value=('SEND', '공급계약을 체결했다.')), \
                 patch.object(news, 'send_telegram', return_value=True) as telegram, \
                 patch.object(news, 'queue_dashboard_news') as queue, \
                 patch.object(news.time, 'sleep', side_effect=stop_after_company):
                with self.assertRaises(StopLoop):
                    news.main()
            self.assertEqual(telegram.call_args.args[0], '롯데케미칼')
            payload = queue.call_args.args[0]
            self.assertEqual(payload['company'], '롯데케미칼')
            self.assertEqual(payload['stock_code'], '011170')
            self.assertEqual(payload['published_at'], '2026-11-23T18:04:00+09:00')
            self.assertEqual(payload['npay_datetime_original'], '202611231804')
            self.assertEqual(json.loads((root / 'sent.json').read_text()), ['999_0000000001'])

    def test_main_skips_wrong_affiliate_before_ai(self):
        news = naver_tests.NpayNewsTest.news_module()
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'targets.txt'
            target.write_text('SK\n', encoding='utf-8')
            mapping = root / 'mapping.json'
            mapping.write_text(json.dumps({'SK': '034730'}), encoding='utf-8')
            fixture = {'id': '999_0000000002', 'title': 'SK하이닉스 메모리 계약',
                       'press': '가상 언론사', 'office_id': '999',
                       'article_id': '0000000002', 'npay_datetime_raw': '202611231804',
                       'published_at': '2026-11-23T18:04:00+09:00'}
            with patch.object(news, 'TARGET_LIST_FILE', str(target)), \
                 patch.object(news, 'MAPPING_FILE', str(mapping)), \
                 patch.object(news, 'SENT_HISTORY_FILE', str(root / 'sent.json')), \
                 patch.object(news, 'DAILY_SENT_CACHE_FILE', str(root / 'cache.json')), \
                 patch.object(news, 'load_exclude_list', return_value=[]), \
                 patch.object(news, 'fetch_listed_companies', return_value=self.listed), \
                 patch.object(news, 'load_or_create_cutover', return_value=datetime(2026, 11, 23)), \
                 patch.object(news, 'is_active_time', return_value=True), \
                 patch.object(news, 'get_latest_news_by_date', return_value=[fixture]), \
                 patch.object(news, 'get_article_body', return_value=(
                     'SK하이닉스는 계약을 체결했다.', 'https://example.invalid/article', {})), \
                 patch.object(news, 'get_ai_summary') as ai, \
                 patch.object(news, 'send_telegram') as telegram, \
                 patch.object(news, 'queue_dashboard_news') as queue, \
                 patch.object(news.time, 'sleep', side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    news.main()
            ai.assert_not_called()
            telegram.assert_not_called()
            queue.assert_not_called()
            self.assertEqual(json.loads((root / 'sent.json').read_text()), ['999_0000000002'])

    def test_unlisted_subject_still_requires_ai_send(self):
        news = naver_tests.NpayNewsTest.news_module()
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'targets.txt'
            target.write_text('HD현대\n', encoding='utf-8')
            mapping = root / 'mapping.json'
            mapping.write_text(json.dumps({'HD현대': '267250'}), encoding='utf-8')
            fixture = {'id': '999_0000000003', 'title': 'HD현대케미칼 행사 개최',
                       'press': '가상 언론사', 'office_id': '999', 'article_id': '0000000003',
                       'npay_datetime_raw': '202611231804',
                       'published_at': '2026-11-23T18:04:00+09:00'}
            with patch.object(news, 'TARGET_LIST_FILE', str(target)), \
                 patch.object(news, 'MAPPING_FILE', str(mapping)), \
                 patch.object(news, 'SENT_HISTORY_FILE', str(root / 'sent.json')), \
                 patch.object(news, 'DAILY_SENT_CACHE_FILE', str(root / 'cache.json')), \
                 patch.object(news, 'load_exclude_list', return_value=[]), \
                 patch.object(news, 'fetch_listed_companies', return_value=self.listed), \
                 patch.object(news, 'load_or_create_cutover', return_value=datetime(2026, 11, 23)), \
                 patch.object(news, 'is_active_time', return_value=True), \
                 patch.object(news, 'get_latest_news_by_date', return_value=[fixture]), \
                 patch.object(news, 'get_article_body', return_value=(
                     'HD현대케미칼은 임직원 행사를 개최했다.', 'https://example.invalid/article', {})), \
                 patch.object(news, 'get_ai_summary', return_value=('NO_SEND', '')) as ai, \
                 patch.object(news, 'send_telegram') as telegram, \
                 patch.object(news, 'queue_dashboard_news') as queue, \
                 patch.object(news.time, 'sleep', side_effect=StopLoop):
                with self.assertRaises(StopLoop):
                    news.main()
            self.assertEqual(ai.call_count, 1)
            self.assertEqual(ai.call_args.args[0], 'HD현대')
            telegram.assert_not_called()
            queue.assert_not_called()
            self.assertEqual(json.loads((root / 'sent.json').read_text()), ['999_0000000003'])

    def test_listing_failure_defers_subsidiary_and_continues_next_company(self):
        news = naver_tests.NpayNewsTest.news_module()
        class StopLoop(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'targets.txt'
            target.write_text('HD현대\nLG화학\n', encoding='utf-8')
            mapping = root / 'mapping.json'
            mapping.write_text(json.dumps({'HD현대': '267250', 'LG화학': '051910'}), encoding='utf-8')
            def listing_for_code(code, not_before=None):
                company = 'HD현대케미칼' if code == '267250' else 'LG화학'
                return [{'id': code, 'title': company + ' 신규 계약', 'press': '가상 언론사',
                         'office_id': code, 'article_id': '1',
                         'npay_datetime_raw': '202611231804',
                         'published_at': '2026-11-23T18:04:00+09:00'}]
            def body_for_code(code, aid, include_metadata=False):
                company = 'HD현대케미칼' if code == '267250' else 'LG화학'
                return (company + '은 신규 계약을 맺었다.',
                        'https://example.invalid/article', {})
            waits = []
            def stop_after_two(seconds):
                if seconds == 90:
                    waits.append(seconds)
                    if len(waits) == 2:
                        raise StopLoop
            with patch.object(news, 'TARGET_LIST_FILE', str(target)), \
                 patch.object(news, 'MAPPING_FILE', str(mapping)), \
                 patch.object(news, 'SENT_HISTORY_FILE', str(root / 'sent.json')), \
                 patch.object(news, 'DAILY_SENT_CACHE_FILE', str(root / 'cache.json')), \
                 patch.object(news, 'load_exclude_list', return_value=[]), \
                 patch.object(news, 'fetch_listed_companies', side_effect=news.requests.Timeout), \
                 patch.object(news, 'load_or_create_cutover', return_value=datetime(2026, 11, 23)), \
                 patch.object(news, 'is_active_time', return_value=True), \
                 patch.object(news, 'get_latest_news_by_date', side_effect=listing_for_code), \
                 patch.object(news, 'get_article_body', side_effect=body_for_code), \
                 patch.object(news, 'get_ai_summary', return_value=('SEND', '계약을 맺었다.')) as ai, \
                 patch.object(news, 'send_telegram', return_value=True) as telegram, \
                 patch.object(news, 'queue_dashboard_news') as queue, \
                 patch.object(news.time, 'sleep', side_effect=stop_after_two):
                with self.assertRaises(StopLoop):
                    news.main()
            self.assertEqual(ai.call_count, 1)
            self.assertEqual(telegram.call_count, 1)
            self.assertEqual(queue.call_count, 1)
            self.assertEqual(queue.call_args.args[0]['company'], 'LG화학')
            self.assertEqual(queue.call_args.args[0]['published_at'],
                             '2026-11-23T18:04:00+09:00')
            self.assertEqual(json.loads((root / 'sent.json').read_text()), ['051910'])


if __name__ == '__main__':
    unittest.main()
