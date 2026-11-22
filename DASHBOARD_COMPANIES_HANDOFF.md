# Research Workspace 기업 목록 인수인계

기준일: 2026-11-22. 기본 목록은 `DASHBOARD_COMPANIES.json`의 `companies` 349개다. BL 343개와 JM 6개의 합집합이고, 확인된 식별 기준 교집합은 0개다. 두 목록은 별도 원본으로 유지한다. ChemHold 35개는 합집합의 부분집합이며 뉴스 대상 35개와 동일한 법인 집합이다. 뉴스 원본의 `TKG휴켐스`는 기본 목록 `티케이지휴켐스`의 별칭으로, 별도 기업으로 등록하지 않는다.

## 수집과 Telegram

`dart_monitor/dart.py`는 BL·JM·ChemHold를 읽는다. BL방은 BL∪JM 전체 공시를 받고, ChemHold방은 해당 기업 공시를 추가로 받는다. JM방 신규 공시 전송 경로는 없다. 같은 방 ID는 `disclosure_rooms()`가 한 번만 반환한다. 뉴스 수집은 `news_monitor/Companies_ChemHold_news.txt`의 35개 이름과 `mapping_news.json`의 코드로 유지된다. 실적 수집이나 별도 방은 이번 자료의 대상이 아니다.

## 이름과 식별

화면 표시명은 공식 KRX의 현재 상장 종목명을 우선한다. 확인된 법인명, 영문명, 공백 차이, 과거 상장명은 `DASHBOARD_COMPANY_ALIASES.json`에 별칭으로 보존했다. 연결 근거 URL과 종목코드를 각 항목에 넣었다. `dart_monitor/disclosure_targets.py`는 같은 별칭 파일로 공시 대상 기업을 판정한다. 별칭 연결 전에 들어온 이벤트의 원래 기업명·식별자는 수정하지 않는다. 수신 대시보드는 원래 필드를 보존하고 이 카탈로그로 검색·필터를 연결해야 한다. 공식 DART 8자리 기업코드가 확인되지 않은 행은 null로 둔다.

`동아쏘시오홀딩스`(000640)는 상장 법인이다. 2013년 분할 뒤의 `동아제약`은 별도 비상장 법인이므로 이를 상장사의 대표명이나 일반 별칭으로 연결하지 않는다. `KG모빌리티플랫폼`(구 케이카)과 `트리니티항공`(구 티웨이항공)은 같은 종목코드의 현재 상장명으로 바꿨다. 공통 종목 매핑의 과거 이름 키는 다른 기능이 사용할 수 있어 보존한다.

## 대시보드 데이터

`disclosure_groups`는 현재 소스 목록 소속, `universe_list`는 BL/JM, `chemhold_member`는 관심 그룹 소속, `news_monitored`는 뉴스 수집 여부, `telegram_disclosure_targets`는 방 기호 이름이다. 산업 `sector`와 별도 속성이다. 확인된 병합 전 소스 이름과 그룹은 `historical_source_names`·`former_source_groups`에 남겼다. 실제 채널 ID는 포함하지 않았다.

기존 SQLite 대기열, 과거 공시·뉴스, 수신 계약의 `event_id`·인증·재시도 규칙은 변경하지 않았다. 이미 저장된 이벤트를 재작성하거나 재전송하지 않는다. 섹터 및 별칭 연결은 카탈로그 조회 단계의 처리다.

## 향후 BL 교체

`review_bl_update()`로 BL 추가·제외와 JM→BL 이동 후보를 먼저 검토한다. 새 BL에 이미 포함된 JM 법인만 확인 후 JM에서 제거한다. 새 BL에서 빠진 법인을 자동으로 JM에 넣지 않는다.
