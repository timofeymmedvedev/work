"""Общий интерфейс источника цен."""

import random
import time


class SourceError(Exception):
    """Площадка не отдала выдачу: сеть, капча, изменившаяся вёрстка."""


class Source:
    name = "base"
    delay = 1.0

    def search(self, query):
        """Возвращает список {title, price, url} в порядке выдачи площадки."""
        raise NotImplementedError

    def close(self):
        pass

    def sleep(self):
        """Пауза между запросами с джиттером.

        Ровный интервал сам по себе выглядит как бот, поэтому базовая задержка
        размывается на четверть в обе стороны.
        """
        time.sleep(self.delay * random.uniform(0.75, 1.25))


def parse_price(value):
    """Приводит цену из любого вида ('12 345 ₽', 1234500, '12345.00') к рублям."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = "".join(c for c in str(value) if c.isdigit() or c == ".")
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None
