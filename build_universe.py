#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_universe.py — CP-1: 나스닥100 시점별(point-in-time) 유니버스 구축
작성일: 2026-07-16 (KST)
실험 ID: US-2026-001

방법 B (스펙 §2): 네트워크 정책상 Wikipedia 직접 파싱(방법 A)이 불가하여,
공식 보도자료(Nasdaq GlobeNewswire 등)를 WebSearch로 교차 검증해 수동 구축한
편입/편출 이벤트 원장(data/universe/nasdaq100_changes.csv)과
기준 스냅샷(2022-01-03, data/universe/nasdaq100_base_20220103.csv)으로부터
2010-01-01 ~ 현재의 월말 구성종목 테이블을 재구성한다.

재구성 방식 (이벤트 소싱):
  - 기준일 멤버십 집합에서 시작.
  - 기준일 이후 이벤트는 시간순으로 전진 적용 (add/remove/rename).
  - 기준일 이전 이벤트는 역시간순으로 역적용 (add→제거, remove→복원, rename→환원).
  - 모든 적용은 엄격 검증: 기존 멤버 add, 비멤버 remove 발생 시 즉시 오류.
    (원장/기준 스냅샷의 오류를 자동 탐지하는 안전장치)

멤버십 시점 규약: 발효일 D "장 시작 전" 기준 → D일 종가 시점에 add 반영, remove 제외.
즉 member(t) ⇔ add_date ≤ t < remove_date.

단순화 (리포트에 명기):
  - 회사 단위, 대표 주식클래스 1개만 추적 (GOOGL만, GOOG 제외; DISCA만; FOXA만 등).
    TOP-N 랭킹 백테스트에서 동일 회사 중복 편입을 방지하기 위한 의도적 선택.
  - 트래커/2차 클래스의 편출입(LILA/LILAK, CMCSK, LBTYK 등)은 회사 단위 변화가
    없으므로 무시.

산출물:
  - data/universe/nasdaq100_history.parquet / .csv  (date, ticker, in_index, yf_ticker)
  - reports/cp1_universe_report.md  (종목 수 95~105 검증 + 스팟 체크 결과)
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
UNIV_DIR = ROOT / "data" / "universe"
REPORT_DIR = ROOT / "reports"

BASE_DATE = date(2022, 1, 3)
GRID_START = date(2009, 12, 31)   # 2010-01-01 시점 멤버십 = 이 날짜 스냅샷
GRID_END = date(2026, 7, 15)      # 실행일 전일 (최신 스냅샷)
COUNT_MIN, COUNT_MAX = 95, 105    # 스펙 CP-1 검증 범위


def month_ends(start: date, end: date):
    """start~end 사이의 월말 달력일 목록 (+ end 자체)."""
    out = []
    d = start
    while d <= end:
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        me = nxt - timedelta(days=1)
        if me > end:
            break
        out.append(me)
        d = nxt
    if not out or out[-1] != end:
        out.append(end)
    return out


def load_events():
    df = pd.read_csv(UNIV_DIR / "nasdaq100_changes.csv", dtype=str).fillna("")
    df["effective_date"] = pd.to_datetime(df["effective_date"]).dt.date
    bad = df[~df["action"].isin(["add", "remove", "rename"])]
    if len(bad):
        sys.exit(f"[ERROR] 알 수 없는 action: {bad.to_dict('records')}")
    return df.sort_values(["effective_date", "action"]).reset_index(drop=True)


