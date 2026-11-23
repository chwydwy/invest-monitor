# Pi 배포 도구

## 공통 AI 서비스 전환 이후

`deploy_pi.py check/apply`는 Pi에서 `monitor-pipeline@ai.service`가 enabled이면 새 다섯 역할 서비스를 관리한다. 공통 runtime·AI·본문·목록·판단 코드나 기업 목록이 바뀌면 캐시된 설정과 import를 사용하는 다섯 역할을 재시작한다. dashboard_outbox만 바뀌면 deliver만 재시작한다. 테스트·문서 변경은 재시작하지 않는다. 기존 dashboard-sender.timer는 계속 상태 조회만 하고 켜거나 끄지 않는다. sudo 권한을 자동 수정하지 않는다.

최초 전환에만 `apply --transition --message <설명>`을 쓴다. 이 모드는 구 두 서비스가 이미 중단돼 있어야 적용하며 코드만 fast-forward하고 어느 서비스도 재시작하지 않는다. 이어서 승인된 별도 설치 절차로 설정과 template unit을 설치한다. 일반 배포에는 --transition을 쓰지 않는다. 적용 커밋·기존 운영 DB·서비스 계정은 그대로 검증한다. 로컬 게이트는 공통 파이프라인과 기존 대시보드/배포 회귀 테스트를 포함한다.

`ai-pipeline.json`·`ai_pipeline_settings.json`은 비밀/개인 운영 설정으로 Git과 배포에서 제외한다. 예산 baseline은 전환 당일 이전 사용량을 모르더라도 0으로 취급하지 않도록 AI DB metadata에 저장하며 다음 UTC 날짜에는 자동 적용되지 않는다.

## 뉴스 소스 전환 (2026-11-23)

기존 네이버 금융 뉴스 목록은 HTTP 410을 반환한다. 뉴스 모니터는 Npay 종목 뉴스 JSON을 읽고 기존 `언론사ID_기사ID` 형식과 24시간·제목·본문·AI·Telegram 판단을 유지한다. 첫 재시작 때 `/home/pi5dart/.local/state/invest-monitor/news-npay-cutover.txt`에 시작 시각을 한 번 기록하고 그 이전 뉴스는 처리 이력에 추가하지 않은 채 건너뛴다. 이전 소스가 중단된 동안 쌓인 기사를 대량 전송하지 않기 위한 경계다. 이 파일은 Git 작업공간과 운영 `sent_articles.json` 밖에 있으며, 이후 재시작에도 같은 경계를 사용한다. 임의로 삭제하면 과거 24시간 기사가 다시 대상이 될 수 있다. 날짜를 특정 시간대로 단정하지 않고 Npay 원본의 시간대 없는 시각과 Pi 현지 시각을 기존 방식대로 비교한다.

`deploy_pi.py`는 Windows 작업공간에서 로컬 검사를 거친 뒤, Git에 기록된 정확한 커밋을 Pi의 `/home/pi5dart/workspace`에 빠른 앞으로 적용한다. Pi의 코드를 직접 편집하지 않고, 변경된 DART/뉴스 서비스만 재시작한다. `dashboard-sender.timer`는 조회만 하며 켜거나 끄지 않는다. 실적 모니터링과 운영 설정·SQLite 데이터는 배포 대상이 아니다.

## 현재 상태

