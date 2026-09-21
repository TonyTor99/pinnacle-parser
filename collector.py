"""Сборщик (Pinnacle-driven): ведём матчи по Pinnacle, перерыв (HT) определяем по live-state
Pinnacle (state==2), захватываем линии угловых коротким окном, затем идём в FlashScore за
статистикой 1-го тайма, прогоняем стратегии и шлём сигналы.

Почему «от Pinnacle»: стратегия применима только к матчам, у которых на Pinnacle есть рынок
угловых (их ~40 из сотен live). Идти «от FlashScore» (сотни матчей) — почти всегда мимо.

Три исхода на перерыве:
1. Подошёл под itb2/f2/f1 → сигнал в чат сигналов.
2. Любой матч с угловыми на HT → карточка в чат мониторинга + строка ht_monitor.
3. Есть на Pinnacle, но не нашёлся в FlashScore → алерт + строка unmatched (снимок FS-списка)
   + ручной алиасинг из бота.

Отдельный процесс, управляемый ботом (pkill+start). Пишет heartbeat в settings для статуса.
"""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta, timezone

import config
import logsetup
import signals as signals_mod
import storage
import tg
from matcher import find_fs_event
from sources.pinnacle import PinnacleAuthError, PinnacleClient
from sources.stats_flashscore import FlashScore
from sources.stats_sofascore import SofaScore

log = logsetup.setup("collector")

PINN_CACHE_TTL = 60          # сек: как часто перечитывать список матчей Pinnacle
PREMATCH_LOOKAHEAD_H = 8     # ч: для каких прематч-матчей фиксировать 1X2 «первого сканирования»
PREMATCH_MAX_PER_CYCLE = 30  # ограничение вызовов на цикл
FS_GRACE = 60                # сек: сколько ждём появления матча в FlashScore, прежде чем «не сшит»
STATS_GRACE = 90             # сек: сколько ждём HT-статистику FlashScore, прежде чем отчитаться
HT_MEM_TTL = 3 * 3600        # сек: сколько помнить обработанный перерыв (чистка словарей)

STRATEGY_NAMES = {"itb2": "itb2", "f2": "f2", "f1": "f1"}
MSK = timezone(timedelta(hours=3))


