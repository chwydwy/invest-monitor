import json
import requests
import re
import os
import time
from datetime import datetime
import disclosure_rules 

# 1. 설정 및 파일 로드
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

def load_list(filename):
    if os.path.exists(filename):
        with open(filename, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    return []

# 2. OpenRouter AI 요약 함수 (모델명 복구 및 무한 루프 적용)
def get_ai_summary(prompt):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['OPENROUTER_API_KEY']}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "inclusionai/ling-3.0-flash-fin:free", # 원래 모델로 복구
        "messages": [{"role": "user", "content": prompt}]
    }
    
    # 지연 시 답변이 올 때까지 무한 재시도
    while True:
        try:
            # 타임아웃 60초로 확장
            response = requests.post(url, headers=headers, data=json.dumps(data), timeout=60)
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result:
                    content = result['choices'][0]['message']['content'].strip()
                    # 정상적인 결과가 오면 반환
                    return content
            elif response.status_code == 429:
                print("⏳ AI 서버 부하(429). 1분 후 다시 시도합니다...")
            else:
                print(f"⏳ AI 지연 중(Status: {response.status_code}). 1분 후 다시 시도합니다.")
        except Exception as e:
            print(f"⚠️ AI 호출 에러: {e}. 1분 후 재시도합니다.")
        
        time.sleep(60)

# 3. 텔레그램 전송 함수
def send_telegram(chat_id, message, channel_name):
    test_message = f"⚠️ [테스트 중 - {channel_name}] ⚠️\n{message}"
    url = f"https://api.telegram.org/bot{config['TELEGRAM_TOKEN']}/sendMessage"
    data = {"chat_id": chat_id, "text": test_message}
    try:
        requests.post(url, data=data)
        print(f"✅ 텔레그램 전송 완료: {channel_name} (ID: {chat_id})")
    except Exception as e:
        print(f"텔레그램 전송 에러 ({channel_name}): {e}")

# --- 테스트 실행 함수 ---
def run_simulation():
    print("\n=== DART 통합 로직 테스트 (무한루프 & SCORE 추출 적용) ===")
    rcp_no = input("1. DART 접수번호(rcpNo) 입력: ").strip()
    original_title = input("2. 공시 제목 입력: ").strip()
    submitter = input("3. 제출인(creator) 입력: ").strip()

    link = f"https://dart.fss.or.kr/api/link.jsp?rcpNo={rcp_no}"
    
    clean_title = re.sub(r'^\([^)]+\)\s*', '', original_title).strip()
    if " - " in clean_title:
        title_company = clean_title.split(" - ")[0].strip()
    else:
        title_company = clean_title.split(' ')[0].strip()

    exclude_list = load_list('exclude_keywords.txt')
    if any(kw in clean_title for kw in exclude_list):
        print("❌ 결과: 제외 키워드 포함")
        return

    jm_list = load_list('companies_JM.txt')
    chem_list = load_list('companies_ChemHold.txt')
    bl_list = load_list('companies_BAMK_BL.txt')
    priority_keywords = load_list('priority_keywords.txt')
    
    is_market_notice = "유가증권시장" in submitter or "코스닥시장" in submitter
    
    def check_list(target_list, company, subm, market_flag):
        for com in target_list:
            if com == company:
                return (com == subm or market_flag)
        return False

    is_in_jm = check_list(jm_list, title_company, submitter, is_market_notice)
    is_in_chem = check_list(chem_list, title_company, submitter, is_market_notice)
    is_in_bl = check_list(bl_list, title_company, submitter, is_market_notice)

    if not (is_in_jm or is_in_chem or is_in_bl):
        print(f"❌ 결과: 대상 리스트에 없거나 제출인 불일치")
        return

    print("🔍 본문 추출 중...")
    raw_content = disclosure_rules.get_raw_text(config['DART_API_KEY'], rcp_no)
    
    if raw_content:
        caution_exclude = ["현물배당", "매출액또는손익구조", "주요경영사항"]
        is_priority = (any(pk in clean_title for pk in priority_keywords) or \
                      any(pk in raw_content for pk in priority_keywords)) and \
                      not any(ex in clean_title for ex in caution_exclude)
        
        status_msg, applied_prompt = disclosure_rules.generate_custom_prompt(raw_content, clean_title)
        print(f"[{status_msg}] AI 분석 요청 중 (지연 시 무한 대기)...")
        
        # AI 요약 결과 대기
        summary = get_ai_summary(applied_prompt)
        summary = summary.replace("**", "").replace("*", "")
        
        # [수정] SCORE 추출 및 본문 정제 로직
        score_match = re.search(r'SCORE:\s*(\d+)', summary, re.IGNORECASE)
        score = score_match.group(1) if score_match else "0"
        summary_clean = re.sub(r'SCORE:\s*\d+', '', summary, flags=re.IGNORECASE).strip()
        
        has_no_send = "NO_SEND" in summary_clean
        
        # [수정] 메시지 포맷 통합 (SCORE 포함)
        msg = f"{clean_title}\n\n🤖 AI 분석({score}):\n{summary_clean}\n\n🔗 {link}"

        if not has_no_send:
            if is_in_jm: send_telegram(config['CHAT_ID_JM'], msg, "JM")
            if is_in_chem: send_telegram(config['CHAT_ID_CHEM'], msg, "ChemHold")
            if is_in_bl: send_telegram(config['CHAT_ID_BL'], msg, "BAMK_BL 일반")

        if is_in_bl and is_priority:
            if has_no_send:
                msg = f"❗ [중요 키워드 포착]\n{clean_title}\n\n알림: AI가 수치상 중요도가 낮다고 판단했으나 설정된 키워드가 발견되어 전송합니다.\n\n🔗 {link}"
            send_telegram(config['CHAT_ID_BL_Caution'], msg, "BAMK_BL 중요")
    else:
        print("❌ 에러: 원문 추출 실패.")

if __name__ == "__main__":
    run_simulation()
