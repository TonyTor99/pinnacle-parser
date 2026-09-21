"""Сшивка события стат-провайдера с матчем Pinnacle.

Стратегия: нормализация названий команд (снятие диакритики, клубных суффиксов, пунктуации)
+ порог похожести (difflib) по обеим командам + окно по времени начала (если известно).
Ручные соответствия — в aliases.json ({"provider name": "pinnacle name"}). Кэш — в БД.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

import config

# Клубные формы, не несущие смысла для сопоставления (безопасный набор — «city/united/real» НЕ трогаем)
_CLUB_TOKENS = {
    "fc", "cf", "afc", "sc", "ac", "sk", "fk", "bk", "ff", "if", "sv", "cd", "ca",
    "ud", "as", "ss", "us", "rc", "kf", "nk", "hk", "sd", "cs", "club", "de", "the",
}

_aliases_cache: Optional[dict] = None
_aliases_mtime: float = -1.0


def _load_aliases() -> dict:
    """aliases.json с авто-перечиткой по mtime: бот дописывает алиасы из «Не сшитых
    матчей», а сборщик подхватывает их без перезапуска (на следующем перерыве)."""
    global _aliases_cache, _aliases_mtime
    try:
        mtime = os.path.getmtime(config.ALIASES_PATH)
    except OSError:
        mtime = -1.0
    if _aliases_cache is None or mtime != _aliases_mtime:
        try:
            with open(config.ALIASES_PATH, encoding="utf-8") as f:
                _aliases_cache = json.load(f)
        except (OSError, ValueError):
            _aliases_cache = {}
        _aliases_mtime = mtime
    return _aliases_cache


def normalize(name: str) -> str:
    if not name:
        return ""
    name = _load_aliases().get(name, name)
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    tokens = [t for t in n.split() if t and t not in _CLUB_TOKENS]
    return " ".join(tokens)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return SequenceMatcher(None, a, b).ratio()


def _kickoff_ok(ev_iso: Optional[str], pm_iso: Optional[str], window_min: int) -> bool:
    if not ev_iso or not pm_iso:
        return True  # нет времени у одной стороны — не отбрасываем, полагаемся на имена
    try:
        a = datetime.fromisoformat(ev_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(pm_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    return abs((a - b).total_seconds()) <= window_min * 60


def find_matchup(event, pinn_mains, window_min: Optional[int] = None,
                 threshold: float = 0.72):
    """event: LiveEvent; pinn_mains: list[PinnMatch]. Возвращает PinnMatch или None."""
    window_min = config.MATCH_WINDOW_MIN if window_min is None else window_min
    eh, ea = normalize(event.home), normalize(event.away)
    best = None
    best_score = 0.0
    for pm in pinn_mains:
        if not _kickoff_ok(event.kickoff_utc, pm.kickoff_utc, window_min):
            continue
        ph, pa = normalize(pm.home), normalize(pm.away)
        # прямая ориентация (home-home, away-away)
        s_direct = min(_sim(eh, ph), _sim(ea, pa))
        # перестановка (на случай разного порядка команд у провайдеров)
        s_swap = min(_sim(eh, pa), _sim(ea, ph))
        score = max(s_direct, s_swap)
        if score > best_score:
            best_score = score
            best = pm
    if best is not None and best_score >= threshold:
        return best
    return None


def find_fs_event(pinn_home: str, pinn_away: str, pinn_kickoff: Optional[str],
                  fs_live, window_min: Optional[int] = None, threshold: float = 0.72):
    """Обратное направление: по матчу Pinnacle найти live-событие стат-провайдера.

    pinn_*: имена/время матча Pinnacle; fs_live: list[LiveEvent]. Возвращает LiveEvent|None.
    Та же нормализация + прямой/своп + окно времени + aliases, что и в find_matchup.
    """
    window_min = config.MATCH_WINDOW_MIN if window_min is None else window_min
    ph, pa = normalize(pinn_home), normalize(pinn_away)
    best = None
    best_score = 0.0
    for ev in fs_live:
        if not _kickoff_ok(pinn_kickoff, ev.kickoff_utc, window_min):
            continue
        eh, ea = normalize(ev.home), normalize(ev.away)
        s_direct = min(_sim(ph, eh), _sim(pa, ea))
        s_swap = min(_sim(ph, ea), _sim(pa, eh))
        score = max(s_direct, s_swap)
        if score > best_score:
            best_score = score
            best = ev
    if best is not None and best_score >= threshold:
        return best
    return None
