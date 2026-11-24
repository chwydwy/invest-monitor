"""Exact disclosure-type exclusions and explicitly declared subsidiary targets."""
import re
import unicodedata

EXCLUDED_TITLE_TYPES = frozenset({
    '특수관계인과의보험거래',
    '의결권대리행사권유참고서류',
    '주주명부폐쇄기간또는기준일설정',
})
CORRECTION_PREFIX = re.compile(r'^(?:\[(?:기재정정|첨부정정|정정)\]|\((?:기재정정|첨부정정|정정)\))')


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))


def reporting_company(title):
    value=title.strip()
    while True:
        stripped=re.sub(r'^\([^)]+\)\s*','',value).strip()
        stripped=re.sub(r'^\[(?:기재정정|첨부정정|정정)\]\s*','',stripped).strip()
        if stripped==value:
            break
        value=stripped
    return re.split(r'\s+[-–—]\s+',value,maxsplit=1)[0].strip() if re.search(r'\s+[-–—]\s+',value) else value.split()[0] if value else ''


def title_type(title, company=None):
    value = re.split(r'\s+[-–—]\s+', title, maxsplit=1)[-1]
    if value==title and company:
        value=re.sub(r'^(?:\[(?:기재정정|첨부정정|정정)\]\s*)+','',value).strip()
        value=re.sub(r'^'+re.escape(company)+r'\s+','',value,count=1)
    value = compact(value)
    while CORRECTION_PREFIX.match(value):
        value = CORRECTION_PREFIX.sub('', value, count=1)
    return re.sub(r'\((?:자회사|종속회사)의주요경영사항\)$', '', value)


def excluded_title_reason(title, company=None):
    kind = title_type(title,company)
    return 'title_type:' + kind if kind in EXCLUDED_TITLE_TYPES else None


def _name_key(name):
    name = compact(name).strip('|\'"「」“”')
    name = re.sub(r'^(?:주식회사|\(주\))', '', name)
    name = re.sub(r'(?:주식회사|\(주\))$', '', name)
    return name.casefold()


def subsidiary_decision(title, body, reporter, companies, aliases):
    """Only the declaration '자회사인 X 의 주요경영사항신고' is evidence.

    Never select arbitrary companies mentioned in the report or acquisition
    target fields. Multiple/nested declarations are retained for review.
    """
    if not re.search(r'(?:자회사|종속회사)의주요경영사항', compact(title)):
        return dict(exclude=False, reason='not_subsidiary_notice', target_codes=[])
    by_name = {}
    by_code = {c['stock_code']: c for c in companies if c.get('stock_code')}
    by_dart = {c['dart_corp_code']: c['stock_code'] for c in companies
               if c.get('dart_corp_code') and c.get('stock_code')}
    def register(name, code):
        if name:
            by_name.setdefault(_name_key(name), set()).add(code)
    for code, row in by_code.items():
        register(row['name'], code)
    for row in aliases['companies']:
        code = row.get('stock_code')
        if code in by_code:
            for name in [row['name'], *row.get('aliases', []), row.get('legal_name')]:
                register(name, code)
    # clean_dart_text retains table cell separators but collapses whitespace.
    declarations = re.findall(r'(?:종속회사|자회사)인\s*\|?\s*(.{1,220}?)\s*\|?\s*의\s*주요\s*경영\s*사항\s*신고',
                              body, flags=re.S)
    codes, declared, unidentified, multiple = set(), set(), False, False
    if not declarations:
        return dict(exclude=False, reason='subsidiary_target_unidentified', target_codes=[])
    for declaration in declarations:
        parts = re.split(r'\s*(?:\||,|;|/|및)\s*', declaration.strip(' |\n'))
        if len(parts) > 1 or re.search(r'외\s*\d+\s*(?:개)?사|등\s*\d+\s*개사', declaration):
            multiple = True
        for part in parts:
            # Codes must be explicitly labelled, never numbers elsewhere in text.
            stock = re.search(r'(?:종목코드|주권단축코드|단축코드)\s*[:：]?\s*([0-9A-Z]{6})\b', part, re.I)
            dart = re.search(r'(?:DART\s*기업코드|기업코드|고유번호)\s*[:：]?\s*(\d{8})\b', part, re.I)
            name = re.sub(r'[\(\[]?(?:종목코드|주권단축코드|단축코드|DART\s*기업코드|기업코드|고유번호)\s*[:：]?\s*[0-9A-Z]{6,8}[\)\]]?', '', part, flags=re.I).strip()
            named = by_name.get(_name_key(name), set())
            coded = stock[1].upper() if stock else by_dart.get(dart[1]) if dart else None
            if len(named)==1 and (not coded or coded in named):
                declared.add(('code', next(iter(named))))
            elif coded and not named:
                declared.add(('code',coded))
            else:
                declared.add(('name',_name_key(name)))
            if len(named) > 1 or (named and coded and coded not in named):
                unidentified = True
            elif named:
                codes.update(named)
            elif coded:
                codes.add(coded)
            else:
                unidentified = True
    if multiple or len(codes) > 1 or len(declared)>1:
        reason = 'subsidiary_multiple_targets'
    elif unidentified or not codes:
        reason = 'subsidiary_target_unidentified'
    else:
        reporter_codes = by_name.get(_name_key(reporter), set())
        if codes & reporter_codes:
            reason = 'subsidiary_direct_submission'
        elif codes <= by_code.keys() and len(reporter_codes) == 1:
            return dict(exclude=True, reason='subsidiary_in_universe', target_codes=sorted(codes))
        else:
            reason = 'subsidiary_outside_universe' if not codes & by_code.keys() else 'subsidiary_reporter_unidentified'
    return dict(exclude=False, reason=reason, target_codes=sorted(codes))
