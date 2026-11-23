"""Conservative article identity, publication time, and issue checks."""

import json
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
KIND_LIST_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
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


def fetch_listed_companies():
    """KIND's full KOSPI/KOSDAQ/KONEX list, fetched once per local day."""
    response = requests.get(KIND_LIST_URL, timeout=20,
                            headers={"User-Agent": "invest-monitor-news/1.0"})
    response.raise_for_status()
    soup = BeautifulSoup(response.content.decode("euc-kr", errors="replace"), "html.parser")
    listed = {}
    for row in soup.select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.select("td")]
        if len(cells) >= 3 and re.fullmatch(r"[0-9A-Z]{6}", cells[2]):
            listed[cells[0]] = cells[2]
    if len(listed) < 2700 or listed.get("SK하이닉스") != "000660":
        raise ValueError("KIND listing format or completeness changed")
    return listed


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


def _prefixed_subject(lookup_company, title):
    pattern = re.compile(r"(?<![가-힣A-Za-z0-9])" + re.escape(lookup_company) +
                         r"[가-힣A-Za-z0-9]{2,16}", re.I)
    for match in pattern.finditer(title):
        candidate = match.group()
        candidate = re.sub(r"(?:에서는|에게는|으로는|에서|에게|으로|은|는|이|가|을|를|의|에|와|과|로|도|만)$", "", candidate)
        if candidate != lookup_company:
            return candidate
    return None


def resolve_article_company(lookup_company, title, body, monitored_companies,
                            names=None, listed_companies=None):
    """Keep unlisted subsidiary news; exclude a different listed subject."""
    if DIGEST_TITLE.search(title):
        return None, "digest_title"
    names = names if names is not None else load_company_names()
    expanded = dict(names)
    if listed_companies is not None:
        for listed_name in listed_companies:
            expanded.setdefault(listed_name, listed_name)
    monitored = (monitored_companies if isinstance(monitored_companies, dict)
                 else {name: name for name in monitored_companies})
    title_hits = _mentions(title, expanded)
    body_hits = _mentions(body[:2000], expanded)
    title_targets = {hit[2] for hit in title_hits if hit[2] in monitored}
    title_other = {hit[2] for hit in title_hits if hit[2] not in monitored}
    body_targets = {hit[2] for hit in body_hits if hit[2] in monitored}
    body_other = {hit[2] for hit in body_hits if hit[2] not in monitored}

    if len(title_targets) > 1:
        return None, "multiple_companies"
    if title_targets:
        if title_other:
            return None, "ambiguous_company"
        target = monitored[next(iter(title_targets))]
        if target != lookup_company:
            return None, "other_listed_company"
        if body_other and not body_targets:
            return None, "other_listed_company"
        return target, "title_company"
    if title_other:
        return None, "other_listed_company"
    subsidiary = _prefixed_subject(lookup_company, title)
    if subsidiary:
        if listed_companies is None:
            return None, "listing_unverified"
        if subsidiary in listed_companies:
            return None, "other_listed_company"
        return lookup_company, "unlisted_subject"
    if len(body_targets) == 1 and not body_other:
        target = monitored[next(iter(body_targets))]
        if target != lookup_company:
            return None, "other_listed_company"
        return target, "body_company"
    if len(body_targets) > 1:
        return None, "multiple_companies"
    if body_other:
        return None, "other_listed_company"
    subsidiary = _prefixed_subject(lookup_company, body[:500])
    if subsidiary:
        if listed_companies is None:
            return None, "listing_unverified"
        if subsidiary in listed_companies:
            return None, "other_listed_company"
        return lookup_company, "unlisted_subject_body"
    return None, "company_unverified"


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
