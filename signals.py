"""Стратегии ugl pinka: itb2 / f2 / f1. Оценка снимка (статистика 1-го тайма + линии Pinnacle).

Соответствие команд: «первая команда» = home (П1/ФОРА1), «вторая команда» = away (П2/ФОРА2).

itb2:
  прематч: П2 в [1.01, 7.00]
  HT: home голы=0 и красные=0; away красные=1 и голы<=1
  ставка: ИТБ2 угловых (Over ИТ away) по ровной (целой) линии, КФ >= MIN_ODDS

f2:
  HT: away красные=1 и красных больше, чем у home (=> home красные=0); сумма голов обеих <=1
  ставка: ФОРА2 угловых с отклонением -1 (away -1), КФ >= MIN_ODDS

f1:
  прематч: П1 в [1.01, 4.00]
  HT: home красные=1, угловые<=4, голы<=away; away голы<=2, красные=0
  ставка: ФОРА1 угловых с отклонением -0.5 (home -0.5), КФ >= MIN_ODDS

ПРИМЕЧАНИЕ: для f1/f2 в исходнике не указан минимальный КФ — применяем общий MIN_ODDS как guard
(см. «Открытые пункты» плана: трактовку «ровной линии/отклонения» подтвердить у клиента).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import config
from sources.models import MatchOdds, MatchStats

EPS = 1e-6


@dataclass
class SignalCandidate:
    strategy: str
    market: str        # человекочитаемый рынок
    line: float
    price: float
    details: dict

    def details_json(self) -> str:
        return json.dumps(self.details, ensure_ascii=False)


def _is_whole(line) -> bool:
    return line is not None and abs(line - round(line)) < EPS


def _in_range(v: Optional[float], lo: float, hi: float) -> bool:
    return v is not None and lo - EPS <= v <= hi + EPS


def _pick_team_total_over(odds: MatchOdds, team: str, min_odds: float):
    """ИТ Over по целой линии с КФ>=min_odds.

    У Over коэффициент растёт с линией, поэтому «за КФ min=1.65» = наименьшая целая линия,
    на которой КФ уже дотягивает до min_odds (не раздуваем тотал сверх необходимого)."""
    best = None
    for e in odds.corner_team_totals:
        if e.get("team") != team:
            continue
        if e.get("period", 0) != 0:
            continue
        line, over = e.get("line"), e.get("over")
        if _is_whole(line) and over is not None and over >= min_odds - EPS:
            if best is None or line < best[0]:
                best = (line, over)
    return best  # (line, price) | None


def _pick_handicap(odds: MatchOdds, home_line: float, side: str, min_odds: float):
    """Фора угловых. Записи нормализованы к форе ХОЗЯЕВ (line = гандикап home).
    home_line — искомый гандикап хозяев; side='home'|'away' — чью цену берём."""
    for e in odds.corner_handicaps:
        if e.get("period", 0) != 0:
            continue
        line = e.get("line")
        if line is not None and abs(line - home_line) < EPS:
            price = e.get(side)
            if price is not None and price >= min_odds - EPS:
                return (home_line if side == "home" else -home_line, price)
    return None


def evaluate(stats: MatchStats, odds: MatchOdds, prematch) -> list[SignalCandidate]:
    """prematch: sqlite Row с prematch_p1/px/p2 (или None). Возвращает список сигналов."""
    out: list[SignalCandidate] = []
    if stats.status != "HT":
        return out

    hs, as_ = stats.home_stat, stats.away_stat
    p1 = prematch["prematch_p1"] if prematch else None
    p2 = prematch["prematch_p2"] if prematch else None
    min_odds = config.MIN_ODDS

    # --- itb2 ---
    if _in_range(p2, 1.01, 7.00):
        if hs.goals == 0 and hs.red_cards == 0 and as_.red_cards == 1 and as_.goals <= 1:
            pick = _pick_team_total_over(odds, "away", min_odds)
            if pick:
                line, price = pick
                out.append(SignalCandidate(
                    strategy="itb2",
                    market=f"ИТБ2 угловых Over {line:g}",
                    line=line, price=price,
                    details={"prematch_p2": p2, "home": _team_dict(hs), "away": _team_dict(as_)},
                ))

    # --- f2 (прематч-условий нет) ---
    if as_.red_cards == 1 and as_.red_cards > hs.red_cards and (hs.goals + as_.goals) <= 1:
        pick = _pick_handicap(odds, home_line=+1.0, side="away", min_odds=min_odds)  # away -1 == home +1
        if pick:
            _, price = pick
            out.append(SignalCandidate(
                strategy="f2",
                market="ФОРА2 угловых -1",
                line=-1.0, price=price,
                details={"home": _team_dict(hs), "away": _team_dict(as_)},
            ))

    # --- f1 ---
    if _in_range(p1, 1.01, 4.00):
        if (hs.red_cards == 1 and hs.corners <= 4 and hs.goals <= as_.goals
                and as_.goals <= 2 and as_.red_cards == 0):
            pick = _pick_handicap(odds, home_line=-0.5, side="home", min_odds=min_odds)
            if pick:
                _, price = pick
                out.append(SignalCandidate(
                    strategy="f1",
                    market="ФОРА1 угловых -0.5",
                    line=-0.5, price=price,
                    details={"prematch_p1": p1, "home": _team_dict(hs), "away": _team_dict(as_)},
                ))

    return out


def _team_dict(t) -> dict:
    return {"goals": t.goals, "reds": t.red_cards, "corners": t.corners}
