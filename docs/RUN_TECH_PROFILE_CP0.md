# TECH_PROFILE v0 — CP-0 로컬 실행 안내
<!-- 작성일: 2026-07-26 (KST) / 대상 종목: NAVER (035420) -->

이 원격 실행 환경에서는 로컬 MariaDB(`stock_db`)에 접근할 수 없어,
CP-0(스키마 및 데이터 가용 범위 점검)는 **로컬에서 실행**한다.
(CP-2 가격 수집을 로컬에서 실행했던 것과 동일한 방식.)

지시서 원칙("코드 작성 전에 스키마를 먼저 확인한다")에 따라
**Phase 1~3 분석 코드는 CP-0 결과 확인 후 작성한다.**
이번 커밋에는 CP-0 점검 스크립트만 포함되어 있다.

## 실행 방법

```
git clone https://github.com/Pepsi999/yong2b.git   (이미 있으면 git pull)
cd yong2b
git checkout claude/naver-tech-profile-analysis-ljfa2p

REM C:\stock 하위 64bit venv 활성화 (32bit에서 pandas 오류 이력 있음)
C:\stock\venv64\Scripts\activate

pip install PyMySQL pandas
python 02_validation\tech_profile\cp0_schema_check.py --code 035420
```

또는 `02_validation\tech_profile\run_cp0.bat` 실행
(CP949 인코딩, 상단의 `VENV` 경로만 실제 64bit venv 경로로 수정).

### DB 접속 정보 (하드코딩 없음)

아래 순서로 탐색한다. **기존 C:\stock 설정 방식이 있으면 그 파일을 `--config`로 지정**:

1. `--config <파일>` (.json 또는 .ini — 형식은 `db_config.example.json` 참조)
2. 환경변수 `STOCK_DB_HOST` / `STOCK_DB_PORT` / `STOCK_DB_USER` / `STOCK_DB_PASSWORD` / `STOCK_DB_NAME`
3. 기본 경로: `C:\stock\config\db_config.json` → `C:\stock\db_config.json` → 스크립트 폴더의 `db_config.json`

기존 설정 파일 형식이 위와 달라 로드가 안 되면, 형식(키 이름)만 알려주면
로더를 그 방식에 맞춘다. **설정 파일은 커밋하지 말 것** (`.gitignore` 처리됨).

## 스크립트가 하는 일 (읽기 전용 — SELECT만 실행)

- `daily_price` / `v_trading_volume_stocks` / `kospi200_history` 존재 여부·컬럼·타입
- 035420 일봉 시작일/종료일/결측 거래일 수 (달력 = `daily_price` 전체 DISTINCT 일자)
- 수급 수치형 컬럼 전수: 비결측 건수, 최초 비결측일, 최초 0이 아닌 일자, 최종 비결측일
  → 백필 유효 시작 시점 판별 (그 이전 구간은 Phase 2에서 공백 처리, 0 채움 금지)
- 기대 수급 컬럼(개인/외국인/기관, fininv·trust·priv·insur·bank·etcfin·govt)과
  실제 컬럼명 키워드 매칭
- 원본 `trading_volume`은 직접 조회하지 않음 (ELW 제외 뷰만 사용)

## 산출물 (실행 후 생성)

| 경로 | 내용 |
|---|---|
| `reports/tech_profile/cp0_schema_report.md` | CP-0 보고서 본문 |
| `reports/tech_profile/cp0_flow_column_stats.csv` | 수급 컬럼별 채움 현황 원자료 |
| `logs/tech_profile_YYYYMMDD.log` | 실행 로그 |

## 실행 후

```
git add reports/tech_profile logs
git commit -m "CP-0: 스키마 점검 결과"
git push
```

또는 `cp0_schema_report.md` + `cp0_flow_column_stats.csv` 두 파일을 대화창에 업로드.
결과 검토(CP-0 승인) 후 Phase 1(거래량 프로파일) 코드를 작성한다.

## 문제 발생 시

- 종목코드/일자 컬럼 자동 탐지 실패 메시지가 리포트에 찍히면:
  `--code-col`, `--date-col` 로 실제 컬럼명을 지정해 재실행
- 접속 오류: 설정 파일 경로/계정 확인 (비밀번호는 로그에 기록되지 않음)
