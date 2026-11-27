# Pi 운영 상태 보고

2026-11-28 뉴스 제목 선별 전환 이후에도 스키마 v1을 사용한다. 기존 `issues`에 `source=news`, `state=stage_counts`로 `stage=title_selection`과 `stage=individual_summary` 두 줄을 제공한다. 제목 대기·묶음 배정·보류, 완료 묶음과 선택·미선정, 새 경로의 개별 요약 상태를 구분한다. 상태 및 누적 수는 KST 일일 활동과 별개다. 호출 수는 UTC 당일 예약을 기준으로 하며 재시도와 그날 전환 이전 개별 분석도 포함한다. 제목 선별 성공은 개별 AI 요약 완료 수에 포함하지 않는다. 두 줄은 현행 운영 현황에서 읽을 수 있으며 카드용 구조화는 별도 계약 변경이 필요하다.

2026-11-25. 계약 근거: 대시보드 프로젝트 `PI-OPERATIONS-REPORT-HANDOFF.md` v1 및 실제 `src/modules/pi-report.js` 검증기. 기존 `/events` 계약과 별개이며 상대 프로젝트는 수정하지 않는다.

## 실행과 격리

`operations-report.timer`가 한국시간 매시 00·30분에 독립 oneshot `operations-report.service`를 실행한다. 재부팅 후에도 활성화된다. `operations_report.py`는 두 SQLite DB를 읽기 전용으로 열고 기존 결과를 요약한다. 수집·원문 조회·AI·Telegram·이벤트 전송 실행자는 호출하지 않는다. 업무 시간 밖에도 상태만 보고한다.

최신 표본과 상태 파일 각각 하나만 유지한다. 실패한 보고를 전송 대기열에 넣지 않는다. 같은 30분 구간의 재실행은 HTTP 요청 없이 종료한다. 네트워크/5xx는 다음 주기 이후 최신 표본으로 재시도하고 429의 Retry-After가 더 길면 존중한다. 401/403은 인증 보류, 400/405/409/413/415/422 및 report_not_configured는 설정 보류다. 원인을 해결한 뒤 각각 `--resume-auth`, `--resume-after-fix`를 사용한다. 보고 실패는 기존 작업을 중단하지 않는다.

POST `/api/operations/pi-report`는 전용 `OPERATIONS_REPORT_TOKEN` Bearer 인증과 고정 User-Agent를 쓴다. Cookie/Origin과 기존 이벤트 토큰은 사용하지 않는다. 토큰은 Windows DPAPI 전달 도구와 `install_operations_token.py`를 통해 SSH 표준입력으로만 옮긴다. Pi 토큰과 설정은 서비스 계정 소유 600, 상위 디렉터리 700이며 Git 제외다. 토큰·본문·AI 응답은 보고/로그에 포함하지 않는다.

## 수치 정의

- `today_kst`: 한국시간 날짜별 최초 발견 원본 ID, AI 완료, 정상 제외/NO_SEND, 실패, 보류 전이. 동일 ID의 같은 지표는 하루 한 번만 센다. 다른 기업 문맥에서 제외되었다가 처리될 수 있으므로 지표는 상호 배타적인 흐름 단계 합계가 아니다. 실패 후 성공도 각각 기록될 수 있다.
- 기존 discoveries/jobs에는 발견/완료 전이 시각이 없어 누적 상태를 오늘 수로 바꾸지 않는다. 집계 시작 당일과 집계 오류가 있는 날은 당일 수를 null로 제공한다. 다음 한국시간 날짜부터 전체 날짜 범위가 확보된다. 운영 중단 중 발생했지만 발견되지 않은 기사 수는 측정하지 못한다.
- 실제 작업 확정 지점에서 `operations_metrics.py`가 기존 AI DB의 작은 일일 카운터를 기록한다. 일일 합계는 7일, ID 중복 억제 키는 오늘·어제만 보존한다. 최근 성공/오류는 단계별 한 행이다. 통계 기록 오류는 업무 결과를 실패로 바꾸지 않는다.
- `last_success`: 유효 RSS/뉴스 페이지 수신·해석, 본문 처리의 정상 완료(정상 제외/중복 처리 포함), 검증된 AI 완료, 대시보드 outbox 인계, 수신 ACK, Telegram 실제 성공을 구분한다. 요청 시작·파일 수정 시각은 성공 시각의 대체값이 아니다. 과거 근거가 없으면 null이다.
- `queue`와 `delivery`는 **현재/누적 상태 분포**이며 오늘 처리량이 아니다. 독립 failed 상태가 없어 실패를 안전하게 분리할 수 없는 항목은 null이다. Telegram uncertain은 성공/자동 재전송 대상이 아니다.
- dashboard_enqueue=공통 outputs에서 기존 outbox에 인계 완료. dashboard_stored=기존 outbox가 event_id 및 stored/duplicate ACK를 확인한 상태. 인계를 Worker 저장 성공으로 표시하지 않는다.
- `ai_budget`: UTC 날짜의 attempts 예약 횟수와 해당 날짜 baseline. 실패·중단·재시도도 포함한다. 총 900/news 600/dart 200/retry 100 자체 예산이며 계정 전체 사용량/크레딧이 아니다. 한국시간 09시에 초기화한다. 수동/다른 프로그램 호출은 이 집계 밖이다.

