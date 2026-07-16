#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_prices.py — CP-2: 가격 데이터 수집 (yfinance)
작성일: 2026-07-16 (KST)
실험 ID: US-2026-001 / 상위 문서: docs/US_TREND_v0_spec.md §3.1, CP-2

독립 실행 가능(로컬 실행용). 필요 패키지:
    pip install yfinance pandas pyarrow

입력:
  - data/universe/nasdaq100_history.csv   (CP-1.1 동결본; date,ticker,in_index,yf_ticker)
  - data/universe/data_overrides.csv      (티커 재사용/승계 별칭·유효구간 규칙)
  - data/universe/listing_dates.csv       (IPO 정합성 실측 재검증용)

동작:
  1. 유니버스 전 티커 + QQQ + KRW=X 일봉 OHLCV (auto_adjust=True) 2005-01-01~현재 수집
  2. yf_alias 적용(RIMM→BB 등 데이터 승계), 유효구간 밖 데이터 제거(GOLD/DELL 등 재사용 오염 방지)
  3. data/prices/{TICKER}.parquet 저장 + 통합 패널(adj close/volume/dollar volume) 저장
  4. 커버리지 리포트: 멤버십 시대(era)별 시작일/결측일수/커버리지, 상폐(결측) 종목 목록,
     월별 유니버스 커버리지 비율 (<85% 경고), IPO 정합성 실측 재검증(CP-1.1 체크 1 보강)