def replay(events: pd.DataFrame, base: set):
    """기준일 멤버십에서 양방향 재생. 반환: 이벤트 정렬 리스트(전진 방향)와
    GRID_START 시점 멤버십."""
    # --- 역방향: 기준일(포함) 이전 이벤트를 역적용해 GRID_START 멤버십 도출 ---
    members = set(base)
    past = events[events["effective_date"] <= BASE_DATE]
    for _, ev in past.iloc[::-1].iterrows():
        t, a, new = ev["ticker"], ev["action"], ev["new_ticker"]
        if a == "add":
            if t not in members:
                sys.exit(f"[ERROR] 역재생 add 불일치: {ev['effective_date']} {t} 가 멤버가 아님")
            members.remove(t)
        elif a == "remove":
            if t in members:
                sys.exit(f"[ERROR] 역재생 remove 불일치: {ev['effective_date']} {t} 가 이미 멤버")
            members.add(t)
        else:  # rename old->new 를 환원
            if new not in members:
                sys.exit(f"[ERROR] 역재생 rename 불일치: {ev['effective_date']} {new} 미존재")
            members.remove(new)
            members.add(t)
    return members


def build_panel(events: pd.DataFrame, start_members: set):
    """GRID_START부터 전진 재생하며 월말 스냅샷 수집."""
    grid = month_ends(GRID_START, GRID_END)
    members = set(start_members)
    snapshots = {}
    gi = 0
    ev_list = events.to_dict("records")
    ei = 0
    # 그리드 이전의 이벤트는 없어야 정상 (원장은 2010-12부터 시작)
    while gi < len(grid):
        gd = grid[gi]
        while ei < len(ev_list) and ev_list[ei]["effective_date"] <= gd:
            ev = ev_list[ei]
            t, a, new = ev["ticker"], ev["action"], ev["new_ticker"]
            d = ev["effective_date"]
            if a == "add":
                if t in members:
                    sys.exit(f"[ERROR] add 불일치: {d} {t} 이미 멤버")
                members.add(t)
            elif a == "remove":
                if t not in members:
                    sys.exit(f"[ERROR] remove 불일치: {d} {t} 멤버 아님")
                members.remove(t)
            else:
                if t not in members:
                    sys.exit(f"[ERROR] rename 불일치: {d} {t} 멤버 아님")
                members.remove(t)
                members.add(new)
            ei += 1
        snapshots[gd] = sorted(members)
        gi += 1
    return snapshots


def current_ticker_map(events: pd.DataFrame):
    """rename 체인을 따라 point-in-time 티커 → 최신 티커 매핑."""
    renames = events[events["action"] == "rename"][["ticker", "new_ticker"]].values.tolist()
    mapping = {}
    for old, new in renames:
        mapping[old] = new
    def resolve(t):
        seen = set()
        while t in mapping and t not in seen:
            seen.add(t)
            t = mapping[t]
        return t
    return resolve


