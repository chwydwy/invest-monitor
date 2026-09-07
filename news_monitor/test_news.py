import json

from news import (
    MAPPING_FILE,
    find_similar_sent_article,
    get_ai_summary,
    get_article_body,
    get_latest_news_by_date,
    load_daily_sent_cache,
    send_telegram,
)

TEST_MODEL = "minimax/minimax-m3:free"


def print_ai_result(company_name, title, body):
    decision, summary = get_ai_summary(company_name, title, body, model=TEST_MODEL)
    print(f"AI 판정: {decision}")
    if decision == "SEND":
        print(f"AI 요약:\n{summary}")
    return decision, summary


def run_article_test():
    company_name = input("테스트할 기업명: ").strip()
    try:
        with open(MAPPING_FILE, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ 기업 매핑을 읽을 수 없습니다: {type(e).__name__}: {e}")
        return

    company_code = mapping.get(company_name)
    if not company_code:
        print(f"❌ 매핑에 없는 기업명입니다: {company_name}")
        return

    print("🚀 [통합 테스트] 운영 뉴스 수집·중복 검사·AI 판정 시작...")
    news_list = get_latest_news_by_date(company_code)
    if not news_list:
        print("❌ 최근 24시간 이내의 기사를 찾을 수 없습니다.")
        return

    news = news_list[0]
    print(f"대상 기업: {company_name}({company_code})")
    print(f"기사 제목: {news['title']}")

    body, final_link = get_article_body(news['office_id'], news['article_id'])
    if not body:
        print("❌ 본문 수집 실패")
        return
    print(f"본문 수집 성공: {len(body)}자")

    # 운영 상태는 기록하지 않고, 당일 전송 캐시만 읽기 전용으로 비교한다.
    duplicate = find_similar_sent_article(news['title'], body, load_daily_sent_cache(prune_expired=False))
    print(f"유사도 중복 판정: {'중복' if duplicate else '비중복'}")
    if duplicate:
        return

    decision, summary = print_ai_result(company_name, news['title'], body)
    if decision == "NO_SEND":
        return

    send_choice = input("실제 Telegram 테스트 메시지를 전송할까요? [y/N]: ").strip().lower()
    if send_choice == 'y':
        if send_telegram(company_name, news['title'], summary, final_link, test_mode=True):
            print("✅ [테스트] Telegram 전송 성공")
        else:
            print("❌ [테스트] Telegram 전송 실패")
    else:
        print("Telegram 전송을 건너뛰었습니다.")


def read_article_body():
    print("기사 본문을 입력하세요. 빈 줄을 입력하면 완료됩니다.")
    lines = []
    while True:
        line = input()
        if not line:
            return "\n".join(lines).strip()
        lines.append(line)


def run_manual_ai_test():
    company_name = input("대상 기업명: ").strip()
    title = input("기사 제목: ").strip()
    body = read_article_body()
    if not company_name or not title or not body:
        print("❌ 기업명, 제목, 본문을 모두 입력해야 합니다.")
        return

    print("🚀 [수동 AI 판정 테스트] Telegram 및 운영 기록을 사용하지 않습니다.")
    print_ai_result(company_name, title, body)


def run_final_test():
    print("1. 실제 네이버 기사 테스트")
    print("2. 제목/본문 직접 입력 테스트")
    mode = input("선택 (1/2): ").strip()
    if mode == '1':
        run_article_test()
    elif mode == '2':
        run_manual_ai_test()
    else:
        print("❌ 1 또는 2를 입력하세요.")


if __name__ == "__main__":
    run_final_test()
