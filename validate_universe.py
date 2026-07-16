#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_universe.py — CP-1.1: 유니버스 원장/스냅샷 자동 검증
작성일: 2026-07-16 (KST)
실험 ID: US-2026-001 / 상위 문서: docs/US_TREND_v0_spec.md, CP-1.1 지시서

체크 목록 (CP-1.1 §2.2):
 1. IPO 정합성: 티커의 유니버스 최초 등장일이 최초 거래 가능일보다 30일 이상
    앞서면 P0 플래그. 1차 소스는 큐레이션 테이블(listing_dates.csv),
    yfinance 사용 가능 환경에서는 실측 최초 가격일로 보강(옵션).
 2. 거래소 정합성: IPO 체크에서 플래그된 종목의 당시 상장 거래소를 수동 확인
    (자동화 불가 — 리포트에 확인 결과 기록).
 3. 쌍 정합성: 동일 발효일의 add/remove 개수 불일치 플래그
    (알려진 무짝/비대칭 이벤트는 화이트리스트).
 4. rename 체인: point-in-time 티커가 당시 실제 티커와 다른 알려진 사례를
    P2로 기록 (yf_ticker 해소가 올바르면 백테스트 영향 없음).

종료 코드: P0 플래그 존재 시 1, 아니면 0.
"""

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
UNIV = ROOT / "data" / "universe"

# 무짝/비대칭이 정상인 발효일 (사유 명기)
PAIR_WHITELIST = {
    "2022-02-02": "CEG 무짝 편입 (Exelon 스핀오프)",
    "2022-12-19": "연례 재구성 6편입/7제거 (공식 PR 확인)",
    "2026-07-07": "SPCX fast-entry 무짝 편입 (지수 일시 101개)",
}

# point-in-time 티커 ≠ 당시 실제 티커로 알려진 사례 (P2: yf_ticker 해소로 무해)
RENAME_P2_NOTES = [
    ("QRTEA", "2010~2015 실제 티커 LINTA, 2015~2018 QVCA. yfinance QRTEA가 체인 전체 가격 보유 → 무해"),
    ("EA", "2010~2011-08 실제 티커 ERTS. yfinance EA가 전체 이력 보유 → 무해"),
    ("TEVA", "2012-05 이후 NYSE 상장이나 티커 동일, yfinance 연속 이력 보유 → 무해"),
    ("INFY", "2012-12 이후 NYSE 상장이나 티커 동일, yfinance 연속 이력 보유 → 무해"),
    ("GOLD", "2011~2013 Randgold ADR. 2019년부터 Barrick이 동일 티커 사용 → CP-2에서 가격 오염 주의 (P1 태깅)"),
    ("SNDK", "2026 신규 Sandisk. 2010~2016 구 SanDisk와 동일 티커, 별개 회사 → CP-2에서 구간 분리 필요 (P1 태깅)"),
    ("VIP", "2017년 VEON으로 개명(제거 후) — 윈도우 내 영향 없음"),
    ("YHOO", "2017-06 제거 직후 AABA로 개명 — 윈도우 내 영향 없음"),
]


def load():
    hist = pd.read_csv(UNIV / "nasdaq100_history.csv", parse_dates=["date"])
    chg = pd.read_csv(UNIV / "nasdaq100_changes.csv", dtype=str).fillna("")
    chg["effective_date"] = pd.to_datetime(chg["effective_date"])
    listing = pd.read_csv(UNIV / "listing_dates.csv", dtype=str)
    listing["first_trade_date"] = pd.to_datetime(listing["first_trade_date"])
    return hist, chg, listing


def membership_eras(hist):
    """티커별 연속 멤버십 구간(월말 스냅샷 연속 런) 목록: {ticker: [(start,end), ...]}"""
    eras = {}
    for t, g in hist.groupby("ticker"):
        dates = sorted(g["date"])
        runs, s, prev = [], dates[0], dates[0]
        for d in dates[1:]:
            if (d - prev).days > 45:  # 월말 그리드에서 한 칸 초과 공백 → 새 구간
                runs.append((s, prev))
                s = d
            prev = d
        runs.append((s, prev))
        eras[t] = runs
    return eras


def check_ipo_consistency(hist, listing):
    """체크 1: 멤버십 구간(era) 시작일 < 최초 거래일 - 30일 → P0.
    티커 재사용(구 회사의 과거 구간이 상장일 이전에 이미 종료) 구간은 이 행의
    대상이 아니므로 스킵하고 참고 노트만 남긴다."""
    eras = membership_eras(hist)
    flags, reuse_notes = [], []
    for _, row in listing.iterrows():
        t = row["ticker"]
        if t not in eras:
            continue
        cutoff = row["first_trade_date"] - timedelta(days=30)
        for (s, e) in eras[t]:
            if e < row["first_trade_date"]:
                # 상장일 이전에 끝난 구간 = 동일 티커의 다른 법인(재사용) → CP-2 분리 대상
                reuse_notes.append((t, str(s.date()), str(e.date()), str(row["first_trade_date"].date())))
                continue
            if s < cutoff:
                flags.append((t, str(s.date()), str(row["first_trade_date"].date()), row["note"]))
    # yfinance 보강 (네트워크 허용 환경에서만 동작)
    yf_status = "미실행 (yfinance/네트워크 불가 환경)"
    try:
        import yfinance as yf  # noqa
        _ = yf.download(["AAPL"], period="5d", progress=False)
        yf_status = "가능 — CP-2에서 전 종목 실측 최초 가격일로 재검증 예정"
    except Exception:
        pass
    return flags, reuse_notes, yf_status


def check_pair_consistency(chg):
    """체크 3: 동일 발효일 add/remove 개수 불일치 → 화이트리스트 외 P0."""
    ev = chg[chg["action"].isin(["add", "remove"])]
    g = ev.groupby([ev["effective_date"].dt.strftime("%Y-%m-%d"), "action"]).size().unstack(fill_value=0)
    flags, whitelisted = [], []
    for d, row in g.iterrows():
        a, r = int(row.get("add", 0)), int(row.get("remove", 0))
        if a != r:
            if d in PAIR_WHITELIST:
                whitelisted.append((d, a, r, PAIR_WHITELIST[d]))
            else:
                flags.append((d, a, r))
    return flags, whitelisted


SHORT_ERA_WHITELIST = {
    "HANS": "2011-12 편입 직후 2012-01 MNST로 rename — 정상 (티커 승계)",
    "NLOK": "2019-11 SYMC→NLOK rename 직후 2019-12 제거 — 정상",
    "SPCX": "2026-07-07 최근 편입 — 정상 (스냅샷 1개는 시간 경과로 해소)",
}


def check_membership_sanity(hist):
    """보조: 스냅샷 1개짜리(한 달 미만 존속) 멤버십은 원장 오류 가능성 → 확인 대상."""
    issues = []
    per = hist.groupby("ticker")["date"].agg(["min", "max", "count"])
    one_shot = per[per["count"] == 1]
    for t, r in one_shot.iterrows():
        if t in SHORT_ERA_WHITELIST:
            issues.append((t, str(r["min"].date()), f"(확인 완료) {SHORT_ERA_WHITELIST[t]}", False))
        else:
            issues.append((t, str(r["min"].date()), "월말 스냅샷 1개만 존재 — 원장 확인 필요", True))
    return issues


def main():
    hist, chg, listing = load()
    p0 = 0

    print("# validate_universe.py 결과 (작성일 2026-07-16 KST)\n")

    ipo_flags, reuse_notes, yf_status = check_ipo_consistency(hist, listing)
    print("## 체크 1 — IPO 정합성 (P0)")
    if ipo_flags:
        p0 += len(ipo_flags)
        for t, seen, listed, note in ipo_flags:
            print(f"- [P0] {t}: 멤버십 구간 시작 {seen} < 최초 거래 {listed} ({note})")
    else:
        print(f"- 플래그 없음 (큐레이션 상장일 {len(listing)}건, 멤버십 구간 단위 대조)")
    for t, s, e, listed in reuse_notes:
        print(f"- (티커 재사용 감지) {t}: 과거 구간 {s}~{e}는 상장일 {listed} 이전 종료 → 다른 법인. CP-2에서 구간 분리")
    print(f"- yfinance 실측 보강: {yf_status}\n")

    pair_flags, whitelisted = check_pair_consistency(chg)
    print("## 체크 3 — 쌍 정합성 (P0)")
    if pair_flags:
        p0 += len(pair_flags)
        for d, a, r in pair_flags:
            print(f"- [P0] {d}: add {a} / remove {r} 불일치 (화이트리스트 외)")
    else:
        print("- 플래그 없음")
    for d, a, r, reason in whitelisted:
        print(f"- (화이트리스트) {d}: add {a}/remove {r} — {reason}")
    print()

    sanity = check_membership_sanity(hist)
    print("## 보조 체크 — 단기 존속 멤버십")
    open_issues = [x for x in sanity if x[3]]
    for t, d, msg, is_open in sanity:
        print(f"- [{'P1-확인필요' if is_open else 'OK'}] {t} @ {d}: {msg}")
    if not sanity:
        print("- 플래그 없음")
    p0 += 0 if not open_issues else 0  # P1은 P0에 합산하지 않음 (리포트로 관리)
    print()

    print("## 체크 4 — rename 체인 / 티커 재사용 (P2 기록)")
    for t, note in RENAME_P2_NOTES:
        print(f"- {t}: {note}")
    print()

    print(f"P0 플래그 합계: {p0} → {'FAIL' if p0 else 'PASS'}")
    sys.exit(1 if p0 else 0)


if __name__ == "__main__":
    main()
