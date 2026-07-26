#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cp0_schema_check.py — TECH_PROFILE v0: CP-0 스키마 및 데이터 가용 범위 점검
작성일: 2026-07-26 (KST)
상위 문서: 작업 지시서 "종목 기술적 구조 분석 (TECH_PROFILE v0)" §2 (CP-0)

로컬(64bit venv) 실행용. 원격 실행 환경에서는 MariaDB 접근이 불가하여
이 스크립트를 로컬에서 실행해 결과 리포트를 공유하는 방식으로 진행한다.

수행 내용 (읽기 전용 — SELECT만 실행, 어떤 데이터도 변경하지 않음):
  1. daily_price / v_trading_volume_stocks / kospi200_history 의
     존재 여부(테이블/뷰)와 컬럼 목록·타입 조회
  2. 대상 종목(기본 035420)의 daily_price 데이터 시작일/종료일/행수,
     결측 거래일 수 (시장 거래일 달력 = daily_price 전체의 DISTINCT 일자 기준)
  3. v_trading_volume_stocks 의 모든 수치형 컬럼에 대해 대상 종목 기준
     비결측 건수 / 최초 비결측일 / 최초 0이 아닌 값 일자 / 최종 비결측일
     → 수급 컬럼 중 실제로 채워진 것과 백필 유효 시작 시점 판별
  4. 기대 수급 컬럼(개인/외국인/기관 합계, fininv, trust, priv, insur,
     bank, etcfin, govt)과 실제 컬럼명의 키워드 매칭 결과
  5. kospi200_history 의 기간/종목 수, 대상 종목 편입 구간

산출물:
  - reports/tech_profile/cp0_schema_report.md
  - reports/tech_profile/cp0_flow_column_stats.csv
  - logs/tech_profile_YYYYMMDD.log

실행 예:
  python cp0_schema_check.py --code 035420
  python cp0_schema_check.py --code 035420 --config C:\\stock\\config\\db_config.json

주의: 원본 trading_volume 테이블은 직접 조회하지 않는다 (ELW 제외 뷰만 사용).
단위: trading_volume 계열은 주식 수(株) 기준.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_loader import (  # noqa: E402
    detect_key_columns, fetch_df, get_connection, is_numeric_type,
    load_db_config, object_type, table_columns,
)

# Windows에서 tzdata 미설치 시 zoneinfo가 실패하므로 고정 오프셋으로 폴백
try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9), "KST")

TABLES = ["daily_price", "v_trading_volume_stocks", "kospi200_history"]

# 기대 수급 컬럼 → 실제 컬럼명 매칭용 키워드 (소문자 부분일치)
EXPECTED_FLOW = {
    "개인": ["indi", "person", "individual", "개인"],
    "외국인": ["frgn", "forei", "foreign", "외국인"],
    "기관 합계": ["inst", "orgn", "기관"],
    "금융투자(fininv)": ["fininv"],
    "투자신탁(trust)": ["trust"],
    "사모(priv)": ["priv"],
    "보험(insur)": ["insur"],
    "은행(bank)": ["bank"],
    "기타금융(etcfin)": ["etcfin"],
    "연기금(govt)": ["govt", "pension"],
}


