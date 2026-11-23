# 공통 AI 처리 전환 — 로컬 연결 완료, 운영 미적용

2026-11-23. 이 문서가 이전 기반 모듈 초안의 미완료 목록을 대체한다. 로컬 검증 후 사용자의 운영 전환 승인을 받아 적용을 진행한다. 실제 시험용 AI 호출·가상 메시지 발송은 하지 않는다.

## 실제 연결된 경로

- `dart_monitor/dart.py` → `pipeline_runtime.py dart`: 당일 RSS 조회·기업/제출인/제목 필터 → SQLite discoveries 저장. 이 프로세스는 본문이나 AI 응답을 기다리지 않는다. 기존 월~금 08~19시 전 처리 후 30초, 나머지 1시간 조회 정책을 유지한다.
- `news_monitor/news.py` → `pipeline_runtime.py news`: 실제 BL·JM 원본 및 별칭으로 생성한 349개 합집합 → Npay 종목 목록 페이지 → discoveries 저장. 최소 요청 시작 간격 45초, 기본 기업당 최대 5페이지. 추가 페이지도 45초를 지킨다. 단순 합산 기본 대기는 4시간 21분 45초이고, 실제 순회 시간은 상태에 기록한다. 평일 06~22시 전, 주말 13시대 운영시간을 유지하고 비활성 시간에는 위치를 이동하지 않는다.
- `pipeline_runtime.py preprocess`: 별도 프로세스로 원문 추출 → 기존 제외 규칙·기업 연결·기존 이력·동일 이슈 비교 → jobs에 프롬프트와 문맥을 영속 저장한다. 본문 실패는 지수 대기로 재시도하고 5회 후 hold. 한 항목이 재시도 대기 중이면 다른 항목을 처리한다.
- `pipeline_runtime.py ai`: 유일한 OpenRouter HTTP 호출 위치. 파일 잠금과 DB 예약으로 1건씩 호출한다. 기존 뉴스 system/user prompt를 `news_monitor/ai_rules.py`로 그대로 옮겼으며 기존 커밋의 요청 JSON과 바이트 단위 동등성을 확인했다. DART는 기존 `generate_custom_prompt()`를 그대로 사용한다. 응답의 최종 content를 파싱하고 결과와 각 목적지 전송 의도를 원자적으로 저장한다.
- `pipeline_runtime.py deliver`: AI를 다시 호출하지 않고 목적지별로 저장된 결과를 전달한다. 대시보드 목적지는 기존 `dashboard_outbox.py.enqueue()`에 인계한다. 그 이후 외부 HTTP·stored/duplicate 확인은 기존 `dashboard_sender.py`와 기존 timer 역할이다. AI 출력의 dashboard=delivered는 **기존 대기열 인계 완료**이며 Worker 저장 성공을 뜻하지 않는다.
- 기존 독립 테스트 스크립트 `test_news.py`, `test_lg.py`, `test_dart.py`는 외부 호출 대신 임시 DB 종단 테스트를 실행한다. 기존 `news.get_ai_summary()`는 명시적 오류를 내므로 우회 호출이 불가능하다. 운영 뉴스·공시 및 테스트 폴더에 직접 OpenRouter HTTP 호출이 남지 않았음을 검색했다.

## 판단과 목적지

뉴스는 별도 상장사가 주체이면 조회 기업에서 제외한다. 비상장 자회사로 식별되면 조회 기업으로 AI 판단을 계속하고 NO_SEND는 전송하지 않는다. 자회사 예외 연결표나 다른 상장사로의 재연결은 없다. 상장 목록 확인 실패가 필요한 문맥은 미완료로 재시도한다. 한 기사에 여러 조회 기업의 문맥을 각각 보존하므로 잘못된 조회 기업에서 제외됐다는 이유로 올바른 문맥까지 전역 완료하지 않는다. 회사 이름에 공통 접두사가 없는 비상장 자회사 관계까지 자동 인식한다고 보장하지는 않는다.

