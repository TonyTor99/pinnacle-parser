"""Сборщик: драйвер на стат-провайдерах ловит перерыв (HT), сшивает матч с Pinnacle,
снимает статистику 1-го тайма + live-линии угловых, прогоняет стратегии и шлёт сигналы.

Отдельный процесс, управляемый ботом (pkill+start). Пишет heartbeat в settings для статуса.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone

import config
import logsetup
import signals as signals_mod
import storage
import tg
from matcher import find_matchup, normalize
from sources.pinnacle import PinnacleAuthError, PinnacleClient
from sources.stats_flashscore import FlashScore
from sources.stats_sofascore import SofaScore

# Логи сборщика уходят в stdout — бот перенаправляет их в collector.log
# (при standalone-запуске видно в консоли). Уровень берётся из settings.
log = logsetup.setup("collector")

PINN_CACHE_TTL = 60          # сек: как часто перечитывать список матчей Pinnacle
PREMATCH_LOOKAHEAD_H = 8     # ч: для каких прематч-матчей фиксировать 1X2 «первого сканирования»
PREMATCH_MAX_PER_CYCLE = 30  # ограничение вызовов на цикл
HT_REPROCESS_TTL = 90        # сек: повторная обработка того же HT-матча не чаще
HT_NOTIFY_TTL = 3 * 3600     # сек: сколько помнить, что HT-отчёт по матчу уже отправлен (чистка памяти)
STATS_GRACE = 75             # сек: сколько ждём статистику FlashScore, прежде чем отчитаться «не собралась»

MSK = timezone(timedelta(hours=3))


class Collector:
    def __init__(self):
        self.pinn = PinnacleClient()
        # SofaScore отключён: блокирует датацентр-IP (403 Varnish) без residential-прокси.
        # Вернуть запасным после покупки прокси: [FlashScore(), SofaScore()]
        self.providers = [FlashScore()]  # порядок = приоритет
        self._pinn_cache = None       # (ts, mains, corner_specials)
        self._processed: dict[str, float] = {}   # ключ события -> ts последней обработки
        self._ht_notified: dict[str, float] = {}  # ключ события -> ts отправки HT-отчёта (дедуп на перерыв)
        self._ht_seen: dict[str, float] = {}       # ключ события -> ts первого обнаружения HT (grace для статистики)
        self._auth_notified = False

    # --- Pinnacle список с кэшем ---

    def _pinn_data(self):
        now = time.time()
        if self._pinn_cache and now - self._pinn_cache[0] < PINN_CACHE_TTL:
            return self._pinn_cache[1], self._pinn_cache[2]
        t0 = time.monotonic()
        mains, corners = self.pinn.fetch_soccer_matchups()
        log.debug("Pinnacle: список обновлён за %.0f мс", (time.monotonic() - t0) * 1000)
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
            except Exception as e:
                log.warning("%s: не удалось получить live-список: %s", prov.name, e)
                continue
            ht = [ev for ev in live if ev.status == "HT"]
            logsetup.live(log, "%s: live=%d, на перерыве (HT)=%d", prov.name, len(live), len(ht))
            for ev in ht:
                key = (normalize(ev.home), normalize(ev.away))
                if key in seen:
                    continue
                seen.add(key)
                result.append((prov, ev))
                logsetup.live(log, "  🕐 HT: [%s] %s — %s (%s)", ev.league, ev.home, ev.away, prov.name)
        return result

    def _recently_processed(self, key: str) -> bool:
        ts = self._processed.get(key)
        return ts is not None and (time.time() - ts) < HT_REPROCESS_TTL

    # --- обработка одного HT-матча ---

    def _handle_ht(self, prov, ev, mains, corners):
        key = f"{prov.name}:{ev.event_id}"
        self._ht_seen.setdefault(key, time.time())
        if self._recently_processed(key):
            return
        stats = prov.get_stats(ev)
        if stats is None:
            # Статистика ещё не спарсилась — даём несколько циклов на ретрай.
            # Если за grace-период так и не собралась, шлём в мониторинг «проблемную»
            # карточку (по данным live-списка), чтобы видеть матчи без статистики FlashScore.
            if time.time() - self._ht_seen[key] > STATS_GRACE:
                self._notify_ht(key, None, None, None, None, ev=ev)
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
            logsetup.live(log, "  ❔ не сшит с Pinnacle: %s — %s", ev.home, ev.away)
            storage.save_stats_snapshot(stats, None)
            # Матч на перерыве, статистика есть, но нет соответствия в Pinnacle (нет линий) —
            # тоже показываем в мониторинге, со статистикой 1Т и пометкой.
            self._notify_ht(key, None, stats, None, None, ev=ev)
            self._processed[key] = time.time()
            return

        logsetup.live(log, "  🔗 сшит с Pinnacle #%d: %s — %s", matchup.id, matchup.home, matchup.away)
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

        # Отчёт мониторинга: один раз за перерыв шлём в отдельный чат карточку матча
        # (дата/время, лига, команды, статистика 1Т FlashScore, прематч 1X2, линии угловых) —
        # чтобы видеть, что по каждому сшитому матчу реально собирается статистика.
        self._notify_ht(key, matchup, stats, prematch, odds, ev=ev)

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
            log.info("🎯 СИГНАЛ %s: %s — %s | %s @ %s (отправлен=%s)",
                     c.strategy, matchup.home, matchup.away, c.market, c.price, sent)

        self._processed[key] = time.time()

    # --- отчёт мониторинга HT ---

    def _notify_ht(self, key, matchup, stats, prematch, odds, ev=None):
        """Отправить в чат мониторинга карточку матча на перерыве. Один раз за перерыв.

        matchup/stats/odds могут быть None: карточка тогда строится по live-событию ev
        (не сшит с Pinnacle либо статистика FlashScore не собралась)."""
        now = time.time()
        # чистим устаревшие отметки (матчи давно завершены), чтобы словари не росли
        for k in [k for k, ts in self._ht_notified.items() if now - ts > HT_NOTIFY_TTL]:
            del self._ht_notified[k]
        for k in [k for k, ts in self._ht_seen.items() if now - ts > HT_NOTIFY_TTL]:
            del self._ht_seen[k]
        if key in self._ht_notified:
            return
        chat = _stats_chat()
        if not chat:
            return
        text = _format_ht_report(matchup, stats, prematch, odds, ev)
        if tg.send_message(chat, text):
            self._ht_notified[key] = now
            home = matchup.home if matchup else (ev.home if ev else "?")
            away = matchup.away if matchup else (ev.away if ev else "?")
            logsetup.live(log, "  📋 HT-отчёт отправлен: %s — %s", home, away)

    # --- главный цикл ---

    def run_once(self):
        mains, corners = self._pinn_data()
        log.info("Pinnacle: %d матчей (%d с угловыми)", len(mains), len(corners))
        self._capture_prematch(mains, corners)
        ht_events = self._ht_events()
        for prov, ev in ht_events:
            try:
                self._handle_ht(prov, ev, mains, corners)
            except PinnacleAuthError:
                raise
            except Exception:
                log.error("Ошибка обработки матча:\n%s", traceback.format_exc())
                storage.set_setting("last_error", traceback.format_exc()[-500:])
        log.info("Цикл завершён: HT-матчей обработано %d", len(ht_events))
        storage.set_setting("last_cycle_ts", str(int(time.time())))

    def run_forever(self):
        storage.init_db()
        logsetup.apply_level(storage.get_setting("log_level", logsetup.DEFAULT_LEVEL))
        log.info("Сборщик запущен (интервал %dс, уровень логов %s)",
                 config.POLL_INTERVAL_LIVE,
                 (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper())
        while True:
            # Перечитываем уровень каждый цикл — смена из бота применяется без перезапуска.
            logsetup.apply_level(storage.get_setting("log_level", logsetup.DEFAULT_LEVEL))
            try:
                self.run_once()
                self._auth_notified = False
                storage.set_setting("pinnacle_status", "ok")
            except PinnacleAuthError as e:
                log.warning("Pinnacle AUTH_ERROR: %s (обновите PINNACLE_API_KEY)", e)
                storage.set_setting("pinnacle_status", "AUTH_ERROR")
                if not self._auth_notified:
                    _notify_admins(f"⚠️ Pinnacle: {e}\nОбновите PINNACLE_API_KEY в .env и перезапустите сбор.")
                    self._auth_notified = True
                time.sleep(60)
            except Exception:
                log.error("Ошибка цикла:\n%s", traceback.format_exc())
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


def _stats_chat():
    return storage.get_setting("stats_chat_id", config.STATS_CHAT_ID or None)


def _fmt_kickoff(kickoff_iso) -> str:
    """ISO-время старта (UTC) -> 'ДД.ММ ЧЧ:ММ МСК'."""
    if not kickoff_iso:
        return "время неизвестно"
    try:
        dt = datetime.fromisoformat(str(kickoff_iso).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(kickoff_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m %H:%M МСК")


def _fmt_corner_lines(odds) -> str:
    """Компактный блок доступных линий угловых Pinnacle (только основное время, period 0)."""
    if odds is None:
        return ""

    def _p0(lst):
        return [e for e in lst if (e.get("period", 0) or 0) == 0]

    lines: list[str] = []

    totals = sorted(_p0(odds.corner_totals), key=lambda e: (e.get("line") is None, e.get("line")))
    tt_parts = []
    for e in totals[:6]:
        over, under = e.get("over"), e.get("under")
        cf = "/".join(x for x in ([f"О {over:g}"] if over else []) + ([f"У {under:g}"] if under else []))
        tt_parts.append(f"{e.get('line'):g} ({cf})" if cf else f"{e.get('line'):g}")
    if tt_parts:
        lines.append("   • Тоталы: " + ", ".join(tt_parts))

    hcaps = sorted(_p0(odds.corner_handicaps), key=lambda e: (e.get("line") is None, e.get("line")))
    hc_parts = []
    for e in hcaps[:6]:
        h, a = e.get("home"), e.get("away")
        cf = "/".join(x for x in ([f"1 {h:g}"] if h else []) + ([f"2 {a:g}"] if a else []))
        hc_parts.append(f"{e.get('line'):+g} ({cf})" if cf else f"{e.get('line'):+g}")
    if hc_parts:
        lines.append("   • Форы: " + ", ".join(hc_parts))

    for side, label in (("home", "ИТ хоз"), ("away", "ИТ гост")):
        team = sorted([e for e in _p0(odds.corner_team_totals) if e.get("team") == side],
                      key=lambda e: (e.get("line") is None, e.get("line")))
        parts = []
        for e in team[:6]:
            over, under = e.get("over"), e.get("under")
            cf = "/".join(x for x in ([f"О {over:g}"] if over else []) + ([f"У {under:g}"] if under else []))
            parts.append(f"{e.get('line'):g} ({cf})" if cf else f"{e.get('line'):g}")
        if parts:
            lines.append(f"   • {label}: " + ", ".join(parts))

    if not lines:
        return "🚩 Угловые Pinnacle: линий нет"
    return "🚩 Угловые Pinnacle (осн. время):\n" + "\n".join(lines)


def _format_ht_report(matchup, stats, prematch, odds, ev=None) -> str:
    """Карточка матча на перерыве для чата мониторинга.

    Три режима: полный (сшит + стата + линии), «не сшит с Pinnacle» (есть стата),
    «нет статистики FlashScore» (только live-событие ev)."""
    league = (matchup.league if matchup else (ev.league if ev else "")) or "—"
    home = matchup.home if matchup else (ev.home if ev else "?")
    away = matchup.away if matchup else (ev.away if ev else "?")
    kickoff = (matchup.kickoff_utc if matchup else None) or (ev.kickoff_utc if ev else None) \
        or (stats.kickoff_utc if stats else None)

    head = (
        f"📋 <b>Перерыв</b> — {_fmt_kickoff(kickoff)}\n"
        f"🏆 {league}\n"
        f"⚽ <b>{home} — {away}</b>"
    )

    if stats is None:
        return head + "\n⚠️ Статистика FlashScore не собралась (матч на перерыве, данных 1Т нет)."

    hs, as_ = stats.home_stat, stats.away_stat
    body = (
        f"\n📊 1Т (FlashScore): голы {hs.goals}:{as_.goals}, "
        f"🟥 {hs.red_cards}:{as_.red_cards}, угловые {hs.corners}:{as_.corners}"
    )

    if matchup is None:
        return head + body + "\n❔ Не сшит с Pinnacle — прематч 1X2 и линии угловых недоступны."

    if prematch and (prematch["prematch_p1"] or prematch["prematch_p2"]):
        p1 = prematch["prematch_p1"]
        px = prematch["prematch_px"]
        p2 = prematch["prematch_p2"]
        body += (f"\n💹 Прематч 1X2: П1 {p1:g} X {px:g} П2 {p2:g}"
                 if all(v is not None for v in (p1, px, p2))
                 else f"\n💹 Прематч 1X2: П1 {p1 or '—'} X {px or '—'} П2 {p2 or '—'}")
    else:
        body += "\n💹 Прематч 1X2: не зафиксирован"

    corners = _fmt_corner_lines(odds)
    if corners:
        body += "\n" + corners
    return head + body


def _notify_admins(text: str):
    for admin in config.ADMIN_IDS:
        tg.send_message(admin, text)


if __name__ == "__main__":
    Collector().run_forever()