class Collector:
    def __init__(self):
        self.pinn = PinnacleClient()
        # SofaScore отключён: блокирует датацентр-IP (403 Varnish) без residential-прокси.
        # Вернуть запасным после покупки прокси: [FlashScore(), SofaScore()]
        self.providers = [FlashScore()]     # порядок = приоритет
        self.provider = self.providers[0]   # активный стат-провайдер
        self._pinn_cache = None             # (ts, mains, corners, watch)
        self._fs_cache = None               # (ts, list[LiveEvent]) — кэш live-списка на цикл
        # HT-память по parent_id: захваченные линии, тайминги, флаг «обработан за перерыв»
        self._ht_seen: dict[int, float] = {}    # pid -> ts первого HT (grace)
        self._ht_odds: dict[int, object] = {}   # pid -> MatchOdds (снимок линий, один раз)
        self._ht_done: dict[int, float] = {}    # pid -> ts финализации (дедуп на перерыв)
        self._auth_notified = False

    # --- Pinnacle список с кэшем ---

    def _pinn_data(self):
        now = time.time()
        if self._pinn_cache and now - self._pinn_cache[0] < PINN_CACHE_TTL:
            return self._pinn_cache[1], self._pinn_cache[2], self._pinn_cache[3]
        t0 = time.monotonic()
        mains, corners, watch = self.pinn.fetch_matchups()
        log.debug("Pinnacle: список обновлён за %.0f мс", (time.monotonic() - t0) * 1000)
        self._pinn_cache = (now, mains, corners, watch)
        return mains, corners, watch

    def _fs_live(self):
        """Live-список FlashScore с коротким кэшем на цикл (переиспользуем для всех HT-матчей)."""
        now = time.time()
        if self._fs_cache and now - self._fs_cache[0] < config.POLL_INTERVAL_LIVE - 2:
            return self._fs_cache[1]
        try:
            live = self.provider.list_live()
        except Exception as e:
            log.warning("%s: не удалось получить live-список: %s", self.provider.name, e)
            live = []
        logsetup.live(log, "%s: live=%d (в т.ч. HT=%d)", self.provider.name,
                      len(live), sum(1 for e in live if e.status == "HT"))
        self._fs_cache = (now, live)
        return live

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

    # --- обработка одного матча на перерыве ---

    def _handle_ht(self, wm):
        """wm: PinnWatchMatch в фазе перерыва. Захватить линии, сшить с FS, оценить стратегии."""
        pid = wm.parent_id
        if pid in self._ht_done:
            return
        now = time.time()
        self._ht_seen.setdefault(pid, now)

        # 1. Линии угловых на перерыве живут коротким окном (~4 мин) — снимаем один раз, сразу.
        if pid not in self._ht_odds:
            try:
                self._ht_odds[pid] = self.pinn.get_match_odds(pid, wm.as_main(), wm.corner_ids)
            except PinnacleAuthError:
                raise
            except Exception:
                self._ht_odds[pid] = None
        odds = self._ht_odds[pid]

        # 2. Сшивка с FlashScore по именам (нормализация + прямой/своп + окно времени + aliases).
        fs_live = self._fs_live()
        ev = find_fs_event(wm.home, wm.away, wm.kickoff_utc, fs_live)

        if ev is None:
            # Не нашли в FS — даём немного времени (FS мог ещё не показать матч), потом фиксируем.
            if now - self._ht_seen[pid] > FS_GRACE:
                self._save_unmatched(wm, fs_live)
                self._ht_done[pid] = now
            return

        # 3. Статистику берём только когда FS сам подтвердил перерыв — иначе угловые 1Т не финальны.
        stats = self.provider.get_stats(ev) if ev.status == "HT" else None
        if stats is None:
            if now - self._ht_seen[pid] > STATS_GRACE:
                self._finalize(wm, ev, None, odds)   # карточка «статистики нет»
                self._ht_done[pid] = now
            return  # ретрай в пределах grace

        self._finalize(wm, ev, stats, odds)
        self._ht_done[pid] = now

    def _save_unmatched(self, wm, fs_live):
        """Сохранить несшитый матч + снимок FS-списка момента, разово алертнуть в мониторинг."""
        snap = json.dumps([_fs_ev_dict(e) for e in fs_live], ensure_ascii=False)
        uid = storage.save_unmatched(wm.parent_id, wm.league, wm.home, wm.away, wm.kickoff_utc, snap)
        logsetup.live(log, "  ⚠️ FS не найден: %s — %s (unmatched id=%s)", wm.home, wm.away, uid)
        if uid:  # только при первой вставке — не спамим
            chat = _stats_chat()
            if chat:
                tg.send_message(chat, _format_unmatched(wm))

    def _finalize(self, wm, ev, stats, odds):
        """Финал по матчу на перерыве: запись в БД, ht_monitor, сигналы, карточка мониторинга."""
        pid = wm.parent_id
        storage.upsert_match(pid, wm.league, wm.home, wm.away, wm.kickoff_utc)
        prematch = storage.get_prematch_moneyline(pid)

        cands = []
        if stats is not None:
            storage.save_stats_snapshot(stats, pid)
            if odds is not None:
                storage.save_odds_snapshot(pid, is_live=True, minute=stats.minute, rows=_odds_rows(odds))
            cands = signals_mod.evaluate(stats, odds, prematch) if odds is not None else []

        matched = {c.strategy for c in cands}
        verdict = _verdict_text(matched) if stats is not None else "нет статистики FS"

        # ht_monitor: одна строка на перерыв
        lines_json = json.dumps(_odds_rows(odds), ensure_ascii=False) if odds is not None else "[]"
        hs = stats.home_stat if stats else None
        as_ = stats.away_stat if stats else None
        storage.save_ht_monitor(
            matchup_id=pid, parent_id=pid, league=wm.league, home=wm.home, away=wm.away,
            ht_home_goals=(hs.goals if hs else None), ht_away_goals=(as_.goals if as_ else None),
            home_reds=(hs.red_cards if hs else None), away_reds=(as_.red_cards if as_ else None),
            home_corners=(hs.corners if hs else None), away_corners=(as_.corners if as_ else None),
            prematch_p1=_pm(prematch, "prematch_p1"), prematch_px=_pm(prematch, "prematch_px"),
            prematch_p2=_pm(prematch, "prematch_p2"),
            corner_lines_json=lines_json, verdict=verdict, matched_fs=True,
        )

        # карточка мониторинга
        chat = _stats_chat()
        if chat:
            tg.send_message(chat, _format_monitor(wm, stats, prematch, odds, matched))

        # сигналы
        for c in cands:
            if storage.signal_exists(c.strategy, pid):
                continue
            sig_chat = _signal_chat()
            sent = bool(sig_chat and tg.send_message(sig_chat, _format_signal(c, wm, stats, prematch)))
            storage.save_signal(
                c.strategy, pid, wm.league, wm.home, wm.away,
                c.market, c.line, c.price, c.details_json(), sent,
            )
            log.info("🎯 СИГНАЛ %s: %s — %s | %s @ %s (отправлен=%s)",
                     c.strategy, wm.home, wm.away, c.market, c.price, sent)

        tag = "ПОДОШЛА " + ",".join(sorted(matched)) if matched else ("нет статистики" if stats is None else "не подошла")
        logsetup.live(log, "  📋 HT %s — %s [%s]", wm.home, wm.away, tag)

    # --- главный цикл ---

    def _cleanup_mem(self):
        now = time.time()
        for d in (self._ht_seen, self._ht_done):
            for k in [k for k, ts in d.items() if now - ts > HT_MEM_TTL]:
                del d[k]
        # линии чистим по «виденным» (odds не хранит ts) — снимаем те, чей HT давно закрыт
        for k in [k for k in self._ht_odds if k not in self._ht_seen]:
            del self._ht_odds[k]

    def run_once(self):
        mains, corners, watch = self._pinn_data()
        ht = [wm for wm in watch if wm.is_ht]
        log.info("Pinnacle: %d матчей, watch(угловые)=%d, на перерыве=%d", len(mains), len(watch), len(ht))
        self._capture_prematch(mains, corners)
        for wm in ht:
            try:
                self._handle_ht(wm)
            except PinnacleAuthError:
                raise
            except Exception:
                log.error("Ошибка обработки матча %s:\n%s", wm.parent_id, traceback.format_exc())
                storage.set_setting("last_error", traceback.format_exc()[-500:])
        self._cleanup_mem()
        log.info("Цикл завершён: HT-матчей %d", len(ht))
        storage.set_setting("last_cycle_ts", str(int(time.time())))

    def run_forever(self):
        storage.init_db()
        logsetup.apply_level(storage.get_setting("log_level", logsetup.DEFAULT_LEVEL))
        log.info("Сборщик запущен (Pinnacle-driven, интервал %dс, уровень логов %s)",
                 config.POLL_INTERVAL_LIVE,
                 (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper())
        while True:
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


def _pm(prematch, key):
    return prematch[key] if prematch else None


def _fs_ev_dict(e) -> dict:
    return {"id": e.event_id, "home": e.home, "away": e.away,
            "kickoff": e.kickoff_utc, "status": e.status}


def _odds_rows(odds) -> list[dict]:
    if odds is None:
        return []
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


def _has_corner_lines(odds) -> bool:
    return bool(odds is not None and odds.has_corners())


def _verdict_text(matched: set[str]) -> str:
    return " · ".join(f"{name} {'✅' if s in matched else '✖'}" for s, name in STRATEGY_NAMES.items())


# --- форматтеры сообщений (лаконично) ---


def _corners_line(stats) -> str:
    hs, as_ = stats.home_stat, stats.away_stat
    tot = hs.corners + as_.corners
    return f"🚩 угл 1Т {hs.corners}:{as_.corners} (Σ{tot})  🟥 {hs.red_cards}:{as_.red_cards}"


def _prematch_for(strategy: str, prematch) -> str:
    if not prematch:
        return ""
    p1, p2 = prematch["prematch_p1"], prematch["prematch_p2"]
    if strategy == "itb2" and p2:
        return f"📊 Прематч П2 {p2:g}"
    if strategy == "f1" and p1:
        return f"📊 Прематч П1 {p1:g}"
    return ""


def _format_signal(c, wm, stats, prematch) -> str:
    hs, as_ = stats.home_stat, stats.away_stat
    parts = [
        f"🎯 <b>СИГНАЛ · {STRATEGY_NAMES.get(c.strategy, c.strategy)}</b>",
        f"🏆 {wm.league or '—'}",
        f"⚽ <b>{wm.home} — {wm.away}</b>",
        f"⏸ Перерыв · счёт {hs.goals}:{as_.goals}",
        _corners_line(stats),
    ]
    pm = _prematch_for(c.strategy, prematch)
    if pm:
        parts.append(pm)
    parts.append(f"💰 {c.market} · КФ <b>{c.price:g}</b>")
    return "\n".join(parts)


def _format_monitor(wm, stats, prematch, odds, matched: set[str]) -> str:
    if stats is None:
        return (
            "⚪ <b>Нет статистики FS</b>\n"
            f"🏆 {wm.league or '—'} · ⚽ <b>{wm.home} — {wm.away}</b>\n"
            "⏸ перерыв (Pinnacle), статистика 1Т FlashScore не собралась"
        )
    hs, as_ = stats.home_stat, stats.away_stat
    head = ("🟢 <b>ПОДОШЛА " + ", ".join(sorted(matched)) + "</b>") if matched else "⚪ Не подошла"
    p1 = _pm(prematch, "prematch_p1")
    p2 = _pm(prematch, "prematch_p2")
    pm_line = (f"📊 П1/П2 {p1:g} / {p2:g}" if (p1 and p2)
               else f"📊 П1/П2 {p1 or '—'} / {p2 or '—'}")
    lines = [
        head,
        f"🏆 {wm.league or '—'} · ⚽ <b>{wm.home} — {wm.away}</b>",
        f"⏸ {hs.goals}:{as_.goals} · {_corners_line(stats)}",
        pm_line,
        "🎯 " + _verdict_text(matched),
    ]
    if not _has_corner_lines(odds):
        lines.append("⚠️ линий угловых нет (окно закрылось)")
    return "\n".join(lines)


def _format_unmatched(wm) -> str:
    return (
        "⚠️ <b>FS не найден</b> · матч на перерыве\n"
        f"🏆 {wm.league or '—'} · ⚽ <b>{wm.home} — {wm.away}</b> ({_fmt_kickoff(wm.kickoff_utc)})\n"
        "🚩 Угловые Pinnacle есть, статистики нет\n"
        "➡️ Разобрать в боте: «⚠️ Не сшитые матчи»"
    )


def _signal_chat():
    return storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or None)


def _stats_chat():
    return storage.get_setting("stats_chat_id", config.STATS_CHAT_ID or None)


def _fmt_kickoff(kickoff_iso) -> str:
    if not kickoff_iso:
        return "время неизвестно"
    try:
        dt = datetime.fromisoformat(str(kickoff_iso).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return str(kickoff_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%d.%m %H:%M МСК")


def _notify_admins(text: str):
    for admin in config.ADMIN_IDS:
        tg.send_message(admin, text)


if __name__ == "__main__":
    Collector().run_forever()