- 뉴스 SEND: 전체 기업 대시보드 전달. 기존 화학지주 목록 소속만 기존 뉴스 Telegram 방에도 전달한다. 화학지주 외 뉴스 Telegram 경로는 없다.
- DART 일반 SEND: BL∪JM 전체 BL방, 화학지주 소속 화학지주방, 대시보드. 기본 두 방이 같은 ID면 한 번만 발송한다.
- DART 중요 키워드: 기존 주의방을 별도로 유지한다. NO_SEND여도 중요 키워드 안내가 주의방으로 전달될 수 있으며 이 경우 대시보드에는 전달하지 않는다.
- DART 원문 추출 실패: 기존처럼 BL방에 분석 불가 안내만 저장·전송하며 AI와 대시보드는 생략한다.
- 공시 파서에서는 정확한 `NO_SEND` 제어 응답을 영어 비중 오류로 재시도하지 않게 했다. 기존 코드는 이 영문 제어값도 영어 과다로 재시도하므로 NO_SEND 예외가 실행되지 않는 모순이 있었다. 프롬프트의 판단 기준이나 SCORE 임계치는 바꾸지 않았다.

기사 등록시간은 원문 등록시간 우선, Npay 등록시간 보완이며 수정시간과 원본을 별도 보존한다. 수신시간으로 대체하지 않는다. 수신 계약 필드와 event_id는 그대로다. 대시보드 뉴스 기간/정렬의 등록시간 기준 전환은 기존 DASHBOARD_HANDOFF.md의 상대 프로젝트 작업으로 남으며 이 저장소의 송신 준비와 구분한다.

## 예산·중복·장애

UTC 날짜 기준 900회 전체/뉴스 최초 600회/공시 최초 200회/재시도 100회. 요청 시작 간 최소 60초, 기본 최대 3회 시도. 종류별 최초 예산은 자동 전용하지 않는다. 종류별 접수순을 유지하며 처리 가능한 공시/뉴스를 교대로 선택한다. 실패·timeout·응답 형식 오류도 예약 시 시도 수에 포함하며, 요청 중 프로세스 중단도 이미 사용된 시도로 보존한다. 실제 API가 응답 전에 처리했는지는 알 수 없으므로 재시작 뒤 재시도는 추가 사용량을 유발할 수 있지만 성공한 분석 결과는 재사용한다.

429는 Retry-After(초/HTTP 날짜), X-RateLimit-Reset을 반영해 공통 대기를 걸고 daily 오류면 다음 UTC 날짜까지 대기한다. 400/404는 해당 job hold, 401/402/403은 설정·인증·잔액 확인용 공통 hold다. 네트워크/5xx/빈 content/잘못된 형식은 제한 횟수 내 재시도한다. account GET /key는 필수가 아니므로 자동 호출하지 않는다. 다른 프로그램·수동 호출은 로컬 예산에 포함되지 않으며 OpenRouter 자체 제한이 우선한다.

jobs 원본 ID 및 원문 URL 중복을 차단하고 본문 추출도 동일 기사에 캐시한다. 기존 sent_articles.json과 실제 전송 캐시는 읽기만 한다. 전송 캐시는 최근 24시간을 비교하고, 공통 AI 실행 직전에는 최근 분석 완료 SEND 기사도 재비교해 대기 중 다른 언론사 기사를 불필요하게 분석하지 않는다. 새 금액·일정·확정 사실 예외는 기존 news_quality 규칙을 재사용한다. 의미상 중복/새 사실의 완전 판별을 보장하지 않는다.

목적지별 성공은 독립 저장한다. 다른 목적지 실패로 성공 목적지를 재전송하거나 AI를 재호출하지 않는다. 명확한 거절은 목적지 재시도, 영구 오류 또는 5회 실패는 hold다. Telegram timeout/응답 유실/발송 직후 상태 저장 전 중단은 uncertain으로 격리한다. Telegram은 멱등키를 지원하지 않으므로 이 구간에서 정확히 한 번 발송을 보장할 수 없고 운영자의 확인 후 재시도가 필요하다. dashboard 인계 중 중단도 보수적으로 uncertain 처리하며, 동일 payload를 재인계해도 기존 대기열 event_id가 중복을 막는다.

## 시작 범위와 누락

`news_start_at`은 전체 확대 시작 시각이며 명시적 오프셋이 필수다. 첫 시작 이전 기사는 분석하지 않고, 기존 queue의 시작 시각을 다른 값으로 바꾸면 거부한다. 기준 전으로 확대하거나 대량 과거 복구하려면 별도 승인·새 계획이 필요하다.

