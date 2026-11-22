# Research Workspace 대시보드 전달 계약 (로컬 준비)

현재 코드는 분석 결과를 `dashboard_outbox.sqlite3`에 기록할 뿐 외부로 보내지 않는다. `deliver_pending()`은 별도 실행자에서 명시적으로 호출할 때만 전송한다. Pi 적용·Worker URL·인증키 설정·재시도 실행자 배치는 별도 단계다.

`dashboard_sender.py`는 별도 일회성 실행자다. `--status`는 상태별 건수, `--holds`는 본문 없는 보류 메타데이터를 표시한다. `--once`는 최대 `--limit`건(기본 5건)의 전송 가능 항목만 처리한다. `--verify-duplicate <event_id>`는 이미 delivered인 **실제** 이벤트 한 건을 명시적으로 다시 보내 `duplicate` 응답을 확인한다. `--release <event_id>`와 `--release-auth`는 원인 해결 후 수동 해제에만 사용한다. `.lock` 파일 잠금으로 동시 실행을 막는다. 실행자는 토큰을 명령줄 인자나 로그에서 받지 않고, `--token-file` 또는 `DASHBOARD_TOKEN_FILE`이 가리키는 서비스 계정 전용 파일에서 읽는다. Linux에서 이 파일은 소유자만 접근 가능해야 한다. URL은 `--endpoint` 또는 `DASHBOARD_INGEST_URL`에서 받으며 운영에서는 HTTPS만 사용한다.

모니터 두 프로세스와 전송 실행자는 동일한 `DASHBOARD_OUTBOX_DB` 경로를 사용해야 한다. 이 경로를 지정하지 않으면 저장소 루트의 `dashboard_outbox.sqlite3`를 쓴다. Pi에서 실제 서비스 계정·실행 경로를 확인한 뒤 재부팅 후에도 남는 디스크 위치와 디렉터리 권한을 정한다. 기존 대기열이 있다면 파일·WAL·SHM을 정지 상태에서 함께 검토하고 백업한 뒤 경로를 전환한다. 시스템 서비스와 타이머는 실제 Pi 구성을 확인한 후 등록한다. 첫 실제 이벤트의 `stored` 및 `duplicate` 검증 전에는 자동 타이머를 켜지 않는다.

Windows의 `.dpapi` 암호화 파일은 Pi에서 직접 복호화할 수 없다. 승인된 `with-monitor-token.ps1`이 현재 Windows 계정에서 복호화해 설치 프로그램의 **환경변수**로만 전달할 수 있다. Pi에 직접 접속하지 않는 운영 방식에 맞춰, Pi에서 생성한 공개키로 Windows에서 토큰을 암호화하고 사용자가 암호화 파일만 별도 안전한 매체로 옮겨 Pi에서 복호화해 서비스 전용 파일로 설치하는 방법을 검토한다. Pi의 암호화 도구와 서비스 계정이 확인되기 전에는 구체적인 명령을 확정하지 않는다. 키를 채팅, Git, 명령줄 인자, shell history 또는 로그에 넣지 않는다.

## Worker 수신 계약 제안

`POST /events`, `Content-Type: application/json`, `Authorization: Bearer <별도 대시보드 전용 토큰>`, `Idempotency-Key: <source>:<source_id>`. HTTPS를 사용하고 Worker에서 토큰을 검증한다. 성공·기존 항목의 재수신 모두 HTTP 200 또는 201과 `{"event_id":"dart:fictional-001","status":"stored"}` 또는 `status: "duplicate"`를 반환한다. D1에는 `event_id`를 UNIQUE/PRIMARY KEY로 저장한다. 단순 HTTP 성공만으로는 대기열을 완료 처리하지 않는다. 대시보드의 현재 수신 제한은 요청 전체 65,536 UTF-8 바이트, `analysis`/`summary` 40,000 JavaScript UTF-16 문자 단위다. 송신 측에서 요청 전에 검사하며 초과 이벤트는 원문을 자르지 않고 `hold_size`로 보존한다.

가상 공시 예시:

```json
{"source":"dart","source_id":"fictional-001","receipt_no":"fictional-001","company":"가상기업","title":"가상기업 - 공급계약","disclosure_type":"공급계약","published_at":null,"published_at_original":"원본 RSS pubDate (확인 전)","url":"https://example.invalid/disclosure","analysis":"가상의 AI 분석 원문","score":5,"monitor_groups":["JM"],"sector":null}
```

가상 뉴스 예시:

```json
{"source":"news","source_id":"fictional-002","article_id":"fictional-002","company":"가상기업","stock_code":"000000","title":"가상 뉴스 제목","press":"가상 언론사","published_at":null,"published_at_original":"2026.01.01 09:00","url":"https://example.invalid/news","summary":"가상 AI 요약","monitor_groups":["ChemHold"],"sector":null}
```

