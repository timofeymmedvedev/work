"""Проверка того, что найденная позиция — действительно тот же товар.

Выдача маркетплейсов по запросу "пульт radiomaster tx12 mkii elrs" содержит
и сам пульт, и чехлы к нему, и стики, и совсем посторонние товары, которые
площадка подмешивает, когда точных совпадений мало. Усреднять такую выдачу
подряд бессмысленно, поэтому каждая позиция проверяется на соответствие
эталонному названию из файла.
"""

from .normalize import (
    is_cyrillic,
    is_latin,
    is_model_token,
    is_strong_article,
    normalize,
    query_head,
    split_model_variants,
    tokenize,
)

# Слова, наличие которых у кандидата означает, что это аксессуар к товару,
# а не сам товар. Ловят "чехол для TX12" в выдаче по запросу "TX12".
ACCESSORY_MARKERS = {
    "чехол", "чехлы", "сумка", "кейс", "футляр", "наклейка", "наклейки",
    "стикер", "защитная", "защитное", "пленка", "плёнка", "ремешок",
    "переходник-заглушка", "инструкция", "case", "cover", "sticker", "bag",
    "стик", "стики", "антенна-заглушка", "запчасть", "запчасти", "ремкомплект",
}


class Reference:
    """Разобранное эталонное название товара из исходного файла."""

    def __init__(self, name):
        self.name = name
        self.norm = normalize(name)
        # Требования строим по голове названия — по той же части, по которой
        # шёл поиск. Артикулы поставщика из скобок и довески после '+' на
        # маркетплейсе не воспроизводятся и отсекали бы верные позиции.
        tokens = tokenize(query_head(name))
        self.tokens = tokens
        self.model = [t for t in tokens if is_model_token(t)]
        self.articles = [t for t in tokens if is_strong_article(t)]
        self.latin = [t for t in tokens if is_latin(t) and not is_model_token(t)]
        self.cyrillic = [t for t in tokens if is_cyrillic(t) and not is_model_token(t)]
        # Тип товара — обычно первое кириллическое слово ("пропеллер", "мотор").
        self.kind = self.cyrillic[0] if self.cyrillic else None

    def _contains(self, haystack, token):
        """Есть ли токен в тексте кандидата с учётом вариантов написания."""
        return any(v in haystack for v in split_model_variants(token))

    def match(self, candidate_title):
        """Возвращает (релевантен, score) для названия из выдачи."""
        cand = normalize(candidate_title)
        if not cand:
            return False, 0.0
        cand_tokens = set(tokenize(candidate_title))

        # Аксессуар вместо товара — но только если сам эталон не аксессуар.
        if cand_tokens & ACCESSORY_MARKERS and not (
            set(self.tokens) & ACCESSORY_MARKERS
        ):
            return False, 0.0

        # 1. Сильные артикулы обязательны: они однозначно задают модель.
        if self.articles:
            hits = sum(1 for a in self.articles if self._contains(cand, a))
            if hits == 0:
                return False, 0.0

        # 2. Модельные токены: требуем большинство.
        model_cover = 1.0
        if self.model:
            hits = sum(1 for m in self.model if self._contains(cand, m))
            model_cover = hits / len(self.model)
            if model_cover < 0.6:
                return False, 0.0

        # 3. Бренд/латиница: хотя бы одно совпадение, если в эталоне латиница есть.
        latin_cover = 1.0
        if self.latin:
            hits = sum(1 for t in self.latin if t in cand_tokens)
            latin_cover = hits / len(self.latin)
            # Когда модельных токенов нет, бренд — единственный якорь, поэтому
            # одного совпадения из четырёх мало: "TBS Tango 2 стики" не должен
            # проходить как "TBS Tango 2 PRO V4".
            floor = 0.6 if not self.model else 0.0
            if hits == 0 or latin_cover < floor:
                return False, 0.0

        # 4. Когда модельных токенов нет, опереться на артикул невозможно, и
        # одного совпадения бренда мало — требуем ещё и совпадения большей
        # части описательных слов.
        cyr_cover = 1.0
        if self.cyrillic:
            hits = sum(1 for t in self.cyrillic if t in cand_tokens)
            cyr_cover = hits / len(self.cyrillic)
        if not self.model:
            threshold = 0.5 if self.latin else 0.6
            if cyr_cover < threshold:
                return False, 0.0

        # 5. Тип товара не должен противоречить: если эталон "мотор", а кандидат
        # про мотор ничего не говорит, это подозрительно — но не блокируем,
        # только снижаем score, потому что площадки часто опускают тип.
        kind_bonus = 0.15 if self.kind and self.kind in cand_tokens else 0.0

        score = 0.5 * model_cover + 0.25 * latin_cover + 0.1 * cyr_cover + kind_bonus
        return True, round(min(score, 1.0), 4)
