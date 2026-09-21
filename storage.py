"""SQLite-хранилище: матчи, снимки линий/статистики, сигналы, кэш сопоставлений, настройки.

Одна БД pinnacle_corners.db (путь в config.DB_PATH). Включён WAL для параллельного
чтения ботом/панелью во время записи сборщиком.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
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

-- Монитор перерывов: одна карточка на каждый ушедший в перерыв матч с угловыми
-- (даже если не подошёл под стратегию). Ведём «от Pinnacle».
CREATE TABLE IF NOT EXISTS ht_monitor (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER,
    matchup_id        INTEGER,          -- id матча Pinnacle (= parent_id)
    parent_id         INTEGER,
    league            TEXT,
    home              TEXT,
    away              TEXT,
    ht_home_goals     INTEGER,
    ht_away_goals     INTEGER,
    home_reds         INTEGER,
    away_reds         INTEGER,
    home_corners      INTEGER,
    away_corners      INTEGER,
    prematch_p1       REAL,
    prematch_px       REAL,
    prematch_p2       REAL,
    corner_lines_json TEXT,             -- снимок доступных линий угловых на перерыве
    verdict           TEXT,             -- какие стратегии подошли / «нет»
    matched_fs        INTEGER           -- 1=сшит с FlashScore, 0=нет
);
CREATE INDEX IF NOT EXISTS idx_htmon_ts ON ht_monitor(ts);

-- Матчи с угловыми на перерыве, которые НЕ нашлись в FlashScore: снимок FS-списка
-- момента + ручной алиасинг из бота.
CREATE TABLE IF NOT EXISTS unmatched (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER,
    parent_id         INTEGER,
    league            TEXT,
    home              TEXT,
    away              TEXT,
    kickoff_utc       TEXT,
    fs_snapshot_json  TEXT,             -- список FS-событий момента [{id,home,away,kickoff,status}]
    resolved          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_unmatched_resolved ON unmatched(resolved, ts);
"""


@contextmanager
def _conn():
    """Соединение с БД как контекстный менеджер: коммит при успехе, ГАРАНТИРОВАННОЕ
    закрытие в finally. Раньше был `return conn` + `with sqlite3.connect() as c` — но
    этот with закрывает лишь транзакцию, не само соединение: в длинном цикле сборщика
    дескрипторы/WAL-файлы копились до SQLITE_CANTOPEN («unable to open database file»).
    """
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Колонки signals, добавленные позже (результат сигнала + привязка к TG-сообщению).
# Добавляются миграцией на существующей БД, чтобы не терять данные.
_SIGNALS_ADDED_COLUMNS = {
    "fs_event_id": "TEXT",
    "result": "TEXT",              # 'win' | 'loss' | 'push'
    "profit_pct": "REAL",          # прибыль в % банка при ставке 1%
    "home_corners_final": "INTEGER",
    "away_corners_final": "INTEGER",
    "resolved_ts": "INTEGER",
    "msg_chat_id": "TEXT",         # куда отправлен сигнал (для правки сообщения)
    "msg_id": "INTEGER",
    "msg_text": "TEXT",            # исходный текст сигнала (дописываем итог при резолве)
}


def _migrate(conn) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(signals)").fetchall()}
    for col, typ in _SIGNALS_ADDED_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {typ}")


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


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
                market: str, line, price, details: str, sent: bool,
                fs_event_id: Optional[str] = None, msg_chat_id: Optional[str] = None,
                msg_id: Optional[int] = None, msg_text: Optional[str] = None) -> int:
    now = int(time.time())
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO signals(ts, strategy, matchup_id, league, home, away, market, line, price, "
            "details, sent, fs_event_id, msg_chat_id, msg_id, msg_text) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, strategy, matchup_id, league, home, away, market, line, price, details,
             1 if sent else 0, fs_event_id, msg_chat_id, msg_id, msg_text),
        )
        return cur.lastrowid


def set_signal_result(signal_id: int, won: bool) -> None:
    with _conn() as conn:
        conn.execute("UPDATE signals SET won=? WHERE id=?", (1 if won else 0, signal_id))


