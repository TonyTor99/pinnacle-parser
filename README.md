# pinnacle-corners-parser

Сигнальный бот по стратегиям **ugl pinka** (угловые): ловит перерыв футбольного матча,
проверяет условия 1-го тайма (голы / красные / угловые) и линии угловых Pinnacle,
шлёт сигнал в Telegram. Полностью на бесплатных источниках.

## Стек данных (бесплатно)

- **Линии** — гостевой arcadia API Pinnacle (без аккаунта): прематч 1X2 + live-угловые
  (индивидуальные тоталы, форы, тоталы). Угловые лежат как special-матчапы.
- **Статистика** — SofaScore (основной) + FlashScore (запасной): голы, красные, угловые 1-го тайма.

## Стратегии

| Код  | Прематч | Условия на перерыве (home/away) | Ставка |
|------|---------|----------------------------------|--------|
| itb2 | П2 1.01–7.00 | home голы=0,кр=0; away кр=1,голы≤1 | ИТБ2 угл Over, ровная линия, КФ≥1.65 |
| f2   | —       | away кр=1 и >home; сумма голов≤1 | ФОРА2 угл −1 |
| f1   | П1 1.01–4.00 | home кр=1,угл≤4,голы≤away; away голы≤2,кр=0 | ФОРА1 угл −0.5 |

## Компоненты

- `config.py` — настройки из `.env`.
- `sources/pinnacle.py` — линии Pinnacle (matchups + related/straight, угловые).
- `sources/stats_sofascore.py` / `stats_flashscore.py` — статистика.
- `matcher.py` — сшивка матчей провайдер↔Pinnacle (нормализация имён + окно времени, `aliases.json`).
- `signals.py` — три стратегии.
- `collector.py` — цикл: ловит HT, снимает статистику+линии, шлёт сигналы. Отдельный процесс.
- `bot.py` — кнопочный TG-бот управления (старт/стоп/статус/отчёты/Excel/сброс) + авто-отчёты.
- `reports.py`, `export_excel.py`, `storage.py`.

## Установка

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # заполнить TG_BOT_TOKEN, ADMIN_IDS, PINNACLE_API_KEY
python storage.py         # инициализировать БД
```

`PINNACLE_API_KEY` — публичный `x-api-key` из JS сайта pinnacle.com (вкладка Network → любой
запрос к `guest.api.arcadia.pinnacle.com`, заголовок `x-api-key`). При 401 обновить.

## Проверка источников (standalone)

```bash
python -m sources.pinnacle           # список матчей + угловые одного матча
python -m sources.stats_sofascore    # live-список + статистика матча на HT
python -m sources.stats_flashscore   # запасной источник (требует сверки, см. docstring)
```

## Запуск

```bash
python bot.py            # бот; сбор стартует кнопкой «▶️ Старт сбора»
```

Бот сам управляет процессом `collector.py` (запуск/остановка кнопками). Логи сбора — `collector.log`.

## Деплой (systemd)

См. `pinnacle-corners.service` и `deploy.sh`. Обновление: `git push` → `bash deploy.sh` на сервере.
