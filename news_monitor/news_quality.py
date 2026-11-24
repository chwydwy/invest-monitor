"""Conservative article identity, publication time, and issue checks."""

import json
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
PARTICLE = r"(?:은|는|이|가|을|를|의|에|에서|에게|와|과|으로|로|도|만|측|발)"
DIGEST_TITLE = re.compile(r"이\s*시각\s*헤드라인|주요\s*뉴스|뉴스\s*브리핑|오늘의\s*뉴스|뉴스\s*모음|한눈에\s*보는")
MATERIAL_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:조|억원?|만원?|%|GW|MW|톤|대|년|월|일)", re.I)
UPDATE_SIGNAL = re.compile(r"추가로|신규|확정|최종 결정|변경|상향|하향|취소|철회|증액|감액")
STATUS_WORDS = {
    "검토": ("검토", "협의", "논의", "추진"),
    "확정": ("확정", "결정", "체결", "완료"),
    "취소": ("취소", "철회", "중단"),
}
ACTION_WORDS = {
    "담보해지": ("담보 해지", "담보 해제", "담보 풀", "담보에서 빠", "담보였던"),
    "공급계약": ("공급계약", "공급 계약", "공급 체결", "납품계약"),
    "투자": ("투자", "증설", "CAPEX"),
    "매각": ("매각", "매도"),
    "인수": ("인수", "매입"),
    "합병": ("합병",),
    "배당": ("배당",),
    "희망퇴직": ("희망퇴직",),
    "가동중단": ("가동 중단", "가동중단", "생산 중단"),
}


@lru_cache(maxsize=1024)
def _company_pattern(name):
    # Short names must not match the start of a longer company name.
    return re.compile(r"(?<![가-힣A-Za-z0-9])" + re.escape(name) +
                      r"(?=$|[^가-힣A-Za-z0-9]|" + PARTICLE + r"($|[^가-힣A-Za-z0-9]))", re.I)


def load_company_names():
    """Use confirmed stock-code identities; relationships remain separate."""
    catalog = json.loads((ROOT / "DASHBOARD_COMPANIES.json").read_text(encoding="utf-8"))
    aliases = json.loads((ROOT / "DASHBOARD_COMPANY_ALIASES.json").read_text(encoding="utf-8"))
    names = {row["name"]: row["name"] for row in catalog["companies"]}
    codes = {row["stock_code"]: row["name"] for row in catalog["companies"] if row.get("stock_code")}
    for row in aliases["companies"]:
        canonical = codes.get(row.get("stock_code"))
        if canonical:
            for alias in (row["name"], *row["aliases"]):
                if alias in names and names[alias] != canonical:
                    raise ValueError("Conflicting company alias")
                names[alias] = canonical
    return names


def _mentions(text, names):
    hits = []
    lowered = text.lower()
    for spelling, company in names.items():
        if spelling.lower() not in lowered:
            continue
        for match in _company_pattern(spelling).finditer(text):
            hits.append((match.start(), match.end(), company, spelling))
    # Longest spelling owns an overlap, preventing SK within SK하이닉스.
    hits.sort(key=lambda hit: (-(hit[1] - hit[0]), hit[0]))
    chosen = []
    for hit in hits:
        if not any(hit[0] < other[1] and other[0] < hit[1] for other in chosen):
            chosen.append(hit)
    return chosen


def _primary_subject(text, names):
    """Conservative headline/lead subject; mentions later in a clause do not count."""
    text = re.sub(r'^\s*(?:\[[^\]]*\]\s*)+', '', text).strip()
    hits = _mentions(text, names)
    leading = [hit for hit in hits if hit[0] == 0]
    if not leading:
        return None
    hit = leading[0]
    tail = text[hit[1]:]
    # A name used as a comparison, source, object or joint mention is not a
    # reliable sole subject. Ambiguous cases continue to the existing AI.
    if re.match(r'(?:와|과|에\s|에서|에게|을|를|의|처럼|보다)', tail):
        return None
    if re.match(r'(?:은|는|이|가)(?=\s|[,，:：])|\s*[,，:：]|\s+[^\s]', tail):
        return hit[2]
    return None