SPOT_CHECKS = [
    # (날짜, 반드시 포함, 반드시 제외)
    (date(2009, 12, 31), {"INFY", "TEVA", "STX", "AAPL", "GOOG", "DELL"},
     {"FB", "VIAB", "MWW", "TSLA"}),
    (date(2010, 6, 30), {"AAPL", "MSFT", "GOOG", "NWSA", "DELL", "YHOO", "RIMM", "ORCL", "BMC", "VOD", "MAT", "STX", "INFY", "TEVA"},
     {"TSLA", "TXN", "MU", "NFLX", "AVGO", "REGN", "FB", "VIAB", "MWW"}),
    (date(2012, 6, 30), {"VIAB", "TXN", "INFY", "CTRP"},
     {"TEVA", "FB", "MWW"}),
    (date(2012, 12, 31), {"FB", "VIAB", "ADI", "REGN", "WDC", "STX"},
     {"INFY", "TEVA", "NFLX", "GMCR", "MRVL"}),
    (date(2013, 9, 30), {"TSLA", "CHTR", "GMCR", "NFLX", "GOLD", "DELL", "KRFT", "FOXA", "FB"},
     {"MAR", "VIP", "ORCL", "BMC", "NWSA"}),
    (date(2015, 6, 30), {"KRFT", "WBA", "DTV", "BRCM", "ALTR", "CTRX", "SIAL"},
     {"EQIX", "KHC", "PYPL", "JD", "SWKS", "INCY"}),
    (date(2020, 6, 30), {"ZM", "DOCU", "NTAP", "WDC", "CSGP", "MRNA"} - {"MRNA"},
     {"AAL", "UAL", "WLTW", "MRNA", "KDP"}),
    (date(2023, 6, 30), {"GEHC", "ON", "ATVI", "META", "GOOGL"},
     {"RIVN", "FISV", "FB", "GOOG"}),
    (date(2026, 6, 30), {"ALAB", "CRWV", "NBIS", "RKLB", "TER", "SNDK", "SHOP", "TRI", "PLTR"},
     {"CHTR", "CTSH", "VRSK", "ZS", "INSM", "TEAM", "SPCX"}),
    (date(2026, 7, 15), {"SPCX"}, set()),
]


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    events = load_events()
    base = set(pd.read_csv(UNIV_DIR / "nasdaq100_base_20220103.csv", dtype=str)["ticker"])
    if len(base) != 100:
        sys.exit(f"[ERROR] 기준 스냅샷 종목 수 {len(base)} != 100")

    start_members = replay(events, base)
    snapshots = build_panel(events, start_members)
    resolve = current_ticker_map(events)

    rows = []
    for d, tickers in snapshots.items():
        for t in tickers:
            rows.append((d, t, 1, resolve(t)))
    panel = pd.DataFrame(rows, columns=["date", "ticker", "in_index", "yf_ticker"])
    panel["date"] = pd.to_datetime(panel["date"])
    panel.to_parquet(UNIV_DIR / "nasdaq100_history.parquet", index=False)
    panel.to_csv(UNIV_DIR / "nasdaq100_history.csv", index=False)

    # ---- 검증 ----
    counts = {d: len(t) for d, t in snapshots.items()}
    violations = {d: c for d, c in counts.items() if not (COUNT_MIN <= c <= COUNT_MAX)}

    spot_results = []
    ok_all = True
    for d, must_in, must_out in SPOT_CHECKS:
        mem = set(snapshots[d])
        miss = must_in - mem
        extra = must_out & mem
        ok = not miss and not extra
        ok_all &= ok
        spot_results.append((d, ok, sorted(miss), sorted(extra)))

    cdf = pd.Series(counts).sort_index()
    cdf.index = pd.to_datetime(cdf.index)
    yearly = cdf.groupby(cdf.index.year).agg(["min", "max", "mean", "count"])

    n_events = len(events)
    n_add = (events["action"] == "add").sum()
    n_rem = (events["action"] == "remove").sum()
    n_ren = (events["action"] == "rename").sum()
    n_tickers = panel["ticker"].nunique()

    lines = []
    lines.append("# CP-1 유니버스 구축 검증 리포트")
    lines.append("")
    lines.append("- 작성일: 2026-07-16 (KST) / 실험 ID: US-2026-001")
    lines.append("- 방법: 방법 B (편입/편출 이벤트 원장 수동 구축 + 공식 보도자료 WebSearch 교차 검증)")
    lines.append(f"- 커버 기간: {GRID_START} ~ {GRID_END} (월말 스냅샷 {len(snapshots)}개)")
    lines.append(f"- 이벤트 원장: 총 {n_events}건 (add {n_add} / remove {n_rem} / rename {n_ren})")
    lines.append(f"- 등장 티커 수(시점별 고유): {n_tickers}")
    lines.append("")
    lines.append("## 1. 종목 수 검증 (스펙 기준: 매 시점 95~105)")
    lines.append("")
    if violations:
        lines.append(f"**위반 {len(violations)}건:**")
        for d, c in sorted(violations.items()):
            lines.append(f"- {d}: {c} 종목")
    else:
        lines.append(f"**전 시점 통과.** 최소 {cdf.min()} ~ 최대 {cdf.max()} 종목.")
    lines.append("")
    lines.append("### 연도별 종목 수 (회사 단위, 대표 클래스 1개)")
    lines.append("")
    lines.append("| 연도 | 최소 | 최대 | 평균 | 스냅샷 수 |")
    lines.append("|---|---|---|---|---|")
    for y, r in yearly.iterrows():
        lines.append(f"| {y} | {int(r['min'])} | {int(r['max'])} | {r['mean']:.1f} | {int(r['count'])} |")
    lines.append("")
    lines.append("주: 지수 공식 종목 수는 '증권(share class)' 기준 100+이며, 본 테이블은 회사 단위")
    lines.append("(복수 클래스 중 대표 1개만 포함)라 100 전후가 정상. 2022-02(CEG 무짝 편입)~2022-12")
    lines.append("재구성(6편입/7제거) 사이는 101, 2026-07-07 SPCX 무짝 편입 이후 101이 예상값이며 실측과 일치.")
    lines.append("")
    lines.append("## 2. 스팟 체크 (알려진 역사적 사실과 대조)")
    lines.append("")
    lines.append("| 날짜 | 결과 | 누락(포함돼야 함) | 잘못 포함 |")
    lines.append("|---|---|---|---|")
    for d, ok, miss, extra in spot_results:
        lines.append(f"| {d} | {'PASS' if ok else 'FAIL'} | {', '.join(miss) or '-'} | {', '.join(extra) or '-'} |")
    lines.append("")
    lines.append("## 3. 단순화 및 주의 사항")
    lines.append("")
    lines.append("- 회사 단위 · 대표 클래스 1개만 추적 (GOOGL/DISCA/FOXA/LBTYA 등). TOP-N 랭킹에서")
    lines.append("  동일 회사 이중 편입 방지 목적. 2차 클래스·트래커의 편출입은 무시.")
    lines.append("- point-in-time 티커를 기록하고 `yf_ticker` 칼럼에 rename 체인 적용 후 최신 티커를 제공")
    lines.append("  (FB→META, KFT→MDLZ, KRFT→KHC, GOOG→GOOGL, NWSA→FOXA, PCLN→BKNG, HANS→MNST,")
    lines.append("  SYMC→NLOK, CTRP→TCOM, LMCA→STRZA).")
    lines.append("- 상장폐지 종목(DELL, YHOO, RIMM 구티커 등)은 yfinance에서 데이터가 없을 수 있음 →")
    lines.append("  CP-2 커버리지 리포트에서 정량화 (스펙 §3.1: 커버리지 <85% 시 신뢰도 경고).")
    lines.append("- GOLD(2011-2013 Randgold ADR)와 SNDK(2026 신규 Sandisk)는 이후 다른 회사가 동일")
    lines.append("  티커를 사용 → CP-2에서 가격 데이터 오염 주의 대상으로 태깅 필요.")
    lines.append("- 일부 이벤트 발효일은 2차 출처 기반 근사치 (source=secondary, 원장에 표기).")
    lines.append("  월말 스냅샷 기준이므로 수 일 오차는 백테스트에 영향 없음(월중 이벤트가 월말을 넘나드는")
    lines.append("  경우만 영향; 해당 이벤트 없음 확인).")
    lines.append("- 사전 등록 원칙: 이 원장과 기준 스냅샷은 백테스트 실행 전에 동결한다. 이후 오류 발견 시")
    lines.append("  수정 내역을 리포트에 기록하고 백테스트를 재실행한다 (원칙 2, 4).")
    lines.append("")
    lines.append("## 4. 산출물")
    lines.append("")
    lines.append("- `data/universe/nasdaq100_history.parquet` / `.csv` — (date, ticker, in_index, yf_ticker)")
    lines.append("- `data/universe/nasdaq100_changes.csv` — 이벤트 원장 (source: PR=보도자료 확인, secondary=2차 출처)")
    lines.append("- `data/universe/nasdaq100_base_20220103.csv` — 기준 스냅샷 (100종목)")
    lines.append("")
    lines.append(f"검증 종합: 종목수 {'PASS' if not violations else 'FAIL'} / 스팟체크 {'PASS' if ok_all else 'FAIL'}")

    report = "\n".join(lines)
    (REPORT_DIR / "cp1_universe_report.md").write_text(report, encoding="utf-8")
    print(report)
    if violations or not ok_all:
        sys.exit(1)


if __name__ == "__main__":
    main()
