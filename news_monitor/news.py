import json
import time
import os
import requests
import re
import sys
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from naver_news import load_or_create_cutover, parse_stock_news
from news_quality import (article_publication_time, choose_registration_time,
                          has_new_headline_fact,
                          has_material_update,
                          fetch_listed_companies, load_company_names,
                          resolve_article_company, same_issue)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dashboard_outbox import enqueue

DASHBOARD_DB = os.environ.get("DASHBOARD_OUTBOX_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard_outbox.sqlite3"))

def queue_dashboard_news(payload):
    try:
        enqueue(DASHBOARD_DB, payload)
    except Exception as exc:
        print(f"dashboard_queue_error type={type(exc).__name__}", file=sys.stderr)

# --- 파일 경로 설정 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_news.json")
MAPPING_FILE = os.path.join(BASE_DIR, "mapping_news.json")
TARGET_LIST_FILE = os.path.join(BASE_DIR, "Companies_ChemHold_news.txt")
NEWS_CUTOVER_FILE = os.path.join(os.path.expanduser("~"), ".local", "state", "invest-monitor", "news-npay-cutover.txt")
SENT_HISTORY_FILE = os.path.join(BASE_DIR, "sent_articles.json")
DAILY_SENT_CACHE_FILE = os.path.join(BASE_DIR, "daily_sent_cache.json")

# [추가] 필터링 파일 경로
EXCLUDE_KEYWORDS_FILE = os.path.join(BASE_DIR, "exclude_keywords.txt")
EXCLUDE_PRESS_FILE = os.path.join(BASE_DIR, "exclude_press.txt")

def load_config():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
TELEGRAM_TOKEN = config['TELEGRAM_TOKEN']
CHAT_ID = config['CHAT_ID_CHEM']
OPENROUTER_API_KEY = config['OPENROUTER_API_KEY']

def load_sent_history():
    if not os.path.exists(SENT_HISTORY_FILE): return []
    with open(SENT_HISTORY_FILE, 'r', encoding='utf-8') as f:
        try: return json.load(f)
        except: return []

def save_sent_history(history):
    if len(history) > 1000: history = history[-1000:]
    with open(SENT_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, indent=4)

def today_key():
    return datetime.now().strftime("%Y-%m-%d")

def cache_item_recent(item, now=None):
    """Compare sent items for a rolling day, including across midnight."""
    now = now or datetime.now()
    try:
        sent_at = datetime.fromisoformat(item['sent_at'])
        return timedelta(0) <= now - sent_at <= timedelta(hours=24)
    except (KeyError, TypeError, ValueError):
        return item.get('date') == now.strftime("%Y-%m-%d")

