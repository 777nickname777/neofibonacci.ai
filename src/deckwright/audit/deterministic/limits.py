"""Пределы Приложения 1 ТЗ: плотность, заглушки, подписи диаграмм, гарнитуры.

Шесть проверок, которых в реестре не было, при том что в Приложении 1 они
перечислены прямым текстом:

* «таблица больше 7 строк или 5 колонок»;
* «больше 5 серий на диаграмме»;
* «слайд заполнен … больше чем на три четверти» (меньше четверти проверялось,
  больше трёх четвертей — нет);
* «остался текст-заглушка: lorem ipsum, XXX, TODO, «вставьте текст»»;
* «у диаграммы нет подписей осей, единиц или легенды»;
* «шрифт не из шаблона **или гарнитур больше двух**» — вторая половина.

Пороги взяты из самого документа, а не из пересказа: 7 строк, 5 колонок,
5 серий, четверть и три четверти площади, две гарнитуры.
"""

from __future__ import annotations

import re
from itertools import pairwise

from deckwright.schemas import (
    Box,
    ChartKind,
    CheckKind,
    DeckIR,
    FixKind,
    Issue,
    IssueCategory,
    ProposedFix,
    Severity,
    SlideIR,
    SlotRole,
)

# ── Пороги Приложения 1 ──────────────────────────────────────────────────
MAX_TABLE_ROWS = 7
MAX_TABLE_COLUMNS = 5
MAX_CHART_SERIES = 5
MAX_FONT_FAMILIES = 2


def _issue(
    check_id: str,
    slide_index: int,
    message: str,
    severity: Severity,
    category: IssueCategory,
    *,
    element_ids: list[str] | None = None,
    bbox: Box | None = None,
) -> Issue:
    return Issue(
        check_id=check_id,
        kind=CheckKind.DETERMINISTIC,
        category=category,
        severity=severity,
        slide_index=slide_index,
        element_ids=element_ids or [],
        bbox=bbox,
        message=message,
        fix=ProposedFix(kind=FixKind.NONE, description="", action="", params={}),
    )


# ── Плотность ────────────────────────────────────────────────────────────


def _union_area(boxes: list[Box]) -> int:
    """Площадь объединения рамок: пересечения не считаются дважды.

    Сумма площадей врёт всегда, когда рамки перекрываются, а они
    перекрываются: подпись лежит на карточке, число — на плашке. Считается
    разбиением на полосы по уникальным координатам — точное объединение без
    приближений.
    """
    boxes = [box for box in boxes if box.w > 0 and box.h > 0]
    if not boxes:
        return 0
    xs = sorted({edge for box in boxes for edge in (box.x, box.right)})
    ys = sorted({edge for box in boxes for edge in (box.y, box.bottom)})
    total = 0
    for left, right in pairwise(xs):
        for top, bottom in pairwise(ys):
            if any(
                box.x <= left and box.right >= right
                and box.y <= top and box.bottom >= bottom
                for box in boxes
            ):
                total += (right - left) * (bottom - top)
    return total


def content_boxes(slide: SlideIR, slide_w: int, slide_h: int) -> list[Box]:
    """Рамки, занятые содержанием слайда, обрезанные по его краям.

    Содержание — наш текст, график, таблица и картинка. Оформление донора и
    фон сюда не входят: полноэкранная заливка иначе давала бы сто процентов
    заполнения на пустом слайде, и порог «меньше четверти» не срабатывал бы
    никогда. Колонтитулы и номера страниц — тоже оформление.
    """
    area = Box(x=0, y=0, w=slide_w, h=slide_h)
    found = []
    for element in slide.all_elements():
        if element.role in (SlotRole.FOOTER, SlotRole.SLIDE_NUMBER, SlotRole.LOGO):
            continue
        carries = (
            element.chart is not None
            or element.table is not None
            or element.image is not None
            or (element.text is not None and any(p.text.strip() for p in element.text.paragraphs))
        )
        if not carries:
            continue
        inside = element.box.intersection(area)
        if inside is not None:
            found.append(inside)
    return found


def fill_ratio(slide: SlideIR, slide_w: int, slide_h: int) -> float:
    """Доля площади слайда, занятая содержанием."""
    area = slide_w * slide_h
    if area <= 0:
        return 0.0
    return _union_area(content_boxes(slide, slide_w, slide_h)) / area