def get_unresolved_signals() -> list[sqlite3.Row]:
    """Отправленные сигналы с известным FS-событием, по которым ещё нет итога."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE sent=1 AND fs_event_id IS NOT NULL AND resolved_ts IS NULL"
        ).fetchall()


def set_signal_resolved(signal_id: int, won, result: str, profit_pct: float,
                        home_corners_final, away_corners_final) -> None:
    """won: 1 (win) | 0 (loss) | None (push). result: 'win'|'loss'|'push'."""
    now = int(time.time())
    with _conn() as conn:
        conn.execute(
            "UPDATE signals SET won=?, result=?, profit_pct=?, home_corners_final=?, "
            "away_corners_final=?, resolved_ts=? WHERE id=?",
            (won, result, profit_pct, home_corners_final, away_corners_final, now, signal_id),
        )


def get_signals_since(ts_from: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE ts>=? ORDER BY ts DESC", (ts_from,)
        ).fetchall()


def get_all_signals() -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute("SELECT * FROM signals ORDER BY ts DESC").fetchall()


# --- монитор перерывов (ht_monitor) ---


def save_ht_monitor(matchup_id, parent_id, league, home, away,
                    ht_home_goals, ht_away_goals, home_reds, away_reds,
                    home_corners, away_corners, prematch_p1, prematch_px, prematch_p2,
                    corner_lines_json, verdict, matched_fs) -> int:
    now = int(time.time())
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO ht_monitor(ts, matchup_id, parent_id, league, home, away, "
            "ht_home_goals, ht_away_goals, home_reds, away_reds, home_corners, away_corners, "
            "prematch_p1, prematch_px, prematch_p2, corner_lines_json, verdict, matched_fs) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, matchup_id, parent_id, league, home, away,
             ht_home_goals, ht_away_goals, home_reds, away_reds, home_corners, away_corners,
             prematch_p1, prematch_px, prematch_p2, corner_lines_json, verdict,
             1 if matched_fs else 0),
        )
        return cur.lastrowid


def get_ht_monitor_since(ts_from: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM ht_monitor WHERE ts>=? ORDER BY ts DESC", (ts_from,)
        ).fetchall()


# --- несшитые матчи (unmatched) ---


def get_open_unmatched(parent_id: int) -> Optional[sqlite3.Row]:
    """Незакрытая запись по матчу (дедуп: один раз за перерыв, живёт до resolved)."""
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM unmatched WHERE parent_id=? AND resolved=0 ORDER BY ts DESC LIMIT 1",
            (parent_id,),
        ).fetchone()


def save_unmatched(parent_id, league, home, away, kickoff_utc, fs_snapshot_json) -> Optional[int]:
    """Сохранить несшитый матч. Если по нему уже есть незакрытая запись — не дублируем."""
    now = int(time.time())
    with _conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM unmatched WHERE parent_id=? AND resolved=0 LIMIT 1", (parent_id,)
        ).fetchone()
        if exists:
            return None
        cur = conn.execute(
            "INSERT INTO unmatched(ts, parent_id, league, home, away, kickoff_utc, fs_snapshot_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (now, parent_id, league, home, away, kickoff_utc, fs_snapshot_json),
        )
        return cur.lastrowid


def list_unmatched(resolved: bool = False) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM unmatched WHERE resolved=? ORDER BY ts DESC",
            (1 if resolved else 0,),
        ).fetchall()


def get_unmatched(unmatched_id: int) -> Optional[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM unmatched WHERE id=?", (unmatched_id,)
        ).fetchone()


def mark_unmatched_resolved(unmatched_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE unmatched SET resolved=1 WHERE id=?", (unmatched_id,))


# --- обслуживание ---


def reset_signals() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM signals")


def reset_all() -> None:
    with _conn() as conn:
        for t in ("odds_snapshots", "stats_snapshots", "signals", "match_map",
                  "ht_monitor", "unmatched", "matches"):
            conn.execute(f"DELETE FROM {t}")


if __name__ == "__main__":
    init_db()
    print("DB initialised at", config.DB_PATH)
