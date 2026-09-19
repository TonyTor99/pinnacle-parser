"""Отчёты по сигналам: статистика и ROI за день/неделю/месяц/всё время.

Ставка — флэт 1 юнит. Прибыль по зашедшему сигналу = (КФ-1), по незашедшему = -1.
Незавершённые (won IS NULL) в ROI не учитываются, показываются отдельно.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import storage

MSK = timezone(timedelta(hours=3))
STRAT_NAMES = {"itb2": "ITB2", "f2": "F2", "f1": "F1"}


def _period_start_ts(period: str) -> int:
    now = datetime.now(MSK)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # all
        return 0
    return int(start.astimezone(timezone.utc).timestamp())


def _agg(rows):
    won = lost = pending = 0
    profit = 0.0
    for r in rows:
        w = r["won"]
        price = r["price"] or 0.0
        if w is None:
            pending += 1
        elif w == 1:
            won += 1
            profit += (price - 1.0)
        else:
            lost += 1
            profit -= 1.0
    settled = won + lost
    roi = (profit / settled * 100.0) if settled else 0.0
    winrate = (won / settled * 100.0) if settled else 0.0
    return won, lost, pending, profit, roi, winrate


def report_text(period: str = "day") -> str:
    ts_from = _period_start_ts(period)
    rows = storage.get_signals_since(ts_from) if ts_from else storage.get_all_signals()
    titles = {"day": "День", "week": "Неделя", "month": "Месяц", "all": "Всё время"}
    head = f"📈 <b>Отчёт: {titles.get(period, period)}</b>\n"
    if not rows:
        return head + "Сигналов нет."

    lines = [head]
    # по стратегиям
    for strat in ("itb2", "f2", "f1"):
        srows = [r for r in rows if r["strategy"] == strat]
        if not srows:
            continue
        won, lost, pending, profit, roi, winrate = _agg(srows)
        sign = "✅" if profit >= 0 else "✖️"
        lines.append(
            f"\n<b>{STRAT_NAMES[strat]}</b>: {len(srows)} сигн. | ♻️{pending} ✅{won} ✖️{lost}\n"
            f"  winrate {winrate:.0f}% | профит {profit:+.2f}u | ROI {roi:+.1f}% {sign}"
        )
    # итог
    won, lost, pending, profit, roi, winrate = _agg(rows)
    sign = "✅" if profit >= 0 else "✖️"
    lines.append(
        f"\n━━━━━━━━━━━━\n<b>Итог</b>: {len(rows)} сигн. | ♻️{pending} ✅{won} ✖️{lost}\n"
        f"winrate {winrate:.0f}% | профит {profit:+.2f}u | ROI {roi:+.1f}% {sign}"
    )
    return "\n".join(lines)
