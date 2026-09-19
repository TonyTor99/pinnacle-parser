"""Запасной стат-провайдер: FlashScore (для лиг, которых нет у SofaScore).

ВНИМАНИЕ: у FlashScore нет официального API. Данные отдаются служебными фидами
d.flashscore.com в собственном разделённом формате (записи через '¬', пары ключ÷значение,
секции через '~') с обязательным заголовком x-fsign. Коды полей меняются со временем.

Реализация — best-effort и ПОДЛЕЖИТ СВЕРКЕ НА ЖИВЫХ ДАННЫХ (этап verification плана):
- используется как ВТОРИЧНЫЙ источник только для матчей, которых нет у SofaScore;
- при любой неуверенности парсинга возвращает []/None (безопасная деградация),
  чтобы не породить ложный сигнал. Доверенный основной источник — SofaScore.

Точки настройки при сверке помечены как TUNE.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

import config
from .models import LiveEvent, MatchStats, TeamStat
from .stats_base import StatsProvider

FEED_BASE = "https://d.flashscore.com/x/feed"
X_FSIGN = "SW9D1eZo"  # TUNE: публичный статический токен фидов FlashScore (может смениться)

# TUNE: кандидаты кодов полей (перебираем по порядку — берём первый непустой)
K_MATCH_ID = ("AA",)
K_HOME = ("AE", "WM", "CX")
K_AWAY = ("AF", "WN", "AY")
K_STAGE = ("AB", "AC", "SB")   # статус/стадия матча
K_START = ("AD",)


class FlashScore(StatsProvider):
    name = "flashscore"

    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "*/*",
            "Referer": "https://www.flashscore.com/",
            "Origin": "https://www.flashscore.com",
            "x-fsign": X_FSIGN,
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get_text(self, path: str, retries: int = 3) -> Optional[str]:
        url = f"{FEED_BASE}{path}"
        for attempt in range(retries):
            try:
                r = self.sess.get(url, timeout=20)
            except requests.RequestException:
                time.sleep(1.2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429) or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r.text
        return None

    # --- список live ---

    def list_live(self) -> list[LiveEvent]:
        # sport 1 = футбол; фид активных матчей
        text = self._get_text("/f_1_0_3_en_1")
        if not text:
            return []
        events: list[LiveEvent] = []
        for fields in _split_matches(text):
            mid = _first(fields, K_MATCH_ID)
            home = _first(fields, K_HOME)
            away = _first(fields, K_AWAY)
            if not mid or not home or not away:
                continue
            events.append(LiveEvent(
                source=self.name,
                event_id=mid,
                league="",  # секция лиги в этом фиде отдельным блоком; для сшивки хватает команд+времени
                home=home,
                away=away,
                kickoff_utc=None,
                status=_stage_to_status(_first(fields, K_STAGE)),
                minute=None,
            ))
        return events

    # --- статистика 1-го тайма ---

    def get_stats(self, event: LiveEvent) -> Optional[MatchStats]:
        # Сводка матча со статистикой; формат специфичен и требует сверки — парсим защитно.
        text = self._get_text(f"/df_sur_1_{event.event_id}")
        if not text:
            return None
        fields = _parse_kv(text)
        # TUNE: коды статистики (угловые/карты) в сводке FlashScore нестабильны.
        # Пока безопасно возвращаем None, если не удалось однозначно извлечь угловые —
        # тогда collector пропустит матч (лучше пропуск, чем ложный сигнал).
        corners = _extract_pair(fields, ("corner",))
        if corners is None:
            return None
        home_c, away_c = corners
        goals = _extract_pair(fields, ("goal", "score")) or (0, 0)
        reds = _extract_pair(fields, ("red card", "redcard")) or (0, 0)
        return MatchStats(
            source=self.name,
            event_id=event.event_id,
            league=event.league,
            home_team=event.home,
            away_team=event.away,
            kickoff_utc=event.kickoff_utc,
            status=event.status,
            minute=event.minute,
            home_stat=TeamStat(goals=goals[0], red_cards=reds[0], corners=home_c),
            away_stat=TeamStat(goals=goals[1], red_cards=reds[1], corners=away_c),
        )


# --- разбор разделённого формата ---


def _parse_kv(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for rec in text.replace("~", "¬").split("¬"):
        if "÷" in rec:
            k, _, v = rec.partition("÷")
            out.append((k.strip(), v.strip()))
    return out


def _split_matches(text: str) -> list[dict[str, str]]:
    """Разбить фид на матчи по появлению ключа AA (id матча)."""
    matches: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for k, v in _parse_kv(text):
        if k == "AA":
            if cur:
                matches.append(cur)
            cur = {k: v}
        elif cur is not None:
            cur.setdefault(k, v)
    if cur:
        matches.append(cur)
    return matches


def _first(fields: dict[str, str], keys) -> str:
    for k in keys:
        v = fields.get(k)
        if v:
            return v
    return ""


def _stage_to_status(stage: str) -> str:
    s = (stage or "").lower()
    if "half" in s or s in ("ht", "45"):
        return "HT"
    if "finished" in s or "ended" in s or "ft" in s:
        return "finished"
    if s:
        return "live"
    return "other"


def _extract_pair(fields: list[tuple[str, str]], needles) -> Optional[tuple[int, int]]:
    """Найти строку статистики по названию и вернуть (home, away). None если не найдено."""
    name = None
    home = away = None
    for k, v in fields:
        low = v.lower()
        if any(n in low for n in needles):
            name = v
            home = away = None
            continue
        if name is not None:
            if home is None and _isint(v):
                home = int(v)
            elif away is None and _isint(v):
                away = int(v)
                return home, away
    return None


def _isint(v: str) -> bool:
    v = (v or "").strip()
    return v.isdigit()


if __name__ == "__main__":
    f = FlashScore()
    live = f.list_live()
    print(f"FlashScore live: {len(live)}")
    for e in live[:5]:
        print(e.status, e.home, "—", e.away)