def resolve_article_company(lookup_company, title, body, monitored_companies,
                            names=None, sectors=None):
    """Only holding-sector lookups exclude another universe company's subject."""
    if DIGEST_TITLE.search(title):
        return None, "digest_title"
    names = names if names is not None else load_company_names()
    lookup_identity = names.get(lookup_company, lookup_company)
    monitored = set(monitored_companies)
    if sectors is None:
        catalog = json.loads((ROOT / "DASHBOARD_COMPANIES.json").read_text(encoding="utf-8"))
        sectors = {row['name']: row.get('sector') for row in catalog['companies'] if row.get('stock_code')}
    if lookup_identity not in monitored or sectors.get(lookup_identity) != '지주':
        return lookup_company, "ai_relevance_check"
    title_subject = _primary_subject(title, names)
    # A clear lookup-company headline wins over incidental body mentions.
    subject = title_subject
    if subject is None:
        lead = re.split(r'(?<=[.!?。])\s+|\n+', body[:500])[0].strip()
        subject = _primary_subject(lead, names)
    if subject and subject != lookup_identity and subject in monitored:
        return None, "holding_other_universe_subject"
    return lookup_company, "ai_relevance_check"


def article_publication_time(soup):
    """Read article registration time; keep modification time distinct."""
    tag = soup.select_one("[data-date-time]")
    raw = tag.get("data-date-time") if tag else None
    modified = soup.select_one("[data-modify-date-time]")
    modified_raw = modified.get("data-modify-date-time") if modified else None
    if not raw:
        return None, None, modified_raw
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", raw):
            return None, raw, modified_raw
        published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=KST)
        return published.isoformat(timespec="seconds"), raw, modified_raw
    except (TypeError, ValueError):
        pass
    return None, raw, modified_raw


def choose_registration_time(article_time, news):
    """Article registration wins; the Npay list is the dated fallback."""
    if article_time.get("published_at"):
        return (article_time["published_at"],
                article_time["article_published_at_original"])
    return news.get("published_at"), news.get("npay_datetime_raw")


def _numbers(text):
    return {re.sub(r"[\s,]", "", x).lower() for x in MATERIAL_NUMBER.findall(text)}


def has_new_headline_fact(title, prior_title):
    """A new material number or changed completion status needs review as news."""
    if _numbers(title) - _numbers(prior_title):
        return True
    new_status = {key for key, words in STATUS_WORDS.items() if any(w in title for w in words)}
    old_status = {key for key, words in STATUS_WORDS.items() if any(w in prior_title for w in words)}
    return bool(new_status - old_status)

def has_material_update(body, prior_body):
    for confirmed in ("확정", "결정", "체결", "완료", "취소", "철회"):
        if confirmed in body[:250] and confirmed not in prior_body:
            return True
    previous_numbers = _numbers(prior_body)
    return any(_numbers(body[max(0, match.start() - 45):match.end() + 45])
               - previous_numbers for match in UPDATE_SIGNAL.finditer(body[:500]))


def _actions(title):
    return {action for action, words in ACTION_WORDS.items()
            if any(word.lower() in title.lower() for word in words)}


def same_issue(title, prior_title, company=None, body="", prior_body=""):
    """Recognize rewritten headlines with a shared action and subject."""
    if not ((_actions(title) | _actions(body[:500])) &
            (_actions(prior_title) | _actions(prior_body[:500]))):
        return False
    if company:
        title = title.replace(company, "")
        prior_title = prior_title.replace(company, "")
    left = re.sub(r"[^가-힣a-z0-9]", "", title.lower())
    right = re.sub(r"[^가-힣a-z0-9]", "", prior_title.lower())
    # A five-character subject anchor plus the same action is safer than
    # grouping every investment or contract headline for one company.
    return any(left[i:i + 5] in right for i in range(max(0, len(left) - 4)))