def slide_too_full(deck: DeckIR, max_fill: float) -> list[Issue]:
    """«Слайд заполнен больше чем на три четверти» — половина, которой не было.

    Проверялась только нижняя граница. Верхняя в Приложении 1 названа ровно
    так же и значит ровно то же: слайд, забитый содержанием, не читается.
    """
    found: list[Issue] = []
    for slide in deck.slides:
        ratio = fill_ratio(slide, deck.slide_width_emu, deck.slide_height_emu)
        if ratio <= max_fill:
            continue
        found.append(
            _issue(
                "density.slide_too_full",
                slide.index,
                f"слайд заполнен на {ratio:.0%} при пороге {max_fill:.0%}",
                Severity.WARNING,
                IssueCategory.DENSITY,
            )
        )
    return found


def slide_too_empty(deck: DeckIR, min_fill: float) -> list[Issue]:
    """«Слайд заполнен меньше чем на четверть».

    Считается по объединению рамок содержания, а не по сумме площадей всех
    фигур: логотип, линия и картинка донора набирали заполненность, и шесть
    слайдов из одних заголовков проходили проверку.
    """
    found: list[Issue] = []
    for slide in deck.slides:
        ratio = fill_ratio(slide, deck.slide_width_emu, deck.slide_height_emu)
        if ratio >= min_fill:
            continue
        found.append(
            _issue(
                "density.slide_too_empty",
                slide.index,
                f"слайд заполнен на {ratio:.0%} при пороге {min_fill:.0%}",
                Severity.INFO,
                IssueCategory.DENSITY,
            )
        )
    return found


def table_too_big(deck: DeckIR) -> list[Issue]:
    """«Таблица больше 7 строк или 5 колонок»."""
    found: list[Issue] = []
    for slide in deck.slides:
        for element in slide.all_elements():
            table = element.table
            if table is None:
                continue
            rows = len(table.rows)
            columns = len(table.header)
            if rows <= MAX_TABLE_ROWS and columns <= MAX_TABLE_COLUMNS:
                continue
            found.append(
                _issue(
                    "density.table_too_big",
                    slide.index,
                    f"таблица {rows}×{columns} при пределе "
                    f"{MAX_TABLE_ROWS}×{MAX_TABLE_COLUMNS} (строк без шапки × колонок)",
                    Severity.WARNING,
                    IssueCategory.DENSITY,
                    element_ids=[element.id],
                    bbox=element.box,
                )
            )
    return found


def too_many_series(deck: DeckIR) -> list[Issue]:
    """«Больше 5 серий на диаграмме»."""
    found: list[Issue] = []
    for slide in deck.slides:
        for element in slide.all_elements():
            chart = element.chart
            if chart is None or len(chart.series) <= MAX_CHART_SERIES:
                continue
            found.append(
                _issue(
                    "density.too_many_series",
                    slide.index,
                    f"на диаграмме {len(chart.series)} серий при пределе {MAX_CHART_SERIES}",
                    Severity.WARNING,
                    IssueCategory.DENSITY,
                    element_ids=[element.id],
                    bbox=element.box,
                )
            )
    return found


# ── Целостность: заглушки и подписи диаграмм ─────────────────────────────

# Заглушки из Приложения 1 и их обычные соседи. Границы слова обязательны:
# «XXX» внутри «XXXL» или «Bruxxxelles» — не заглушка, а часть слова, и
# ловить её значит заводить находку на пустом месте.
_PLACEHOLDERS = (
    r"lorem\s+ipsum",
    r"\bTODO\b",
    r"\bFIXME\b",
    r"\bTBD\b",
    r"\bXXX\b",
    r"\bХХХ\b",  # кириллические «ха», как их пишут в русских шаблонах
    r"вставьте\s+текст",
    r"ваш\s+текст\s+здесь",
    r"текст\s+заголовка",
    r"заголовок\s+слайда",
)
_PLACEHOLDER_RE = re.compile("|".join(_PLACEHOLDERS), re.IGNORECASE)


