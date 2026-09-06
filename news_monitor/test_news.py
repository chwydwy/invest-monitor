import json
import os
import requests
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

# 1. 설정 로드
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config_news.json")

with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    config = json.load(f)

# 2. OpenRouter AI 요약 함수 (공시 프로그램 방식: 무한 재시도)
def get_ai_summary(prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "nvidia/nemotron-3-nano-30b-a3b:free",
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        # 타임아웃 60초 설정
        response = requests.post(url, headers=headers, json=data, timeout=60)
        if response.status_code == 200:
            result = response.json()
            if 'choices' in result:
                return result['choices'][0]['message']['content'].strip()
        elif response.status_code == 429:
            return "AI_DELAY_RETRY"
    except Exception as e:
        print(f"⚠️ AI 호출 에러: {e}")
    return "AI_DELAY_RETRY"

# 3. 텔레그램 전송 함수
def send_telegram(message):
    url = f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage"
    data = {
        "chat_id": config['CHAT_ID_CHEM'],
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=data)
    except Exception as e:
        print(f"⚠️ 텔레그램 전송 에러: {e}")

# 4. 실행
def run_final_test():
    print("🚀 [최종 테스트] 뉴스 수집 및 AI 요약 시작...")
    code = "014830"
    list_url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Referer": f"https://finance.naver.com/item/news.naver?code={code}"
    }

    try:
        # 뉴스 리스트에서 기사 링크 가져오기
        res = requests.get(list_url, headers=headers)
        res.encoding = 'euc-kr'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        title_tag = soup.select_one('td.title a') # 첫 번째 기사
        if not title_tag:
            print("❌ 기사 링크를 찾을 수 없습니다.")
            return

        title = title_tag.get_text(strip=True)
        f_link = "https://finance.naver.com" + title_tag['href']
        
        # 기사 ID 추출하여 원문 URL 조립
        qs = parse_qs(urlparse(f_link).query)
        oid, aid = qs.get('office_id',[''])[0], qs.get('article_id',[''])[0]
        final_url = f"https://n.news.naver.com/mnews/article/{oid}/{aid}"
        
        # 본문 추출
        res_b = requests.get(final_url, headers=headers)
        soup_b = BeautifulSoup(res_b.text, 'html.parser')
        content = soup_b.select_one('#newsct_article') or soup_b.select_one('#articleBodyContents')
        
        if not content:
            print("❌ 본문을 추출할 수 없습니다.")
            return
        
        body_text = content.get_text(separator="\n", strip=True)
        print(f"✅ 기사 확보: {title}")

        # AI 요약 (공시 프로그램의 무한 루프 적용)
        prompt = f"다음 뉴스 기사를 2~3줄로 핵심 요약해줘. 강조 기호(**)는 빼고 텍스트만 줘:\n\n{body_text[:2000]}"
        
        while True:
            print("🤖 AI 요약 시도 중 (답변 대기)...")
            summary = get_ai_summary(prompt)
            if summary == "AI_DELAY_RETRY":
                print(f"⏳ [{time.strftime('%H:%M:%S')}] AI 지연 발생. 1분 후 재시도...")
                time.sleep(60)
                continue
            break
        
        # 텔레그램 전송
        msg = f"<b>[유니드]</b>\n{title}\n\n{summary}\n\n🔗 <a href='{final_url}'>기사 원문 보기</a>"
        send_telegram(msg)
        print("🎉 전송 완료! 텔레그램을 확인하세요.")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")

if __name__ == "__main__":
    run_final_test()