공시의 `analysis`는 SCORE를 제거한 AI 응답 원문이다. 주요 내용·유의사항은 별도 구조화하지 않는다. 공시 유형은 제목에서 `기업 - 유형` 형태가 확인될 때만 채운다. 기업 그룹은 산업 섹터가 아니며 `sector`는 확인 전까지 null이다. 실적 화면으로 공시를 자동 분류하지 않는다.

## 시각 계약

뉴스 목록의 원본 시각은 `YYYY.MM.DD HH:MM` 형태로 파싱되지만 시간대 표기가 없다. `published_at_original`에 화면의 원본 문자열을 보존하고 `published_at`은 null로 보낸다. DART 코드는 `https://dart.fss.or.kr/api/todayRSS.xml`의 `pubDate`를 읽는다. 조사 시점 RSS에는 항목이 없어 실제 `pubDate` 표본과 시간대 표기를 검증하지 못했다. 따라서 `published_at_original`에 원본 `pubDate`(없으면 null)를 보존하고 `published_at`은 null이다. 실제 RSS 표본에서 시간대 근거가 확인될 때만 명시적 오프셋을 가진 ISO 8601 변환을 추가한다. 정렬용 시각은 수신 측 `received_at`을 별도로 사용하고, `published_at_original`은 원본 표시용이며 시간대가 없으면 “시간대 미제공”으로 표시한다. 수신일 정렬은 공시/기사 발생 순서를 뜻하지 않는다.

현재 대시보드 수신기는 추가 필드를 `payload_json`에 저장하지만 조회 API와 카드에는 `published_at_original`을 내보내지 않는다. 대시보드 측에서 조회 응답과 카드에 이 값을 추가하고, `published_at`이 null일 때 원본 시각 및 시간대 미제공 문구를 표시해야 한다. 원본이 null이면 “시각 미제공”으로 표시한다.

## 대기열 상태와 재시도

`enqueue()`는 동일 `event_id`를 중복 삽입하거나 기존 내용을 덮어쓰지 않는다. `counts(db_path)`와 `inspect_items(db_path, state=...)`는 본문·인증키 없이 상태를 확인한다. `pending`만 전송하며, 성공 확인 응답의 event_id/status가 일치할 때 `delivered`로 전환한다. 409는 `conflict`, 413 또는 사전 크기 초과는 `hold_size`, 400/405/415/422는 `hold_payload`, 401/403은 `hold_auth`다. 인증 오류 시 동일 자격 증명을 사용하는 나머지 pending도 보류해 반복 요청을 막는다. 429는 `Retry-After`를 최소 대기시간으로 반영한다. 네트워크·타임아웃·5xx·확인 응답 오류는 지수 간격(30초부터 최대 1시간)으로 재시도한다. 한 이벤트의 보류·충돌은 다른 정상 이벤트의 전송을 막지 않는다. 단, 전역 인증 오류는 전체 대기열에 적용된다.

문제 해결 후 `release_item(db_path, event_id)`로 해당 보류 항목을 명시적으로 pending으로 돌린다. 원문은 변경하지 않는다. 인증 오류는 새 이벤트가 추가되어도 전송을 멈추며, 자격 증명을 수정한 뒤 `release_auth_holds(db_path)`로 전체 인증 보류를 해제한다. `hold_size`는 현재 제한을 만족하지 않으면 해제되지 않는다. 크기 초과는 수신 한도 변경 또는 별도의 원문 보존형 분할/첨부 계약을 양쪽에 먼저 구현해야 한다. 409는 기존 저장 내용과 원문을 대조해 충돌 원인을 해결한 뒤 해제한다. 예를 들어 재분석, AI 응답 변화, 기사 메타데이터·URL 변경이 같은 ID의 다른 payload를 만들 수 있다. 로그에는 상태·HTTP 코드·오류 분류만 남기고 본문과 토큰은 출력하지 않는다. 현재 자동 전송 실행자는 설치되어 있지 않다.

## 운영 적용 시 확인

Pi에 동일 코드를 배포하기 전 공유 SQLite 파일을 둘 영속 디스크 경로와 두 서비스 계정의 쓰기 권한을 확인한다. Worker/D1 수신 계약과 전용 토큰을 설정한 뒤 별도 재전송 실행자를 주기적으로 운영한다. 현재 대기열은 분석 완료 후의 전송 실패만 복구한다. Pi 중단 중 누락된 DART RSS 항목은 복구하지 않으므로 접수번호 체크포인트와 OpenDART 과거 목록 대조를 별도 설계해야 한다.