순회 cursor, 추가 page, 최신 확인 시각과 마지막 성공 checkpoint는 SQLite에 남는다. 마지막 페이지까지 정상 확인하거나 이전 체크포인트보다 오래된 항목을 확인한 경우에만 성공 체크포인트를 전진시킨다. 같은 분의 서로 다른 기사를 놓치지 않도록 경계 시각과 같은 항목도 원본 ID로 비교한다. 페이지 상한/실패 구간은 gaps에 계속 남고 다음 순회에서 이전 성공 경계부터 다시 대조한다. 페이지 상한이 계속 초과되면 오래된 구간은 자동으로 해결됐다고 표시하지 않는다. 제공처 페이지가 고정 스냅샷이 아니므로 페이지 이동 중 데이터 변경으로 생기는 완전한 누락 방지를 보장할 수 없다.

DART는 첫 시작 때 기존 RSS 항목을 건너뛰는 정책을 유지하되 이후 마지막 RSS 링크를 영속 보존한다. 재시작 후에도 피드에 남은 구간은 다시 비교할 수 있다. 이전 링크가 RSS에서 사라지면 gap을 기록하며 과거 DART 목록을 자동 대량 조회하지 않는다. 현재 queue 밖의 과거 공시 복구는 별도 작업이다.

## 설정 및 Pi 전환 명령(준비만, 이번에 실행하지 않음)

`ai_pipeline_settings.example.json`은 enabled=false이며 실제 적용 시각을 넣어야 한다. API 키는 넣지 않고 기존 비밀 설정 파일을 런타임에서 읽는다. `ai_db`는 기존 dashboard DB와 다른 영속 경로여야 한다. 예시 `/home/pi5dart/.local/state/invest-monitor/analysis.sqlite3`의 부모 디렉터리는 서비스 계정 소유 700, DB 생성은 UMask=0077을 사용한다. 운영 설정은 `/home/pi5dart/.config/invest-monitor/ai-pipeline.json`에 600으로 설치하고 Git에 넣지 않는다.

1. 나중에 사용자 승인 후 이 코드 버전을 Git으로 적용한다. 구 dart.service/news.service를 먼저 stop·disable한다. 기존 프로세스가 완전히 종료됐는지 확인한 뒤 새 처리기를 시작한다. 이 코드의 구 진입점도 공통 runtime만 실행하므로 직접 AI 경로로 돌아가지 않는다.
2. 설정 파일을 별도 위치에 만들고 실제 확대 시작시각·두 DB 경로를 확인한 다음 enabled=true로 바꾼다. 시작 전 운영 데이터는 백업하되 삭제하지 않는다.
3. `monitor-pipeline@.service.example`을 검토해 `/etc/systemd/system/monitor-pipeline@.service`로 설치하고 `systemd-analyze verify` 후 `systemctl daemon-reload`한다.
4. `sudo systemctl enable --now monitor-pipeline@dart monitor-pipeline@news monitor-pipeline@preprocess monitor-pipeline@ai monitor-pipeline@deliver`로 역할을 시작한다. 기존 dashboard-sender.timer는 기존 상태를 유지한다. ai 역할 하나만 provider 호출을 한다. 서비스의 역할별 lock과 AI worker lock이 중복 실행을 막는다.
5. `systemctl is-active monitor-pipeline@dart monitor-pipeline@news monitor-pipeline@preprocess monitor-pipeline@ai monitor-pipeline@deliver`와 역할 로그의 상태/예외 타입을 확인한다. 본문·키는 로그에 출력하지 않는다.
6. `/usr/bin/python3 /home/pi5dart/workspace/ai_pipeline_cli.py --db /home/pi5dart/.local/state/invest-monitor/analysis.sqlite3`로 순회·체크포인트·공백·대기/완료/보류·가장 오래 기다린 시간·시도/실패/남은 예산·전송 상태를 확인한다. dashboard 실제 수신 상태는 기존 `dashboard_sender.py --status`로 별도 확인한다. 적체가 하루 최초 분석 용량을 초과하면 backlog_over_daily_capacity가 true다.

원인 해결 후 `ai_pipeline_cli.py --db <DB> --release-job <job-id>`, `--release-discovery <id> <company>`, `--resume-auth`로 수동 해제한다. 출력의 uncertain을 다시 보내려면 `--retry-output <job-id> <destination> --ack-duplicate-risk`가 필요하며 Telegram의 실제 발송 여부부터 확인한다. 자동으로 기존 기록을 지우거나 보류 항목을 완료하지 않는다.

