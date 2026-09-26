"""Сверка чисел с входом: число есть в источнике или его нет на слайде.

Модель, приводящая вход к контент-пакету, пишет факты своими словами и
числа — своими значениями. Правило одно: число, которого во входе нет, в
контент-пакет не попадает. Проверка детерминированная и идёт по тексту
входа, а не по ответу модели: регулярка здесь читает документ пользователя.

Одно и то же число пишут по-разному: «1 200», «1200», «1,2 тыс.», «34 %» и
0.34. Поэтому число ответа ищется в источнике с точностью до масштаба
(проценты, тысячи, миллионы, миллиарды) и округления.
"""

from __future__ import annotations

import re

# Число в тексте документа: целое с пробелами-разделителями разрядов
# («1 200 000»), десятичное с точкой или запятой.
_NUMBER = re.compile(r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")

# Масштабы, в которых одно и то же число пишут по-разному.
_SCALES = (1.0, 100.0, 0.01, 1e3, 1e-3, 1e6, 1e-6, 1e9, 1e-9)

# Относительная погрешность: округление «4 666» до «4,7 тыс.».
TOLERANCE = 0.011


def source_numbers(text: str) -> set[float]:
    """Все числа текста документа."""
    found: set[float] = set()
    for match in _NUMBER.findall(text):
        cleaned = re.sub(r"[   ]", "", match).replace(",", ".")
        try:
            found.add(float(cleaned))
        except ValueError:
            continue
    return found


def is_grounded(value: float, numbers: set[float]) -> bool:
    """Есть ли число во входе — как есть или в другом масштабе записи."""
    if value == 0:
        return 0.0 in numbers
    for scale in _SCALES:
        target = value * scale
        for number in numbers:
            if number and abs(number - target) <= abs(target) * TOLERANCE:
                return True
    return False


def ungrounded(text: str, numbers: set[float], small: int = 0) -> list[float]:
    """Числа строки, которых во входе нет.

    `small` — целые до этого значения считаются счётом, а не фактом: «три
    шага», нумерация этапов, «2 варианта». Их проверять не с чем.
    """
    return [
        value
        for value in sorted(source_numbers(text))
        if not (value.is_integer() and 0 <= value <= small) and not is_grounded(value, numbers)
    ]


def normalize(text: str) -> str:
    """Текст для поиска цитаты: без различий в пробелах, кавычках и регистре."""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'“”„]", "", text)
    return re.sub(r"\s+", " ", text).strip()
