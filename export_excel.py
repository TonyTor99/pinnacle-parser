"""Выгрузка сигналов в Excel (openpyxl). Файл кладётся в exports/."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.styles import Font

import config
import storage

MSK = timezone(timedelta(hours=3))
HEADERS = ["Дата (МСК)", "Стратегия", "Лига", "Хозяева", "Гости", "Рынок", "Линия", "КФ", "Отпр.", "Итог"]


def _msk(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=MSK).strftime("%Y-%m-%d %H:%M")


def _won_str(w) -> str:
    return {None: "—", 1: "зашёл", 0: "не зашёл"}.get(w, "—")


def export_signals() -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "signals"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in storage.get_all_signals():
        ws.append([
            _msk(r["ts"]), r["strategy"], r["league"], r["home"], r["away"],
            r["market"], r["line"], r["price"], "да" if r["sent"] else "нет", _won_str(r["won"]),
        ])
    widths = [17, 10, 22, 20, 20, 22, 8, 7, 7, 11]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i)].width = w

    fname = f"signals_{datetime.now(MSK).strftime('%Y%m%d_%H%M')}.xlsx"
    path = str(config.EXPORTS_DIR / fname)
    wb.save(path)
    return path


if __name__ == "__main__":
    storage.init_db()
    print(export_signals())
