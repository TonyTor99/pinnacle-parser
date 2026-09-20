"""Тонкий клиент Telegram Bot API поверх requests (без тяжёлых async-фреймворков).

Используется и ботом (bot.py), и сборщиком (collector.py) для отправки сигналов.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import requests

import config

_API = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}"

log = logging.getLogger("tg")


def _call(method: str, params: dict, timeout: int = 30, quiet: bool = False) -> Optional[dict]:
    t0 = time.monotonic()
    try:
        r = requests.post(f"{_API}/{method}", json=params, timeout=timeout)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        ms = (time.monotonic() - t0) * 1000
        log.warning("TG %s: сетевая ошибка за %.0f мс: %s", method, ms, e)
        return None
    ms = (time.monotonic() - t0) * 1000
    if not data.get("ok"):
        desc = data.get("description", "")
        # «message is not modified» — безобидно (повторный edit тем же текстом).
        lvl = logging.DEBUG if "not modified" in desc else logging.WARNING
        log.log(lvl, "TG %s: ответ not ok за %.0f мс: %s", method, ms, desc)
        return None
    if not quiet:
        log.debug("TG %s: ok за %.0f мс", method, ms)
    return data.get("result")


def send_message(chat_id, text: str, reply_markup: Optional[dict] = None,
                 parse_mode: str = "HTML", disable_preview: bool = True) -> Optional[dict]:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    return _call("sendMessage", params)


def edit_message_text(chat_id, message_id: int, text: str,
                      reply_markup: Optional[dict] = None, parse_mode: str = "HTML") -> Optional[dict]:
    params: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        params["reply_markup"] = json.dumps(reply_markup)
    return _call("editMessageText", params)


def answer_callback(callback_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def get_updates(offset: Optional[int], timeout: int = 25) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    res = _call("getUpdates", params, timeout=timeout + 10, quiet=True)
    if res:
        log.debug("getUpdates: получено %d апдейт(ов)", len(res))
    return res or []


def send_document(chat_id, file_path: str, caption: str = "") -> Optional[dict]:
    try:
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{_API}/sendDocument",
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
                timeout=120,
            )
        data = r.json()
    except (requests.RequestException, ValueError, OSError):
        return None
    return data.get("result") if data.get("ok") else None
