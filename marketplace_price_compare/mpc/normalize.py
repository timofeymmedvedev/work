"""Нормализация названий товаров и построение поисковых запросов.

Названия в исходном файле пишутся так, как их ведёт поставщик: с типом товара
по-русски, брендом и артикулом на латинице, комплектностью в скобках и хвостом
из характеристик. Маркетплейсы по такой строке целиком ищут плохо — выдача
схлопывается в ноль. Поэтому запрос собирается из значимой головы названия,
а полное название остаётся для проверки релевантности.
"""

import re
import unicodedata

# Слова, которые не сужают поиск, но сбивают выдачу.
STOPWORDS = {
    "шт", "штук", "штуки", "штука", "пара", "пары", "комплект", "компл",
    "набор", "оригинальный", "оригинальная", "оригинальное", "новый", "новая",
    "для", "и", "с", "в", "на", "из", "по", "не", "или", "а", "от", "до",
    "цвет", "цвета", "версия", "версии", "тип", "вид", "шт.", "уп", "упак",
    "the", "for", "and", "with", "of", "new", "pcs", "pc", "set",
}

# Единицы измерения: сами по себе не идентифицируют модель.
UNITS = {
    "мм", "см", "м", "км", "г", "кг", "мг", "мл", "л", "в", "а", "ма", "вт",
    "квт", "мвт", "гц", "кгц", "мгц", "ггц", "ом", "ком", "мом", "дюйм",
    "дюйма", "дюймов", "мач", "ач", "гб", "тб", "мб", "кб", "бит", "байт",
    "mm", "cm", "km", "kg", "ml", "mah", "wh", "hz", "khz", "mhz", "ghz",
    "gb", "tb", "mb", "kb", "w", "kw", "v", "mv", "ma", "a",
}

_PUNCT_RE = re.compile(r"[^\w\s.+/×x-]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_BRACKETS_RE = re.compile(r"\([^)]*\)")


def normalize(text):
    """Приводит строку к сравнимому виду: нижний регистр, ё→е, без пунктуации."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.lower().replace("ё", "е")
    # Типографские кавычки и тире к ASCII, чтобы токенизация не расходилась.
    text = text.replace("«", " ").replace("»", " ").replace("—", "-")
    text = text.replace("–", "-").replace("“", " ").replace("”", " ")
    # Десятичная запятая между цифрами -> точка, иначе "1,75 мм" распадается
    # на бессмысленные токены "1" и "75".
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def tokenize(text):
    """Разбивает нормализованный текст на значимые токены."""
    tokens = []
    for raw in normalize(text).split():
        tok = raw.strip(".-+/")
        if not tok or tok in STOPWORDS:
            continue
        if len(tok) == 1 and not tok.isdigit():
            continue
        tokens.append(tok)
    return tokens


def is_model_token(tok):
    """Токен-артикул: смесь букв и цифр (tx12, s5322, anv15-52, 5x5x3, 380kv).

    Именно такие токены отличают конкретную модель от всего остального в
    выдаче, поэтому релевантность строится вокруг них.
    """
    if tok in UNITS or tok in STOPWORDS:
        return False
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    if has_digit and has_alpha:
        return len(tok) >= 3
    # Чистое число: значимо, только если это похоже на типоразмер (9045, 1260,
    # 5322), а не на количество или год.
    if has_digit and not has_alpha:
        return len(tok) >= 4
    return False


def is_latin(tok):
    return bool(re.search(r"[a-z]", tok)) and not re.search(r"[а-я]", tok)


def is_cyrillic(tok):
    return bool(re.search(r"[а-я]", tok)) and not re.search(r"[a-z]", tok)


def split_model_variants(tok):
    """Возвращает варианты написания артикула: tx12 → {tx12, tx, 12}.

    Маркетплейсы пишут артикулы то слитно, то через пробел или дефис
    (TX12 / TX-12 / TX 12), поэтому сравнивать нужно с учётом обеих форм.
    """
    variants = {tok, tok.replace("-", ""), tok.replace("-", " ")}
    parts = re.findall(r"[a-zа-я]+|\d+", tok)
    if len(parts) > 1:
        variants.add("".join(parts))
        variants.add(" ".join(parts))
    return {v for v in variants if v}


def query_head(name):
    """Отрезает от названия комплектацию, скобочные уточнения и хвост
    характеристик, оставляя ту часть, которая идентифицирует сам товар.

    Используется и для поискового запроса, и для разбора эталона: требования
    к релевантности должны опираться на ту же часть названия, по которой
    выполнялся поиск, иначе внутренние коды поставщика из скобок и довески
    после '+' отсекают заведомо правильные позиции выдачи.
    """
    if not name:
        return ""
    head = str(name)
    # Всё после '+' и ';' — это, как правило, довесок к основному товару.
    head = re.split(r"\s\+\s|;", head)[0]
    # Хвост характеристик после запятой отрезаем, только если голова осмысленная.
    comma_head = head.split(",")[0]
    if len(tokenize(comma_head)) >= 3:
        head = comma_head

    without_brackets = _BRACKETS_RE.sub(" ", head)
    if len(tokenize(without_brackets)) >= 3:
        head = without_brackets

    # Сильный артикул (x1605va-mb2103, rb952ui-5ac2nd-tc) один задаёт модель:
    # всё, что идёт после него, — характеристики, которые и выдачу ухудшают,
    # и требования к релевантности завышают.
    tokens = tokenize(head)
    for i, tok in enumerate(tokens):
        if is_strong_article(tok):
            return " ".join(tokens[: i + 1])
    return head


def build_query(name, max_tokens=8):
    """Строит поисковый запрос из названия товара.

    Длинные запросы у Ozon и Яндекс.Маркета дают пустую выдачу, поэтому
    берём только голову названия и ограничиваем её длину.
    """
    tokens = tokenize(query_head(name))
    if not tokens:
        tokens = tokenize(name)[:max_tokens]

    # Единицы измерения в запросе только мешают ранжированию.
    tokens = [t for t in tokens if t not in UNITS]
    return " ".join(tokens[:max_tokens])


def is_strong_article(tok):
    """Артикул, однозначно указывающий на модель: длинный, с цифрами и буквами."""
    if not is_model_token(tok) or len(tok) < 6:
        return False
    if not any(c.isdigit() for c in tok) or not any(c.isalpha() for c in tok):
        return False
    # Артикул почти всегда содержит разделитель или чередование букв и цифр.
    return "-" in tok or len(re.findall(r"[a-zа-я]+|\d+", tok)) >= 3
