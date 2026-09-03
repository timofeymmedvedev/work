#!/usr/bin/env python3
"""Сбор средних цен маркетплейсов и сборка обновлённого прайса.

Пример:
    python run.py --input прайс.xlsx --output прайс_с_ценами.xlsx
    python run.py --input прайс.xlsx --sources wb --limit 50
"""

import argparse
import os
import sys
import time

# Консоль Windows по умолчанию работает не в UTF-8, и печать русских названий
# товаров в прогресс-строке роняет прогон с UnicodeEncodeError. Переключаем
# вывод на UTF-8 с заменой непечатаемых символов, чтобы многочасовой сбор не
# прерывался из-за одного названия.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from mpc import config
from mpc.aggregate import pick_matches, summarize
from mpc.browser import chromium_context
from mpc.excel_io import read_products, write_report
from mpc.normalize import build_query
from mpc.relevance import Reference
from mpc.sources.base import SourceError
from mpc.sources.ozon import OzonSource
from mpc.sources.wildberries import WildberriesSource
from mpc.sources.yandex_market import YandexMarketSource
from mpc.storage import Store

# WB обходится обычным запросом, но при обрыве TLS переключается на браузер,
# поэтому Chromium поднимается и ради него тоже.
BROWSER_SOURCES = {"ozon", "yandex", "wb"}

# После стольких ошибок подряд площадка отключается до конца прогона.
# Капча и блокировка не рассасываются сами: без этого предохранителя прогон
# потратил бы часы на повторы по всем 3 872 товарам и не собрал ничего.
FAILURE_STREAK_LIMIT = 12


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="исходный .xlsx с прайсом")
    parser.add_argument("--output", help="куда сохранить результат")
    parser.add_argument("--sheet", help="имя листа (по умолчанию первый)")
    parser.add_argument(
        "--db", default="prices.sqlite3",
        help="файл чекпоинта; повторный запуск продолжает с места остановки",
    )
    parser.add_argument(
        "--sources", default="ozon,yandex,wb",
        help="площадки через запятую: ozon, yandex, wb",
    )
    parser.add_argument("--limit", type=int, help="обработать только первые N товаров")
    parser.add_argument("--offset", type=int, default=0, help="пропустить первые N товаров")
    parser.add_argument("--top-n", type=int, default=config.TOP_N)
    parser.add_argument("--scan-depth", type=int, default=config.SCAN_DEPTH)
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="повторить товары, по которым прошлый запуск не получил выдачу",
    )
    parser.add_argument(
        "--no-headless", action="store_true",
        help="показать окно браузера (нужно, чтобы вручную пройти капчу)",
    )
    parser.add_argument(
        "--chromium-path",
        help="путь к готовому chrome/chromium, если playwright install недоступен",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        help="ничего не собирать, только собрать .xlsx из уже накопленной базы",
    )
    return parser.parse_args(argv)


def build_sources(names, browser, page_timeout):
    """Создаёт объекты площадок; браузерные — только если они запрошены."""
    sources = []
    for name in names:
        if name == "wb":
            sources.append(WildberriesSource(
                dest=config.WB_DEST, delay=config.DELAY_WB,
                browser=browser, page_timeout=page_timeout))
        elif name == "ozon":
            sources.append(OzonSource(
                browser, delay=config.DELAY_OZON, timeout=page_timeout))
        elif name == "yandex":
            sources.append(YandexMarketSource(
                browser, delay=config.DELAY_YM, timeout=page_timeout))
        else:
            raise SystemExit(f"Неизвестная площадка: {name}")
    return sources


def collect(products, sources, store, args):
    """Обходит товары по площадкам, складывая результат в чекпоинт."""
    done = store.done_keys()
    total = len(products) * len(sources)
    processed = 0
    started = time.time()
    streak = {source.name: 0 for source in sources}
    disabled = set()

    for product in products:
        reference = Reference(product["name"])
        query = build_query(product["name"])
        if not query:
            continue

        for source in sources:
            processed += 1
            key = (product["row"], source.name)
            if key in done or source.name in disabled:
                continue

            summary, status, offers = fetch_one(source, query, reference, args)
            store.save(product["row"], source.name, query, summary, status, offers)
            source.sleep()

            if status.startswith("error"):
                streak[source.name] += 1
                if streak[source.name] >= FAILURE_STREAK_LIMIT:
                    disabled.add(source.name)
                    print(
                        f"\n[!] {source.name}: {FAILURE_STREAK_LIMIT} ошибок подряд, "
                        f"площадка отключена до конца прогона.\n"
                        f"    Последняя: {status}\n"
                        f"    Разберитесь с причиной и запустите с --retry-failed."
                    )
            else:
                streak[source.name] = 0

            _progress(processed, total, started, product["name"], source.name, summary, status)

        if len(disabled) == len(sources):
            print("\n[!] Все площадки отключены — прогон остановлен.")
            break


