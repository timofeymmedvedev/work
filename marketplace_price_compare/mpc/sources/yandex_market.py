"""Яндекс.Маркет: выдача снимается с DOM страницы поиска в браузере.

Публичного поискового API у Маркета нет, а разметка карточек размечена
атрибутами data-auto — они задуманы для собственных автотестов Яндекса и
поэтому меняются реже, чем классы. На случай их изменения предусмотрен
запасной разбор по ссылкам на товар.
"""

from urllib.parse import quote

from .base import Source, SourceError, parse_price

SEARCH_URL = "https://market.yandex.ru/search?text={q}"

# Порядок важен: сначала пробуем разметку автотестов, затем более общие
# структурные селекторы.
CARD_SELECTORS = (
    "[data-auto='searchOrganic']",
    "[data-zone-name='snippetList'] article",
    "article[data-autotest-id]",
)
TITLE_SELECTORS = (
    "[data-auto='snippet-title']",
    "[data-zone-name='title']",
    "h3",
)
PRICE_SELECTORS = (
    "[data-auto='snippet-price-current']",
    "[data-auto='price-value']",
    "[data-zone-name='price']",
)


class YandexMarketSource(Source):
    name = "yandex"

    def __init__(self, browser, delay=3.0, timeout=45000):
        self.browser = browser
        self.delay = delay
        self.timeout = timeout

    def search(self, query):
        page = self.browser.new_page()
        try:
            page.goto(SEARCH_URL.format(q=quote(query)), timeout=self.timeout)
            # Выдача догружается скриптом, поэтому ждём появления карточек,
            # а не события load: без этого со страницы снимается пустой каркас.
            _wait_for_cards(page, self.timeout)

            if _is_captcha(page):
                raise SourceError("yandex: капча вместо выдачи")

            offers = _read_cards(page)
            if not offers:
                offers = _read_links_fallback(page)
            return offers
        finally:
            page.close()


def _wait_for_cards(page, timeout):
    for selector in CARD_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=min(timeout, 15000))
            return
        except Exception:
            continue
    # Ни один селектор не сработал: либо капча, либо пустая выдача —
    # решение принимает вызывающий код по содержимому страницы.


def _is_captcha(page):
    url = (page.url or "").lower()
    if "showcaptcha" in url or "captcha" in url:
        return True
    try:
        body = page.inner_text("body")[:1500].lower()
    except Exception:
        return False
    return "подтвердите, что запросы отправляли вы" in body or "captcha" in body


def _read_cards(page):
    offers = []
    for selector in CARD_SELECTORS:
        cards = page.query_selector_all(selector)
        if not cards:
            continue
        for card in cards:
            title = _first_text(card, TITLE_SELECTORS)
            price = parse_price(_first_text(card, PRICE_SELECTORS))
            if not title or not price:
                continue
            link = card.query_selector("a[href]")
            href = link.get_attribute("href") if link else ""
            offers.append({
                "title": title,
                "price": price,
                "url": _absolute(href),
            })
        if offers:
            return offers
    return offers


def _read_links_fallback(page):
    """Запасной разбор: карточка — это ссылка на товар с ценой рядом.

    Срабатывает, когда Яндекс поменял data-auto: точность ниже, но столбец
    не обнуляется молча.
    """
    offers = []
    for link in page.query_selector_all("a[href*='/product']"):
        title = (link.get_attribute("title") or link.inner_text() or "").strip()
        if len(title) < 10:
            continue
        container = link
        price = None
        # Цена лежит рядом с названием, но глубина вложенности плавает.
        for _ in range(4):
            container = container.evaluate_handle("e => e.parentElement")
            element = container.as_element()
            if not element:
                break
            text = element.inner_text()
            if "₽" in text:
                price = parse_price(text.split("₽")[0].split("\n")[-1])
                if price:
                    break
        if price:
            offers.append({
                "title": title.split("\n")[0],
                "price": price,
                "url": _absolute(link.get_attribute("href")),
            })
    return offers


def _first_text(card, selectors):
    for selector in selectors:
        node = card.query_selector(selector)
        if node:
            text = (node.inner_text() or "").strip()
            if text:
                return text
    return None


def _absolute(href):
    if not href:
        return ""
    if href.startswith("http"):
        return href.split("?")[0]
    return f"https://market.yandex.ru{href.split('?')[0]}"
