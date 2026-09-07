from news import (
    find_similar_sent_article,
    get_ai_summary,
    get_article_body,
    get_latest_news_by_date,
    load_daily_sent_cache,
    send_telegram,
)


def run_lg_test():
    company_name = "LG화학"
    company_code = "051910"
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

    decision, summary = get_ai_summary(company_name, news['title'], body)
    print(f"AI 판정: {decision}")
    if decision == "NO_SEND":
        return
    print(f"AI 요약:\n{summary}")

    send_choice = input("실제 Telegram 테스트 메시지를 전송할까요? [y/N]: ").strip().lower()
    if send_choice == "y":
        if send_telegram(company_name, news['title'], summary, final_link, test_mode=True):
            print("✅ [테스트] Telegram 전송 성공")
        else:
            print("❌ [테스트] Telegram 전송 실패")
    else:
        print("Telegram 전송을 건너뛰었습니다.")


if __name__ == "__main__":
    run_lg_test()
