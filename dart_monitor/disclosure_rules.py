import requests
import re
import zipfile
import io
import warnings
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# BeautifulSoup의 XML/HTML 혼용 관련 경고 메시지 무시
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# --- [공통: 텍스트 정제 함수] ---
def clean_dart_text(raw_data):
    if not raw_data:
        return ""
    soup = BeautifulSoup(raw_data, 'html.parser')
    for s in soup(['style', 'script']):
        s.decompose()
    for tr in soup.find_all('tr'):
        tr.append('\n')
    for td in soup.find_all('td'):
        td.append(' | ')
    text = soup.get_text()
    text = re.sub(r'&nbsp;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()

# --- [공통 도구: dcmNo 추출 (Web Viewer용)] ---
def get_dcm_no_from_web(rcp_no):
    main_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
    try:
        resp = requests.get(main_url, timeout=10)
        pattern = r"viewDoc\(['\"]" + rcp_no + r"['\"]\s*,\s*['\"](\d+)['\"]"
        match = re.search(pattern, resp.text)
        return match.group(1) if match else None
    except:
        return None

# --- [핵심: 3단계 하이브리드 원문 추출] ---
def get_raw_text(api_key, rcp_no):
    api_url = f"https://opendart.fss.or.kr/api/document.xml?crtfc_key={api_key}&rcept_no={rcp_no}"
    try:
        response = requests.get(api_url, timeout=15)
        if response.status_code != 200:
            return None
        
        if response.content.startswith(b'PK'):
            all_text = []
            with zipfile.ZipFile(io.BytesIO(response.content)) as z:
                for file_name in z.namelist():
                    if file_name.endswith(('.xml', '.html')):
                        with z.open(file_name) as f:
                            data = f.read()
                            try:
                                raw = data.decode('utf-8')
                            except:
                                raw = data.decode('euc-kr', errors='ignore')
                            cleaned = clean_dart_text(raw)
                            if len(cleaned) > 50:
                                all_text.append(cleaned)
            final_text = "\n\n".join(all_text)
            return final_text if len(final_text) > 50 else None
        
        try:
            raw_data = response.content.decode('utf-8')
        except:
            raw_data = response.content.decode('euc-kr', errors='ignore')
        
        if "파일이 존재하지 않습니다" not in raw_data and "미지원" not in raw_data:
            cleaned = clean_dart_text(raw_data)
            if len(cleaned) > 200:
                return cleaned
                
        dcm_no = get_dcm_no_from_web(rcp_no)
        if dcm_no:
            viewer_url = f"https://dart.fss.or.kr/report/viewer.do?rcpNo={rcp_no}&dcmNo={dcm_no}&eleId=0&offset=0&length=0&dtd=HTML"
            v_resp = requests.get(viewer_url, timeout=10)
            cleaned = clean_dart_text(v_resp.text)
            return cleaned if len(cleaned) > 50 else None
            
    except Exception as e:
        print(f"❌ 원문 추출 실패: {type(e).__name__}")
    return None

# --- [메인 엔진: 하이브리드 프롬프트 생성] ---
def generate_custom_prompt(clean_text, title, *, classify_importance=False):
    if not clean_text or len(clean_text) < 50:
        return "⚠️ 본문 부족", None

    importance_rule = (
        "4. 마지막 줄에 IMPORTANCE: high 또는 IMPORTANCE: normal을 정확히 한 번 쓰세요. 점수는 쓰지 마세요.\n"
        "   high(매우 중요): 합병·분할·거래정지, 기업 규모 대비 중대한 공급계약, 지배구조나 자본구조의 중대한 변화 등 투자 판단을 다시 볼 만한 새 사실입니다. 본문에서 거래 규모, 조건, 확정 수준을 확인하세요. 제목이나 유형만으로 high를 정하지 마세요.\n"
        "   normal(일반): 그 밖의 전송 대상 공시입니다. 기재정정·첨부정정은 기존 공시가 알려진 상태로 보고 정정 전후의 실제 차이만 평가하세요. 계약금액, 거래 조건, 성사 여부 또는 중요한 일정의 실질적 변경은 high가 될 수 있습니다. 오탈자, 형식, 단순 첨부 교체는 normal입니다. 첨부에서도 확인된 실질 변경은 반영하되 확인되지 않은 차이는 추정하지 마세요.\n\n"
    ) if classify_importance else (
        "4. **가장 마지막 줄에 'SCORE: [점수]' 형태로 이 공시의 중요도를 0에서 10 사이로 매기세요.**\n"
        "   - 중요도 기준: 분할, 합병, 거래정지, 비상장으로 분할 등 펀드투자 컴플라이언스 위배 소지가 있거나 주가에 영향을 많이 줄 것으로 예상되어 투자자가 반드시 유의해야 하는 정도.\n\n"
    )
    base_instruction = (
        f"당신은 전문 금융 분석가입니다. 제공된 [공시 본문]을 분석하여 투자자가 핵심을 즉시 파악할 수 있게 정리하세요.\n"
        f"**절대 제목만 보고 유추하지 마십시오.** 본문 내의 사실(Fact)과 수치만을 근거로 삼으세요.\n\n"
        f"[공시 제목]: {title}\n"
        f"--- [공시 본문 시작] ---\n"
        f"{clean_text[:4500]}\n"
        f"--- [공시 본문 끝] ---\n\n"
        f"[출력 형식 지침]\n"
        f"1. 첫 부분은 별도의 제목 없이, 공시의 핵심 사실과 결론을 2-3줄의 문장으로 서술하세요. 원문에 명시되지 않은 기업 영향은 쓰지 마세요.\n"
        f"2. 그다음은 별도의 제목 없이, 본문 내 주요 데이터(수치, 금액, 기간, 명칭 등)를 '-' 기호를 사용하여 목록 형태로 나열하세요. **반드시 '항목명: 수치' 형식을 지켜서 해당 숫자가 무엇을 의미하는지 구체적으로 적으세요. 설명 없이 숫자만 나열하는 것은 엄격히 금지합니다.** (중요한 데이터만 5~6가지 이하로)\n"
        f"3. 마지막 부분에 '투자자 유의사항:'이라는 제목을 쓰고 줄바꿈 후에 원문에 명시된 미확정 사항이나 특이사항을 1-2줄로 적으세요. 원문에 없는 리스크 수준이나 전망은 쓰지 마세요.\n"
        f"{importance_rule}"
        f"[주의 사항]\n"
        f"- 공시 원문에 명시된 사실만 요약하세요. 주가 영향, 투자 판단, 리스크 수준, 확정 가능성, 회사 의도나 전망을 스스로 추론하지 마세요.\n"
        f"- 미확정 공시는 '검토 중', '결정된 사항 없음', '추후 재공시 예정' 등 원문의 확정 수준과 표현을 그대로 유지하세요. 원문에 없는 수치와 날짜를 만들거나 기간을 임의로 계산하지 마세요.\n"
        f"- 회사나 증권사의 전망·평가가 원문에 직접 있다면 '회사에 따르면', '증권사는 ~라고 평가했다'처럼 출처를 명시하고, AI 자신의 판단처럼 쓰지 마세요.\n"
        f"- 각 문장과 숫자를 원문과 대조하고, 직접 근거가 없는 내용은 삭제하세요. 중요도 판단을 요약에 추론으로 덧붙이지 마세요.\n"
        f"- 답변 시 마크다운(Markdown) 형식을 절대로 사용하지 마세요.\n"
        f"- 답변 중간에 한자를 사용하지 마세요.\n"
        f"- 섹션 구분 시 숫자를 쓰지 말고 줄바꿈만 활용하세요.\n"
    )

    extra_instructions = []
    status_tags = []

    if "최대주주등소유주식변동신고서" in title:
        extra_instructions.append(
            "- [중요 판단] 이 공시는 지분율 변동 보고서입니다. 본문의 '변동 전'과 '변동 후' 지분율 차이(절대값)를 계산하세요.\n"
            "  👉 **만약 지분율 변동폭이 0.5%p 미만이라면, 다른 말은 일절 하지 말고 오직 'NO_SEND' 라고만 답변하세요.**\n"
            "  👉 0.5%p 이상 변동일 경우에만: 변동 주체, 변동 전/후 지분율, 변동 사유(장내매수/매도 등)를 요약하세요."
        )
        status_tags.append("📊지분계산")

    if "기재정정" in title:
        extra_instructions.append("- [중요] 이 공시는 기존 내용을 수정하는 '기재정정' 건입니다. 무엇이 어떻게 정정되었는지(전/후 비교)를 최우선으로 분석하세요.")
        status_tags.append("🔧정정")
    
    if "단일판매" in title or "공급계약" in title:
        extra_instructions.append("- [중요] 공급계약 건입니다. 주요 데이터 및 항목 정리에서 계약상대(업체명), 계약내용, 금액, 매출액 대비 비중, 계약기간을 명확히 추출하세요.")
        status_tags.append("🎯계약")
        
    if "자회사의 주요경영사항" in title:
        extra_instructions.append("- [중요] 자회사 관련 공시입니다. 해당 '자회사 이름'이 어디인지 분석 내용에 반드시 포함하세요.")
        status_tags.append("🏢자회사")

    if "영업(잠정)실적" in title:
        extra_instructions.append("- [중요] 실적 발표 관련 공시입니다. 오직 주요 재무데이터(매출액, 영업이익, 당기순이익 등)만 '-' 목록 형태로 정리하세요.")
        status_tags.append("실적")

    conditional_part = "\n" + "\n".join(extra_instructions) if extra_instructions else ""
    final_prompt = base_instruction + conditional_part
    final_status = f"📄 [{'+'.join(status_tags) if status_tags else '일반'}] AI 정밀 분석 중..."

    return final_status, final_prompt


def parse_importance(content):
    if content.strip() == 'NO_SEND':
        return None, ''
    lines = content.strip().splitlines()
    summary = '\n'.join(lines[:-1]).strip()
    marker = re.fullmatch(r'IMPORTANCE: (high|normal)', lines[-1].strip()) if lines else None
    if (not marker or not re.search('[가-힣]', summary) or
            re.search(r'(?im)^\s*(?:IMPORTANCE|SCORE)\s*:|\bNO_SEND\b', summary)):
        raise ValueError('invalid DART importance response')
    return marker[1], summary
