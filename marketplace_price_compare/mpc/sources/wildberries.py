"""Wildberries: цены берутся из открытого поискового JSON-API.

Браузер здесь не нужен — search.wb.ru отдаёт ту же выдачу, что и сайт,
обычным GET-запросом, поэтому WB собирается на порядок быстрее остальных.
"""

import requests

from .base import Source, SourceError

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v13/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


class WildberriesSource(Source):
    name = "wb"

    def __init__(self, dest=-1257786, delay=0.4, timeout=25):
        self.dest = dest
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def search(self, query):
        params = {
            "ab_testing": "false",
            "appType": 1,
            "curr": "rub",
            "dest": self.dest,
            "query": query,
            "resultset": "catalog",
            "sort": "popular",
            "spp": 30,
            "suppressSpellcheck": "false",
        }
        try:
            resp = self.session.get(SEARCH_URL, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SourceError(f"wb: сеть: {exc}") from exc

        if resp.status_code != 200:
            raise SourceError(f"wb: HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError("wb: ответ не является JSON") from exc

        products = (payload.get("data") or {}).get("products") or []
        return [self._offer(p) for p in products if self._offer(p)]

    @staticmethod
    def _offer(product):
        price = _extract_price(product)
        if not price:
            return None
        pid = product.get("id")
        brand = (product.get("brand") or "").strip()
        name = (product.get("name") or "").strip()
        # В выдаче WB бренд вынесен в отдельное поле и в name не дублируется,
        # поэтому для сопоставления их нужно склеить.
        title = f"{brand} {name}".strip()
        return {
            "title": title,
            "price": price,
            "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx" if pid else "",
        }


def _extract_price(product):
    """Достаёт итоговую цену в рублях.

    WB несколько раз менял форму ответа: в старых версиях цена лежала в
    salePriceU копейками на верхнем уровне, в v13 — внутри sizes[].price.
    Поддерживаем обе, иначе обновление API молча обнулит весь столбец.
    """
    sizes = product.get("sizes") or []
    for size in sizes:
        price = size.get("price") or {}
        for key in ("total", "product"):
            value = price.get(key)
            if value:
                return round(value / 100, 2)

    for key in ("salePriceU", "priceU"):
        value = product.get(key)
        if value:
            return round(value / 100, 2)
    return None
