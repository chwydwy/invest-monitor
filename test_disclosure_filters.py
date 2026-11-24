import json
import unittest
from pathlib import Path
from dart_monitor.disclosure_filters import excluded_title_reason, subsidiary_decision, reporting_company
from news_scan import universe


class DisclosureFiltersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.companies=universe()
        cls.aliases=json.loads((Path(__file__).parent/'DASHBOARD_COMPANY_ALIASES.json').read_text(encoding='utf-8'))

    def decision(self,body,reporter='LG',title='LG - 투자판단 관련 주요경영사항 (자회사의 주요경영사항)'):
        return subsidiary_decision(title,body,reporter,self.companies,self.aliases)

    def test_exact_title_types_and_whitespace_corrections(self):
        for kind in ['특수관계인과의보험거래','의결권대리행사권유참고서류','주주명부폐쇄기간또는기준일설정']:
            for title in [kind,'LG - '+kind,'LG - [기재정정] '+kind,
                          '[첨부정정]\n'+ ' \n'.join(kind),
                          '[기재정정] LG - '+kind,
                          'LG - [기재정정] '+kind+' (자회사의 주요경영사항)']:
                self.assertEqual(excluded_title_reason(title),'title_type:'+kind,title)
            self.assertEqual(excluded_title_reason('LG [기재정정] '+kind,'LG'),'title_type:'+kind)
        self.assertEqual(reporting_company('[기재정정] (유) LG화학 - 공급계약'),'LG화학')

    def test_nearby_normal_titles_not_excluded(self):
        for title in ['LG - 특수관계인과의자금거래','LG - 특수관계인과의보험거래변경',
                      'LG - 의결권대리행사권유참고서류관련안내',
                      'LG - 주주명부폐쇄기간또는기준일설정취소','LG - 공급계약',
                      'LG - [보험거래관련] 투자판단 관련 주요경영사항']:
            self.assertIsNone(excluded_title_reason(title),title)

    def test_inside_target_alias_and_explicit_codes(self):
        for target in ['LG화학','(주) LG화학','LG화학 주식회사','엘에스일렉트릭',
                       'LSELECTRIC','LG화학 (종목코드: 051910)']:
            result=self.decision('자회사인 | '+target+' | 의 주요경영사항신고 계약 상대방은 SK하이닉스다.')
            self.assertTrue(result['exclude'],target)
        result=self.decision('종속회사인 | LG화학 | 의 주요경영사항 신고',
                             title='LG - 공급계약 (종속회사의 주요경영사항)')
        self.assertEqual(result['target_codes'],['051910'])
        synthetic=[dict(c, dart_corp_code='00123456') if c['stock_code']=='051910' else c
                   for c in self.companies]
        result=subsidiary_decision('LG - 공급계약 (자회사의 주요경영사항)',
            '자회사인 | LG화학 (DART 기업코드: 00123456) | 의 주요경영사항신고',
            'LG',synthetic,self.aliases)
        self.assertTrue(result['exclude'])

    def test_outside_unknown_and_body_mentions_do_not_exclude(self):
        for body in ['자회사인 | 비상장외부회사 | 의 주요경영사항신고 LG화학과 계약했다.',
                     '자회사인 | 외부회사 (종목코드: 999999) | 의 주요경영사항신고',
                     '제출인 LG. 주요내용에서 LG화학과 SK하이닉스를 언급했다.',
                     '인수 대상은 LG화학이다. 발행회사 회사명 LG화학.',
                     '자회사인 | SK하이닉스소재 | 의 주요경영사항신고']:
            self.assertFalse(self.decision(body)['exclude'],body)
        self.assertEqual(self.decision('식별 근거 없음')['reason'],'subsidiary_target_unidentified')

    def test_direct_subsidiary_keeps_existing_path(self):
        result=self.decision('자회사인 | LG화학 | 의 주요경영사항신고',reporter='LG화학')
        self.assertFalse(result['exclude'])
        self.assertEqual(result['reason'],'subsidiary_direct_submission')
        self.assertFalse(self.decision('LG화학은 계약했다.',reporter='LG화학',title='LG화학 - 공급계약')['exclude'])

    def test_multiple_nested_and_conflicting_identification_keep(self):
        for body in ['자회사인 | LG화학 및 외부회사 | 의 주요경영사항신고',
                     '자회사인 | LG화학 외 1개사 | 의 주요경영사항신고',
                     '자회사인 | LG화학 | 의 주요경영사항신고 종속회사인 | 외부회사 | 의 주요경영사항신고',
                     '자회사인 | LG화학 | 의 주요경영사항신고 자회사인 | SK하이닉스 | 의 주요경영사항신고',
                     '자회사인 | LG화학 (종목코드: 000660) | 의 주요경영사항신고']:
            self.assertFalse(self.decision(body)['exclude'],body)
        self.assertEqual(self.decision('자회사인 | LG화학 및 외부회사 | 의 주요경영사항신고')['reason'],
                         'subsidiary_multiple_targets')
        self.assertEqual(self.decision('자회사인 | LG화학 | 의 주요경영사항신고 종속회사인 | 외부회사 | 의 주요경영사항신고')['reason'],
                         'subsidiary_multiple_targets')
        repeated='자회사인 | LG화학 | 의 주요경영사항신고 '*2
        self.assertTrue(self.decision(repeated)['exclude'])


if __name__=='__main__':unittest.main()
