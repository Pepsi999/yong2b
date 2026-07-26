#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
db_loader.py — TECH_PROFILE v0: DB 접속 + 스키마 검증 공통 모듈
작성일: 2026-07-26 (KST)
상위 문서: 작업 지시서 "종목 기술적 구조 분석 (TECH_PROFILE v0)" §2 (CP-0)

역할:
  - MariaDB(stock_db) 접속 (접속 정보 하드코딩 금지 — 설정 파일 / 환경변수)
  - 테이블/뷰 존재 확인 및 컬럼 스키마 조회
  - 키 컬럼(종목코드/일자) 자동 탐지 (스키마를 모르는 상태에서 CP-0를 돌리기 위함)

접속 정보 해석 순서:
  1. --config 로 지정한 파일 (.json 또는 .ini)
  2. 환경변수 STOCK_DB_HOST / STOCK_DB_PORT / STOCK_DB_USER / STOCK_DB_PASSWORD / STOCK_DB_NAME
  3. 기본 후보 경로: C:\\stock\\config\\db_config.json, C:\\stock\\db_config.json,
     스크립트 폴더의 db_config.json
  ※ 기존 C:\\stock 설정 방식이 위와 다르면 --config 로 기존 파일을 지정하거나,
     형식을 알려주면 로더를 그 방식에 맞춘다. (형식 예: db_config.example.json)

필요 패키지: pip install PyMySQL pandas
"""

import configparser
import json
import os
from pathlib import Path

import pandas as pd

try:
    import pymysql
except ImportError:  # pragma: no cover
    raise SystemExit("PyMySQL이 필요합니다: pip install PyMySQL")

_DEFAULT_CONFIG_CANDIDATES = [
    Path(r"C:\stock\config\db_config.json"),
    Path(r"C:\stock\db_config.json"),
    Path(__file__).resolve().parent / "db_config.json",
]

# 키 컬럼 자동 탐지 후보 (소문자 비교)
_CODE_COL_CANDIDATES = ["code", "stock_code", "stockcode", "shcode", "ticker",
                        "isu_cd", "isu_srt_cd", "종목코드"]
_DATE_COL_CANDIDATES = ["date", "trade_date", "tradedate", "ymd", "base_date",
                        "basedate", "일자", "날짜"]


def load_db_config(config_path: str | None = None) -> dict:
    """접속 정보를 dict(host, port, user, password, database)로 반환."""
    # 1) 명시된 설정 파일
    if config_path:
        return _parse_config_file(Path(config_path))
    # 2) 환경변수
    if os.environ.get("STOCK_DB_HOST"):
        return {
            "host": os.environ["STOCK_DB_HOST"],
            "port": int(os.environ.get("STOCK_DB_PORT", "3306")),
            "user": os.environ.get("STOCK_DB_USER", ""),
            "password": os.environ.get("STOCK_DB_PASSWORD", ""),
            "database": os.environ.get("STOCK_DB_NAME", "stock_db"),
        }
    # 3) 기본 후보 경로
    for p in _DEFAULT_CONFIG_CANDIDATES:
        if p.is_file():
            return _parse_config_file(p)
    raise SystemExit(
        "DB 접속 정보를 찾지 못했습니다.\n"
        "  --config <파일> 로 지정하거나, 환경변수 STOCK_DB_HOST 등을 설정하거나,\n"
        "  C:\\stock\\config\\db_config.json 을 생성하세요 (형식: db_config.example.json 참조)."
    )


def _parse_config_file(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"설정 파일이 없습니다: {path}")
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    else:  # .ini / .cfg — [db] 섹션 우선, 없으면 첫 섹션
        cp = configparser.ConfigParser()
        cp.read(path, encoding="utf-8")
        section = "db" if cp.has_section("db") else cp.sections()[0]
        raw = dict(cp[section])
    # 키 이름 이형(host/hostname, database/db/dbname 등) 흡수
    def pick(*keys, default=None):
        for k in keys:
            if k in raw and str(raw[k]).strip() != "":
                return raw[k]
        return default
    cfg = {
        "host": pick("host", "hostname", "server", default="127.0.0.1"),
        "port": int(pick("port", default=3306)),
        "user": pick("user", "username", "id", default=""),
        "password": pick("password", "passwd", "pw", default=""),
        "database": pick("database", "db", "dbname", "name", default="stock_db"),
    }
    if not cfg["user"]:
        raise SystemExit(f"설정 파일에 user가 없습니다: {path}")
    return cfg


def get_connection(cfg: dict):
    return pymysql.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], database=cfg["database"],
        charset="utf8mb4", cursorclass=pymysql.cursors.Cursor,
    )


def fetch_df(conn, sql: str, params=None) -> pd.DataFrame:
    """pandas.read_sql의 DBAPI 경고를 피하기 위한 얇은 래퍼."""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def object_type(conn, name: str) -> str | None:
    """'BASE TABLE' / 'VIEW' / None(없음)."""
    df = fetch_df(
        conn,
        "SELECT TABLE_TYPE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    return None if df.empty else str(df.iloc[0, 0])


def table_columns(conn, name: str) -> pd.DataFrame:
    """컬럼명/타입/NULL 허용/코멘트."""
    return fetch_df(
        conn,
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION",
        (name,),
    )


def detect_key_columns(columns: list[str],
                       code_col: str | None = None,
                       date_col: str | None = None) -> tuple[str | None, str | None]:
    """종목코드/일자 컬럼 자동 탐지. 명시 인자가 있으면 그대로 사용."""
    lower = {c.lower(): c for c in columns}
    if code_col is None:
        for cand in _CODE_COL_CANDIDATES:
            if cand in lower:
                code_col = lower[cand]
                break
    if date_col is None:
        for cand in _DATE_COL_CANDIDATES:
            if cand in lower:
                date_col = lower[cand]
                break
    return code_col, date_col


def is_numeric_type(column_type: str) -> bool:
    t = column_type.lower()
    return t.startswith(("int", "bigint", "smallint", "mediumint", "tinyint",
                         "decimal", "double", "float", "numeric"))