- 사용자가 새 공개키를 Pi에 등록하고 Windows SSH 에이전트에 키를 추가해 `pi5` 비대화형 접속이 성공했다. 기존 SSH 설정과 키는 덮어쓰지 않았다.
- 두 재시작 명령은 실행하지 않고 `sudo -n -l`로 허용됨을 확인했다. 배포 도구는 기존의 광범위한 sudo 권한을 보고하지만, 필요한 두 명령이 허용되면 이 사유만으로 중단하지 않는다.
- 실제 배포 명령은 `sudo -n /usr/bin/systemctl restart dart.service`와 `sudo -n /usr/bin/systemctl restart news.service`뿐이다. Pi의 `systemctl`은 `/usr/bin/systemctl`이며, 해당 unit 파일은 root 소유(644), unit 디렉터리는 root 소유(755)다. 두 서비스는 `User=pi5dart`이고 별도 관리자 권한의 사전·사후 실행 명령이나 drop-in이 확인되지 않았다.
- 대시보드의 실제 이벤트 `stored`/`duplicate` 검증은 아직 미완료다. 현재 대기열은 비어 있고 타이머는 `disabled`다. 실제 이벤트가 생긴 뒤 별도로 검증한다.
- 배포 도구 자체를 검증하려고 가상 운영 변경이나 서비스 재시작을 하지 않는다.

## 인증 준비

Windows에서 `ssh -o BatchMode=yes pi5 'id -un'`이 `pi5dart`를 반환해야 한다. 기존 `~/.ssh/config`나 키를 덮어쓰지 않는다. 개인키·비밀번호는 채팅이나 Git에 넣지 않는다. 원격 서비스 재시작은 `sudo -n /usr/bin/systemctl restart dart.service`/`news.service`가 비대화형으로 허용돼야 한다. 허용되지 않으면 배포 도구는 코드 적용 전에 중단한다.

최소 sudoers 설정안은 전용 `/etc/sudoers.d/invest-monitor-deploy` 파일의 다음 **한 줄**이다. 전체 sudo 권한, 와일드카드, 다른 systemctl 하위 명령은 허용하지 않는다. 기존 sudoers 파일은 수정하지 않는다.

Pi 확인 결과 기존 `/etc/sudoers.d/010_pi-nopasswd`가 이미 `pi5dart ALL=(ALL) NOPASSWD: ALL`을 부여한다. 전용 규칙을 추가해도 이 기존 전체 권한은 줄지 않는다. **현재 구성은 최소 권한이 아니다.** `/etc/sudoers`의 `%sudo ALL=(ALL:ALL) ALL`도 `pi5dart`의 비밀번호 기반 전체 권한을 제공한다. `pi5dart`는 `docker` 그룹에도 속하며 실제 Docker 소켓을 사용할 수 있다. 이 권한들은 현재 유지하고, 보안 구조 변경은 별도 작업으로 보류한다. 배포 도구는 광범위한 sudo를 상태 보고에 표시하지만 그것만으로 배포를 막지 않는다. 기존 sudoers·그룹을 자동 수정하지 않는다.

```text
pi5dart ALL=(root) NOPASSWD: NOSETENV: /usr/bin/systemctl restart dart.service, /usr/bin/systemctl restart news.service
```

설치 전 후보 파일을 `visudo -cf`로 검사하고, 성공할 때만 root:root 및 권한 0440으로 설치한다. 설치된 파일도 다시 `visudo -cf`로 검사한다. 확인은 `sudo -n -l /usr/bin/systemctl restart dart.service`와 동일한 news 명령으로 **권한 조회만** 한다. 이 단계에서 `restart` 자체를 실행하지 않는다. 서비스 unit 파일·디렉터리의 root 소유권이 바뀌거나 root 실행 지시문이 추가되면 이 제한 권한의 안전성을 다시 검토한다.

### 별도 보안 작업으로 보류한 대체안

Pi의 일반 계정은 현재 `pi5dart` 하나이며 root 비밀번호는 잠겨 있다. 따라서 기존 전체 sudo 규칙을 먼저 없애면 관리자 접근을 잃을 수 있다. 별도 관리자 계정의 SSH·sudo를 다른 세션에서 검증한 뒤, 현재 SSH 세션을 유지한 채 기존 `010_pi-nopasswd`와 그룹 설정을 root 전용 위치에 백업한다. 기존 전용 두 재시작 규칙은 유지하면서 전체 비밀번호 없는 규칙, `sudo` 그룹, `docker` 그룹을 단계적으로 재검토한다. 각 단계마다 `visudo -c`, 새 세션의 관리자 접속, 필요한 명령의 권한 조회, 불필요한 권한 차단을 확인하고 실패하면 유지한 관리자 세션에서 백업 규칙과 그룹을 복원한다. 주가·전송 서비스까지 모두 `pi5dart`로 실행되므로 그룹 변경은 다른 운영 작업에 미치는 영향도 확인해야 한다. 이 절차는 **이번 배포 자동화 범위에 포함되지 않으며 실행하지 않았다.**

