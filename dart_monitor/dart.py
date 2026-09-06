import os
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
import requests
import re
from datetime import datetime
import disclosure_rules # 규칙 파일 임포트

# 1. 설정 및 파일 로드
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

def load_list(filename):
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    return []

# 2. OpenRouter AI 요약 함수 (지연 신호 반환값 수정)
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
        response = requests.post(url, headers=headers, data=json.dumps(data), timeout=60)
        if response.status_code == 200:
            result = response.json()
            if 'choices' in result:
                return result['choices'][0]['message']['content'].strip()
        elif response.status_code == 429:
            return "AI_DELAY_RETRY"
    except Exception as e:
        print(f"AI 호출 에러: {e}")
    return "AI_DELAY_RETRY"

# 3. 텔레그램 전송 함수
def send_telegram(chat_id, message):
    url = f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"텔레그램 전송 에러: {e}")

# 4. 메인 루프
last_processed_link = None
print("🚀 4개 채널 통합 및 AI 필터링 적용 DART 감시 시작...")

ns = {'dc': 'http://purl.org/dc/elements/1.1/'}

while True:
    now = datetime.now()
    is_business_hour = now.weekday() < 5 and 8 <= now.hour < 19

    try:
        rss_url = "https://dart.fss.or.kr/api/todayRSS.xml"
        response = urllib.request.urlopen(rss_url).read()
        root = ET.fromstring(response)
        entries = root.findall(".//item")

        if last_processed_link is None and entries:
            last_processed_link = entries[0].find("link").text

        new_entries = []
        for entry in entries:
            link = entry.find("link").text
            if link == last_processed_link:
                break
            new_entries.append(entry)

        if new_entries:
            new_entries.reverse()

            jm_list = load_list('companies_JM.txt')
            chem_list = load_list('companies_ChemHold.txt')
            bl_list = load_list('companies_BAMK_BL.txt')
            exclude_list = load_list('exclude_keywords.txt')
            priority_keywords = load_list('priority_keywords.txt')

            for entry in new_entries:
                original_title = entry.find("title").text
                link = entry.find("link").text
                submitter = entry.find("dc:creator", ns).text.strip() if entry.find("dc:creator", ns) is not None else ""

                clean_title = re.sub(r'^\([^)]+\)\s*', '', original_title).strip()
                if " - " in clean_title:
                    title_company = clean_title.split(" - ")[0].strip()
                else:
                    title_company = clean_title.split(' ')[0].strip()

                if any(kw in clean_title for kw in exclude_list): continue
                if "증권" in clean_title:
                    if any(bkw in clean_title for bkw in ["증권발행", "투자설명", "일괄신고"]): continue

                is_market_notice = "유가증권시장" in submitter or "코스닥시장" in submitter

                def check_list(target_list, company, subm, market_flag):
                    for com in target_list:
                        if com == company:
                            return (com == subm or market_flag)
                    return False

                is_in_jm = check_list(jm_list, title_company, submitter, is_market_notice)
                is_in_chem = check_list(chem_list, title_company, submitter, is_market_notice)
                is_in_bl = check_list(bl_list, title_company, submitter, is_market_notice)

                if is_in_jm or is_in_chem or is_in_bl:
                    print(f"[{now.strftime('%H:%M:%S')}] 🔔 탐지: {title_company}")
                    
                    rcp_no_match = re.search(r'rcpNo=(\d+)', link)
                    rcp_no = rcp_no_match.group(1) if rcp_no_match else None
                    
                    if rcp_no:
                        raw_content = disclosure_rules.get_raw_text(config['DART_API_KEY'], rcp_no)
                        if raw_content:
                            caution_exclude = ["현물배당", "매출액또는손익구조", "주요경영사항"]
                            
                            is_priority = (any(pk in clean_title for pk in priority_keywords) or \
                                          any(pk in raw_content for pk in priority_keywords)) and \
                                          not any(ex in clean_title for ex in caution_exclude)
                            
                            status_msg, applied_prompt = disclosure_rules.generate_custom_prompt(raw_content, clean_title)
                            print(f"[{now.strftime('%H:%M:%S')}] {status_msg}")
                            
                            # [핵심 수정 1] AI 지연 및 언어 오류 발생 시 답변 올 때까지 무한 루프
                            while True:
                                summary = get_ai_summary(applied_prompt)
                                if summary == "AI_DELAY_RETRY" or "AI 분석 지연" in summary:
                                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⏳ AI 지연 발생. 1분 후 재시도...")
                                    time.sleep(60)
                                    continue
                                
                                # [추가 수정 사항 2] 영어 답변 비중 체크 (전체 글자의 50% 이상이 영어면 재시도)
                                if len(summary) > 0:
                                    eng_chars = len(re.findall(r'[a-zA-Z]', summary))
                                    if eng_chars > (len(summary) * 0.5):
                                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ AI 답변 언어 오류(영어 비중 과다). 5초 후 재시도...")
                                        time.sleep(5)
                                        continue
                                
                                break
                            
                            summary = summary.replace("**", "").replace("*", "")
                            
                            score_match = re.search(r'SCORE:\s*(\d+)', summary, re.IGNORECASE)
                            score = score_match.group(1) if score_match else "0"
                            summary_clean = re.sub(r'SCORE:\s*\d+', '', summary, flags=re.IGNORECASE).strip()
                            
                            has_no_send = "NO_SEND" in summary_clean

                            msg = f"{clean_title}\n\n🤖 AI 분석({score}):\n{summary_clean}\n\n🔗 {link}"
                            
                            if not has_no_send:
                                if is_in_jm: send_telegram(config['CHAT_ID_JM'], msg)
                                if is_in_chem: send_telegram(config['CHAT_ID_CHEM'], msg)
                                if is_in_bl: send_telegram(config['CHAT_ID_BL'], msg)
                            
                            if is_in_bl and is_priority:
                                if has_no_send:
                                    msg = f"❗ [중요 키워드 포착]\n{clean_title}\n\n알림: AI가 중요도가 낮다고 판단했으나 설정된 키워드가 발견되어 전송합니다.\n\n🔗 {link}"
                                send_telegram(config['CHAT_ID_BL_Caution'], msg)

                        else:
                            print(f"[{now.strftime('%H:%M:%S')}] ⚠️ 원문 추출 실패")
                            if is_in_bl:
                                fail_msg = f"{clean_title}\n\n내용: 원문을 가져오지 못해 AI 분석을 수행하지 못했습니다.\n\n🔗 {link}"
                                send_telegram(config['CHAT_ID_BL'], fail_msg)
                    
                    time.sleep(1)

            last_processed_link = entries[0].find("link").text

    except Exception as e:
        print(f"에러 발생: {e}")

    wait_time = 30 if is_business_hour else 3600
    print(f"[{now.strftime('%H:%M:%S')}] 대기: {'30초' if is_business_hour else '1시간'}")
    time.sleep(wait_time)