import json
import time
import os
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

# --- 설정 로드 (기본 news.py와 동일) ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_news.json")

def load_config():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
TELEGRAM_TOKEN = config['TELEGRAM_TOKEN']
CHAT_ID = config['CHAT_ID_CHEM']
OPENROUTER_API_KEY = config['OPENROUTER_API_KEY']

# --- 핵심 함수들 (news.py와 동일 로직) ---

def get_latest_news_by_date(code):
    url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": f"https://finance.naver.com/item/news.naver?code={code}"
    }
    two_days_ago = datetime.now() - timedelta(days=2)
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        news_list = []
        rows = soup.select('table.type5 tbody tr')
        
        for row in rows:
            if row.select_one('span.ico_reply'): continue
            title_tag = row.select_one('td.title a')
            date_tag = row.select_one('td.date')
            if not title_tag or not date_tag: continue
            
            try:
                article_date = datetime.strptime(date_tag.get_text(strip=True), "%Y.%m.%d %H:%M")
            except: continue

            if article_date >= two_days_ago:
                link = "https://finance.naver.com" + title_tag['href']
                qs = parse_qs(urlparse(link).query)
                aid, oid = qs.get('article_id', [''])[0], qs.get('office_id', [''])[0]
                news_list.append({
                    'id': f"{oid}_{aid}",
                    'title': title_tag.get_text(strip=True),
                    'office_id': oid,
                    'article_id': aid,
                    'date': article_date
                })
        return news_list
    except: return []

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

def get_ai_summary(text):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "inclusionai/ling-3.0-flash-fin:free",
        "messages": [
            {"role": "system", "content": "너는 주식 뉴스 분석가야. 2~3줄로 핵심 요약해줘. 강조기호 빼고."},
            {"role": "user", "content": f"요약:\n\n{text[:2000]}"}
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            return response.json()['choices'][0]['message']['content'].strip()
    except: pass
    return "요약 실패"

def send_telegram(company, title, summary, link):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    summary_clean = summary.replace("**", "").replace("*", "")
    message = f"<b>[{company}] TEST</b>\n{title}\n\n{summary_clean}\n\n🔗 <a href='{link}'>뉴스 원문 보기</a>"
    try:
        res = requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return res.status_code == 200
    except: return False

# --- 테스트 실행 로직 ---
def run_lg_test():
    company_name = "LG화학"
    company_code = "051910"
    
    print(f"🔍 {company_name}({company_code}) 최근 2일 뉴스 검색 중...")
    
    news_list = get_latest_news_by_date(company_code)
    
    if not news_list:
        print("❌ 최근 2일 이내의 기사가 없습니다.")
        return

    print(f"✅ 총 {len(news_list)}개의 기사를 발견했습니다. 전송을 시작합니다.\n")

    # 과거 기사부터 순서대로 테스트 전송
    for news in reversed(news_list):
        print(f"📰 처리 중: {news['title']} ({news['date']})")
        
        # 1. 본문 추출
        body, final_link = get_article_body(news['office_id'], news['article_id'])
        
        if body:
            # 2. AI 요약
            print("🤖 AI 요약 중...")
            summary = get_ai_summary(body)
            
            # 3. 텔레그램 전송
            print("📤 텔레그램 전송 중...")
            success = send_telegram(company_name, news['title'], summary, final_link)
            
            if success:
                print(f"✨ 전송 성공: {news['id']}\n")
            else:
                print(f"⚠️ 전송 실패: {news['id']}\n")
        
        time.sleep(1) # 테스트 간 짧은 간격

if __name__ == "__main__":
    run_lg_test()
