"""SQLite-хранилище: матчи, снимки линий/статистики, сигналы, кэш сопоставлений, настройки.

Одна БД pinnacle_corners.db (путь в config.DB_PATH). Включён WAL для параллельного
чтения ботом/панелью во время записи сборщиком.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    matchup_id     INTEGER PRIMARY KEY,   -- id матча Pinnacle
    league         TEXT,
    home_raw       TEXT,
    away_raw       TEXT,
    kickoff_utc    TEXT,
    -- прематч 1X2 «первого сканирования»
    prematch_p1    REAL,
    prematch_px    REAL,
    prematch_p2    REAL,
    prematch_ts    INTEGER,
    created_ts     INTEGER
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    matchup_id   INTEGER,
    snap_ts      INTEGER,
    is_live      INTEGER,
    minute       INTEGER,
    market       TEXT,   -- 'moneyline' | 'corner_tt' | 'corner_hcap' | 'corner_total'
    side         TEXT,   -- 'home'|'away'|'over'|'under'|NULL
    line         REAL,
    price        REAL
);
CREATE INDEX IF NOT EXISTS idx_odds_match ON odds_snapshots(matchup_id, snap_ts);

CREATE TABLE IF NOT EXISTS stats_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    matchup_id     INTEGER,          -- NULL если ещё не сшит с Pinnacle
    source         TEXT,
    event_id       TEXT,
    snap_ts        INTEGER,
    status         TEXT,
    minute         INTEGER,
    home_goals     INTEGER,
    away_goals     INTEGER,
    home_reds      INTEGER,
    away_reds      INTEGER,
    home_corners   INTEGER,
    away_corners   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stats_event ON stats_snapshots(event_id, snap_ts);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER,
    strategy      TEXT,             -- 'itb2' | 'f2' | 'f1'
    matchup_id    INTEGER,
    league        TEXT,
    home          TEXT,
    away          TEXT,
    market        TEXT,             -- человекочитаемый рынок (напр. 'ИТБ2 угл 4.5')
    line          REAL,
    price         REAL,
    details       TEXT,             -- JSON условий срабатывания
    sent          INTEGER DEFAULT 0,
    won           INTEGER           -- NULL=не рассчитан, 1=зашёл, 0=нет
);

CREATE TABLE IF NOT EXISTS match_map (
    source          TEXT,
    provider_event  TEXT,
    matchup_id      INTEGER,
    ts              INTEGER,
    PRIMARY KEY (source, provider_event)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)


# --- settings ---


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# --- matches ---


def upsert_match(matchup_id: int, league: str, home: str, away: str, kickoff_utc: Optional[str]) -> None:
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO matches(matchup_id, league, home_raw, away_raw, kickoff_utc, created_ts) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(matchup_id) DO UPDATE SET league=excluded.league, "
            "home_raw=excluded.home_raw, away_raw=excluded.away_raw, kickoff_utc=excluded.kickoff_utc",
            (matchup_id, league, home, away, kickoff_utc, now),
        )


def save_prematch_moneyline(matchup_id: int, p1, px, p2) -> None:
    """Сохранить 1X2 «первого сканирования» — только если ещё не заполнено."""
    now = int(time.time())
    with _conn() as conn:
        row = conn.execute(
            "SELECT prematch_ts FROM matches WHERE matchup_id=?", (matchup_id,)
        ).fetchone()
        if row and row["prematch_ts"]:
            return  # уже зафиксировано первое сканирование
        conn.execute(
            "UPDATE matches SET prematch_p1=?, prematch_px=?, prematch_p2=?, prematch_ts=? "
            "WHERE matchup_id=?",
            (p1, px, p2, now, matchup_id),
        )


def get_prematch_moneyline(matchup_id: int) -> Optional[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT prematch_p1, prematch_px, prematch_p2, prematch_ts FROM matches WHERE matchup_id=?",
            (matchup_id,),
        ).fetchone()


# --- odds snapshots ---


def save_odds_snapshot(matchup_id: int, is_live: bool, minute, rows: list[dict]) -> None:
    """rows: [{'market','side','line','price'}...]"""
    now = int(time.time())
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO odds_snapshots(matchup_id, snap_ts, is_live, minute, market, side, line, price) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (matchup_id, now, 1 if is_live else 0, minute,
                 r.get("market"), r.get("side"), r.get("line"), r.get("price"))
                for r in rows
            ],
        )


# --- stats snapshots ---


def save_stats_snapshot(stats, matchup_id: Optional[int]) -> None:
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO stats_snapshots(matchup_id, source, event_id, snap_ts, status, minute, "
            "home_goals, away_goals, home_reds, away_reds, home_corners, away_corners) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                matchup_id, stats.source, stats.event_id, now, stats.status, stats.minute,
                stats.home_stat.goals, stats.away_stat.goals,
                stats.home_stat.red_cards, stats.away_stat.red_cards,
                stats.home_stat.corners, stats.away_stat.corners,
            ),
        )


# --- match map cache ---


def get_mapped_matchup(source: str, provider_event: str) -> Optional[int]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT matchup_id FROM match_map WHERE source=? AND provider_event=?",
            (source, provider_event),
        ).fetchone()
        return row["matchup_id"] if row else None


def save_mapping(source: str, provider_event: str, matchup_id: int) -> None:
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO match_map(source, provider_event, matchup_id, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(source, provider_event) DO UPDATE SET matchup_id=excluded.matchup_id, ts=excluded.ts",
            (source, provider_event, matchup_id, now),
        )


# --- signals ---


def signal_exists(strategy: str, matchup_id: int) -> bool:
    """Дедуп: сигнал по стратегии+матчу уже отправлялся."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM signals WHERE strategy=? AND matchup_id=? LIMIT 1",
            (strategy, matchup_id),
        ).fetchone()
        return row is not None


def save_signal(strategy: str, matchup_id: int, league: str, home: str, away: str,
                market: str, line, price, details: str, sent: bool) -> int:
    now = int(time.time())
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO signals(ts, strategy, matchup_id, league, home, away, market, line, price, details, sent) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (now, strategy, matchup_id, league, home, away, market, line, price, details, 1 if sent else 0),
        )
        return cur.lastrowid


def set_signal_result(signal_id: int, won: bool) -> None:
    with _conn() as conn:
        conn.execute("UPDATE signals SET won=? WHERE id=?", (1 if won else 0, signal_id))


def get_signals_since(ts_from: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE ts>=? ORDER BY ts DESC", (ts_from,)
        ).fetchall()


def get_all_signals() -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM signals ORDER BY ts DESC").fetchall()


# --- обслуживание ---


def reset_signals() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM signals")


def reset_all() -> None:
    with _conn() as conn:
        for t in ("odds_snapshots", "stats_snapshots", "signals", "match_map", "matches"):
            conn.execute(f"DELETE FROM {t}")


if __name__ == "__main__":
    init_db()
    print("DB initialised at", config.DB_PATH)
