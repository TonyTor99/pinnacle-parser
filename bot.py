"""Кнопочный TG-бот управления сборщиком угловых.

Функции: старт/стоп сбора (отдельный процесс collector.py, pkill+start), статус,
отчёты день/неделя/месяц/всё, Excel-экспорт, задать чат сигналов, сброс БД.
Плюс фоновый планировщик авто-отчётов (Пн 09:00 и 1-е число 09:00 МСК).

Доступ только для ADMIN_IDS. Long-polling на requests (без async-фреймворков).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import export_excel
import reports
import storage
import tg

BASE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(BASE, "collector.py")
LOG_PATH = os.path.join(BASE, "collector.log")
MSK = timezone(timedelta(hours=3))


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
    return True


def stop_collector() -> bool:
    if not collector_running():
        return False
    subprocess.run(["pkill", "-9", "-f", "collector.py"])
    return True


# --- UI ---


def main_kb() -> dict:
    running = collector_running()
    toggle = ("⏹ Стоп сбора", "stop") if running else ("▶️ Старт сбора", "start")
    return {"inline_keyboard": [
        [{"text": toggle[0], "callback_data": toggle[1]}, {"text": "📊 Статус", "callback_data": "status"}],
        [{"text": "📈 День", "callback_data": "rep:day"}, {"text": "Неделя", "callback_data": "rep:week"},
         {"text": "Месяц", "callback_data": "rep:month"}, {"text": "Всё", "callback_data": "rep:all"}],
        [{"text": "📥 Excel", "callback_data": "excel"}, {"text": "💬 Чат сигналов сюда", "callback_data": "setchat"}],
        [{"text": "🗑 Сброс сигналов", "callback_data": "reset:sig"},
         {"text": "🗑 Сброс всей БД", "callback_data": "reset:all"}],
    ]}


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
    uid = (cq.get("from") or {}).get("id")
    cid = cq["id"]
    chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
    msg_id = (cq.get("message") or {}).get("message_id")
    data = cq.get("data") or ""
    if not is_admin(uid):
        tg.answer_callback(cid, "Нет доступа")
        return

    if data == "start":
        ok = start_collector()
        tg.answer_callback(cid, "Запущен" if ok else "Уже работает")
    elif data == "stop":
        ok = stop_collector()
        tg.answer_callback(cid, "Остановлен" if ok else "Не был запущен")
    elif data == "status":
        tg.answer_callback(cid)
        tg.edit_message_text(chat_id, msg_id, status_text(), reply_markup=main_kb())
        return
    elif data.startswith("rep:"):
        period = data.split(":", 1)[1]
        tg.answer_callback(cid)
        tg.send_message(chat_id, reports.report_text(period))
        return
    elif data == "excel":
        tg.answer_callback(cid, "Готовлю файл…")
        try:
            path = export_excel.export_signals()
            tg.send_document(chat_id, path, caption="Сигналы")
        except Exception as e:
            tg.send_message(chat_id, f"Ошибка экспорта: {e}")
        return
    elif data == "setchat":
        storage.set_setting("signal_chat_id", str(chat_id))
        tg.answer_callback(cid, "Чат сигналов задан")
    elif data == "reset:sig":
        storage.reset_signals()
        tg.answer_callback(cid, "Сигналы очищены")
    elif data == "reset:all":
        tg.answer_callback(cid)
        kb = {"inline_keyboard": [[
            {"text": "⚠️ Да, стереть всё", "callback_data": "reset:all:yes"},
            {"text": "Отмена", "callback_data": "status"},
        ]]}
        tg.edit_message_text(chat_id, msg_id, "Стереть ВСЮ базу (матчи, снимки, сигналы, сопоставления)?", reply_markup=kb)
        return
    elif data == "reset:all:yes":
        storage.reset_all()
        tg.answer_callback(cid, "База очищена")
    else:
        tg.answer_callback(cid)

    tg.edit_message_text(chat_id, msg_id, "Меню управления:", reply_markup=main_kb())


def handle_message(msg):
    uid = (msg.get("from") or {}).get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    if not is_admin(uid):
        return
    if text in ("/start", "/menu", "меню"):
        tg.send_message(chat_id, "Меню управления сборщиком угловых Pinnacle:", reply_markup=main_kb())
    elif text == "/status":
        tg.send_message(chat_id, status_text(), reply_markup=main_kb())


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
    threading.Thread(target=_scheduler, daemon=True).start()
    for admin in config.ADMIN_IDS:
        tg.send_message(admin, "🤖 Бот запущен.", reply_markup=main_kb())

    offset = None
    while True:
        try:
            updates = tg.get_updates(offset, timeout=25)
        except Exception:
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd:
                    handle_message(upd["message"])
            except Exception as e:
                print("handler error:", e)


if __name__ == "__main__":
    main()