## 사용 순서

1. Windows에서 필요한 코드만 수정하고 관련 테스트를 확인한다. 운영 설정과 대기열은 Git에서 제외한다.
2. 배포할 파일만 명시적으로 `git add -- <파일...>`로 스테이징한다. 도구는 자동으로 파일을 스테이징하지 않는다. 스테이징하지 않은 수정 또는 새 파일이 있으면 중단한다.
3. `python deploy_pi.py check`로 로컬 문법·대기열 테스트, Git 상태, Pi 작업 트리·커밋·서비스 상태와 필요한 재시작 권한을 **읽기 전용**으로 확인한다. 비대화형 SSH 인증이나 필요한 재시작 권한이 없으면 여기서 멈춘다.
4. `python deploy_pi.py apply --message "변경 설명"`을 실행한다. 도구가 동일 점검을 반복하고, 스테이징한 변경을 commit/push한 뒤, Pi에서 원격 브랜치가 지정한 커밋과 일치하는지 검사한다. Pi가 점검 당시 커밋에서 달라졌거나 작업 트리가 더럽거나 빠른 앞으로 이동할 수 없으면 강제 초기화 없이 중단한다.
5. Pi에서 정확한 커밋으로 fast-forward한 뒤 변경된 `dart_monitor`/`news_monitor`와 공통 `dashboard_outbox.py`에 해당하는 서비스만 재시작한다. 문서·테스트·일회성 `dashboard_sender.py` 변경만 있으면 모니터를 재시작하지 않는다. `stock_monitor` 또는 requirements 변경은 서비스/의존성 적용 계약이 없으므로 자동 배포를 중단한다.
6. 적용 커밋, 재시작 서비스, 두 모니터의 `active` 및 `Result=success`, 타이머 상태, Pi 작업 트리 청결을 확인한다. 로그 본문이나 인증키는 출력하지 않는다.

스테이징할 변경이 없는 경우에도 `apply`는 현재 로컬 HEAD를 push 확인 후 Pi에 적용할 수 있다. 도구는 `origin/main`을 강제 push하지 않는다.

## 실패와 복구

- 인증·Git 상태·테스트·원격 브랜치 불일치·sudo 권한 문제는 가능한 한 코드 변경 전에 중단한다. 실패 원인을 해결한 뒤 `check`부터 다시 실행한다.
- Pi 코드 적용 후 서비스 재시작/상태 확인에 실패할 수 있다. 자동으로 Git을 되돌리거나 SQLite를 복원하지 않는다. `systemctl show dart.service news.service -p ActiveState -p Result -p ExecMainStatus`로 상태를 확인하고, 기록된 이전 커밋과 적용 커밋을 비교한다.
- 코드 버전 복구는 Windows에서 문제 커밋을 `git revert`로 되돌리는 **새 커밋**을 만들고 테스트·push·배포하는 방식을 기본으로 한다. Pi에서 `git reset --hard`나 운영 데이터 삭제를 하지 않는다. DB 스키마/데이터 변경은 별도 백업·마이그레이션 절차가 필요하며 코드 복구가 DB를 되돌리지 않는다.
- 비밀 설정, `dashboard_outbox.sqlite3` 및 SQLite 보조 파일, 뉴스 중복 기록은 Git 제외를 유지한다. 도구는 이 파일들을 출력하거나 전송하지 않는다.
