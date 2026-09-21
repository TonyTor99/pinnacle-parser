"""Кнопочный TG-бот управления сборщиком угловых.

Меню разбито на вкладки (Главная → Отчёты / Данные / Настройки), навигация —
через редактирование одного сообщения. Обработка апдейтов вынесена в пул потоков,
чтобы медленные вызовы Telegram API не «глушили» опрос getUpdates.

Доступ только для ADMIN_IDS. Long-polling на requests (без async-фреймворков).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import config
import export_excel
import logsetup
import reports
import storage
import tg
from matcher import _sim, normalize

BASE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(BASE, "collector.py")
LOG_PATH = os.path.join(BASE, "collector.log")
BOT_LOG_PATH = os.path.join(BASE, "bot.log")
MSK = timezone(timedelta(hours=3))

log = logsetup.setup("bot", to_file=BOT_LOG_PATH)

# Пул для обработки апдейтов: главный цикл только опрашивает getUpdates и раздаёт задачи.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="upd")

# Кто из админов сейчас вводит значение (напр. ID чата сигналов): uid -> что ждём.
_awaiting: dict[int, str] = {}


# --- управление процессом сборщика ---


def collector_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "collector.py"], capture_output=True, text=True)
        return bool(out.stdout.strip())
    except Exception:
        return False


def start_collector() -> bool:
    if collector_running():
        return False
    logf = open(LOG_PATH, "ab")
    subprocess.Popen(
        [sys.executable, COLLECTOR],
        stdout=logf, stderr=logf, cwd=BASE, start_new_session=True,
    )
    log.info("Сборщик запущен")
    return True


def stop_collector() -> bool:
    if not collector_running():
        return False
    subprocess.run(["pkill", "-9", "-f", "collector.py"])
    log.info("Сборщик остановлен")
    return True


# --- клавиатуры вкладок ---


def home_kb() -> dict:
    running = collector_running()
    toggle = ("⏹ Остановить сбор", "stop") if running else ("▶️ Запустить сбор", "start")
    return {"inline_keyboard": [
        [{"text": toggle[0], "callback_data": toggle[1]}],
        [{"text": "📊 Статус", "callback_data": "status"}],
        [{"text": "📈 Отчёты", "callback_data": "tab:reports"},
         {"text": "🗄 Данные", "callback_data": "tab:data"}],
        [{"text": "⚙️ Настройки", "callback_data": "tab:settings"}],
    ]}


def reports_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "📅 Сегодня", "callback_data": "rep:day"},
         {"text": "🗓 Неделя", "callback_data": "rep:week"}],
        [{"text": "📆 Месяц", "callback_data": "rep:month"},
         {"text": "♾ Всё время", "callback_data": "rep:all"}],
        [{"text": "📥 Выгрузить Excel", "callback_data": "excel"}],
        [{"text": "« Назад", "callback_data": "tab:home"}],
    ]}


def data_kb() -> dict:
    n = len(storage.list_unmatched(resolved=False))
    um_label = "⚠️ Не сшитые матчи" + (f" ({n})" if n else "")
    return {"inline_keyboard": [
        [{"text": um_label, "callback_data": "unmatched"}],
        [{"text": "📥 Экспорт в Excel", "callback_data": "excel"}],
        [{"text": "🧹 Очистить сигналы", "callback_data": "reset:sig"}],
        [{"text": "💣 Стереть всю базу", "callback_data": "reset:all"}],
        [{"text": "« Назад", "callback_data": "tab:home"}],
    ]}


# --- «Не сшитые матчи»: ручной алиасинг FlashScore ↔ Pinnacle ---


def _fmt_ko(kickoff_iso) -> str:
    if not kickoff_iso:
        return "?"
    try:
        dt = datetime.fromisoformat(str(kickoff_iso).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK).strftime("%H:%M")


def _btn_short(s: str, limit: int = 28) -> str:
    return s if len(s) <= limit else s[:limit - 1] + "…"


def unmatched_list_kb() -> dict:
    rows = []
    for u in storage.list_unmatched(resolved=False)[:12]:
        label = _btn_short(f"{u['home']}—{u['away']} ({_fmt_ko(u['kickoff_utc'])})")
        rows.append([{"text": label, "callback_data": f"um:{u['id']}"}])
    rows.append([{"text": "« Назад", "callback_data": "tab:data"}])
    return {"inline_keyboard": rows}


def unmatched_list_text() -> str:
    n = len(storage.list_unmatched(resolved=False))
    if not n:
        return ("⚠️ <b>Не сшитые матчи</b>\n\nПусто — все матчи с угловыми на перерыве "
                "сшивались с FlashScore. 👍")
    return ("⚠️ <b>Не сшитые матчи</b>\n\n"
            f"Матчей с угловыми на перерыве, которые не нашлись в FlashScore: <b>{n}</b>.\n"
            "Выберите матч, чтобы вручную указать соответствие из FS-списка того момента.")


def _snapshot(u) -> list[dict]:
    try:
        return json.loads(u["fs_snapshot_json"] or "[]")
    except (ValueError, TypeError):
        return []


def unmatched_detail(u_id: int, show_all: bool):
    """(текст, клавиатура) экрана выбора FS-кандидата для несшитого матча."""
    u = storage.get_unmatched(u_id)
    if u is None or u["resolved"]:
        return "Запись уже обработана.", unmatched_list_kb()
    snap = _snapshot(u)
    ht = [c for c in snap if c.get("status") == "HT"]
    shown = snap if show_all else (ht or snap)
    rows = []
    for c in shown[:16]:
        idx = snap.index(c)
        mark = "⏸" if c.get("status") == "HT" else "▶"
        label = _btn_short(f"{mark} {c.get('home')}—{c.get('away')} {_fmt_ko(c.get('kickoff'))}")
        rows.append([{"text": label, "callback_data": f"umpick:{u_id}:{idx}"}])
    if not show_all and len(snap) > len(ht):
        rows.append([{"text": f"🔽 Показать все живые ({len(snap)})", "callback_data": f"um:{u_id}:all"}])
    rows.append([{"text": "🗑 Пропустить (удалить)", "callback_data": f"umskip:{u_id}"}])
    rows.append([{"text": "« Назад", "callback_data": "unmatched"}])
    text = (
        "⚠️ <b>Сшивка вручную</b>\n\n"
        f"🏆 {u['league'] or '—'}\n"
        f"⚽ Pinnacle: <b>{u['home']} — {u['away']}</b> ({_fmt_ko(u['kickoff_utc'])})\n\n"
        f"Выберите соответствующий матч из FlashScore ({'все живые' if show_all else 'на перерыве'}):\n"
        "<i>⏸ — на перерыве, ▶ — идёт. Выбор запишет алиас в aliases.json, "
        "сборщик подхватит его на следующем перерыве.</i>"
    )
    if not shown:
        text += "\n\n<i>FS-снимок пуст — нечего сопоставлять, можно пропустить.</i>"
    return text, {"inline_keyboard": rows}


def _write_alias(pinn_home, pinn_away, fs_home, fs_away) -> None:
    """Записать соответствие FS→Pinnacle с учётом возможной перестановки команд."""
    direct = min(_sim(normalize(pinn_home), normalize(fs_home)),
                 _sim(normalize(pinn_away), normalize(fs_away)))
    swap = min(_sim(normalize(pinn_home), normalize(fs_away)),
               _sim(normalize(pinn_away), normalize(fs_home)))
    try:
        with open(config.ALIASES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    if swap > direct:
        data[fs_home] = pinn_away
        data[fs_away] = pinn_home
    else:
        data[fs_home] = pinn_home
        data[fs_away] = pinn_away
    with open(config.ALIASES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("Алиас записан: %s→%s, %s→%s", fs_home, pinn_home, fs_away, pinn_away)


def settings_kb() -> dict:
    cur = (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper()
    row = []
    kb = [[{"text": "💬 Сигналы в этот чат", "callback_data": "setchat"},
           {"text": "✏️ Ввести ID чата", "callback_data": "setchat:ask"}],
          [{"text": "📋 Мониторинг в этот чат", "callback_data": "setstats"},
           {"text": "✏️ Ввести ID мониторинга", "callback_data": "setstats:ask"}],
          [{"text": "🔊 Уровень логов:", "callback_data": "noop"}]]
    for name in ("ERROR", "INFO", "LIVE", "DEBUG"):
        mark = "✅ " if name == cur else ""
        row.append({"text": mark + logsetup.LEVEL_LABELS[name], "callback_data": f"log:{name}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([{"text": "« Назад", "callback_data": "tab:home"}])
    return {"inline_keyboard": kb}


# --- тексты экранов ---


HOME_TEXT = (
    "🎯 <b>Угловые Pinnacle</b> — панель управления\n\n"
    "Выберите раздел или запустите сбор сигналов."
)
REPORTS_TEXT = "📈 <b>Отчёты по сигналам</b>\n\nВыберите период или выгрузите Excel."
DATA_TEXT = "🗄 <b>Данные</b>\n\nЭкспорт и очистка базы."
SETCHAT_ASK = (
    "✏️ <b>Куда слать сигналы</b>\n\n"
    "Пришлите ID чата или @username канала одним сообщением. Примеры:\n"
    "• <code>-1001234567890</code> — приватный чат/канал\n"
    "• <code>@my_channel</code> — публичный канал\n"
    "• <code>307658038</code> — личка\n\n"
    "⚠️ Бот должен состоять в этом чате (в канал — добавить админом с правом писать).\n"
    "Для отмены пришлите /cancel."
)
SETSTATS_ASK = (
    "✏️ <b>Чат мониторинга перерывов</b>\n\n"
    "Сюда на каждом перерыве будет падать карточка матча (дата, лига, команды, "
    "статистика 1Т FlashScore, прематч 1X2 и линии угловых).\n\n"
    "Пришлите ID чата или @username канала одним сообщением. Примеры:\n"
    "• <code>-1001234567890</code> — приватный чат/канал\n"
    "• <code>@my_channel</code> — публичный канал\n\n"
    "⚠️ Бот должен состоять в этом чате (в канал — админом с правом писать).\n"
    "Для отмены пришлите /cancel."
)


def _set_signal_chat(target: str) -> bool:
    """Назначить чат сигналов: сперва пробуем отправить туда тест — сохраняем только при успехе."""
    ok = bool(tg.send_message(target, "✅ Этот чат назначен для сигналов угловых Pinnacle."))
    if ok:
        storage.set_setting("signal_chat_id", target)
        log.info("Чат сигналов задан: %s", target)
    return ok


def _set_stats_chat(target: str) -> bool:
    """Назначить чат мониторинга перерывов (тест-отправка, сохраняем только при успехе)."""
    ok = bool(tg.send_message(target, "✅ Этот чат назначен для мониторинга перерывов (HT)."))
    if ok:
        storage.set_setting("stats_chat_id", target)
        log.info("Чат мониторинга задан: %s", target)
    return ok


def settings_text() -> str:
    chat = storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or "не задан")
    stats_chat = storage.get_setting("stats_chat_id", config.STATS_CHAT_ID or "не задан")
    cur = (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper()
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"💬 Чат сигналов: <code>{chat}</code>\n"
        f"📋 Чат мониторинга: <code>{stats_chat}</code>\n"
        f"🔊 Уровень логов: <b>{logsetup.LEVEL_LABELS.get(cur, cur)}</b>\n\n"
        "🟢 Live-матчи — видно доступные матчи в логах сбора.\n"
        "🔵 Отладка — тайминги кнопок и запросов (если бот тормозит).\n"
        "<i>Смена уровня для сборщика применится при перезапуске сбора.</i>"
    )


def status_text() -> str:
    running = "🟢 работает" if collector_running() else "🔴 остановлен"
    last = storage.get_setting("last_cycle_ts")
    last_s = "—"
    if last:
        dt = datetime.fromtimestamp(int(last), tz=MSK)
        ago = int(time.time()) - int(last)
        last_s = f"{dt.strftime('%H:%M:%S')} МСК ({ago}с назад)"
    pinn = storage.get_setting("pinnacle_status", "—")
    chat = storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or "не задан")
    stats_chat = storage.get_setting("stats_chat_id", config.STATS_CHAT_ID or "не задан")
    err = storage.get_setting("last_error")
    txt = (
        f"📊 <b>Статус сборщика</b>\n"
        f"Сбор: {running}\n"
        f"Последний цикл: {last_s}\n"
        f"Pinnacle: {pinn}\n"
        f"Чат сигналов: <code>{chat}</code>\n"
        f"Чат мониторинга: <code>{stats_chat}</code>\n"
        f"Интервал: {config.POLL_INTERVAL_LIVE}с | окно матчинга: ±{config.MATCH_WINDOW_MIN}м | min КФ: {config.MIN_ODDS}"
    )
    if err:
        txt += f"\n\n⚠️ Посл. ошибка:\n<code>{err[-300:]}</code>"
    return txt


# --- обработка апдейтов ---


def is_admin(uid) -> bool:
    return uid in config.ADMIN_IDS


def handle_callback(cq):
    t0 = time.monotonic()
    cid = cq["id"]
    uid = (cq.get("from") or {}).get("id")
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    msg_id = (cq.get("message") or {}).get("message_id")
    data = cq.get("data") or ""
    if not is_admin(uid):
        tg.answer_callback(cid, "Нет доступа")
        return
    # Гасим спиннер на кнопке сразу — не ждём завершения тяжёлой работы.
    tg.answer_callback(cid)
    log.debug("callback от %s: %s", uid, data)

    # навигация по вкладкам
    if data == "tab:home":
        tg.edit_message_text(chat_id, msg_id, HOME_TEXT, reply_markup=home_kb())
    elif data == "tab:reports":
        tg.edit_message_text(chat_id, msg_id, REPORTS_TEXT, reply_markup=reports_kb())
    elif data == "tab:data":
        tg.edit_message_text(chat_id, msg_id, DATA_TEXT, reply_markup=data_kb())
    elif data == "tab:settings":
        tg.edit_message_text(chat_id, msg_id, settings_text(), reply_markup=settings_kb())
    elif data == "noop":
        pass

    # действия
    elif data == "start":
        ok = start_collector()
        tg.edit_message_text(chat_id, msg_id,
                             ("✅ Сбор запущен.\n\n" if ok else "ℹ️ Сбор уже работает.\n\n") + HOME_TEXT,
                             reply_markup=home_kb())
    elif data == "stop":
        ok = stop_collector()
        tg.edit_message_text(chat_id, msg_id,
                             ("⏹ Сбор остановлен.\n\n" if ok else "ℹ️ Сбор не был запущен.\n\n") + HOME_TEXT,
                             reply_markup=home_kb())
    elif data == "status":
        tg.edit_message_text(chat_id, msg_id, status_text(), reply_markup=home_kb())
    elif data.startswith("rep:"):
        period = data.split(":", 1)[1]
        tg.send_message(chat_id, reports.report_text(period))
    elif data == "excel":
        try:
            path = export_excel.export_signals()
            tg.send_document(chat_id, path, caption="📄 Сигналы")
        except Exception as e:
            log.warning("Ошибка экспорта Excel: %s", e)
            tg.send_message(chat_id, f"⚠️ Ошибка экспорта: {e}")
    elif data == "setchat":
        ok = _set_signal_chat(str(chat_id))
        prefix = "✅ Сигналы будут приходить в этот чат.\n\n" if ok else "⚠️ Не удалось назначить чат.\n\n"
        tg.edit_message_text(chat_id, msg_id, prefix + settings_text(), reply_markup=settings_kb())
    elif data == "setchat:ask":
        _awaiting[uid] = "signal_chat"
        tg.edit_message_text(chat_id, msg_id, SETCHAT_ASK, reply_markup=settings_kb())
    elif data == "setstats":
        ok = _set_stats_chat(str(chat_id))
        prefix = "✅ Мониторинг перерывов будет приходить в этот чат.\n\n" if ok else "⚠️ Не удалось назначить чат.\n\n"
        tg.edit_message_text(chat_id, msg_id, prefix + settings_text(), reply_markup=settings_kb())
    elif data == "setstats:ask":
        _awaiting[uid] = "stats_chat"
        tg.edit_message_text(chat_id, msg_id, SETSTATS_ASK, reply_markup=settings_kb())
    elif data.startswith("log:"):
        level = data.split(":", 1)[1].upper()
        if level in logsetup.LEVELS:
            storage.set_setting("log_level", level)
            logsetup.apply_level(level)  # для самого бота — сразу
            log.info("Уровень логов переключён на %s", level)
        tg.edit_message_text(chat_id, msg_id, settings_text(), reply_markup=settings_kb())
    elif data == "reset:sig":
        storage.reset_signals()
        log.info("Сигналы очищены")
        tg.edit_message_text(chat_id, msg_id, "🧹 Сигналы очищены.\n\n" + DATA_TEXT, reply_markup=data_kb())
    elif data == "reset:all":
        kb = {"inline_keyboard": [[
            {"text": "⚠️ Да, стереть всё", "callback_data": "reset:all:yes"},
            {"text": "Отмена", "callback_data": "tab:data"},
        ]]}
        tg.edit_message_text(chat_id, msg_id,
                             "💣 Стереть ВСЮ базу (матчи, снимки, сигналы, сопоставления)?",
                             reply_markup=kb)
    elif data == "reset:all:yes":
        storage.reset_all()
        log.info("База полностью очищена")
        tg.edit_message_text(chat_id, msg_id, "🗑 База очищена.\n\n" + DATA_TEXT, reply_markup=data_kb())

    # --- «Не сшитые матчи» ---
    elif data == "unmatched":
        tg.edit_message_text(chat_id, msg_id, unmatched_list_text(), reply_markup=unmatched_list_kb())
    elif data.startswith("um:"):
        parts = data.split(":")
        u_id = int(parts[1])
        show_all = len(parts) > 2 and parts[2] == "all"
        text, kb = unmatched_detail(u_id, show_all)
        tg.edit_message_text(chat_id, msg_id, text, reply_markup=kb)
    elif data.startswith("umpick:"):
        _, sid, sidx = data.split(":")
        u = storage.get_unmatched(int(sid))
        if u is None or u["resolved"]:
            tg.edit_message_text(chat_id, msg_id, "Запись уже обработана.", reply_markup=unmatched_list_kb())
        else:
            snap = _snapshot(u)
            idx = int(sidx)
            if 0 <= idx < len(snap):
                cand = snap[idx]
                _write_alias(u["home"], u["away"], cand.get("home"), cand.get("away"))
                storage.mark_unmatched_resolved(int(sid))
                msg = (f"✅ Соответствие записано:\n"
                       f"Pinnacle <b>{u['home']} — {u['away']}</b>\n"
                       f"FlashScore <b>{cand.get('home')} — {cand.get('away')}</b>\n\n"
                       "Сборщик подхватит алиас на следующем перерыве этого матча.")
            else:
                msg = "⚠️ Кандидат не найден в снимке."
            tg.edit_message_text(chat_id, msg_id, msg, reply_markup=unmatched_list_kb())
    elif data.startswith("umskip:"):
        u_id = int(data.split(":")[1])
        storage.mark_unmatched_resolved(u_id)
        tg.edit_message_text(chat_id, msg_id, "🗑 Матч пропущен (помечен обработанным).",
                             reply_markup=unmatched_list_kb())

    log.debug("callback %s обработан за %.0f мс", data, (time.monotonic() - t0) * 1000)


_CHAT_RE = re.compile(r"^(-?\d+|@[A-Za-z0-9_]{3,})$")


def _process_chat_value(chat_id, target: str, kind: str = "signal") -> None:
    """Проверить введённое значение чата и назначить его (с тест-отправкой).

    kind: 'signal' — чат сигналов, 'stats' — чат мониторинга перерывов."""
    if not _CHAT_RE.match(target):
        tg.send_message(chat_id, "⚠️ Не похоже на ID или @username. Пришлите число "
                        "(напр. <code>-1001234567890</code>) или <code>@username</code>, либо /cancel.")
        return
    setter = _set_stats_chat if kind == "stats" else _set_signal_chat
    label = "Чат мониторинга" if kind == "stats" else "Чат сигналов"
    if setter(target):
        tg.send_message(chat_id, f"✅ {label} задан: <code>{target}</code>", reply_markup=home_kb())
    else:
        tg.send_message(chat_id, f"⚠️ Не удалось отправить в <code>{target}</code>.\n"
                        "Добавьте бота в этот чат (в канал — админом с правом писать) и попробуйте снова.")


def handle_message(msg):
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not is_admin(uid):
        return

    # Ожидаем введённый вручную ID чата (сигналы или мониторинг)
    awaiting = _awaiting.get(uid)
    if awaiting in ("signal_chat", "stats_chat"):
        if text.lower() in ("/cancel", "отмена"):
            _awaiting.pop(uid, None)
            tg.send_message(chat_id, "Отменено.", reply_markup=home_kb())
            return
        _awaiting.pop(uid, None)
        _process_chat_value(chat_id, text, kind="stats" if awaiting == "stats_chat" else "signal")
        return

    if text in ("/start", "/menu", "меню"):
        tg.send_message(chat_id, HOME_TEXT, reply_markup=home_kb())
    elif text == "/status":
        tg.send_message(chat_id, status_text(), reply_markup=home_kb())
    elif text.startswith("/setstatschat"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            _process_chat_value(chat_id, parts[1].strip(), kind="stats")
        else:
            _awaiting[uid] = "stats_chat"
            tg.send_message(chat_id, SETSTATS_ASK)
    elif text.startswith("/setchat"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            _process_chat_value(chat_id, parts[1].strip())
        else:
            _awaiting[uid] = "signal_chat"
            tg.send_message(chat_id, SETCHAT_ASK)


def _dispatch(upd):
    try:
        if "callback_query" in upd:
            handle_callback(upd["callback_query"])
        elif "message" in upd:
            handle_message(upd["message"])
    except Exception:
        log.exception("Ошибка обработки апдейта")


# --- авто-отчёты ---


def _scheduler():
    sent_marks: set[str] = set()
    while True:
        now = datetime.now(MSK)
        target = _signal_chat()
        if target and now.hour == 9 and now.minute == 0:
            if now.weekday() == 0:  # понедельник — недельный
                mark = f"week-{now.isocalendar()[1]}"
                if mark not in sent_marks:
                    tg.send_message(target, reports.report_text("week"))
                    sent_marks.add(mark)
            if now.day == 1:        # 1-е число — месячный
                mark = f"month-{now.year}-{now.month}"
                if mark not in sent_marks:
                    tg.send_message(target, reports.report_text("month"))
                    sent_marks.add(mark)
        time.sleep(30)


def _signal_chat():
    return storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or None)


# --- главный цикл ---


def main():
    if not config.TG_BOT_TOKEN:
        print("TG_BOT_TOKEN не задан в .env")
        sys.exit(1)
    storage.init_db()
    logsetup.apply_level(storage.get_setting("log_level", logsetup.DEFAULT_LEVEL))
    log.info("Бот запущен (уровень логов %s)",
             (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper())
    threading.Thread(target=_scheduler, daemon=True).start()
    for admin in config.ADMIN_IDS:
        tg.send_message(admin, "🤖 <b>Бот запущен.</b>\n\n" + HOME_TEXT, reply_markup=home_kb())

    offset = None
    while True:
        try:
            updates = tg.get_updates(offset, timeout=25)
        except Exception:
            log.exception("Ошибка getUpdates")
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            _pool.submit(_dispatch, upd)


if __name__ == "__main__":
    main()
