import json
import time
import os
import requests
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

# --- 파일 경로 설정 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_news.json")
MAPPING_FILE = os.path.join(BASE_DIR, "mapping_news.json")
TARGET_LIST_FILE = os.path.join(BASE_DIR, "Companies_ChemHold_news.txt")
SENT_HISTORY_FILE = os.path.join(BASE_DIR, "sent_articles.json")

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
def get_latest_news_by_date(code):
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": f"https://finance.naver.com/item/news.naver?code={code}"
    }
    
    one_day_ago = datetime.now() - timedelta(days=1)
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        news_list = []
        rows = soup.select('table.type5 tbody tr')
        
        for row in rows:
            if row.select_one('span.ico_reply'): continue
            
            title_tag = row.select_one('td.title a')
            press_tag = row.select_one('td.info') # [수정] 언론사 정보 추출
            date_tag = row.select_one('td.date')
            if not title_tag or not date_tag: continue
            
            try:
                article_date = datetime.strptime(date_tag.get_text(strip=True), "%Y.%m.%d %H:%M")
            except:
                continue

            if article_date >= one_day_ago:
                link = "https://finance.naver.com" + title_tag['href']
                qs = parse_qs(urlparse(link).query)
                aid, oid = qs.get('article_id', [''])[0], qs.get('office_id', [''])[0]
                
                news_list.append({
                    'id': f"{oid}_{aid}",
                    'title': title_tag.get_text(strip=True),
                    'press': press_tag.get_text(strip=True) if press_tag else "알수없음", # [추가]
                    'office_id': oid,
                    'article_id': aid,
                    'date': article_date
                })
        return news_list
    except:
        return []

def get_article_body(oid, aid):
    final_url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = requests.get(final_url, headers=headers, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        content = soup.select_one('#newsct_article') or \
                  soup.select_one('#articleBodyContents') or \
                  soup.select_one('.article_body')
        if content: return content.get_text(separator="\n", strip=True), final_url
        return None, final_url
    except: return None, final_url

# [추가] 한글 포함 여부 체크 함수
def contains_korean(text):
    return bool(re.search('[가-힣]', text))

def get_ai_summary(text):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "nvidia/nemotron-3-nano-30b-a3b:free",
        "messages": [
            {"role": "system", "content": "너는 주식 뉴스 분석가야. 반드시 한국어로만 대답해. 2~3줄로 핵심 요약해줘. 강조기호 빼고."},
            {"role": "user", "content": f"요약:\n\n{text[:2000]}"}
        ]
    }
    while True:
        try:
            response = requests.post(url, headers=headers, json=data, timeout=60)
            if response.status_code == 200:
                summary = response.json()['choices'][0]['message']['content'].strip()
                # [수정] 한글이 포함되어 있는지 확인
                if contains_korean(summary):
                    return summary
                else:
                    print("⚠️ AI 답변에 한글이 없음. 재시도 중...")
                    time.sleep(2)
                    continue
            print(f"⏳ AI 지연 발생. 1분 후 재시도...")
            time.sleep(60)
        except: time.sleep(60)

def send_telegram(company, title, summary, link):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    message = f"<b>[{company}]</b>\n{title}\n\n{summary.replace('**','')}\n\n🔗 <a href='{link}'>뉴스 원문 보기</a>"
    try: requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

def main():
    with open(TARGET_LIST_FILE, 'r', encoding='utf-8') as f:
        target_names = [line.strip() for line in f if line.strip()]
    with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
        mapping = json.load(f)

    # [추가] 제외 리스트 로드
    exclude_keywords = load_exclude_list(EXCLUDE_KEYWORDS_FILE, ["코스피", "코스닥", "시황", "ETF"])
    exclude_press = load_exclude_list(EXCLUDE_PRESS_FILE, ["동행미디어"])
        
    print(f"🚀 모니터링 시작: {len(target_names)}개 기업 (필터링 적용)")

    while True:
        if not is_active_time():
            time.sleep(1800)
            continue

        sent_history = load_sent_history()
        
        for name in target_names:
            code = mapping.get(name)
            if not code: continue
            
            news_list = get_latest_news_by_date(code)
            
            if news_list:
                for news in reversed(news_list):
                    unique_id = news['id']
                    
                    if unique_id not in sent_history:
                        # [추가] 1. 제목 키워드 필터링
                        if any(kw in news['title'] for kw in exclude_keywords):
                            print(f"⏩ 키워드 제외: {news['title']}")
                            sent_history.append(unique_id) # 다시 안 보게 기록은 함
                            save_sent_history(sent_history)
                            continue

                        # [추가] 2. 언론사 필터링
                        if any(pr in news['press'] for pr in exclude_press):
                            print(f"⏩ 언론사 제외: {news['press']} - {news['title']}")
                            sent_history.append(unique_id)
                            save_sent_history(sent_history)
                            continue

                        print(f"🔔 신규 뉴스 발견: [{name}] {news['title']}")
                        body, final_link = get_article_body(news['office_id'], news['article_id'])
                        
                        if body:
                            summary = get_ai_summary(body)
                            send_telegram(name, news['title'], summary, final_link)
                            
                            sent_history.append(unique_id)
                            save_sent_history(sent_history)
                            time.sleep(2)
            
            time.sleep(90)

if __name__ == "__main__":
    main()