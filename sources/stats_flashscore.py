"""Стат-провайдер FlashScore (основной, пока SofaScore недоступен без residential-прокси).

У FlashScore нет официального API. Данные отдаются служебными фидами в собственном
разделённом формате: записи через '¬', пары ключ÷значение, секции через '~'.
Обязателен заголовок x-fsign. Коды полей могут меняться со временем (помечены TUNE).

Разбор кодов подтверждён на живых данных 2026-09-20:
- Список матчей: feed f_1_0_3_en_1 на host global.flashscore.ninja/2/x/feed.
- Матч начинается с AA (id). Команды: AE (home) / AF (away). Время старта: AD (unix).
- Статус матча AB: 1=прематч, 2=live, 3=завершён.
- Стадия AC: 12=1-й тайм (ht-счёт ещё пуст), 13=2-й тайм (ht-счёт заполнен).
- Счёт: AG/AH — текущий; BC/BD — счёт 1-го тайма (появляется на перерыве и остаётся).
- ПЕРЕРЫВ (HT) определяем по инварианту, не завися от точного кода стадии:
  матч live (AB=2) + счёт 1-го тайма уже есть (BC/BD) + это ещё не 2-й тайм (AC≠13).
- Статистика: feed df_st_1_{id}. Секции '~', внутри SG=название, SH=home, SI=away.
  На перерыве это статистика ровно за 1-й тайм (2-й ещё не начался) — что и нужно.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from .models import LiveEvent, MatchStats, TeamStat
from .stats_base import StatsProvider

FEED_BASE = "https://global.flashscore.ninja/2/x/feed"
X_FSIGN = "SW9D1eZo"          # TUNE: публичный статический токен фидов FlashScore
LIVE_FEED = "f_1_0_3_en_1"    # футбол (sport 1), обзорный фид со всеми матчами дня

# TUNE: кандидаты кодов полей (перебор по порядку — берём первый непустой)
K_MATCH_ID = ("AA",)
K_HOME = ("AE", "CX", "WM")
K_AWAY = ("AF", "WN")
K_START = ("AD", "ADE")
K_STATUS = ("AB",)            # 1=прематч, 2=live, 3=завершён
K_STAGE = ("AC",)            # 12=1-й тайм, 13=2-й тайм
K_HT_HOME = ("BC",)          # голы хозяев за 1-й тайм
K_HT_AWAY = ("BD",)          # голы гостей за 1-й тайм


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
        if config.STATS_PROXIES:
            self.sess.proxies.update(config.STATS_PROXIES)

    def _get_text(self, path: str, retries: int = 3) -> Optional[str]:
        url = f"{FEED_BASE}/{path}"
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
        text = self._get_text(LIVE_FEED)
        if not text:
            return []
        events: list[LiveEvent] = []
        for fields in _split_matches(text):
            mid = _first(fields, K_MATCH_ID)
            home = _first(fields, K_HOME)
            away = _first(fields, K_AWAY)
            if not mid or not home or not away:
                continue
            status = _status(fields)
            if status not in ("live", "HT"):
                continue  # драйверу нужны только идущие матчи
            events.append(LiveEvent(
                source=self.name,
                event_id=mid,
                league="",  # заголовок лиги в фиде отдельным блоком; для сшивки хватает команд+времени
                home=home,
                away=away,
                kickoff_utc=_iso(_first(fields, K_START)),
                status=status,
                minute=45 if status == "HT" else None,
                ht_home_goals=_int_or_none(_first(fields, K_HT_HOME)),
                ht_away_goals=_int_or_none(_first(fields, K_HT_AWAY)),
            ))
        return events

    # --- статусы всех матчей дня (для резолва результатов) ---

    def event_status_map(self) -> dict[str, str]:
        """{event_id -> status} по дневному фиду, включая finished (для резолва сигналов)."""
        text = self._get_text(LIVE_FEED)
        if not text:
            return {}
        out: dict[str, str] = {}
        for fields in _split_matches(text):
            mid = _first(fields, K_MATCH_ID)
            if mid:
                out[mid] = _status(fields)
        return out

    def final_corners(self, event_id: str) -> Optional[tuple[int, int]]:
        """Итоговые угловые за ВЕСЬ матч (первая секция 'Corner kicks' = полный матч)."""
        text = self._get_text(f"df_st_1_{event_id}")
        if not text:
            return None
        return _stat_pair(text, "corner")

    # --- статистика 1-го тайма ---

    def get_stats(self, event: LiveEvent) -> Optional[MatchStats]:
        text = self._get_text(f"df_st_1_{event.event_id}")
        if not text:
            return None
        corners = _stat_pair(text, "corner")
        if corners is None:
            # без угловых сигнал по угловым не построить — безопасный пропуск
            return None
        reds = _stat_pair(text, "red card") or (0, 0)
        # Голы 1-го тайма надёжнее берём из ht-счёта основного фида (BC/BD).
        hg = event.ht_home_goals if event.ht_home_goals is not None else 0
        ag = event.ht_away_goals if event.ht_away_goals is not None else 0
        return MatchStats(
            source=self.name,
            event_id=event.event_id,
            league=event.league,
            home_team=event.home,
            away_team=event.away,
            kickoff_utc=event.kickoff_utc,
            status=event.status,
            minute=event.minute,
            home_stat=TeamStat(goals=hg, red_cards=reds[0], corners=corners[0]),
            away_stat=TeamStat(goals=ag, red_cards=reds[1], corners=corners[1]),
        )


# --- разбор разделённого формата ---


def _split_matches(text: str) -> list[dict[str, str]]:
    """Разбить обзорный фид на матчи по появлению ключа AA (id матча)."""
    matches: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for rec in text.replace("~", "¬").split("¬"):
        if "÷" not in rec:
            continue
        k, _, v = rec.partition("÷")
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


def _status(fields: dict[str, str]) -> str:
    ab = fields.get("AB")
    ac = fields.get("AC")
    ht = fields.get("BC")
    if ab == "3":
        return "finished"
    if ab == "1":
        return "prematch"
    if ab == "2":
        # перерыв: 1-й тайм отыгран (есть ht-счёт), 2-й ещё не идёт
        if ht not in (None, "") and ac != "13":
            return "HT"
        return "live"
    return "other"


def _stat_pair(text: str, needle: str) -> Optional[tuple[int, int]]:
    """Найти секцию статистики по названию (SG) и вернуть (home=SH, away=SI). None если нет."""
    for sec in text.split("~"):
        kv: dict[str, str] = {}
        for rec in sec.split("¬"):
            if "÷" in rec:
                k, _, v = rec.partition("÷")
                kv[k] = v
        if needle in kv.get("SG", "").lower():
            sh, si = kv.get("SH"), kv.get("SI")
            hi, ai = _int_or_none(sh), _int_or_none(si)
            if hi is not None and ai is not None:
                return hi, ai
    return None


def _int_or_none(v: Optional[str]) -> Optional[int]:
    v = (v or "").strip()
    if v.lstrip("-").isdigit():
        return int(v)
    return None


def _iso(ad: str) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ad), timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


if __name__ == "__main__":
    f = FlashScore()
    live = f.list_live()
    ht = [e for e in live if e.status == "HT"]
    print(f"FlashScore live: {len(live)}, на перерыве (HT): {len(ht)}")
    for e in ht[:5]:
        print("  HT", e.home, "—", e.away, f"({e.ht_home_goals}:{e.ht_away_goals})")
        st = f.get_stats(e)
        if st:
            print("     угловые:", st.home_stat.corners, ":", st.away_stat.corners,
                  " красные:", st.home_stat.red_cards, ":", st.away_stat.red_cards)
