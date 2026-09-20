"""Единая настройка логирования для бота и сборщика.

Уровень выбирается из TG-бота и хранится в settings (ключ ``log_level``), поэтому
и bot.py, и collector.py читают его из одного места. Введён кастомный уровень
LIVE (между DEBUG и INFO) — на нём сборщик печатает список доступных матчей.

Иерархия (что видно на каждом уровне, от тихого к подробному):
    ERROR  → только предупреждения и ошибки (WARNING+)
    INFO   → сводки циклов, сигналы, действия кнопок
    LIVE   → то же + построчный список live/HT-матчей и их сшивки с Pinnacle
    DEBUG  → то же + тайминги вызовов Telegram API и обработчиков кнопок
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

# Кастомный уровень: подробнее INFO, но тише DEBUG.
LIVE = 15
logging.addLevelName(LIVE, "LIVE")

# Человекочитаемое имя (то, что хранится в settings и на кнопках) -> числовой уровень.
LEVELS: dict[str, int] = {
    "ERROR": logging.WARNING,
    "INFO": logging.INFO,
    "LIVE": LIVE,
    "DEBUG": logging.DEBUG,
}
DEFAULT_LEVEL = "LIVE"

# Подписи для кнопок в боте.
LEVEL_LABELS: dict[str, str] = {
    "ERROR": "🔴 Ошибки",
    "INFO": "🟡 Обычный",
    "LIVE": "🟢 Live-матчи",
    "DEBUG": "🔵 Отладка",
}


def level_value(name: str | None) -> int:
    return LEVELS.get((name or "").upper(), LEVELS[DEFAULT_LEVEL])


def setup(component: str, to_file: str | None = None, level_name: str | None = None) -> logging.Logger:
    """Однократно настроить корневой логгер (stdout + опционально файл) и вернуть логгер компонента."""
    root = logging.getLogger()
    if not root.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
        if to_file:
            fh = RotatingFileHandler(to_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        # Гасим болтливые сторонние логгеры (иначе DEBUG тонет в urllib3).
        for noisy in ("urllib3", "requests", "charset_normalizer"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    apply_level(level_name)
    return logging.getLogger(component)


def apply_level(level_name: str | None) -> None:
    """Сменить уровень корневого логгера на лету (по нажатию кнопки / раз в цикл сбора)."""
    logging.getLogger().setLevel(level_value(level_name))


def live(logger: logging.Logger, msg: str, *args) -> None:
    """Хелпер для записи на уровне LIVE."""
    logger.log(LIVE, msg, *args)
