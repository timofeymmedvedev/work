"""Wildberries: цены берутся из открытого поискового JSON-API.

Обычный HTTP-запрос здесь на порядок быстрее браузера, поэтому он идёт
первым. Но WB отвечает не всем одинаково: библиотечный клиент отличается от
браузера TLS-отпечатком, и запрос обрывается на рукопожатии
(UNEXPECTED_EOF_WHILE_READING) либо возвращает страницу вместо JSON. В этом
случае тот же адрес открывается в Chromium — для сайта это обычная вкладка.
"""

import json

import requests

from .base import Source, SourceError

# WB несколько раз менял версию поискового эндпоинта, и старые перестают
# отвечать без предупреждения. Кандидаты пробуются по очереди, рабочий
# запоминается на весь прогон.
SEARCH_URLS = (
    "https://search.wb.ru/exactmatch/ru/common/v13/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


class WildberriesSource(Source):
    name = "wb"

    def __init__(self, dest=-1257786, delay=0.4, timeout=25, browser=None,
                 page_timeout=45000):
        self.dest = dest
        self.delay = delay
        self.timeout = timeout          # секунды, для обычного запроса
        self.page_timeout = page_timeout  # миллисекунды, для вкладки браузера
        self.browser = browser
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._endpoint = None
        self._http_broken = False

    def search(self, query):
        errors = []

        # Быстрый путь: обычный запрос. Отключается насовсем после того,
        # как выяснилось, что WB его не принимает, — иначе каждый товар
        # платил бы за бесполезную попытку и таймаут.
        if not self._http_broken:
            offers, error = self._search_http(query)
            if error is None:
                return offers
            errors.append(f"http: {error}")
            self._http_broken = True

        if self.browser is not None:
            offers, error = self._search_browser(query)
            if error is None:
                return offers
            errors.append(f"браузер: {error}")

        raise SourceError("wb: " + "; ".join(errors))

    def _params(self, query):
        return {
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

    def _search_http(self, query):
        endpoints = (self._endpoint,) if self._endpoint else SEARCH_URLS
        last = None
        for url in endpoints:
            try:
                resp = self.session.get(
                    url, params=self._params(query), timeout=self.timeout
                )
            except requests.RequestException as exc:
                last = _short(exc)
                continue

            if resp.status_code != 200:
                last = f"HTTP {resp.status_code}"
                continue
            try:
                payload = resp.json()
            except ValueError:
                last = "ответ не является JSON"
                continue

            self._endpoint = url
            return parse_payload(payload), None
        return [], last

    def _search_browser(self, query):
        """Тот же запрос вкладкой Chromium: проходит там, где клиент не проходит."""
        from urllib.parse import urlencode

        endpoints = (self._endpoint,) if self._endpoint else SEARCH_URLS
        last = None
        page = self.browser.new_page()
        try:
            for url in endpoints:
                full = f"{url}?{urlencode(self._params(query))}"
                try:
                    page.goto(full, timeout=self.page_timeout)
                    body = page.inner_text("body")
                    payload = json.loads(body)
                except ValueError:
                    last = "ответ не является JSON"
                    continue
                except Exception as exc:
                    last = _short(exc)
                    continue

                self._endpoint = url
                return parse_payload(payload), None
            return [], last
        finally:
            page.close()


def _short(exc):
    text = str(exc)
    return f"{type(exc).__name__}: {text[:110]}"


def parse_payload(payload):
    """Превращает ответ WB в список позиций выдачи."""
    products = (payload.get("data") or {}).get("products") or []
    offers = []
    for product in products:
        price = _extract_price(product)
        if not price:
            continue
        pid = product.get("id")
        brand = (product.get("brand") or "").strip()
        name = (product.get("name") or "").strip()
        # В выдаче WB бренд вынесен в отдельное поле и в name не дублируется,
        # поэтому для сопоставления их нужно склеить.
        offers.append({
            "title": f"{brand} {name}".strip(),
            "price": price,
            "url": (
                f"https://www.wildberries.ru/catalog/{pid}/detail.aspx"
                if pid else ""
            ),
        })
    return offers


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
