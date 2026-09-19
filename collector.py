"""Сборщик: драйвер на стат-провайдерах ловит перерыв (HT), сшивает матч с Pinnacle,
снимает статистику 1-го тайма + live-линии угловых, прогоняет стратегии и шлёт сигналы.

Отдельный процесс, управляемый ботом (pkill+start). Пишет heartbeat в settings для статуса.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone

import config
import signals as signals_mod
import storage
import tg
from matcher import find_matchup, normalize
from sources.pinnacle import PinnacleAuthError, PinnacleClient
from sources.stats_flashscore import FlashScore
from sources.stats_sofascore import SofaScore

PINN_CACHE_TTL = 60          # сек: как часто перечитывать список матчей Pinnacle
PREMATCH_LOOKAHEAD_H = 8     # ч: для каких прематч-матчей фиксировать 1X2 «первого сканирования»
PREMATCH_MAX_PER_CYCLE = 30  # ограничение вызовов на цикл
HT_REPROCESS_TTL = 90        # сек: повторная обработка того же HT-матча не чаще


class Collector:
    def __init__(self):
        self.pinn = PinnacleClient()
        self.providers = [SofaScore(), FlashScore()]  # порядок = приоритет
        self._pinn_cache = None       # (ts, mains, corner_specials)
        self._processed: dict[str, float] = {}   # ключ события -> ts последней обработки
        self._auth_notified = False

    # --- Pinnacle список с кэшем ---

    def _pinn_data(self):
        now = time.time()
        if self._pinn_cache and now - self._pinn_cache[0] < PINN_CACHE_TTL:
            return self._pinn_cache[1], self._pinn_cache[2]
        mains, corners = self.pinn.fetch_soccer_matchups()
        self._pinn_cache = (now, mains, corners)
        return mains, corners

    # --- прематч «первого сканирования» ---

    def _capture_prematch(self, mains, corners):
        now = datetime.now(timezone.utc)
        done = 0
        for m in mains:
            if done >= PREMATCH_MAX_PER_CYCLE:
                break
            if m.is_live:
                continue
            if storage.get_prematch_moneyline(m.id) and _has_prematch(m.id):
                continue
            if not _within_lookahead(m.kickoff_utc, now, PREMATCH_LOOKAHEAD_H):
                continue
            try:
                odds = self.pinn.get_match_odds(m.id, m, corners.get(m.id, set()))
            except PinnacleAuthError:
                raise
            except Exception:
                continue
            ml = odds.moneyline
            if ml and (ml.p1 or ml.p2):
                storage.upsert_match(m.id, m.league, m.home, m.away, m.kickoff_utc)
                storage.save_prematch_moneyline(m.id, ml.p1, ml.px, ml.p2)
                done += 1

    # --- сбор live-событий на перерыве ---

    def _ht_events(self):
        """Собрать HT-события со всех провайдеров, дедуп по нормализованным именам."""
        seen: set[tuple[str, str]] = set()
        result = []
        for prov in self.providers:
            try:
                live = prov.list_live()
            except Exception:
                continue
            for ev in live:
                if ev.status != "HT":
                    continue
                key = (normalize(ev.home), normalize(ev.away))
                if key in seen:
                    continue
                seen.add(key)
                result.append((prov, ev))
        return result

    def _recently_processed(self, key: str) -> bool:
        ts = self._processed.get(key)
        return ts is not None and (time.time() - ts) < HT_REPROCESS_TTL

    # --- обработка одного HT-матча ---

    def _handle_ht(self, prov, ev, mains, corners):
        key = f"{prov.name}:{ev.event_id}"
        if self._recently_processed(key):
            return
        stats = prov.get_stats(ev)
        if stats is None:
            return  # ретрай на следующем цикле (не помечаем обработанным)

        # сшивка с Pinnacle (сначала кэш соответствий)
        matchup = None
        cached = storage.get_mapped_matchup(prov.name, ev.event_id)
        if cached is not None:
            matchup = next((m for m in mains if m.id == cached), None)
        if matchup is None:
            matchup = find_matchup(ev, mains)
            if matchup is not None:
                storage.save_mapping(prov.name, ev.event_id, matchup.id)

        if matchup is None:
            storage.save_stats_snapshot(stats, None)
            self._processed[key] = time.time()
            return

        storage.upsert_match(matchup.id, matchup.league, matchup.home, matchup.away, matchup.kickoff_utc)

        try:
            odds = self.pinn.get_match_odds(matchup.id, matchup, corners.get(matchup.id, set()))
        except PinnacleAuthError:
            raise
        except Exception:
            self._processed[key] = time.time()
            return

        storage.save_stats_snapshot(stats, matchup.id)
        storage.save_odds_snapshot(matchup.id, is_live=True, minute=stats.minute, rows=_odds_rows(odds))

        prematch = storage.get_prematch_moneyline(matchup.id)
        cands = signals_mod.evaluate(stats, odds, prematch)
        for c in cands:
            if storage.signal_exists(c.strategy, matchup.id):
                continue
            text = _format_signal(c, matchup, stats)
            chat = _signal_chat()
            sent = bool(chat and tg.send_message(chat, text))
            storage.save_signal(
                c.strategy, matchup.id, matchup.league, matchup.home, matchup.away,
                c.market, c.line, c.price, c.details_json(), sent,
            )

        self._processed[key] = time.time()

    # --- главный цикл ---

    def run_once(self):
        mains, corners = self._pinn_data()
        self._capture_prematch(mains, corners)
        for prov, ev in self._ht_events():
            try:
                self._handle_ht(prov, ev, mains, corners)
            except PinnacleAuthError:
                raise
            except Exception:
                storage.set_setting("last_error", traceback.format_exc()[-500:])
        storage.set_setting("last_cycle_ts", str(int(time.time())))

    def run_forever(self):
        storage.init_db()
        while True:
            try:
                self.run_once()
                self._auth_notified = False
                storage.set_setting("pinnacle_status", "ok")
            except PinnacleAuthError as e:
                storage.set_setting("pinnacle_status", "AUTH_ERROR")
                if not self._auth_notified:
                    _notify_admins(f"⚠️ Pinnacle: {e}\nОбновите PINNACLE_API_KEY в .env и перезапустите сбор.")
                    self._auth_notified = True
                time.sleep(60)
            except Exception:
                storage.set_setting("last_error", traceback.format_exc()[-500:])
            time.sleep(config.POLL_INTERVAL_LIVE)


# --- вспомогательное ---


def _has_prematch(matchup_id: int) -> bool:
    row = storage.get_prematch_moneyline(matchup_id)
    return bool(row and row["prematch_ts"])


def _within_lookahead(kickoff_iso, now, hours: int) -> bool:
    if not kickoff_iso:
        return False
    try:
        ko = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    delta = (ko - now).total_seconds()
    return 0 <= delta <= hours * 3600


def _odds_rows(odds) -> list[dict]:
    rows: list[dict] = []
    ml = odds.moneyline
    if ml:
        rows.append({"market": "moneyline", "side": "home", "line": None, "price": ml.p1})
        rows.append({"market": "moneyline", "side": "draw", "line": None, "price": ml.px})
        rows.append({"market": "moneyline", "side": "away", "line": None, "price": ml.p2})
    for e in odds.corner_team_totals:
        rows.append({"market": "corner_tt", "side": f"{e.get('team')}_over", "line": e.get("line"), "price": e.get("over")})
        rows.append({"market": "corner_tt", "side": f"{e.get('team')}_under", "line": e.get("line"), "price": e.get("under")})
    for e in odds.corner_handicaps:
        rows.append({"market": "corner_hcap", "side": "home", "line": e.get("line"), "price": e.get("home")})
        rows.append({"market": "corner_hcap", "side": "away", "line": e.get("line"), "price": e.get("away")})
    for e in odds.corner_totals:
        rows.append({"market": "corner_total", "side": "over", "line": e.get("line"), "price": e.get("over")})
        rows.append({"market": "corner_total", "side": "under", "line": e.get("line"), "price": e.get("under")})
    return [r for r in rows if r["price"] is not None]


def _format_signal(c, matchup, stats) -> str:
    hs, as_ = stats.home_stat, stats.away_stat
    names = {"itb2": "ITB2", "f2": "F2", "f1": "F1"}
    return (
        f"🎯 <b>Стратегия {names.get(c.strategy, c.strategy)}</b>\n"
        f"🏆 {matchup.league}\n"
        f"⚽ <b>{matchup.home} — {matchup.away}</b>\n"
        f"📊 1Т: голы {hs.goals}:{as_.goals}, красные {hs.red_cards}:{as_.red_cards}, "
        f"угловые {hs.corners}:{as_.corners}\n"
        f"💰 Ставка: <b>{c.market}</b> @ <b>{c.price:g}</b> (Pinnacle)"
    )


def _signal_chat():
    return storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or None)


def _notify_admins(text: str):
    for admin in config.ADMIN_IDS:
        tg.send_message(admin, text)


if __name__ == "__main__":
    Collector().run_forever()
