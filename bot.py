"""Кнопочный TG-бот управления сборщиком угловых.

Меню разбито на вкладки (Главная → Отчёты / Данные / Настройки), навигация —
через редактирование одного сообщения. Обработка апдейтов вынесена в пул потоков,
чтобы медленные вызовы Telegram API не «глушили» опрос getUpdates.

Доступ только для ADMIN_IDS. Long-polling на requests (без async-фреймворков).
"""
from __future__ import annotations

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
    return {"inline_keyboard": [
        [{"text": "📥 Экспорт в Excel", "callback_data": "excel"}],
        [{"text": "🧹 Очистить сигналы", "callback_data": "reset:sig"}],
        [{"text": "💣 Стереть всю базу", "callback_data": "reset:all"}],
        [{"text": "« Назад", "callback_data": "tab:home"}],
    ]}


def settings_kb() -> dict:
    cur = (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper()
    row = []
    kb = [[{"text": "💬 Сигналы в этот чат", "callback_data": "setchat"},
           {"text": "✏️ Ввести ID чата", "callback_data": "setchat:ask"}],
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


def _set_signal_chat(target: str) -> bool:
    """Назначить чат сигналов: сперва пробуем отправить туда тест — сохраняем только при успехе."""
    ok = bool(tg.send_message(target, "✅ Этот чат назначен для сигналов угловых Pinnacle."))
    if ok:
        storage.set_setting("signal_chat_id", target)
        log.info("Чат сигналов задан: %s", target)
    return ok


def settings_text() -> str:
    chat = storage.get_setting("signal_chat_id", config.SIGNAL_CHAT_ID or "не задан")
    cur = (storage.get_setting("log_level", logsetup.DEFAULT_LEVEL) or "").upper()
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"💬 Чат сигналов: <code>{chat}</code>\n"
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
    err = storage.get_setting("last_error")
    txt = (
        f"📊 <b>Статус сборщика</b>\n"
        f"Сбор: {running}\n"
        f"Последний цикл: {last_s}\n"
        f"Pinnacle: {pinn}\n"
        f"Чат сигналов: <code>{chat}</code>\n"
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

    log.debug("callback %s обработан за %.0f мс", data, (time.monotonic() - t0) * 1000)


_CHAT_RE = re.compile(r"^(-?\d+|@[A-Za-z0-9_]{3,})$")


def _process_chat_value(chat_id, target: str) -> None:
    """Проверить введённое значение чата и назначить его (с тест-отправкой)."""
    if not _CHAT_RE.match(target):
        tg.send_message(chat_id, "⚠️ Не похоже на ID или @username. Пришлите число "
                        "(напр. <code>-1001234567890</code>) или <code>@username</code>, либо /cancel.")
        return
    if _set_signal_chat(target):
        tg.send_message(chat_id, f"✅ Чат сигналов задан: <code>{target}</code>", reply_markup=home_kb())
    else:
        tg.send_message(chat_id, f"⚠️ Не удалось отправить в <code>{target}</code>.\n"
                        "Добавьте бота в этот чат (в канал — админом с правом писать) и попробуйте снова.")


def handle_message(msg):
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not is_admin(uid):
        return

    # Ожидаем введённый вручную ID чата сигналов
    if _awaiting.get(uid) == "signal_chat":
        if text.lower() in ("/cancel", "отмена"):
            _awaiting.pop(uid, None)
            tg.send_message(chat_id, "Отменено.", reply_markup=home_kb())
            return
        _awaiting.pop(uid, None)
        _process_chat_value(chat_id, text)
        return

    if text in ("/start", "/menu", "меню"):
        tg.send_message(chat_id, HOME_TEXT, reply_markup=home_kb())
    elif text == "/status":
        tg.send_message(chat_id, status_text(), reply_markup=home_kb())
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
