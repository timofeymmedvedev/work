"""Запуск Chromium для площадок, которые не отдают выдачу простым запросом."""

from contextlib import contextmanager

# Реальный профиль браузера: дефолтный Playwright-контекст отличается от
# обычного Chrome набором признаков, по которым Ozon и Маркет и распознают
# автоматизацию.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


@contextmanager
def chromium_context(headless=True, timeout=45000, executable_path=None):
    """Отдаёт готовый browser context; закрывает браузер при выходе.

    executable_path пригодится, когда в системе уже есть Chromium, но его
    версия не совпадает с той, что ожидает установленный Playwright — иначе
    launch падает с требованием выполнить "playwright install".
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            executable_path=executable_path,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=UA,
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"},
        )
        context.add_init_script(STEALTH_JS)
        context.set_default_timeout(timeout)
        try:
            yield context
        finally:
            context.close()
            browser.close()
