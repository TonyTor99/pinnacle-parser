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

# FlashScore в международных турнирах дописывает страну: «Kyoto (Jpn)», «Shanghai Port (Chn)».
# У Pinnacle её нет («Kyoto Sanga») — без среза сходство падает ниже порога и матч не сшивается.
_COUNTRY_SUFFIX = re.compile(r"\s*\([A-Z][a-z]{2}\)")

# Вторые составы пишут по-разному: Pinnacle «Genk II», FlashScore «Genk U23» — сводим к одному токену.
_RESERVE_TOKENS = {"ii": "res", "u21": "res", "u23": "res"}

# Сходство «по сокращениям» (см. _abbr_sim) — ниже точного/вложенного (1.0/0.92), выше порога 0.72.
ABBR_SCORE = 0.9

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
    name = _COUNTRY_SUFFIX.sub(" ", name)
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    tokens = [_RESERVE_TOKENS.get(t, t) for t in n.split() if t and t not in _CLUB_TOKENS]
    return " ".join(tokens)


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92
    return max(SequenceMatcher(None, a, b).ratio(), _abbr_sim(a, b))


def _abbr_sim(a: str, b: str) -> float:
    """Сокращения FlashScore: «U. Cluj» = Universitatea Cluj, «UC Dublin» = University College
    Dublin, «Ind. Medellin», «Dag & Red» = Dagenham and Redbridge, «Zacatecas Mineros» =
    Mineros de Zacatecas. Каждое слово более короткого названия должно найтись в длинном —
    целиком, началом слова (от 3 букв) или инициалами подряд идущих слов. Плюс хотя бы 4
    буквы совпали не инициалами, иначе «U.» совпадало бы с любым «United»."""
    ta, tb = a.split(), b.split()
    if len(ta) > len(tb):
        ta, tb = tb, ta
    s = _abbr_cover(ta, tb)
    if not s and len(ta) == len(tb):            # «u cluj» / «universitatea cluj»: сокращение может быть с любой стороны
        s = _abbr_cover(tb, ta)
    return s


def _abbr_cover(ta: list[str], tb: list[str]) -> float:
    """Все слова ta нашлись в tb (целиком / началом / инициалами) → ABBR_SCORE, иначе 0."""
    used = [False] * len(tb)
    solid = 0                                   # буквы, совпавшие словами/началами слов
    for t in ta:
        j = next((j for j, u in enumerate(tb) if not used[j] and
                  (t == u or (len(t) >= 3 and u.startswith(t)) or (len(u) >= 3 and t.startswith(u)))), None)
        if j is not None:
            used[j] = True
            solid += min(len(t), len(tb[j]))
            continue
        if len(t) <= 3:                         # инициалы: «u» -> universitatea, «uc» -> university college
            j = next((j for j in range(len(tb) - len(t) + 1)
                      if not any(used[j:j + len(t)]) and all(tb[j + k].startswith(t[k]) for k in range(len(t)))), None)
            if j is not None:
                for k in range(len(t)):
                    used[j + k] = True
                continue
        return 0.0
    return ABBR_SCORE if solid >= 4 else 0.0


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
                  fs_live, window_min: Optional[int] = None, threshold: float = 0.72,
                  pinn_league: str = ""):
    """Обратное направление: по матчу Pinnacle найти live-событие стат-провайдера.

    pinn_*: имена/время/лига матча Pinnacle; fs_live: list[LiveEvent]. Возвращает LiveEvent|None.
    Та же нормализация + прямой/своп + окно времени + aliases, что и в find_matchup, плюс:
    - женская лига Pinnacle → у команд FlashScore срезаем « W» (Pinnacle «Toluca», FS «Toluca W»);
    - время старта у Pinnacle бывает сдвинуто на часы («Cherno More — Botev Vratsa»: 16:30 против
      15:00) — вне окна принимаем только почти точные имена (≥ ABBR_SCORE) у события FS,
      которое тоже на перерыве (ищем ведь в момент перерыва на Pinnacle).
    """
    window_min = config.MATCH_WINDOW_MIN if window_min is None else window_min
    ph, pa = normalize(pinn_home), normalize(pinn_away)
    women = _is_women_league(pinn_league)
    best = None
    best_score = 0.0
    for ev in fs_live:
        eh, ea = normalize(ev.home), normalize(ev.away)
        if women:
            eh, ea = _strip_women(eh), _strip_women(ea)
        s_direct = min(_sim(ph, eh), _sim(pa, ea))
        s_swap = min(_sim(ph, ea), _sim(pa, eh))
        score = max(s_direct, s_swap)
        if not _kickoff_ok(pinn_kickoff, ev.kickoff_utc, window_min):
            if not (score >= ABBR_SCORE and ev.status == "HT"):
                continue
        if score > best_score:
            best_score = score
            best = ev
    if best is not None and best_score >= threshold:
        return best
    return None


_WOMEN_LEAGUE = re.compile(r"women|femen|femin|frauen|nwsl|liga f\b", re.I)


def _is_women_league(league: str) -> bool:
    return bool(league and _WOMEN_LEAGUE.search(league))


def _strip_women(n: str) -> str:
    return n[:-2] if n.endswith(" w") else n