def fetch_one(source, query, reference, args):
    """Один запрос к площадке с повторами; возвращает (сводка, статус, позиции)."""
    last_error = None
    for attempt in range(max(1, config.MAX_RETRIES)):
        try:
            offers = source.search(query)
        except SourceError as exc:
            last_error = str(exc)
            time.sleep(config.RETRY_BACKOFF * (attempt + 1))
            continue
        except Exception as exc:  # непредвиденное: сеть, таймаут браузера
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(config.RETRY_BACKOFF * (attempt + 1))
            continue

        matched = pick_matches(
            reference, offers, args.top_n, args.scan_depth, config.OUTLIER_RATIO
        )
        summary = summarize(matched)
        # Пустая выдача и выдача без единого совпадения — разные случаи:
        # первый чаще означает проблему с запросом, второй — что товара на
        # площадке действительно нет. Различаем их в статусе.
        if not offers:
            status = "empty"
        elif not matched:
            status = "no_match"
        else:
            status = "ok"
        return summary, status, matched

    return summarize([]), f"error: {last_error}", []


def _progress(done, total, started, name, source, summary, status):
    elapsed = time.time() - started
    rate = done / elapsed if elapsed > 0 else 0
    left = (total - done) / rate if rate else 0
    avg = summary.get("avg")
    price = f"{avg:>10,.0f}".replace(",", " ") if avg else "         —"
    sys.stdout.write(
        f"\r[{done}/{total}] {source:<7} {price} {status:<9} "
        f"осталось ~{left/60:5.0f} мин | {name[:40]:<40}"
    )
    sys.stdout.flush()


def main(argv=None):
    args = parse_args(argv)
    names = [s.strip() for s in args.sources.split(",") if s.strip()]
    args.top_n = args.top_n or config.TOP_N

    if not os.path.exists(args.input):
        raise SystemExit(
            f"Файл не найден: {args.input}\n"
            f"Проверьте, что прайс лежит в текущей папке ({os.getcwd()})\n"
            f"и что имя указано верно. Список файлов: dir *.xlsx"
        )

    try:
        products = read_products(args.input, args.sheet)
    except KeyError:
        raise SystemExit(
            f"В файле нет листа {args.sheet!r}. "
            f"Уберите --sheet, чтобы взять первый лист."
        )
    if not products:
        raise SystemExit("В файле не найдено ни одной строки с названием товара.")
    if args.offset:
        products = products[args.offset:]
    if args.limit:
        products = products[: args.limit]
    print(f"Товаров к обработке: {len(products)}; площадки: {', '.join(names)}")

    store = Store(args.db)
    if args.retry_failed:
        # Неуспешные записи удаляются, чтобы done_keys их не пропустил.
        store.conn.execute("DELETE FROM results WHERE status != 'ok'")
        store.conn.commit()

    try:
        if not args.report_only:
            needs_browser = bool(BROWSER_SOURCES & set(names))
            headless = not args.no_headless
            if needs_browser:
                with chromium_context(
                    headless=headless,
                    timeout=config.PAGE_TIMEOUT,
                    executable_path=args.chromium_path,
                ) as browser:
                    sources = build_sources(names, browser, config.PAGE_TIMEOUT)
                    collect(products, sources, store, args)
            else:
                sources = build_sources(names, None, config.PAGE_TIMEOUT)
                collect(products, sources, store, args)
            print()

        out_path = args.output or _default_output(args.input)
        write_report(args.input, out_path, products, store.load_all(), names, args.sheet)
        print(f"Готово: {out_path}")

        for source, status, count in store.stats():
            print(f"  {source:<8} {status:<12} {count}")
    finally:
        store.close()


def _default_output(path):
    if path.lower().endswith(".xlsx"):
        return path[:-5] + "_с_ценами_маркетплейсов.xlsx"
    return path + "_с_ценами_маркетплейсов.xlsx"


if __name__ == "__main__":
    main()
