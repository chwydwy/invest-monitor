"""Existing production prompt and validation, without credentials or I/O."""
import re
from difflib import SequenceMatcher
from .news_quality import has_new_headline_fact, has_material_update, same_issue

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

def build_request(company, title, body, model=None):
    data = {
        "model": model or "nvidia/nemotron-3-ultra-550b-a55b:free",
        "messages": [
            {"role": "system", "content": "너는 대상 기업을 분석하는 주식 뉴스 분석가다. 투자자의 기업가치 및 향후 실적 판단에 실질적으로 가치 있는 기사만 SEND로 판정한다. 실적과 전망, 매출·이익·비용, 제품 가격·판매량·가동률, 수주·공급계약·고객사, 증설·CAPEX·공장, 신규 사업·핵심 전략, M&A·매각·분할·합병, 배당·자사주·증자·감자, 지배구조·중요 경영진 변화, 직접 영향이 있는 정책·규제, 중요한 소송·사고·파업·생산 차질, 경쟁구도 변화는 SEND 후보이다. 단순 주가 등락, 시장 시황의 단순 언급, 채용, 봉사·기부·사회공헌, 행사·수상, 홍보성 기사, 투자판단과 무관한 임직원 소식, 주변 사례로만 언급된 기사는 NO_SEND다. 단 이런 유형이라도 대상 기업의 실적·비용·전략·규제·중대한 평판 리스크에 직접 영향이 있으면 SEND한다. 대상 기업이 핵심 주체가 아니어도 사업·실적·기업가치에 직접 영향이 있으면 SEND한다. 반드시 한국어를 사용하고, 출력은 정확히 둘 중 하나만 사용한다. 제외는 첫 줄과 전체 응답을 NO_SEND만으로 작성한다. 전송은 첫 줄 SEND, 이후 한국어 2~3줄 핵심 요약만 작성한다. 요약에는 기사 본문에 명시된 사실만 사용한다. 요약의 각 문장은 본문에 직접 근거가 있어야 하며, 본문에 없는 계산·기간 환산·평가·인과관계를 추가하지 않는다. 날짜 범위를 '몇 년'으로 환산하거나, 계약 체결 사실에서 매출·수익·가동률·실적이 확보된다고 추론하지 않는다. 본문에 두 가지 사실만 있으면 그 두 사실만 요약하고 효과나 의미를 덧붙이지 않는다. AI 자체의 전망·투자 의견·가치판단·주가 영향 추정은 절대 추가하지 않는다. 특히 '기대된다', '전망이다', '성장 동력', '기업가치에 영향', '주시할 필요', '가시성이 높아진다', '투자 판단에 유의미' 같은 해석 문구를 본문에 없는 한 쓰지 않는다. 기사에서 회사나 관계자가 직접 제시한 전망은 '회사는 ~라고 밝혔다'처럼 출처를 유지한다. 미확정·검토·협의 중인 내용은 확정된 것처럼 바꾸지 않는다. 숫자, 계약 규모, 일정, 생산능력 등 중요 정보는 가능한 한 보존한다. 답변을 내기 전에 각 문장이 본문 사실인지 점검하고, 근거 없는 문장은 삭제한다. 마크다운 강조 기호는 쓰지 않는다." + AI_RELEVANCE_RULES},
            {"role": "user", "content": f"대상 기업명: {company}\n기사 제목: {title}\n기사 본문:\n{body[:2000]}"}
        ]
    }
    return data

def parse_response(content):
    lines = content.strip().splitlines()
    decision = lines[0].strip().upper() if lines else ""
    summary = "\n".join(lines[1:]).strip()
    if decision == "NO_SEND" and len(lines) == 1:
        return decision, ""
    if decision == "SEND" and summary and is_korean_summary(summary):
        return decision, summary
    raise ValueError("invalid news AI response")