산출물:
  - data/prices/*.parquet, data/prices/panel_close.parquet, panel_volume.parquet, panel_dollarvol.parquet
  - reports/cp2_coverage_report.md
"""

import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance가 필요합니다: pip install yfinance")

ROOT = Path(__file__).resolve().parent
UNIV = ROOT / "data" / "universe"
PRICES = ROOT / "data" / "prices"
REPORTS = ROOT / "reports"

START = "2005-01-01"
BENCH = ["QQQ", "KRW=X"]
COVERAGE_WARN = 0.85
RUN_DATE = date.today().isoformat()


def load_universe():
    hist = pd.read_csv(UNIV / "nasdaq100_history.csv", parse_dates=["date"])
    ovr = pd.read_csv(UNIV / "data_overrides.csv", dtype=str).fillna("")
    ovr = ovr.set_index("ticker")
    return hist, ovr


def membership_eras(hist):
    eras = {}
    for t, g in hist.groupby("ticker"):
        dates = sorted(g["date"])
        runs, s, prev = [], dates[0], dates[0]
        for d in dates[1:]:
            if (d - prev).days > 45:
                runs.append((s, prev))
                s = d
            prev = d
        runs.append((s, prev))
        eras[t] = runs
    return eras


def fetch_symbol(sym: str, retries: int = 3) -> pd.DataFrame:
    for i in range(retries):
        try:
            df = yf.download(sym, start=START, auto_adjust=True, progress=False, multi_level_index=False)
            if df is not None and len(df):
                df.index.name = "date"
                return df[["Open", "High", "Low", "Close", "Volume"]].rename(
                    columns=str.lower)
            return pd.DataFrame()
        except Exception as e:
            if i == retries - 1:
                print(f"  [WARN] {sym}: {e}")
                return pd.DataFrame()
            time.sleep(2 * (i + 1))
    return pd.DataFrame()


def resolve_fetch_candidates(t: str, hist: pd.DataFrame, ovr: pd.DataFrame) -> list:
    """수집 후보 yahoo 티커 목록(순서대로 시도).
    override alias(파이프 | 로 복수 지정 가능) > rename 체인 결과(yf_ticker)의 alias > yf_ticker."""
    cands = []
    if t in ovr.index and ovr.loc[t, "yf_alias"]:
        cands += ovr.loc[t, "yf_alias"].split("|")
    yft = hist.loc[hist["ticker"] == t, "yf_ticker"].iloc[0] or t
    if yft in ovr.index and ovr.loc[yft, "yf_alias"]:
        cands += ovr.loc[yft, "yf_alias"].split("|")
    cands.append(yft)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def apply_validity(t: str, df: pd.DataFrame, ovr: pd.DataFrame) -> pd.DataFrame:
    """유효구간 밖 데이터 제거 (티커 재사용 오염 방지)."""
    if t not in ovr.index or df.empty:
        return df
    vf, vt = ovr.loc[t, "valid_from"], ovr.loc[t, "valid_to"]
    if vf:
        df = df[df.index >= pd.Timestamp(vf)]
    if vt:
        df = df[df.index <= pd.Timestamp(vt)]
    return df


def main():
    PRICES.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    hist, ovr = load_universe()
    eras = membership_eras(hist)
    tickers = sorted(hist["ticker"].unique())
    listing = pd.read_csv(UNIV / "listing_dates.csv", dtype=str)
    listing["first_trade_date"] = pd.to_datetime(listing["first_trade_date"])
    listing = listing.set_index("ticker")

    print(f"유니버스 티커 {len(tickers)}개 + 벤치마크 {BENCH} 수집 시작 ({RUN_DATE})")

    frames = {}
    fetch_map = {}

    def fetch_one(t):
        """후보 심볼을 순서대로 시도, 첫 비어있지 않은 데이터 채택."""
        for ysym in resolve_fetch_candidates(t, hist, ovr):
            raw = fetch_symbol(ysym)
            if not raw.empty:
                return ysym, apply_validity(t, raw, ovr)
        return resolve_fetch_candidates(t, hist, ovr)[0], pd.DataFrame()

    for i, t in enumerate(tickers, 1):
        fetch_map[t], frames[t] = fetch_one(t)
        if i % 20 == 0:
            print(f"  {i}/{len(tickers)} …")

    # 재시도 패스: 빈 결과는 레이트리밋 등 일시 실패일 수 있음 (예: 활성 티커 HOLX가 0건)
    empty1 = [t for t in tickers if frames[t].empty and t not in ovr.index[
        (ovr["valid_from"] == "9999-01-01")].tolist()]
    if empty1:
        print(f"1차 결측 {len(empty1)}건 재시도 (30초 대기 후)…")
        time.sleep(30)
        for t in empty1:
            fetch_map[t], frames[t] = fetch_one(t)

    for t in tickers:
        if not frames[t].empty:
            frames[t].to_parquet(PRICES / f"{t}.parquet")
    for b in BENCH:
        df = fetch_symbol(b)
        safe = b.replace("=", "_")
        if df.empty:
            sys.exit(f"[ERROR] 벤치마크 {b} 수집 실패 — 중단")
        df.to_parquet(PRICES / f"{safe}.parquet")
        frames[b] = df

    # ---- 통합 패널 ----
    close = pd.DataFrame({t: f["close"] for t, f in frames.items() if not f.empty and t not in BENCH})
    vol = pd.DataFrame({t: f["volume"] for t, f in frames.items() if not f.empty and t not in BENCH})
    dollarvol = close * vol
    close.to_parquet(PRICES / "panel_close.parquet")
    vol.to_parquet(PRICES / "panel_volume.parquet")
    dollarvol.to_parquet(PRICES / "panel_dollarvol.parquet")

    qqq = frames["QQQ"]["close"]
    trading_days = qqq.index  # 거래일 달력 기준

    # ---- 커버리지 (era 단위) ----
    rows = []
    for t in tickers:
        df = frames[t]
        first_px = df.index.min() if not df.empty else None
        last_px = df.index.max() if not df.empty else None
        for (s, e) in eras[t]:
            era_days = trading_days[(trading_days >= s) & (trading_days <= e)]
            if df.empty:
                have = 0
            else:
                have = df.index[(df.index >= s) & (df.index <= e)].size
            cov = have / len(era_days) if len(era_days) else float("nan")
            rows.append({
                "ticker": t, "yf_symbol": fetch_map[t],
                "era_start": s.date(), "era_end": e.date(),
                "era_trading_days": len(era_days), "days_with_price": have,
                "era_coverage": round(cov, 4) if cov == cov else None,
                "first_price": first_px.date() if first_px is not None else None,
                "last_price": last_px.date() if last_px is not None else None,
            })
    cov_df = pd.DataFrame(rows)
    cov_df.to_csv(REPORTS / "cp2_coverage_by_era.csv", index=False)

    # ---- 월별 유니버스 커버리지 (스냅샷 시점에 유효 가격 보유 멤버 비율) ----
    monthly = []
    for d, g in hist.groupby("date"):
        members = list(g["ticker"])
        ok = 0
        for t in members:
            df = frames[t]
            if not df.empty and df.index[(df.index >= d - pd.Timedelta(days=7)) & (df.index <= d)].size:
                ok += 1
        monthly.append({"date": d.date(), "members": len(members), "with_price": ok,
                        "coverage": round(ok / len(members), 4)})
    mon_df = pd.DataFrame(monthly)
    mon_df.to_csv(REPORTS / "cp2_coverage_monthly.csv", index=False)

    # ---- IPO 정합성 실측 재검증 (CP-1.1 체크 1 보강) ----
    # 알려진 승계 갭 (유니버스 오류 아님; 데이터 부재로 결측 처리가 올바른 상태)
    KNOWN_GAPS = {
        "FOXA": "2013-06~2019-03 구간은 21세기폭스(舊 FOXA) 시대 — 현 yahoo FOXA(Fox Corp, 2019 상장)에 해당 이력 없음. 결측 인지 후 진행",
    }
    ipo_flags = []
    for t in tickers:
        if t in KNOWN_GAPS:
            continue
        df = frames[t]
        if df.empty:
            continue
        first_px = df.index.min()
        for (s, e) in eras[t]:
            if e < first_px - pd.Timedelta(days=1):
                continue  # 데이터 이전에 끝난 era(재사용 구 시대)는 결측으로 별도 집계됨
            if s < first_px - pd.Timedelta(days=30):
                ipo_flags.append((t, str(s.date()), str(first_px.date())))

    # 0거래일 era(rename 전환 달의 스냅샷 1개짜리 인공물: HANS→MNST, SYMC→NLOK 등)는 결측 아님
    real_eras = cov_df[cov_df["era_trading_days"] > 0]
    missing = real_eras[real_eras["era_coverage"] == 0]
    low = real_eras[(real_eras["era_coverage"] > 0) & (real_eras["era_coverage"] < 0.95)]
    overall = mon_df["coverage"].mean()
    worst = mon_df["coverage"].min()

    lines = [
        "# CP-2 가격 데이터 커버리지 리포트",
        "",
        f"- 실행일: {RUN_DATE} (KST 기준 작성) / 실험 ID: US-2026-001",
        f"- 수집: 유니버스 {len(tickers)}티커 + QQQ, KRW=X / 기간 {START}~{RUN_DATE} / auto_adjust=True",
        f"- 수집 성공: {sum(1 for t in tickers if not frames[t].empty)} / {len(tickers)}",
        "",
        "## 1. 유니버스 커버리지 (월별 멤버 중 가격 보유 비율)",
        f"- 전체 평균: **{overall:.1%}** / 최저 월: **{worst:.1%}**",
        f"- 판정: {'경고 — 신뢰도 주의 (스펙 §3.1: <85%)' if overall < COVERAGE_WARN else '기준(85%) 충족'}",
        f"- 상세: reports/cp2_coverage_monthly.csv",
        "",
        "## 2. 결측(상폐 등) 멤버십 구간 — 가격 전무",
        "",
        missing.to_markdown(index=False) if len(missing) else "- 없음",
        "",
        "## 3. 부분 결측 구간 (era 커버리지 <95%)",
        "",
        low.to_markdown(index=False) if len(low) else "- 없음",
        "",
        "## 4. IPO 정합성 실측 재검증 (P0)",
        "",
    ]
    if ipo_flags:
        lines += [f"- [P0] {t}: era 시작 {s} < 실측 최초 가격 {p} - 30일" for t, s, p in ipo_flags]
    else:
        lines.append("- 플래그 없음")
    lines += [""] + [f"- [알려진 갭, 통과 처리] {t}: {msg}" for t, msg in KNOWN_GAPS.items()]
    lines += [
        "",
        "## 5. 티커 재사용/승계 처리 내역 (data_overrides.csv 적용)",
        "",
        pd.read_csv(UNIV / "data_overrides.csv").to_markdown(index=False),
        "",
        "주의: GOLD·구DELL·구NWSA·구SNDK 시대는 다른 법인 가격 오염 방지를 위해 의도적으로 결측 처리됨.",
    ]
    (REPORTS / "cp2_coverage_report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:14]))
    print(f"\n리포트 저장: reports/cp2_coverage_report.md")
    if ipo_flags:
        sys.exit(1)


if __name__ == "__main__":
    main()