def setup_logging(root: Path) -> logging.Logger:
    logdir = root / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"tech_profile_{datetime.now(KST):%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("cp0")


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(비어 있음)_"
    header = "| " + " | ".join(str(c) for c in df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    rows = ["| " + " | ".join("" if v is None else str(v) for v in r) + " |"
            for r in df.itertuples(index=False)]
    return "\n".join([header, sep] + rows)


def schema_section(conn, log) -> tuple[list[str], dict[str, pd.DataFrame]]:
    lines, schemas = [], {}
    lines.append("## 1. 테이블/뷰 존재 및 스키마\n")
    for t in TABLES:
        typ = object_type(conn, t)
        if typ is None:
            log.warning("%s: 존재하지 않음", t)
            lines.append(f"### `{t}` — **존재하지 않음**\n")
            continue
        cols = table_columns(conn, t)
        schemas[t] = cols
        log.info("%s: %s, %d개 컬럼", t, typ, len(cols))
        lines.append(f"### `{t}` ({typ}, 컬럼 {len(cols)}개)\n")
        lines.append(md_table(cols))
        lines.append("")
    return lines, schemas


def price_section(conn, log, code: str, schemas: dict,
                  code_col_arg, date_col_arg) -> list[str]:
    lines = [f"## 2. `daily_price` — {code} 데이터 가용 범위\n"]
    if "daily_price" not in schemas:
        lines.append("_daily_price가 없어 건너뜀_\n")
        return lines
    cols = list(schemas["daily_price"]["COLUMN_NAME"])
    code_col, date_col = detect_key_columns(cols, code_col_arg, date_col_arg)
    if not code_col or not date_col:
        lines.append(f"_종목코드/일자 컬럼 자동 탐지 실패 (컬럼: {cols}). "
                     f"`--code-col` / `--date-col` 로 지정 후 재실행 필요_\n")
        return lines
    lines.append(f"- 키 컬럼: 종목코드=`{code_col}`, 일자=`{date_col}` (자동 탐지)")

    q = (f"SELECT COUNT(*) AS n_rows, COUNT(DISTINCT `{date_col}`) AS n_dates, "
         f"MIN(`{date_col}`) AS first_date, MAX(`{date_col}`) AS last_date "
         f"FROM daily_price WHERE `{code_col}` = %s")
    s = fetch_df(conn, q, (code,)).iloc[0]
    log.info("daily_price %s: %s ~ %s, rows=%s", code, s["first_date"], s["last_date"], s["n_rows"])
    if not s["n_rows"]:
        lines.append(f"- **{code} 데이터 없음**\n")
        return lines
    lines.append(f"- 시작일: **{s['first_date']}** / 종료일: **{s['last_date']}**")
    lines.append(f"- 행수: {s['n_rows']:,} (고유 일자 {s['n_dates']:,}"
                 + (", **중복 일자 존재**" if s["n_rows"] != s["n_dates"] else ", 중복 없음") + ")")

    # 시장 거래일 달력: daily_price 전체의 DISTINCT 일자 (해당 종목 보유 범위 내)
    cal = fetch_df(
        conn,
        f"SELECT COUNT(DISTINCT `{date_col}`) AS n FROM daily_price "
        f"WHERE `{date_col}` BETWEEN %s AND %s",
        (s["first_date"], s["last_date"]),
    ).iloc[0]["n"]
    missing = int(cal) - int(s["n_dates"])
    lines.append(f"- 시장 거래일(달력=daily_price 전체 DISTINCT 일자, 동일 범위): {int(cal):,}")
    lines.append(f"- **결측 거래일 수: {missing:,}** (보간하지 않음, 결측은 결측으로 처리 예정)")
    if 0 < missing <= 60:
        miss_df = fetch_df(
            conn,
            f"SELECT DISTINCT a.`{date_col}` AS missing_date FROM daily_price a "
            f"LEFT JOIN (SELECT `{date_col}` AS d FROM daily_price WHERE `{code_col}` = %s) b "
            f"ON a.`{date_col}` = b.d "
            f"WHERE b.d IS NULL AND a.`{date_col}` BETWEEN %s AND %s "
            f"ORDER BY 1",
            (code, s["first_date"], s["last_date"]),
        )
        lines.append("- 결측 일자 목록:")
        lines.append(md_table(miss_df))
    lines.append("")
    return lines


def flow_section(conn, log, code: str, schemas: dict, outdir: Path,
                 code_col_arg, date_col_arg) -> list[str]:
    view = "v_trading_volume_stocks"
    lines = [f"## 3. `{view}` — {code} 수급 컬럼별 채움 현황\n",
             "단위 주의: trading_volume 계열은 **주식 수(株)** 기준. "
             "금액 기준 분석 시 종가를 곱해 별도 컬럼으로 파생하고 리포트에 명시 예정.\n"]
    if view not in schemas:
        lines.append(f"_{view}가 없어 건너뜀_\n")
        return lines
    sch = schemas[view]
    cols = list(sch["COLUMN_NAME"])
    code_col, date_col = detect_key_columns(cols, code_col_arg, date_col_arg)
    if not code_col or not date_col:
        lines.append(f"_종목코드/일자 컬럼 자동 탐지 실패 (컬럼: {cols})_\n")
        return lines
    lines.append(f"- 키 컬럼: 종목코드=`{code_col}`, 일자=`{date_col}` (자동 탐지)")

    num_cols = [r["COLUMN_NAME"] for _, r in sch.iterrows()
                if is_numeric_type(str(r["COLUMN_TYPE"]))
                and r["COLUMN_NAME"] not in (code_col, date_col)]
    if not num_cols:
        lines.append("_수치형 컬럼 없음_\n")
        return lines

    # 단일 스캔으로 컬럼별 집계 (뷰 1회 조회)
    parts = ["COUNT(*) AS n_rows",
             f"MIN(`{date_col}`) AS first_date", f"MAX(`{date_col}`) AS last_date"]
    for c in num_cols:
        parts += [
            f"SUM(`{c}` IS NOT NULL) AS `{c}__nn`",
            f"MIN(CASE WHEN `{c}` IS NOT NULL THEN `{date_col}` END) AS `{c}__first_nn`",
            f"MIN(CASE WHEN `{c}` IS NOT NULL AND `{c}` <> 0 THEN `{date_col}` END) AS `{c}__first_nz`",
            f"MAX(CASE WHEN `{c}` IS NOT NULL THEN `{date_col}` END) AS `{c}__last_nn`",
        ]
    q = f"SELECT {', '.join(parts)} FROM {view} WHERE `{code_col}` = %s"
    s = fetch_df(conn, q, (code,)).iloc[0]
    n_rows = int(s["n_rows"])
    log.info("%s %s: %s ~ %s, rows=%d, 수치형 컬럼 %d개",
             view, code, s["first_date"], s["last_date"], n_rows, len(num_cols))
    lines.append(f"- {code} 행수: {n_rows:,} / 기간: {s['first_date']} ~ {s['last_date']}\n")
    if n_rows == 0:
        lines.append(f"- **{code} 데이터 없음**\n")
        return lines

    rows = []
    for c in num_cols:
        nn = int(s[f"{c}__nn"] or 0)
        rows.append({
            "column": c,
            "type": sch.loc[sch["COLUMN_NAME"] == c, "COLUMN_TYPE"].iloc[0],
            "non_null": nn,
            "coverage_pct": round(100.0 * nn / n_rows, 1),
            "first_non_null": s[f"{c}__first_nn"],
            "first_nonzero": s[f"{c}__first_nz"],
            "last_non_null": s[f"{c}__last_nn"],
        })
    stats = pd.DataFrame(rows)
    outdir.mkdir(parents=True, exist_ok=True)
    csv_path = outdir / "cp0_flow_column_stats.csv"
    stats.to_csv(csv_path, index=False, encoding="utf-8-sig")
    log.info("저장: %s", csv_path)
    lines.append(md_table(stats))
    lines.append("\n> `first_non_null`/`first_nonzero`가 곧 해당 컬럼 백필의 유효 시작 시점. "
                 "그 이전 구간은 Phase 2에서 **공백으로 표시**한다 (0 채움 금지).\n")

    # 기대 수급 컬럼 키워드 매칭
    lines.append("### 3.1 기대 수급 컬럼 매칭 결과\n")
    mrows = []
    for label, keys in EXPECTED_FLOW.items():
        hit = [c for c in num_cols if any(k in c.lower() for k in keys)]
        mrows.append({"기대 컬럼": label,
                      "매칭된 실제 컬럼": ", ".join(hit) if hit else "(없음)"})
    lines.append(md_table(pd.DataFrame(mrows)))
    lines.append("\n> '(없음)'이 있으면 실제 컬럼 목록(§1)을 보고 수동 매핑을 확정한 뒤 "
                 "Phase 2 코드에 반영한다.\n")
    return lines


def universe_section(conn, log, code: str, schemas: dict,
                     code_col_arg, date_col_arg) -> list[str]:
    t = "kospi200_history"
    lines = [f"## 4. `{t}` — 유니버스 가용 범위 (Phase 3 생존편향 보정용)\n"]
    if t not in schemas:
        lines.append(f"_{t}가 없어 건너뜀_\n")
        return lines
    cols = list(schemas[t]["COLUMN_NAME"])
    code_col, date_col = detect_key_columns(cols, code_col_arg, date_col_arg)
    if not code_col or not date_col:
        lines.append(f"_종목코드/일자 컬럼 자동 탐지 실패 (컬럼: {cols})_\n")
        return lines
    s = fetch_df(
        conn,
        f"SELECT COUNT(*) AS n_rows, COUNT(DISTINCT `{code_col}`) AS n_codes, "
        f"COUNT(DISTINCT `{date_col}`) AS n_dates, "
        f"MIN(`{date_col}`) AS first_date, MAX(`{date_col}`) AS last_date FROM {t}",
    ).iloc[0]
    log.info("%s: %s ~ %s, codes=%s", t, s["first_date"], s["last_date"], s["n_codes"])
    lines.append(f"- 키 컬럼: 종목코드=`{code_col}`, 일자=`{date_col}` (자동 탐지)")
    lines.append(f"- 기간: **{s['first_date']} ~ {s['last_date']}** / "
                 f"행수 {int(s['n_rows']):,} / 고유 일자 {int(s['n_dates']):,} / "
                 f"고유 종목 {int(s['n_codes']):,}")
    me = fetch_df(
        conn,
        f"SELECT COUNT(*) AS n, MIN(`{date_col}`) AS first_date, "
        f"MAX(`{date_col}`) AS last_date FROM {t} WHERE `{code_col}` = %s",
        (code,),
    ).iloc[0]
    if int(me["n"]):
        lines.append(f"- {code} 편입 기록: {int(me['n']):,}건, "
                     f"{me['first_date']} ~ {me['last_date']}")
    else:
        lines.append(f"- **{code} 편입 기록 없음** — Phase 3 표본 포함 여부 확인 필요")
    lines.append("")
    return lines


def main():
    ap = argparse.ArgumentParser(description="TECH_PROFILE v0 CP-0 스키마 점검")
    ap.add_argument("--code", default="035420", help="종목코드 (기본 035420)")
    ap.add_argument("--config", default=None, help="DB 설정 파일 (.json/.ini)")
    ap.add_argument("--code-col", default=None, help="종목코드 컬럼명 수동 지정")
    ap.add_argument("--date-col", default=None, help="일자 컬럼명 수동 지정")
    args = ap.parse_args()

    # 산출물 루트: C:\stock\02_validation\tech_profile 기준 상위(C:\stock)의
    # reports/logs 가 아니라, 지시서 산출물 경로(reports/tech_profile/)를
    # 저장소 루트 기준으로 통일한다.
    root = Path(__file__).resolve().parents[2]
    outdir = root / "reports" / "tech_profile"
    log = setup_logging(root)
    log.info("CP-0 시작: code=%s", args.code)

    cfg = load_db_config(args.config)
    log.info("DB 접속: host=%s db=%s (자격증명은 로그에 기록하지 않음)",
             cfg["host"], cfg["database"])
    conn = get_connection(cfg)
    try:
        lines = [
            "# CP-0 스키마 및 데이터 가용 범위 리포트",
            "",
            f"- 실행일시: {datetime.now(KST):%Y-%m-%d %H:%M} (KST)",
            f"- 대상 종목: {args.code}",
            f"- DB: `{cfg['database']}` (읽기 전용 조회만 수행)",
            "- 원본 `trading_volume` 직접 조회 없음 (ELW 제외 뷰만 사용)",
            "",
        ]
        sec1, schemas = schema_section(conn, log)
        lines += sec1
        lines += price_section(conn, log, args.code, schemas, args.code_col, args.date_col)
        lines += flow_section(conn, log, args.code, schemas, outdir, args.code_col, args.date_col)
        lines += universe_section(conn, log, args.code, schemas, args.code_col, args.date_col)
        lines += [
            "## 5. 다음 단계",
            "",
            "- 이 리포트를 공유하면 CP-0 검토 후 Phase 1(거래량 프로파일) 코드를 작성한다.",
            "- §3.1 매칭 '(없음)' 항목이 있으면 실제 컬럼명 매핑을 함께 확정한다.",
            "",
        ]
        outdir.mkdir(parents=True, exist_ok=True)
        report = outdir / "cp0_schema_report.md"
        report.write_text("\n".join(lines), encoding="utf-8")
        log.info("리포트 저장: %s", report)
        print(f"\n완료. 리포트: {report}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
