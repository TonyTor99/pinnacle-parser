"""Общие dataclass-модели для слоёв линий (Pinnacle) и статистики (SofaScore/FlashScore)."""
from __future__ import annotations

from dataclasses import dataclass, field


# --- Линии Pinnacle ---


@dataclass
class MoneyLine:
    """Исход основного времени 1X2 (десятичные коэффициенты)."""
    p1: float | None = None
    px: float | None = None
    p2: float | None = None


@dataclass
class MatchOdds:
    matchup_id: int
    league: str
    home: str
    away: str
    kickoff_utc: str | None
    is_live: bool
    moneyline: MoneyLine | None = None
    # Угловые. Каждый элемент — словарь с линией и десятичными КФ:
    #   corner_team_totals: {"team": "home"|"away", "line": float, "over": float|None, "under": float|None}
    #   corner_handicaps:   {"line": float, "home": float|None, "away": float|None}  (line = фора ХОЗЯЕВ)
    #   corner_totals:      {"line": float, "over": float|None, "under": float|None}
    corner_team_totals: list[dict] = field(default_factory=list)
    corner_handicaps: list[dict] = field(default_factory=list)
    corner_totals: list[dict] = field(default_factory=list)

    def has_corners(self) -> bool:
        return bool(self.corner_team_totals or self.corner_handicaps or self.corner_totals)


# --- Статистика провайдеров ---


@dataclass
class TeamStat:
    goals: int = 0
    red_cards: int = 0
    corners: int = 0  # угловые за учитываемый период (обычно 1-й тайм на HT)


@dataclass
class MatchStats:
    source: str            # 'sofascore' | 'flashscore'
    event_id: str
    league: str
    home_team: str
    away_team: str
    kickoff_utc: str | None
    status: str            # 'prematch' | 'live' | 'HT' | 'finished' | 'other'
    minute: int | None
    home_stat: TeamStat = field(default_factory=TeamStat)
    away_stat: TeamStat = field(default_factory=TeamStat)


@dataclass
class LiveEvent:
    """Лёгкая запись live-списка для драйвера (без тяжёлой статистики)."""
    source: str
    event_id: str
    league: str
    home: str
    away: str
    kickoff_utc: str | None
    status: str            # 'live' | 'HT' | 'finished' | ...
    minute: int | None
    # Голы 1-го тайма, если провайдер отдаёт их прямо в live-списке (FlashScore: BC/BD).
    # SofaScore оставляет None и считает голы в get_stats. Нужны на HT для стратегий.
    ht_home_goals: int | None = None
    ht_away_goals: int | None = None
