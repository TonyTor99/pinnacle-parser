#!/usr/bin/env bash
# Деплой/обновление на сервере: git pull -> зависимости -> рестарт бота.
# Бот сам перезапустит сборщик (collector.py) по кнопке после рестарта.
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "[deploy] git pull"
git pull --ff-only

if [ ! -d venv ]; then
  echo "[deploy] create venv"
  python3 -m venv venv
fi

echo "[deploy] pip install"
./venv/bin/pip install -q -r requirements.txt

echo "[deploy] init db"
./venv/bin/python storage.py

echo "[deploy] restart service"
sudo systemctl restart pinnacle-corners.service
sudo systemctl --no-pager status pinnacle-corners.service | head -n 5

echo "[deploy] done"
