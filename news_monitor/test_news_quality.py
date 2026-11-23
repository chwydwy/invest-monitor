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






    def test_shared_prompt_parser_preserves_relevance_rules(self):
        from news_monitor.ai_rules import build_request, parse_response
        prompt = build_request('LG화학', '제목', '본문')['messages'][0]['content']
        self.assertIn('과거 사실의 반복', prompt)
        self.assertEqual(parse_response('NO_SEND'), ('NO_SEND',''))
        with self.assertRaises(ValueError):
            parse_response('추론만 존재')


if __name__ == '__main__':
    unittest.main()
