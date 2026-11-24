import unittest
from datetime import datetime
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup

from news_monitor.news_quality import (article_publication_time, choose_registration_time,
                                       load_company_names, resolve_article_company)
from news_monitor import test_naver_news as naver_tests


class NewsQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from news_scan import universe
        companies=universe()
        cls.names=load_company_names()
        cls.monitored={row['name']:row['name'] for row in companies}
        cls.sectors={row['name']:row.get('sector') for row in companies}

    def resolve(self, lookup, title, body):
        return resolve_article_company(lookup,title,body,self.monitored,self.names,self.sectors)

    def test_holding_excludes_only_different_universe_subject(self):
        self.assertEqual(self.resolve('SK','SK하이닉스 메모리 공급','SK하이닉스는 계약을 체결했다.'),
                         (None,'holding_other_universe_subject'))
        self.assertEqual(self.resolve('삼성물산','삼성전자 신규 계약','삼성전자는 신규 계약을 체결했다.')[0],None)
        self.assertEqual(self.resolve('SK','반도체 신규 계약','SK하이닉스는 신규 계약을 체결했다.')[0],None)

    def test_nonholding_or_unknown_sector_keeps_ai_judgment(self):
        self.assertEqual(self.resolve('LG화학','SK하이닉스 메모리 계약','SK하이닉스는 계약했다.')[0],'LG화학')
        self.assertEqual(self.resolve('POSCO홀딩스','LG화학 공급계약','LG화학은 계약했다.')[0],'POSCO홀딩스')
        self.assertEqual(resolve_article_company('SK','SK하이닉스 공급계약','본문',self.monitored,self.names,{})[0],'SK')
        self.assertEqual(resolve_article_company('미확인','SK하이닉스 공급계약','본문',self.monitored,self.names,{'미확인':'지주'})[0],'미확인')

    def test_outside_universe_never_uses_listing_status(self):
        for subject in ('HD현대케미칼','테슬라','BL밖상장사'):
            self.assertEqual(self.resolve('HD현대',subject+' 신규 계약',subject+'은 계약했다.')[0],'HD현대')

    def test_alias_and_short_name_are_distinct(self):
        self.assertEqual(self.resolve('SK','LSELECTRIC 신규 계약','엘에스일렉트릭은 계약했다.')[0],None)
        self.assertEqual(self.resolve('SK','SK 신규 투자','SK는 신규 투자를 결정했다.' )[0],'SK')
        self.assertEqual(self.resolve('TKG휴켐스','티케이지휴켐스 신규 계약','티케이지휴켐스는 계약했다.')[0],'TKG휴켐스')

    def test_incidental_or_joint_mentions_are_not_sole_subjects(self):
        for title,body in [
            ('SK, SK하이닉스 사례 참고해 신규 투자','SK는 투자했다. SK하이닉스도 언급됐다.'),
            ('지주 투자전략 발표','SK는 SK하이닉스를 사례로 언급했다.'),
            ('SK하이닉스와 SK, 공동 사업 검토','양사는 공동 사업을 검토한다.'),
            ('SK하이닉스의 사례를 본 SK 투자전략','업계에서는 두 회사를 비교했다.')]:
            self.assertEqual(self.resolve('SK',title,body)[0],'SK',title)
        self.assertEqual(self.resolve('SK','[연합뉴스 이 시각 헤드라인] - 18:00','SK가 언급됐다')[1],'digest_title')

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