def placeholder_text(deck: DeckIR) -> list[Issue]:
    """«Остался текст-заглушка: lorem ipsum, XXX, TODO, «вставьте текст»».

    Ищется по границам слов: `XXX` внутри `XXXL` — часть слова, а не
    заглушка. Регистр не важен, кириллические «ХХХ» считаются наравне с
    латинскими.
    """
    found: list[Issue] = []
    for slide in deck.slides:
        for element in slide.all_elements():
            texts = []
            if element.text is not None:
                texts += [p.text for p in element.text.paragraphs]
            if element.table is not None:
                texts += list(element.table.header)
                texts += [cell for row in element.table.rows for cell in row]
            if element.chart is not None:
                texts += list(element.chart.categories)
                texts += [chart_series.name for chart_series in element.chart.series]
            for text in texts:
                hit = _PLACEHOLDER_RE.search(text or "")
                if hit is None:
                    continue
                found.append(
                    _issue(
                        "content.placeholder_left",
                        slide.index,
                        f"{element.id}: остался текст-заглушка «{hit.group(0)}»",
                        Severity.ERROR,
                        IssueCategory.INTEGRITY,
                        element_ids=[element.id],
                        bbox=element.box,
                    )
                )
                break
    return found


#: Виды диаграмм без осей: у круга и кольца их нет по построению, и требовать
#: подписи осей от них — не проверка, а ложная находка.
_AXIS_FREE = frozenset({ChartKind.PIE, ChartKind.DOUGHNUT})


def chart_labels(deck: DeckIR) -> list[Issue]:
    """«У диаграммы нет подписей осей, единиц или легенды».

    Применимость важнее строгости. У круговой диаграммы осей нет — с неё
    спрашивается легенда. Единицы не нужны, когда они написаны в подписях
    категорий или в заголовке самой диаграммы. Легенда не нужна ряду-одиночке
    с подписями значений: она повторила бы название ряда и только.
    """
    found: list[Issue] = []
    for slide in deck.slides:
        for element in slide.all_elements():
            chart = element.chart
            if chart is None:
                continue
            missing: list[str] = []
            if chart.chart_kind not in _AXIS_FREE and not (
                chart.axis_title_x.strip() or chart.axis_title_y.strip()
            ):
                missing.append("подписей осей")
            if not chart.unit.strip() and not chart.title.strip():
                missing.append("единиц")
            needs_legend = len(chart.series) > 1 or chart.chart_kind in _AXIS_FREE
            if needs_legend and not chart.has_legend:
                missing.append("легенды")
            if not missing:
                continue
            found.append(
                _issue(
                    "integrity.chart_unlabelled",
                    slide.index,
                    f"{element.id}: у диаграммы нет " + ", ".join(missing),
                    Severity.WARNING,
                    IssueCategory.INTEGRITY,
                    element_ids=[element.id],
                    bbox=element.box,
                )
            )
    return found


# ── Шаблон: число гарнитур ───────────────────────────────────────────────


def too_many_fonts(deck: DeckIR) -> list[Issue]:
    """«Гарнитур больше двух» — вторая половина пункта Приложения 1.

    Считается по колоде целиком, а не по слайду: две гарнитуры на слайде и
    две другие на соседнем — это четыре в презентации, и читается она как
    четыре. Пустые имена не в счёт: отсутствие своего имени значит
    наследование, а не новую гарнитуру.
    """
    families: dict[str, int] = {}
    for slide in deck.slides:
        for element in slide.all_elements():
            if element.text is None:
                continue
            for paragraph in element.text.paragraphs:
                name = (paragraph.style.font_family or "").strip()
                if name:
                    families[name] = families.get(name, 0) + 1
    if len(families) <= MAX_FONT_FAMILIES:
        return []
    listed = ", ".join(sorted(families, key=lambda n: -families[n]))
    return [
        _issue(
            "template.too_many_fonts",
            1,
            f"в колоде {len(families)} гарнитур при пределе {MAX_FONT_FAMILIES}: {listed}",
            Severity.WARNING,
            IssueCategory.TEMPLATE,
        )
    ]


def run(deck: DeckIR, min_fill: float, max_fill: float) -> list[Issue]:
    """Все пределы Приложения 1 одним проходом."""
    found: list[Issue] = []
    found.extend(slide_too_empty(deck, min_fill))
    found.extend(slide_too_full(deck, max_fill))
    found.extend(table_too_big(deck))
    found.extend(too_many_series(deck))
    found.extend(placeholder_text(deck))
    found.extend(chart_labels(deck))
    found.extend(too_many_fonts(deck))
    return found


CHECK_IDS = (
    "density.slide_too_empty",
    "density.slide_too_full",
    "density.table_too_big",
    "density.too_many_series",
    "content.placeholder_left",
    "integrity.chart_unlabelled",
    "template.too_many_fonts",
)