롤백: 새 다섯 역할을 모두 stop한 뒤 새 AI DB와 WAL/SHM을 보존하고 이전 Git 버전을 적용해 구 두 서비스를 시작한다. 새 대기 작업은 보존되지만 이전 코드가 처리하지 않으므로 queue 복구는 이후 별도 재개해야 한다. 구 코드로 돌아가면 새 경로에서 성공한 기사도 구 이력에 없을 수 있어 중복 발송 위험이 있다. 롤백 전에 새 delivered 기사 ID를 확인하고 이전 수집 경계/중복 기준과 대조해야 하며 자동 재전송이나 이력 병합을 하지 않는다. DB rollback을 코드 rollback으로 대신할 수 없다.

## 로컬 검증과 남은 실제 확인

임시 디렉터리·임시 두 DB·가짜 HTTP·시각 제어로 기존 실제 로더, DART RSS 필터/본문 추출/프롬프트, Npay 페이지 파서/기사 본문/기업 연결, 공통 AI 응답 파서, 기존 dashboard enqueue, Telegram 요청 어댑터를 연결해 테스트한다. 실제 계정·운영 파일·외부 API를 사용하지 않는다. 테스트에 표시되는 SEND나 delivered는 가짜 응답 결과다.

실제 OpenRouter 모델의 현재 응답 품질은 이전 HTTP429 이후 미검증 상태다. 장시간 349개 실제 순회 소요와 한도·유입량, 새 경로 Telegram/D1 수신 성공도 자연 이벤트로 확인해야 한다. 코드 연결을 막는 미완료 어댑터는 없다. 기존 배포 도구는 이번 전환에서 새 역할 서비스 재시작 매핑을 추가했고 이후 일반 배포에서 새 다섯 역할을 검증한다.

최종 로컬 검증: `test_ai_pipeline`, `test_pipeline_integration`, `news_monitor.test_news_quality`, `news_monitor.test_naver_news`, `test_dashboard_outbox`, `test_deploy_pi` 총 **47건 통과**. 기존 대시보드 회귀 테스트는 localhost 가짜 수신 서버만 사용했고 deploy 테스트는 SSH/Git 동작을 가짜로 대체했다. 문법 검사 및 git diff --check도 통과했다. OpenRouter 호출 위치 검색은 공통 Runtime.transport 한 곳만 반환했다. 별도 환경 의존 패키지는 추가하지 않았으며 기존 requests/beautifulsoup4와 Python 표준 라이브러리를 사용한다.

전환 직전 추가 검증은 **49건 통과**(당일 기존 사용량 baseline/UTC 초기화와 새 서비스 배포 매핑 포함). Pi의 /usr/bin/python3에서 requests/bs4/sqlite3를 확인했다. 복구 지점은 이전 커밋 `a69589e78c155c2393321e73e74d3880d192c9f5`, 접근 제한 백업 `/home/pi5dart/.local/state/invest-monitor/backups/pipeline-20261123T130002Z`다. Git bundle·기존 unit·설정/이력/캐시 archive·SQLite backup API로 일관성 있게 복사한 기존 대시보드 DB를 포함한다. 구 두 서비스는 2026-11-23 22:00:03 KST에 중단·disable했다. 기존 대시보드 timer는 중단하지 않았다.

오늘 이전 경로의 전체 요청 시도를 정확히 복원할 수 없으므로 2026-11-23 UTC 자체 예산은 뉴스600/공시200/재시도100을 이미 사용한 것으로 보수 반영한다. 이는 실제 사용량의 주장이나 OpenRouter 할당량 변경이 아니다. 당일 새 AI 잔여는 0이며 2026-11-24 09:00 KST(00:00 UTC)에 정상 일일 예산으로 돌아간다. 수집·본문·대기열 저장은 계속 가능하다.

영문 포함 종목코드 호환: 전체 목록의 삼성에피스홀딩스 `0126Z0`는 현재 수신 계약의 숫자 코드 제한에 맞춰 stock_code=null과 stock_code_original 원본 보존 방식으로 전달한다. 기업명 매칭으로 기존 카탈로그 연결을 유지한다. 자세한 수신측 설명은 DASHBOARD_HANDOFF.md에 기록했다.
