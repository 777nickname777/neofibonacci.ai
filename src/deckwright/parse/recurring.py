"""Повторяющиеся элементы: логотип, колонтитул, номер слайда.

Опознаются тем, что стоят на одном месте почти во всей колоде. Это не
эвристика от бедности, а единственный доступный признак: в колодах,
экспортированных через Google Slides, все фигуры называются `Google
Shape;NNN`, и ни имя, ни тип не говорят, логотип перед нами или иллюстрация.

Результат нужен дважды. Вёрстке — чтобы не ставить содержание туда, где у
шаблона всегда стоит логотип. Аудиту — чтобы проверка «логотип или колонтитул
сдвинуты с положенного места» знала, где это место.
"""

from __future__ import annotations

import re
from collections import defaultdict

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.schemas import Box, Provenance, RecurringElement, SlotRole, SourceKind

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Положения ближе этого расстояния считаются одним местом (0.05 дюйма):
# редакторы дают небольшой разброс при копировании слайда.
POSITION_TOLERANCE_EMU = 45_720

# Элемент, встретившийся реже этой доли слайдов, повторяющимся не является.
MIN_FREQUENCY = 0.5

# Поле номера слайда в OOXML.
SLIDE_NUMBER_FIELD = "slidenum"

_DIGITS_ONLY = re.compile(r"^\d{1,3}$")

P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _is_placeholder(element: etree._Element) -> bool:
    """Плейсхолдер — место под содержание, а не повторяющийся элемент.

    Без этой проверки в повторяющиеся попадает рамка заголовка: она стоит на
    одном месте во всей колоде и формально неотличима от логотипа. Разница в
    назначении, и назначение объявлено в самом файле тегом `p:ph`.
    """
    return element.find(f".//{{{P_NS}}}ph") is not None


def _text_of(element: etree._Element) -> str:
    return " ".join((node.text or "") for node in element.findall(f".//{{{A_NS}}}t")).strip()


def _is_slide_number(element: etree._Element, text: str) -> bool:
    """Номер слайда — либо поле, либо просто число в углу."""
    for field in element.findall(f".//{{{A_NS}}}fld"):
        if (field.get("type") or "").lower() == SLIDE_NUMBER_FIELD:
            return True
    return bool(_DIGITS_ONLY.match(text))


def _role_of(element: etree._Element, text: str, box: Box, slide_h: int) -> SlotRole:
    tag = etree.QName(element).localname
    if _is_slide_number(element, text):
        return SlotRole.SLIDE_NUMBER
    if tag == "pic":
        return SlotRole.LOGO
    if text and box.bottom > slide_h * 0.85:
        return SlotRole.FOOTER
    return SlotRole.LOGO


def _is_meaningful(element: etree._Element, text: str) -> bool:
    """Логотип, колонтитул и номер — это картинка или текст.

    Декоративные точки, полоски и уголки тоже стоят на одном месте во всей
    колоде, но местом под содержание не являются и вёрстке ничего не говорят.
    Отличить их можно по тому, несут ли они хоть что-нибудь: на `vk_workspace`
    без этой проверки в повторяющиеся попадали двадцать пять фигур вместо трёх.
    """
    return etree.QName(element).localname == "pic" or bool(text)


def _fits_slide(box: Box, slide_w: int, slide_h: int) -> bool:
    """Фигура целиком на слайде.

    Направляющие и вылеты за обрез стоят на одном месте во всех слайдах и
    формально выглядят повторяющимся элементом, но за краем слайда их не видно
    ни человеку, ни аудиту.
    """
    return (
        box.x >= 0
        and box.y >= 0
        and box.right <= slide_w
        and box.bottom <= slide_h
    )


def _key(box: Box) -> tuple[int, int, int, int]:
    """Огрублённое положение: по нему одинаковые места сливаются в одно."""
    step = POSITION_TOLERANCE_EMU
    return (
        round(box.x / step),
        round(box.y / step),
        round(box.w / step),
        round(box.h / step),
    )


def find_recurring(
    slides: list[list[etree._Element]], slide_w: int, slide_h: int
) -> list[RecurringElement]:
    """Элементы, стоящие на одном месте более чем на половине слайдов.

    На вход идут деревья фигур **каждого слайда вместе с его layout'ом и
    мастером**: логотип и колонтитул почти никогда не лежат на самом слайде.
    В датасете подписи «VK WorkSpace» и «vk tech» приходят именно с layout'а,
    и поиск только по слайдам не находит ни одного настоящего логотипа —
    зато находит рамку заголовка.
    """
    if not slides:
        return []

    seen: dict[tuple, list[tuple[etree._Element, Box]]] = defaultdict(list)
    for trees in slides:
        # На одном слайде элемент засчитывается один раз, иначе ряд одинаковых
        # маркеров внутри слайда выглядел бы как повтор по колоде.
        local: set[tuple] = set()
        for container in trees:
            for element, box, _ in iter_shapes(container):
                if box is None or box.area <= 0:
                    continue
                if _is_placeholder(element) or not _fits_slide(box, slide_w, slide_h):
                    continue
                if not _is_meaningful(element, _text_of(element)):
                    continue
                # Фигура во весь слайд — подложка, а не колонтитул.
                if box.w >= slide_w * 0.9 and box.h >= slide_h * 0.9:
                    continue
                key = _key(box)
                if key in local:
                    continue
                local.add(key)
                seen[key].append((element, box))

    total = len(slides)
    elements: list[RecurringElement] = []
    ranked = sorted(seen.values(), key=len, reverse=True)
    for index, members in enumerate(ranked):
        frequency = len(members) / total
        if frequency < MIN_FREQUENCY:
            continue
        element, box = members[0]
        text = _text_of(element)
        # Логотип и колонтитул повторяют не только место, но и текст. Рамка
        # с разным текстом на каждом слайде — заголовок шаблона без
        # плейсхолдеров («Проект проходит шесть этапов», «Цель задаёт…»), а
        # не логотип. Номер страницы меняется законно.
        texts = {_text_of(other) for other, _ in members}
        if len(texts) > 1 and not _is_slide_number(element, text):
            continue
        elements.append(
            RecurringElement(
                id=f"recurring{index}",
                role=_role_of(element, text, box, slide_h),
                box=box,
                frequency=round(frequency, 3),
                text=text or None,
                provenance=Provenance(
                    kind=SourceKind.DERIVED,
                    ref="кластеризация положений по колоде",
                    confidence=round(frequency, 3),
                    note=f"встретился на {len(members)} слайдах из {total}",
                ),
            )
        )
    return elements