def load_daily_sent_cache(prune_expired=True):
    if not os.path.exists(DAILY_SENT_CACHE_FILE):
        return []

    try:
        with open(DAILY_SENT_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        if not isinstance(cache, list):
            raise ValueError("일별 전송 캐시 형식이 목록이 아닙니다.")

        today_cache = [item for item in cache if cache_item_recent(item)]
        if prune_expired and len(today_cache) != len(cache):
            save_daily_sent_cache(today_cache)
        return today_cache
    except Exception as e:
        print(f"⚠️ 일별 전송 캐시 로드 실패: {type(e).__name__}: {e}")
        return []

def save_daily_sent_cache(cache):
    with open(DAILY_SENT_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# [추가] 필터링 리스트 로드 함수
def load_exclude_list(filepath, default_items):
    if not os.path.exists(filepath):
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(default_items))
        return default_items
    with open(filepath, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]

def is_active_time():
    now = datetime.now()
    if now.weekday() >= 5: return now.hour == 13
    return 6 <= now.hour < 22

# --- 날짜 기반 뉴스 목록 가져오기 ---
def get_latest_news_by_date(code, not_before=None):
    url = "https://stock.naver.com/api/domestic/detail/news"
    headers = {
        "User-Agent": "invest-monitor-news/1.0",
        "Accept": "application/json",
        "Referer": f"https://stock.naver.com/domestic/stock/{code}/news",
    }
    one_day_ago = datetime.now() - timedelta(days=1)
    cutoff = max(one_day_ago, not_before) if not_before else one_day_ago
    try:
        res = requests.get(url, params={"itemCode": code, "page": 1, "pageSize": 15},
                           headers=headers, timeout=10)
        res.raise_for_status()
        return parse_stock_news(res.json(), cutoff)
    except (requests.RequestException, ValueError, TypeError) as exc:
        status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
        print(f"news_list_error code={code} status={status} type={type(exc).__name__}", file=sys.stderr)
        return []

def get_article_body(oid, aid, include_metadata=False):
    final_url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(final_url, headers=headers, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, 'html.parser')
        content = soup.select_one('#newsct_article') or \
                  soup.select_one('#articleBodyContents') or \
                  soup.select_one('.article_body')
        if content:
            body = content.get_text(separator="\n", strip=True)
            if include_metadata:
                published_at, original_time, modified_time = article_publication_time(soup)
                return body, final_url, {
                    "published_at": published_at,
                    "article_published_at_original": original_time,
                    "article_modified_at_original": modified_time,
                }
            return body, final_url
        print(f"news_body_missing article_id={oid}_{aid} status={res.status_code}", file=sys.stderr)
        return (None, final_url, {}) if include_metadata else (None, final_url)
    except requests.RequestException as exc:
        status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response is not None else None
        print(f"news_body_error article_id={oid}_{aid} status={status} type={type(exc).__name__}", file=sys.stderr)
        return (None, final_url, {}) if include_metadata else (None, final_url)

def normalize_for_comparison(text):
    text = re.sub(r'\s+', ' ', text or ' ')
    text = re.sub(r'[^\w가-힣 ]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip().lower()

def find_similar_sent_article(title, body, daily_sent_cache, company=None):
    normalized_title = normalize_for_comparison(title)
    normalized_body = normalize_for_comparison(body[:2000])
    if not normalized_title:
        return None

    for cached in daily_sent_cache:
        if company and cached.get('company') != company:
            continue
        cached_title = normalize_for_comparison(cached.get('title', ''))
        cached_body = normalize_for_comparison(cached.get('body', ''))
        if not cached_title:
            continue

        title_similarity = SequenceMatcher(None, normalized_title, cached_title).ratio()
        body_similarity = (
            SequenceMatcher(None, normalized_body, cached_body).ratio()
            if normalized_body and cached_body else 0.0
        )
        reason = None
        if has_new_headline_fact(title, cached.get('title', '')):
            continue
        if has_material_update(body, cached.get('body', '')):
            continue
        if normalized_title == cached_title:
            reason = "제목 동일"
        elif title_similarity >= 0.90:
            reason = "제목 유사도 0.90 이상"
        elif body_similarity >= 0.92:
            reason = "본문 유사도 0.92 이상"
        elif title_similarity >= 0.70 and body_similarity >= 0.85:
            reason = "제목/본문 유사도 기준 충족"
        elif same_issue(title, cached.get('title', ''), company, body,
                        cached.get('body', '')):
            reason = "동일 사건·제목 재작성"

        if reason:
            return {
                'cached': cached,
                'title_similarity': title_similarity,
                'body_similarity': body_similarity,
                'reason': reason
            }
    return None

# [추가] 한글 포함 여부 체크 함수
def contains_korean(text):
    return bool(re.search('[가-힣]', text))

def is_korean_summary(text):
    if not contains_korean(text):
        return False
    # 영문 회사명·제품명·약어는 허용하되, 한자권·일본어 문자는 한국어 요약으로 보지 않는다.
    return not bool(re.search(r'[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]', text))

AI_RELEVANCE_RULES = (
    " 조회 종목의 뉴스 목록에 실렸다는 사실만으로 대상 기업의 기사라고 판단하지 마라."
    " 기사 제목과 본문에서 확인된 대상 기업의 구체적인 새 사실만 평가하라."
    " 여러 소식을 묶은 기사에서는 대상 기업이 짧게 언급된 것만으로 SEND하지 마라."
    " 실적·재무·수주·투자·정책·지분·지배구조 등에서 새로 확인된 사실과"
    " 대상 기업에 대한 직접 관련성이 있어야 SEND한다."
    " 개인 관련 소식은 그 자체로 제외하지 말고 기업의 지분·지배구조 등에"
    " 직접적인 새 영향이 기사에 명시됐는지 판단하라."
    " 과거 사실의 반복 설명만 있다면 NO_SEND한다."
    " 기사에 없는 영향이나 인과관계는 추정하지 마라."
)

def get_ai_summary(*args, **kwargs):
    raise RuntimeError("Direct AI disabled; submit through pipeline_runtime.py")


def send_telegram(company, title, summary, link, test_mode=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    prefix = "[테스트] " if test_mode else ""
    message = f"<b>{prefix}[{company}]</b>\n{title}\n\n{summary.replace('**','')}\n\n🔗 <a href='{link}'>뉴스 원문 보기</a>"
    try:
        response = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        result = response.json()
        if response.status_code == 200 and result.get('ok') is True:
            return True
        print(f"telegram_error status={response.status_code} error_code={result.get('error_code')}", file=sys.stderr)
    except Exception as e:
        print(f"telegram_request_error type={type(e).__name__}", file=sys.stderr)
    return False

def main():
    from pipeline_runtime import main as run
    run(default_role="news")

if __name__ == "__main__":
    main()
