"""Ozon: выдача забирается через внутренний composer-api в браузере.

Прямой HTTP-запрос к Ozon упирается в антибот-защиту, а вёрстка карточек
обфусцирована и меняется. Поэтому страница открывается настоящим Chromium,
но запрашивается не HTML, а JSON того же composer-api, которым пользуется
сам сайт: его структура переживает редизайны заметно дольше, чем классы.
"""

import json
from urllib.parse import quote

from .base import Source, SourceError, parse_price

API_URL = (
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/%3Ftext%3D{q}"
)
HTML_URL = "https://www.ozon.ru/search/?text={q}"


class OzonSource(Source):
    name = "ozon"

    def __init__(self, browser, delay=2.5, timeout=45000):
        self.browser = browser
        self.delay = delay
        self.timeout = timeout

    def search(self, query):
        page = self.browser.new_page()
        try:
            page.goto(API_URL.format(q=quote(query)), timeout=self.timeout)
            body = page.inner_text("body")
            if _looks_like_challenge(body):
                raise SourceError("ozon: антибот-проверка вместо выдачи")
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise SourceError("ozon: ответ не является JSON") from exc
            return extract_offers(payload)
        finally:
            page.close()


def _looks_like_challenge(text):
    lowered = (text or "")[:2000].lower()
    markers = ("доступ ограничен", "проверка", "captcha", "challenge", "robot")
    return any(m in lowered for m in markers) and "widgetstates" not in lowered


def extract_offers(payload):
    """Достаёт карточки товаров из ответа composer-api.

    Полезная нагрузка спрятана в widgetStates: словарь, где ключ — имя виджета
    с меняющимся хешем, а значение — JSON-строка. Поэтому вместо обращения по
    конкретным именам обходим дерево и собираем всё, что выглядит как товар:
    узел со ссылкой на /product/ и ценой внутри.
    """
    states = payload.get("widgetStates") or {}
    offers = []
    seen = set()

    for raw in states.values():
        if not isinstance(raw, str):
            continue
        try:
            state = json.loads(raw)
        except ValueError:
            continue
        for item in _walk_products(state):
            key = item["url"] or item["title"]
            if key and key not in seen:
                seen.add(key)
                offers.append(item)
    return offers


def _walk_products(node):
    """Рекурсивно ищет узлы-товары в произвольном поддереве виджета."""
    if isinstance(node, dict):
        link = _product_link(node)
        if link:
            title = _find_title(node)
            price = _find_price(node)
            if title and price:
                yield {"title": title, "price": price, "url": link}
                return
        for value in node.values():
            yield from _walk_products(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_products(value)


def _product_link(node):
    action = node.get("action")
    if isinstance(action, dict):
        link = action.get("link") or ""
        if "/product/" in link:
            return _absolute(link)
    link = node.get("link") or ""
    if isinstance(link, str) and "/product/" in link:
        return _absolute(link)
    return None


def _absolute(link):
    link = link.split("?")[0]
    return link if link.startswith("http") else f"https://www.ozon.ru{link}"


def _find_title(node):
    """Название товара — самый длинный текстовый атом в карточке.

    Ozon кладёт в карточку и название, и служебные подписи ("Осталось мало",
    "Доставка завтра"); название из них — самое длинное.
    """
    texts = []
    _collect_texts(node, texts)
    if not texts:
        return None
    return max(texts, key=len)


def _collect_texts(node, out, depth=0):
    if depth > 12:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("text", "title") and isinstance(value, str):
                cleaned = value.strip()
                # Отсекаем подписи и ценники: они короткие либо состоят
                # почти целиком из цифр.
                if len(cleaned) >= 12 and "₽" not in cleaned:
                    out.append(cleaned)
            else:
                _collect_texts(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect_texts(value, out, depth + 1)


def _find_price(node):
    """Берёт цену со стилем PRICE — это финальная цена, а не зачёркнутая."""
    prices = []
    _collect_prices(node, prices)
    if not prices:
        return None
    styled = [p for style, p in prices if style == "PRICE"]
    if styled:
        return min(styled)
    return min(p for _, p in prices)


def _collect_prices(node, out, depth=0):
    if depth > 12:
        return
    if isinstance(node, dict):
        if "price" in node and isinstance(node["price"], list):
            for entry in node["price"]:
                if isinstance(entry, dict) and "text" in entry:
                    value = parse_price(entry["text"])
                    if value:
                        out.append((entry.get("textStyle"), value))
        for value in node.values():
            _collect_prices(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect_prices(value, out, depth + 1)
