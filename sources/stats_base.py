"""Единый интерфейс стат-провайдера. Реализации: SofaScore (основной), FlashScore (запасной)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import LiveEvent, MatchStats

# Статусы, которые считаем перерывом
HT_STATUS = "HT"


class StatsProvider(ABC):
    name: str = "base"

    @abstractmethod
    def list_live(self) -> list[LiveEvent]:
        """Лёгкий список идущих матчей (для драйвера, без тяжёлой статистики)."""
        raise NotImplementedError

    @abstractmethod
    def get_stats(self, event: LiveEvent) -> Optional[MatchStats]:
        """Снять статистику 1-го тайма (голы/красные/угловые по командам). None при неудаче."""
        raise NotImplementedError
