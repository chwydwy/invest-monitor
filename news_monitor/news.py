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

def get_ai_summary(company, title, body, model=None):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": model or "nvidia/nemotron-3-ultra-550b-a55b:free",
        "messages": [
            {"role": "system", "content": "너는 대상 기업을 분석하는 주식 뉴스 분석가다. 투자자의 기업가치 및 향후 실적 판단에 실질적으로 가치 있는 기사만 SEND로 판정한다. 실적과 전망, 매출·이익·비용, 제품 가격·판매량·가동률, 수주·공급계약·고객사, 증설·CAPEX·공장, 신규 사업·핵심 전략, M&A·매각·분할·합병, 배당·자사주·증자·감자, 지배구조·중요 경영진 변화, 직접 영향이 있는 정책·규제, 중요한 소송·사고·파업·생산 차질, 경쟁구도 변화는 SEND 후보이다. 단순 주가 등락, 시장 시황의 단순 언급, 채용, 봉사·기부·사회공헌, 행사·수상, 홍보성 기사, 투자판단과 무관한 임직원 소식, 주변 사례로만 언급된 기사는 NO_SEND다. 단 이런 유형이라도 대상 기업의 실적·비용·전략·규제·중대한 평판 리스크에 직접 영향이 있으면 SEND한다. 대상 기업이 핵심 주체가 아니어도 사업·실적·기업가치에 직접 영향이 있으면 SEND한다. 반드시 한국어를 사용하고, 출력은 정확히 둘 중 하나만 사용한다. 제외는 첫 줄과 전체 응답을 NO_SEND만으로 작성한다. 전송은 첫 줄 SEND, 이후 한국어 2~3줄 핵심 요약만 작성한다. 요약에는 기사 본문에 명시된 사실만 사용한다. 요약의 각 문장은 본문에 직접 근거가 있어야 하며, 본문에 없는 계산·기간 환산·평가·인과관계를 추가하지 않는다. 날짜 범위를 '몇 년'으로 환산하거나, 계약 체결 사실에서 매출·수익·가동률·실적이 확보된다고 추론하지 않는다. 본문에 두 가지 사실만 있으면 그 두 사실만 요약하고 효과나 의미를 덧붙이지 않는다. AI 자체의 전망·투자 의견·가치판단·주가 영향 추정은 절대 추가하지 않는다. 특히 '기대된다', '전망이다', '성장 동력', '기업가치에 영향', '주시할 필요', '가시성이 높아진다', '투자 판단에 유의미' 같은 해석 문구를 본문에 없는 한 쓰지 않는다. 기사에서 회사나 관계자가 직접 제시한 전망은 '회사는 ~라고 밝혔다'처럼 출처를 유지한다. 미확정·검토·협의 중인 내용은 확정된 것처럼 바꾸지 않는다. 숫자, 계약 규모, 일정, 생산능력 등 중요 정보는 가능한 한 보존한다. 답변을 내기 전에 각 문장이 본문 사실인지 점검하고, 근거 없는 문장은 삭제한다. 마크다운 강조 기호는 쓰지 않는다." + AI_RELEVANCE_RULES},
            {"role": "user", "content": f"대상 기업명: {company}\n기사 제목: {title}\n기사 본문:\n{body[:2000]}"}
        ]
    }
    for attempt in range(1, 4):
        retry_delay = 60
        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                content = (response.json()['choices'][0]['message']['content'] or '').strip()
                lines = content.splitlines()
                decision = lines[0].strip().upper() if lines else ""
                summary = "\n".join(lines[1:]).strip()

                if decision == "NO_SEND" and len(lines) == 1:
                    return "NO_SEND", ""
                if decision == "SEND" and summary and is_korean_summary(summary):
                    return "SEND", summary

                print(f"ai_format_error attempt={attempt}", file=sys.stderr)
                retry_delay = 2
            else:
                print(f"ai_http_error status={response.status_code} attempt={attempt}", file=sys.stderr)
                if response.status_code in (400, 401, 403, 404):
                    break

        except Exception as e:
            print(f"ai_request_error type={type(e).__name__} attempt={attempt}", file=sys.stderr)
        if attempt < 3:
            time.sleep(retry_delay)
    print("ai_attempts_exhausted", file=sys.stderr)
    return None, ""

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
    with open(TARGET_LIST_FILE, 'r', encoding='utf-8') as f:
        target_names = [line.strip() for line in f if line.strip()]
    with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
        mapping = json.load(f)
    company_names = load_company_names()
    monitored_identities = {company_names.get(name, name): name
                            for name in target_names if mapping.get(name)}
    listed_companies = None
    listing_date = None

    # [추가] 제외 리스트 로드
    exclude_keywords = load_exclude_list(EXCLUDE_KEYWORDS_FILE, ["코스피", "코스닥", "시황", "ETF"])
    exclude_press = load_exclude_list(EXCLUDE_PRESS_FILE, ["동행미디어"])

    # The legacy Naver endpoint stopped serving articles. Establish a durable
    # first-run boundary so switching sources cannot replay a day's backlog.
    news_cutover = load_or_create_cutover(NEWS_CUTOVER_FILE)
    print(f"news_source=npay targets={len(target_names)} cutover={news_cutover.isoformat(timespec='seconds')}", file=sys.stderr)

    while True:
        if not is_active_time():
            time.sleep(1800)
            continue

        sent_history = load_sent_history()
        daily_sent_cache = load_daily_sent_cache()
        if listing_date != today_key():
            try:
                listed_companies = fetch_listed_companies()
                listing_date = today_key()
                print(f"news_listing_loaded count={len(listed_companies)}", file=sys.stderr)
            except (requests.RequestException, ValueError) as exc:
                listed_companies = None
                print(f"news_listing_error type={type(exc).__name__}", file=sys.stderr)
        
        for name in target_names:
            code = mapping.get(name)
            if not code:
                print(f"news_mapping_missing company={name}", file=sys.stderr)
                continue
            
            news_list = get_latest_news_by_date(code, not_before=news_cutover)
            print(f"news_list company={name} code={code} recent={len(news_list)}", file=sys.stderr)
            
            if news_list:
                for news in reversed(news_list):
                    unique_id = news['id']
                    
                    if unique_id not in sent_history:
                        # [추가] 1. 제목 키워드 필터링
                        if any(kw in news['title'] for kw in exclude_keywords):
                            print(f"news_excluded article_id={unique_id} reason=keyword", file=sys.stderr)
                            sent_history.append(unique_id) # 다시 안 보게 기록은 함
                            save_sent_history(sent_history)
                            continue

                        # [추가] 2. 언론사 필터링
                        if any(pr in news['press'] for pr in exclude_press):
                            print(f"news_excluded article_id={unique_id} reason=press", file=sys.stderr)
                            sent_history.append(unique_id)
                            save_sent_history(sent_history)
                            continue

                        print(f"news_candidate company={name} article_id={unique_id}", file=sys.stderr)
                        body, final_link, article_time = get_article_body(
                            news['office_id'], news['article_id'], include_metadata=True)
                        
                        if body:
                            print(f"news_body_ok article_id={unique_id} chars={len(body)}", file=sys.stderr)
                            company, link_reason = resolve_article_company(
                                name, news['title'], body, monitored_identities,
                                company_names, listed_companies)
                            if not company:
                                state = "news_deferred" if link_reason == "listing_unverified" else "news_excluded"
                                print(f"{state} article_id={unique_id} reason={link_reason}", file=sys.stderr)
                                if link_reason != "listing_unverified":
                                    sent_history.append(unique_id)
                                    save_sent_history(sent_history)
                                continue
                            print(f"news_company article_id={unique_id} lookup={name} linked={company} reason={link_reason}", file=sys.stderr)
                            duplicate = find_similar_sent_article(news['title'], body, daily_sent_cache, company)
                            if duplicate:
                                print(f"news_excluded article_id={unique_id} reason=similarity rule={duplicate['reason']} title_ratio={duplicate['title_similarity']:.2f} body_ratio={duplicate['body_similarity']:.2f}", file=sys.stderr)
                                sent_history.append(unique_id)
                                save_sent_history(sent_history)
                                continue

                            decision, summary = get_ai_summary(company, news['title'], body)
                            if decision is None:
                                print(f"news_ai_deferred article_id={unique_id}", file=sys.stderr)
                                continue
                            if decision == "NO_SEND":
                                print(f"news_excluded article_id={unique_id} reason=ai_no_send", file=sys.stderr)
                                sent_history.append(unique_id)
                                save_sent_history(sent_history)
                                continue

                            print(f"news_ai_send article_id={unique_id}", file=sys.stderr)

                            published_at, published_at_original = choose_registration_time(
                                article_time, news)

                            queue_dashboard_news({
                                "source": "news", "source_id": unique_id,
                                "article_id": unique_id, "company": company, "stock_code": mapping[company],
                                "title": news['title'], "press": news['press'],
                                "published_at": published_at, "url": final_link,
                                "published_at_original": published_at_original,
                                "article_published_at_original": article_time.get('article_published_at_original'),
                                "npay_datetime_original": news.get('npay_datetime_raw'),
                                "article_modified_at_original": article_time.get('article_modified_at_original'),
                                "summary": summary, "monitor_groups": ["ChemHold"], "sector": None,
                            })

                            if not send_telegram(company, news['title'], summary, final_link):
                                print(f"news_telegram_retry article_id={unique_id}", file=sys.stderr)
                                continue

                            print(f"news_telegram_sent article_id={unique_id}", file=sys.stderr)

                            sent_history.append(unique_id)
                            save_sent_history(sent_history)
                            daily_sent_cache.append({
                                'date': today_key(),
                                'article_id': unique_id,
                                'company': company,
                                'title': news['title'],
                                'body': body[:2000],
                                'url': final_link,
                                'sent_at': datetime.now().isoformat(timespec='seconds')
                            })
                            save_daily_sent_cache(daily_sent_cache)
                            time.sleep(2)
            
            time.sleep(90)

if __name__ == "__main__":
    main()
