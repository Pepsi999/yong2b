# CP-2 로컬 실행 안내 (b안)
<!-- 작성일: 2026-07-16 (KST) / 실험 ID: US-2026-001 -->

이 원격 실행 환경은 네트워크 정책상 Yahoo Finance(`query1.finance.yahoo.com` 등)가
차단되어 있어 CP-2(가격 수집)는 로컬에서 실행한다.

## 실행 방법

```bash
git clone https://github.com/Pepsi999/yong2b.git
cd yong2b
git checkout claude/cp1-checkpoint-process-y7rhma

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install yfinance pandas pyarrow tabulate

python fetch_prices.py
```

소요: 240여 티커 × 20년 일봉, 약 5~15분 (레이트리밋에 따라 변동).

## 산출물 (실행 후 생성됨)

| 경로 | 내용 |
|---|---|
| `data/prices/*.parquet` | 티커별 일봉 OHLCV (수정주가) |
| `data/prices/panel_close.parquet` 등 | 통합 패널 (종가/거래량/거래대금) |
| `reports/cp2_coverage_report.md` | 커버리지 리포트 (85% 기준 판정 포함) |
| `reports/cp2_coverage_by_era.csv` | 멤버십 구간별 커버리지 상세 |
| `reports/cp2_coverage_monthly.csv` | 월별 유니버스 커버리지 |

## 실행 후

아래 중 편한 방법으로 결과를 공유:
1. **git push** (권장): `git add data/prices reports && git commit -m "CP-2: 가격 수집 결과" && git push`
   - parquet 총량이 수백 MB를 넘으면 `data/prices/panel_*.parquet` + `reports/`만 커밋해도 CP-3 진행 가능
2. 또는 `reports/` 3개 파일 + `data/prices/panel_*.parquet`을 대화창에 zip 업로드

## 주의

- `fetch_prices.py`는 CP-1.1 동결본(`nasdaq100_history.csv`, `data_overrides.csv`)을 입력으로 사용 — 수정 금지
- IPO 정합성 실측 재검증(P0)이 FAIL(exit 1)이면 결과를 공유하고 CP-3 진행 보류
- 대안: claude.ai 환경 설정의 네트워크 허용 목록에 `query1.finance.yahoo.com`,
  `query2.finance.yahoo.com`, `fc.yahoo.com`을 추가하면 원격 세션에서 직접 실행 가능
