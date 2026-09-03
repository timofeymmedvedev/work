"""Ozon: выдача забирается через внутренний composer-api в браузере.

Прямой HTTP-запрос упирается в антибот-защиту, а вёрстка карточек
обфусцирована и меняется. Поэтому страница открывается настоящим Chromium,
но запрашивается не HTML, а JSON того же composer-api, которым пользуется
сам сайт: его структура переживает редизайны заметно дольше, чем классы.

Дёргать API «с холодного старта» нельзя: без куки, которые Ozon выдаёт при
обычном заходе, вместо JSON приходит страница антибот-проверки. Поэтому
перед первым запросом сессия прогревается заходом на главную, а если API
всё же закрылся — выдача снимается с обычной страницы поиска.
"""

import json
from urllib.parse import quote

from .base import Source, SourceError, parse_price

API_URL = (
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/%3Ftext%3D{q}"
)
HTML_URL = "https://www.ozon.ru/search/?text={q}&from_global=true"
HOME_URL = "https://www.ozon.ru/"


class OzonSource(Source):
    name = "ozon"

    def __init__(self, browser, delay=2.5, timeout=45000):
        self.browser = browser
        self.delay = delay
        self.timeout = timeout
        self._warmed = False

    def search(self, query):
        self._warm_up()

        offers, api_error = self._search_api(query)
        if offers:
            return offers

        # API закрылся — пробуем обычную страницу поиска. Она тяжелее и
        # медленнее, но отдаёт те же карточки.
        offers, html_error = self._search_html(query)
        if offers:
            return offers

        if api_error and html_error:
            raise SourceError(f"{api_error}; страница поиска: {html_error}")
        # Обе попытки прошли без ошибок, но выдача пуста — товара нет.
        return []

    def _warm_up(self):
        """Заход на главную, чтобы Ozon выдал куки сессии.

        Без него первый же запрос к composer-api возвращает антибот-страницу.
        Выполняется один раз на весь прогон.
        """
        if self._warmed:
            return
        page = self.browser.new_page()
        try:
            page.goto(HOME_URL, timeout=self.timeout, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception:
            # Прогрев не критичен: если он не удался, запрос всё равно
            # стоит попробовать — ошибку вернёт уже сам поиск.
            pass
        finally:
            page.close()
            self._warmed = True

    def _search_api(self, query):
        page = self.browser.new_page()
        try:
            page.goto(API_URL.format(q=quote(query)), timeout=self.timeout)
            body = page.inner_text("body")
            try:
                payload = json.loads(body)
            except ValueError:
                return [], f"ozon: вместо JSON пришло: {_describe(body)}"
            return extract_offers(payload), None
        except Exception as exc:
            return [], f"ozon: {type(exc).__name__}: {str(exc)[:120]}"
        finally:
            page.close()

    def _search_html(self, query):
        page = self.browser.new_page()
        try:
            page.goto(HTML_URL.format(q=quote(query)), timeout=self.timeout,
                      wait_until="domcontentloaded")
            try:
                page.wait_for_selector("a[href*='/product/']", timeout=15000)
            except Exception:
                pass
            if _is_challenge_page(page):
                return [], "антибот-проверка"
            return _read_dom(page), None
        except Exception as exc:
            return [], f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            page.close()


def _describe(body):
    """Короткая характеристика ответа — чтобы причина была видна в логе."""
    text = (body or "").strip()
    if not text:
        return "пустой ответ"
    lowered = text[:3000].lower()
    if "captcha" in lowered or "робот" in lowered or "доступ ограничен" in lowered:
        return "страница антибот-проверки"
    return repr(text[:120])


def _is_challenge_page(page):
    try:
        body = page.inner_text("body")[:3000].lower()
    except Exception:
        return False
    return any(m in body for m in ("доступ ограничен", "captcha", "вы робот"))


def _read_dom(page):
    """Снимает карточки с обычной страницы поиска.

    Классы у Ozon обфусцированы, поэтому опираемся на единственное
    устойчивое — ссылку на товар — и ищем цену в ближайших предках.
    """
    script = """
    () => {
      const out = [];
      const seen = new Set();
      for (const a of document.querySelectorAll("a[href*='/product/']")) {
        const href = a.getAttribute('href') || '';
        const id = href.split('?')[0];
        if (!id || seen.has(id)) continue;
        let node = a, price = null, title = '';
        for (let i = 0; i < 5 && node; i++) {
          const text = node.innerText || '';
          if (!title) {
            // Название — самая длинная строка карточки без знака рубля.
            const lines = text.split('\\n')
              .map(s => s.trim())
              .filter(s => s.length > 12 && !s.includes('₽'));
            if (lines.length) title = lines.sort((x, y) => y.length - x.length)[0];
          }
          if (!price) {
            const m = text.match(/(\\d[\\d\\s\\u00a0]{2,})\\s*₽/);
            if (m) price = m[1].replace(/[\\s\\u00a0]/g, '');
          }
          if (title && price) break;
          node = node.parentElement;
        }
        if (title && price) {
          seen.add(id);
          out.push({title: title, price: price, url: id});
        }
      }
      return out;
    }
    """
    offers = []
    for item in page.evaluate(script) or []:
        price = parse_price(item.get("price"))
        if not price:
            continue
        offers.append({
            "title": item.get("title", ""),
            "price": price,
            "url": _absolute(item.get("url", "")),
        })
    return offers


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
    if not link:
        return ""
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
