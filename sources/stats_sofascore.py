"""Основной стат-провайдер: неофициальный API SofaScore.

Эндпоинты (api.sofascore.com/api/v1):
- sport/football/events/live         — список live-матчей;
- event/{id}/incidents               — голы и карточки с минутой и стороной;
- event/{id}/statistics              — статистика по периодам, в т.ч. угловые (Corner kicks).

На перерыве (status.code == 31 / description 'Halftime') все накопленные голы/красные и есть
показатели 1-го тайма. Угловые 1-го тайма берём из периода '1ST' (fallback 'ALL').

Риски: rate-limit/блокировки (Cloudflare) — ретраи, браузерный UA; при неудаче возвращаем None.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

import config
from .models import LiveEvent, MatchStats, TeamStat
from .stats_base import StatsProvider

API = "https://api.sofascore.com/api/v1"
SOFA_HT_CODE = 31  # Halftime


class SofaScore(StatsProvider):
    name = "sofascore"

    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept": "*/*",
            "Referer": "https://www.sofascore.com/",
            "Origin": "https://www.sofascore.com",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def _get(self, path: str, retries: int = 3) -> Optional[dict]:
        url = f"{API}{path}"
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
            try:
                return r.json()
            except ValueError:
                return None
        return None

    # --- список live ---

    def list_live(self) -> list[LiveEvent]:
        data = self._get("/sport/football/events/live")
        events: list[LiveEvent] = []
        for ev in (data or {}).get("events", []) or []:
            events.append(LiveEvent(
                source=self.name,
                event_id=str(ev.get("id")),
                league=_tournament(ev),
                home=(ev.get("homeTeam") or {}).get("name") or "",
                away=(ev.get("awayTeam") or {}).get("name") or "",
                kickoff_utc=_iso(ev.get("startTimestamp")),
                status=_status(ev),
                minute=None,
            ))
        return events

    # --- статистика 1-го тайма ---

    def get_stats(self, event: LiveEvent) -> Optional[MatchStats]:
        eid = event.event_id
        incidents = self._get(f"/event/{eid}/incidents")
        stats = self._get(f"/event/{eid}/statistics")
        if incidents is None and stats is None:
            return None

        home_g = away_g = home_r = away_r = 0
        for inc in (incidents or {}).get("incidents", []) or []:
            itype = (inc.get("incidentType") or "").lower()
            is_home = bool(inc.get("isHome"))
            # на HT все инциденты относятся к 1-му тайму; на всякий случай ограничим минутой <=45(+)
            minute = inc.get("time") or 0
            if minute and minute > 47:
                continue
            if itype == "goal":
                if is_home:
                    home_g += 1
                else:
                    away_g += 1
            elif itype == "card":
                cls = (inc.get("incidentClass") or "").lower()
                if cls in ("red", "yellowred"):
                    if is_home:
                        home_r += 1
                    else:
                        away_r += 1

        home_c, away_c = _corners_1h(stats)

        return MatchStats(
            source=self.name,
            event_id=eid,
            league=event.league,
            home_team=event.home,
            away_team=event.away,
            kickoff_utc=event.kickoff_utc,
            status=event.status,
            minute=event.minute,
            home_stat=TeamStat(goals=home_g, red_cards=home_r, corners=home_c),
            away_stat=TeamStat(goals=away_g, red_cards=away_r, corners=away_c),
        )


def _tournament(ev: dict) -> str:
    t = ev.get("tournament") or {}
    uniq = t.get("uniqueTournament") or {}
    return uniq.get("name") or t.get("name") or ""


def _iso(ts) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _status(ev: dict) -> str:
    st = ev.get("status") or {}
    code = st.get("code")
    stype = (st.get("type") or "").lower()
    desc = (st.get("description") or "").lower()
    if code == SOFA_HT_CODE or "halftime" in desc or desc == "ht":
        return "HT"
    if stype == "inprogress":
        return "live"
    if stype == "finished":
        return "finished"
    if stype == "notstarted":
        return "prematch"
    return "other"


def _corners_1h(stats: Optional[dict]) -> tuple[int, int]:
    if not stats:
        return 0, 0
    periods = {p.get("period"): p for p in stats.get("statistics", []) or []}
    block = periods.get("1ST") or periods.get("ALL")
    if not block:
        return 0, 0
    for group in block.get("groups", []) or []:
        for item in group.get("statisticsItems", []) or []:
            name = (item.get("name") or "").lower()
            if "corner" in name:
                return _to_int(item.get("home")), _to_int(item.get("away"))
    return 0, 0


def _to_int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    s = SofaScore()
    live = s.list_live()
    print(f"live матчей: {len(live)}")
    ht = [e for e in live if e.status == "HT"]
    print(f"на перерыве: {len(ht)}")
    sample = ht[0] if ht else (live[0] if live else None)
    if sample:
        print(f"\n{sample.home} — {sample.away} [{sample.league}] status={sample.status}")
        st = s.get_stats(sample)
        if st:
            print("home:", st.home_stat)
            print("away:", st.away_stat)