## 설치와 배포

Git 배포 도구의 check/apply를 사용한다. 통계 기록 공통 모듈 변경은 다섯 파이프라인 역할을 재시작한다. 보고기 자체는 oneshot이므로 다음 실행 때 새 코드를 읽는다. 기존 dashboard-sender.timer는 그대로 유지한다.

서비스/타이머 예제와 `operations_report_settings.example.json`이 저장소에 있다. Pi 개인 설정은 `/home/pi5dart/.config/invest-monitor/operations-report.json`, 보고 상태는 `/home/pi5dart/.local/state/invest-monitor/operations-report.json` 및 같은 이름의 `.snapshot.json`이다. AI/이벤트 대기열은 기존 경로를 사용한다. 최초 실제 보고 stored와 동일 표본 duplicate를 확인한 뒤 타이머를 enable --now 한다. DB에 추가되는 것은 통계 테이블이며 기존 작업/이력/본문/예산을 삭제하거나 재분석하지 않는다.

## 하루 관찰과 장애 확인

Pi에서 아래 명령은 비밀값/본문을 출력하지 않는다.

```sh
systemctl list-timers operations-report.timer --all --no-pager
systemctl show operations-report.service -p Result -p ExecMainStatus -p ActiveEnterTimestamp
python3 /home/pi5dart/workspace/operations_report.py --settings /home/pi5dart/.config/invest-monitor/operations-report.json --status
journalctl -u operations-report.service --since '24 hours ago' --no-pager
systemctl is-active monitor-pipeline@dart.service monitor-pipeline@news.service monitor-pipeline@preprocess.service monitor-pipeline@ai.service monitor-pipeline@deliver.service dashboard-sender.timer
```

30분마다 report_id와 reported_at이 갱신되는지, HTTP 200/stored와 수신 화면의 시각이 일치하는지 확인한다. 75분 이상 갱신되지 않으면 대시보드는 상태 미확인을 표시한다. 실패 중의 과거 성공 시각을 새 성공으로 표시하지 않는다. 보고를 위해 AI나 이벤트를 수동 실행하지 않는다.

한국시간 00시에는 활동 날짜만, 09시에는 UTC 예산 날짜만 바뀌는지 비교한다. 첫 완전한 날에는 collected/ai_completed 등의 일일 수가 실제 새 처리에 따라 증가하는지 확인한다. AI used=usage 합계, usage=local_usage+baseline, remaining=max(0,900-used)를 대조한다. 인계 수와 ACK 수는 대기 중 차이가 날 수 있으며 오류가 없다고 억지로 맞추지 않는다. 보고 실행 전후 AI attempts가 보고 때문에 증가하지 않는지와 기존 순회/대기열의 진행을 함께 확인한다. 보고 타이머 실행 자체가 기존 서비스의 재시작·예산 예약을 일으키지 않는다.

장애 시 토큰/설정 원문이나 전체 snapshot 대신 --status와 제한된 서비스 결과만 공유한다. 네트워크 일시 장애는 다음 주기를 기다린다. 인증/계약 보류는 원인 해결 전 반복 실행하지 않는다. 보고만 중단하려면 `sudo systemctl disable --now operations-report.timer`를 사용하며 기존 수집/AI/전송 서비스는 그대로 둔다. 코드 복구는 Git revert 배포를 사용하고 DB 백업을 자동 덮어쓰지 않는다.

24시간 실제 관찰 결과는 설치 직후 검증과 구분해야 한다. 이 문서는 하루 관찰 방법이며 하루 무장애를 미리 보장하지 않는다.
