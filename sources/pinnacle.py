"""Адаптер линий Pinnacle через гостевой arcadia API (бесплатно, без аккаунта).

Даёт:
- список футбольных матчей (прематч + live) для сшивки со стат-провайдером;
- прематч 1X2 (moneyline period 0);
- линии угловых: индивидуальные тоталы (team_total), форы (spread), тоталы (total)
  — они лежат на отдельных «special»-матчапах, привязанных к основному матчу.

Особенности arcadia:
- обязателен заголовок x-api-key (публичный, из JS pinnacle.com); при 401 ключ протух → обновить.
- коэффициенты приходят в американском формате → переводим в десятичные.
- один вызов /matchups/{id}/markets/related/straight отдаёт рынки основного матча И его специалов.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import requests

import config
from .models import MatchOdds, MoneyLine


class PinnacleAuthError(RuntimeError):
    """401 — публичный x-api-key протух, нужно обновить из браузера."""


def american_to_decimal(american) -> Optional[float]:
    if american is None:
        return None
    try:
        a = float(american)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    if a > 0:
        return round(a / 100.0 + 1.0, 4)
    return round(100.0 / abs(a) + 1.0, 4)


@dataclass
class PinnMatch:
    """Разобранный основной матч из списка matchups."""
    id: int
    league: str
    home: str
    away: str
    kickoff_utc: Optional[str]
    is_live: bool


class PinnacleClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or config.PINNACLE_API_KEY
        self.base = config.PINNACLE_BASE
        self.sess = requests.Session()
        self.sess.headers.update({
            "x-api-key": self.api_key,
            "User-Agent": config.USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://www.pinnacle.com/",
            "Origin": "https://www.pinnacle.com",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        if config.PINNACLE_PROXIES:
            self.sess.proxies.update(config.PINNACLE_PROXIES)

    def _get(self, path: str, params: dict | None = None, retries: int = 4):
        url = f"{self.base}{path}"
        last_exc = None
        for attempt in range(retries):
            try:
                r = self.sess.get(url, params=params, timeout=20)
            except requests.RequestException as e:
                last_exc = e
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise PinnacleAuthError("Pinnacle 401: x-api-key протух, обновите PINNACLE_API_KEY")
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        if last_exc:
            raise last_exc
        raise RuntimeError(f"Pinnacle GET {path} не удался после {retries} попыток")

    # --- матчи ---

    def fetch_soccer_matchups(self) -> tuple[list[PinnMatch], dict[int, set[int]]]:
        """Возвращает (основные матчи, {parent_id -> {corner_special_id,...}}).

        arcadia отдаёт плоский список, где реальные матчи перемешаны со special-подматчапами.
        Специалы имеют parent (id основного матча); угловые определяем по units/description.
        """
        data = self._get(f"/sports/{config.SOCCER_SPORT_ID}/matchups")
        mains: list[PinnMatch] = []
        corner_specials: dict[int, set[int]] = {}
        for m in data or []:
            parent = _parent_id(m)
            if parent is None and _is_regular(m):
                pm = _parse_main(m)
                if pm:
                    mains.append(pm)
            elif parent is not None and _is_corner_special(m):
                corner_specials.setdefault(parent, set()).add(int(m["id"]))
        return mains, corner_specials

    def fetch_markets(self, matchup_id: int) -> list[dict]:
        """Straight-рынки основного матча и связанных специалов."""
        return self._get(f"/matchups/{matchup_id}/markets/related/straight") or []

    def build_odds(self, main: PinnMatch, corner_ids: set[int], markets: list[dict]) -> MatchOdds:
        odds = MatchOdds(
            matchup_id=main.id, league=main.league, home=main.home, away=main.away,
            kickoff_utc=main.kickoff_utc, is_live=main.is_live,
        )
        for mk in markets:
            mid = mk.get("matchupId")
            mtype = mk.get("type")
            period = mk.get("period", 0)
            if mid == main.id and mtype == "moneyline" and period == 0:
                odds.moneyline = _parse_moneyline(mk)
            elif mid in corner_ids:
                _route_corner_market(odds, mk)
        return odds

    def get_match_odds(self, matchup_id: int, main: PinnMatch, corner_ids: set[int]) -> MatchOdds:
        markets = self.fetch_markets(matchup_id)
        return self.build_odds(main, corner_ids, markets)


# --- разбор структуры matchups ---


def _parent_id(m: dict) -> Optional[int]:
    p = m.get("parent")
    if isinstance(p, dict):
        return p.get("id")
    if isinstance(p, int):
        return p
    return m.get("parentId")


def _is_regular(m: dict) -> bool:
    t = (m.get("type") or "").lower()
    if t in ("special",):
        return False
    parts = m.get("participants") or []
    return len(parts) >= 2 and _alignment_ok(parts)


def _alignment_ok(parts: list[dict]) -> bool:
    aligns = {(p.get("alignment") or "").lower() for p in parts}
    return "home" in aligns and "away" in aligns


def _corner_text(m: dict) -> str:
    bits = [
        m.get("units") or "",
        (m.get("special") or {}).get("category") or "" if isinstance(m.get("special"), dict) else "",
        (m.get("special") or {}).get("description") or "" if isinstance(m.get("special"), dict) else "",
        m.get("category") or "",
    ]
    return " ".join(str(b) for b in bits).lower()


def _is_corner_special(m: dict) -> bool:
    return "corner" in _corner_text(m)


def _parse_main(m: dict) -> Optional[PinnMatch]:
    parts = m.get("participants") or []
    home = away = None
    for p in parts:
        al = (p.get("alignment") or "").lower()
        if al == "home":
            home = p.get("name")
        elif al == "away":
            away = p.get("name")
    if not home or not away:
        return None
    league = (m.get("league") or {}).get("name") or ""
    is_live = bool(m.get("isLive") or (m.get("liveStatus") == 1))
    return PinnMatch(
        id=int(m["id"]), league=league, home=home, away=away,
        kickoff_utc=m.get("startTime") or m.get("startsAt"), is_live=is_live,
    )


# --- разбор рынков ---


def _prices_by_designation(mk: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for pr in mk.get("prices") or []:
        des = (pr.get("designation") or "").lower()
        if des:
            out[des] = pr
    return out


def _parse_moneyline(mk: dict) -> MoneyLine:
    pd = _prices_by_designation(mk)
    return MoneyLine(
        p1=american_to_decimal((pd.get("home") or {}).get("price")),
        px=american_to_decimal((pd.get("draw") or {}).get("price")),
        p2=american_to_decimal((pd.get("away") or {}).get("price")),
    )


def _route_corner_market(odds: MatchOdds, mk: dict) -> None:
    mtype = (mk.get("type") or "").lower()
    period = mk.get("period", 0)
    prices = mk.get("prices") or []
    if mtype == "total":
        for pr in prices:
            des = (pr.get("designation") or "").lower()
            line = pr.get("points")
            dec = american_to_decimal(pr.get("price"))
            entry = _find_or_new(odds.corner_totals, line, period)
            if des == "over":
                entry["over"] = dec
            elif des == "under":
                entry["under"] = dec
    elif mtype == "team_total":
        side = (mk.get("side") or "").lower()  # 'home'|'away'
        if side not in ("home", "away"):
            return
        for pr in prices:
            des = (pr.get("designation") or "").lower()
            line = pr.get("points")
            dec = american_to_decimal(pr.get("price"))
            entry = _find_or_new_tt(odds.corner_team_totals, side, line, period)
            if des == "over":
                entry["over"] = dec
            elif des == "under":
                entry["under"] = dec
    elif mtype == "spread":
        for pr in prices:
            des = (pr.get("designation") or "").lower()  # 'home'|'away'
            line = pr.get("points")  # фора со стороны des
            dec = american_to_decimal(pr.get("price"))
            # нормализуем к форе ХОЗЯЕВ: home line как есть, away line -> -line
            hcap_home = line if des == "home" else (-line if line is not None else None)
            entry = _find_or_new(odds.corner_handicaps, hcap_home, period)
            entry[des] = dec


def _find_or_new(lst: list[dict], line, period) -> dict:
    for e in lst:
        if e.get("line") == line and e.get("period") == period:
            return e
    e = {"line": line, "period": period}
    lst.append(e)
    return e


def _find_or_new_tt(lst: list[dict], side, line, period) -> dict:
    for e in lst:
        if e.get("team") == side and e.get("line") == line and e.get("period") == period:
            return e
    e = {"team": side, "line": line, "period": period}
    lst.append(e)
    return e


# --- ручная проверка ---

if __name__ == "__main__":
    import sys

    cli = PinnacleClient()
    mains, corners = cli.fetch_soccer_matchups()
    live = [m for m in mains if m.is_live]
    print(f"Всего матчей: {len(mains)}, из них live: {len(live)}, у {len(corners)} матчей есть угловые-специалы")
    target = None
    if len(sys.argv) > 1:
        target = next((m for m in mains if m.id == int(sys.argv[1])), None)
    if target is None:
        target = next((m for m in live if m.id in corners), None) or (live[0] if live else (mains[0] if mains else None))
    if target is None:
        print("Нет матчей для показа")
        sys.exit(0)
    print(f"\nМатч {target.id}: {target.home} — {target.away} ({target.league}), live={target.is_live}")
    odds = cli.get_match_odds(target.id, target, corners.get(target.id, set()))
    print("1X2:", odds.moneyline)
    print("Угловые ИТ:", odds.corner_team_totals)
    print("Угловые форы:", odds.corner_handicaps)
    print("Угловые тоталы:", odds.corner_totals)
