🚀 라즈베리파이 5 주식 공시 AI 비서 프로젝트 (DART Monitor)
이 프로젝트는 DART(전자공시시스템)를 실시간 감시하여 특정 기업의 중요 공시를 AI로 요약하고 텔레그램으로 전송하는 자동화 시스템입니다.

1. 개발 환경 및 폴더 구조
기기: Raspberry Pi 5 4GB (NVMe SSD 사용)

OS: Debian GNU/Linux (Lite 64bit)

언어: Python 3.13.5

경로: ~/workspace/dart_monitor/

Plaintext

/home/pi5dart/workspace/
└── dart_monitor/           # 공시 감시 메인 프로젝트
    ├── dart.py             # 실시간 감시 및 AI 요약 실행 엔진
    ├── disclosure_rules.py # 공시 유형별 분석 규칙 및 원문 추출 로직
    ├── config.json         # API 키 및 텔레그램 설정값
    ├── companies_JM.txt    # 모니터링 그룹 1 (종목 리스트)
    ├── companies_ChemHold.txt # 모니터링 그룹 2 (화학/지주 리스트)
    ├── exclude_keywords.txt # 알림에서 제외할 키워드 목록
    └── venv/               # 파이썬 가상환경
2. 핵심 기능 및 로직
실시간 감시: DART RSS(todayRSS.xml)를 주기적으로 체크하여 새로운 공시를 탐지합니다.

원문 분석: 공시 번호(rcp_no)를 이용해 DART 서버에서 직접 ZIP 파일을 다운로드하고, 내부의 HTML 표 구조를 텍스트로 추출합니다.

AI 요약: 추출된 본문 데이터를 OpenRouter API(xiaomi/mimo-v2-flash:free 모델)에 전달하여 핵심 내용을 팩트체크 형식으로 요약합니다.

필터링 전송: disclosure_rules.py에 정의된 규칙에 따라 '수주 공시', '실적 발표' 등을 우선 구분하며, 설정된 종목 리스트에 따라 채널을 분리하여 전송합니다.

3. 설정 정보 (config.json)
프로그램 실행을 위해 다음 API 키와 채널 ID가 config.json에 올바르게 입력되어야 합니다.

DART_API_KEY: 공시 원문 다운로드용 키

OPENROUTER_API_KEY: AI 요약용 키

TELEGRAM_TOKEN: 알림 봇 토큰

CHAT_ID_JM / CHAT_ID_CHEM: 각 텔레그램 채널 아이디

4. 실행 및 관리 방법
프로그램 실행
Bash

cd ~/workspace/dart_monitor
source venv/bin/activate
python3 dart.py
주의 사항
중복 전송 방지: dart.py는 last_processed_link 변수를 통해 마지막 처리된 공시를 기억합니다. 프로그램 재시작 시 중복 알림이 올 수 있습니다.

원문 추출 제한: DART API의 문서 다운로드 용량 제한이나 본문 구조의 복잡성에 따라 AI 요약 품질이 달라질 수 있습니다.
