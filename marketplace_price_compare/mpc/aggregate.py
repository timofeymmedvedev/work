"""Отбор позиций выдачи и расчёт средней рыночной цены."""

from statistics import median


def pick_matches(reference, offers, top_n, scan_depth, outlier_ratio):
    """Отбирает до top_n релевантных позиций из выдачи.

    offers — список словарей {title, price, url} в порядке выдачи площадки.
    Просматриваем не более scan_depth позиций: выдача отсортирована по
    релевантности самой площадкой, и если нужного товара нет в первых
    тридцати, дальше его почти наверняка нет вовсе.
    """
    matched = []
    for offer in offers[:scan_depth]:
        price = offer.get("price")
        if not price or price <= 0:
            continue
        ok, score = reference.match(offer.get("title", ""))
        if not ok:
            continue
        matched.append({**offer, "score": score})
        if len(matched) >= top_n:
            break

    return drop_outliers(matched, outlier_ratio)


def drop_outliers(matched, ratio):
    """Убирает позиции, чья цена расходится с медианой группы более чем в ratio раз.

    Даже после фильтра релевантности в выдачу попадают лоты другой
    комплектности — одиночный пропеллер против набора из четырёх, — и одна
    такая позиция сдвигает среднее сильнее, чем все остальные вместе.
    """
    if len(matched) < 3:
        return matched
    med = median(m["price"] for m in matched)
    if med <= 0:
        return matched
    kept = [m for m in matched if (1 / ratio) <= (m["price"] / med) <= ratio]
    # Если фильтр съел почти всё, значит медиана посчиталась по мусору —
    # безопаснее вернуть исходную группу и показать разброс в отчёте.
    return kept if len(kept) >= 2 else matched


def summarize(matched):
    """Возвращает среднюю цену, количество позиций и разброс."""
    if not matched:
        return {"avg": None, "count": 0, "min": None, "max": None}
    prices = [m["price"] for m in matched]
    return {
        "avg": round(sum(prices) / len(prices), 2),
        "count": len(prices),
        "min": min(prices),
        "max": max(prices),
    }
