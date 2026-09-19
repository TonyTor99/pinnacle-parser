"""Конфигурация проекта. Значения читаются из .env (python-dotenv)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "") or ""
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return out


# --- Telegram ---
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
ADMIN_IDS = _ids("ADMIN_IDS")
SIGNAL_CHAT_ID = os.getenv("SIGNAL_CHAT_ID", "").strip()  # хранится в БД/настройках, .env — начальное

# --- Pinnacle ---
PINNACLE_API_KEY = os.getenv("PINNACLE_API_KEY", "").strip()
PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"
SOCCER_SPORT_ID = 29  # id футбола в arcadia API

# --- Параметры сбора ---
POLL_INTERVAL_LIVE = _int("POLL_INTERVAL_LIVE", 30)
MATCH_WINDOW_MIN = _int("MATCH_WINDOW_MIN", 20)
MIN_ODDS = _float("MIN_ODDS", 1.65)

# --- Пути ---
DB_PATH = str(BASE_DIR / "pinnacle_corners.db")
ALIASES_PATH = str(BASE_DIR / "aliases.json")
EXPORTS_DIR = BASE_DIR / "exports"
EXPORTS_DIR.mkdir(exist_ok=True)

# Общий User-Agent (можно ротировать в источниках)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
