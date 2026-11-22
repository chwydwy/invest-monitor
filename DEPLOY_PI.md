# Pi 배포 도구

`deploy_pi.py`는 Windows 작업공간에서 로컬 검사를 거친 뒤, Git에 기록된 정확한 커밋을 Pi의 `/home/pi5dart/workspace`에 빠른 앞으로 적용한다. Pi의 코드를 직접 편집하지 않고, 변경된 DART/뉴스 서비스만 재시작한다. `dashboard-sender.timer`는 조회만 하며 켜거나 끄지 않는다. 실적 모니터링과 운영 설정·SQLite 데이터는 배포 대상이 아니다.

## 현재 상태

- SSH 별칭 `pi5`는 설정돼 있으나 2026-11-22 비대화형 인증은 `Permission denied`였다. 기존 SSH 설정과 키는 수정하지 않았다.
- 대시보드의 실제 이벤트 `stored`/`duplicate` 검증은 아직 미완료다. 현재 대기열은 비어 있고 타이머는 `disabled`다. 실제 이벤트가 생긴 뒤 별도로 검증한다.
- 배포 도구 자체를 검증하려고 가상 운영 변경이나 서비스 재시작을 하지 않는다.

## 인증 준비

Windows에서 `ssh -o BatchMode=yes pi5 'id -un'`이 `pi5dart`를 반환하도록 **사용자가** SSH 공개키 인증 또는 이미 운영 중인 SSH 에이전트를 설정해야 한다. 기존 `~/.ssh/config`나 키를 덮어쓰지 않는다. 개인키·비밀번호는 채팅이나 Git에 넣지 않는다. 원격 서비스 재시작은 `sudo -n systemctl restart dart.service`/`news.service`가 비대화형으로 허용돼야 한다. 허용되지 않으면 배포 도구는 코드 적용 전에 중단한다. 최소 권한 sudoers는 실제 Pi 정책을 확인한 뒤 사용자 측에서 설정한다.

## 사용 순서

1. Windows에서 필요한 코드만 수정하고 관련 테스트를 확인한다. 운영 설정과 대기열은 Git에서 제외한다.
2. 배포할 파일만 명시적으로 `git add -- <파일...>`로 스테이징한다. 도구는 자동으로 파일을 스테이징하지 않는다. 스테이징하지 않은 수정 또는 새 파일이 있으면 중단한다.
3. `python deploy_pi.py check`로 로컬 문법·대기열 테스트, Git 상태, Pi 작업 트리·커밋·서비스 상태를 **읽기 전용**으로 확인한다. 비대화형 SSH 인증이 안 되면 여기서 멈춘다.
4. `python deploy_pi.py apply --message "변경 설명"`을 실행한다. 도구가 동일 점검을 반복하고, 스테이징한 변경을 commit/push한 뒤, Pi에서 원격 브랜치가 지정한 커밋과 일치하는지 검사한다. Pi가 점검 당시 커밋에서 달라졌거나 작업 트리가 더럽거나 빠른 앞으로 이동할 수 없으면 강제 초기화 없이 중단한다.
5. Pi에서 정확한 커밋으로 fast-forward한 뒤 변경된 `dart_monitor`/`news_monitor`와 공통 `dashboard_outbox.py`에 해당하는 서비스만 재시작한다. 문서·테스트·일회성 `dashboard_sender.py` 변경만 있으면 모니터를 재시작하지 않는다. `stock_monitor` 또는 requirements 변경은 서비스/의존성 적용 계약이 없으므로 자동 배포를 중단한다.
6. 적용 커밋, 재시작 서비스, 두 모니터의 `active` 및 `Result=success`, 타이머 상태, Pi 작업 트리 청결을 확인한다. 로그 본문이나 인증키는 출력하지 않는다.

스테이징할 변경이 없는 경우에도 `apply`는 현재 로컬 HEAD를 push 확인 후 Pi에 적용할 수 있다. 도구는 `origin/main`을 강제 push하지 않는다.

## 실패와 복구

- 인증·Git 상태·테스트·원격 브랜치 불일치·sudo 권한 문제는 가능한 한 코드 변경 전에 중단한다. 실패 원인을 해결한 뒤 `check`부터 다시 실행한다.
- Pi 코드 적용 후 서비스 재시작/상태 확인에 실패할 수 있다. 자동으로 Git을 되돌리거나 SQLite를 복원하지 않는다. `systemctl show dart.service news.service -p ActiveState -p Result -p ExecMainStatus`로 상태를 확인하고, 기록된 이전 커밋과 적용 커밋을 비교한다.
- 코드 버전 복구는 Windows에서 문제 커밋을 `git revert`로 되돌리는 **새 커밋**을 만들고 테스트·push·배포하는 방식을 기본으로 한다. Pi에서 `git reset --hard`나 운영 데이터 삭제를 하지 않는다. DB 스키마/데이터 변경은 별도 백업·마이그레이션 절차가 필요하며 코드 복구가 DB를 되돌리지 않는다.
- 비밀 설정, `dashboard_outbox.sqlite3` 및 SQLite 보조 파일, 뉴스 중복 기록은 Git 제외를 유지한다. 도구는 이 파일들을 출력하거나 전송하지 않는다.
