"""Пределы Приложения 1: испорченный пример и чистый контроль на каждую.

Шесть проверок, которых в реестре не было. На каждую — три случая:
намеренно испорченный (находка обязана быть), чистый (тишина обязательна) и
граничный, где порог ровно достигнут и находки ещё нет.
"""

from __future__ import annotations

import pytest

from deckwright.audit.deterministic import limits
from deckwright.schemas import (
    Box,
    ChartContent,
    ChartKind,
    ChartSeries,
    Color,
    DeckIR,
    Element,
    ElementKind,
    Paragraph,
    Provenance,
    SlideIR,
    SlotRole,
    SourceKind,
    TableContent,
    TextContent,
    TextStyle,
)

WIDTH, HEIGHT = 12_192_000, 6_858_000
BLACK = Color(rgb="000000")


def _style(family: str = "Play", size: float = 18.0) -> TextStyle:
    return TextStyle(font_family=family, size_pt=size, color=BLACK)


WHENCE = Provenance(kind=SourceKind.LAYOUT, ref="проба")


def _text(element_id: str, text: str, box: Box, family: str = "Play") -> Element:
    return Element(
        id=element_id,
        kind=ElementKind.TEXT,
        role=SlotRole.BODY,
        box=box,
        text=TextContent(paragraphs=[Paragraph(text=text, style=_style(family))]),
        provenance=WHENCE,
    )


def _deck(elements: list[Element]) -> DeckIR:
    return DeckIR(
        variant="проба",
        template_sha256="0" * 64,
        slide_width_emu=WIDTH,
        slide_height_emu=HEIGHT,
        slides=[SlideIR(index=1, elements=elements)],
    )


def _ids(found) -> list[str]:
    return [issue.check_id for issue in found]


# ── заполненность: объединение, а не сумма ──────────────────────────────


def test_overlapping_boxes_are_not_counted_twice():
    """Сумма площадей врёт, когда рамки перекрываются, — считается объединение."""
    box = Box(x=0, y=0, w=WIDTH // 2, h=HEIGHT)
    deck = _deck([_text("a", "раз", box), _text("b", "два", box)])
    ratio = limits.fill_ratio(deck.slides[0], WIDTH, HEIGHT)
    assert ratio == pytest.approx(0.5, abs=0.01), (
        f"две одинаковые рамки в половину слайда дали {ratio:.0%}"
    )


def test_content_outside_the_slide_is_clipped():
    """Рамка, торчащая за край, занимает только то, что на слайде."""
    box = Box(x=WIDTH // 2, y=0, w=WIDTH, h=HEIGHT)
    deck = _deck([_text("a", "текст", box)])
    assert limits.fill_ratio(deck.slides[0], WIDTH, HEIGHT) == pytest.approx(0.5, abs=0.01)


def test_decoration_does_not_count_as_content():
    """Логотип и колонтитул — оформление: заполненность они не набирают."""
    whole = Box(x=0, y=0, w=WIDTH, h=HEIGHT)
    logo = Element(
        id="logo", kind=ElementKind.TEXT, role=SlotRole.LOGO, box=whole,
        text=TextContent(paragraphs=[Paragraph(text="VK", style=_style())]),
        provenance=WHENCE,
    )
    deck = _deck([logo])
    assert limits.fill_ratio(deck.slides[0], WIDTH, HEIGHT) == 0.0


def test_a_too_full_slide_is_caught():
    box = Box(x=0, y=0, w=WIDTH, h=int(HEIGHT * 0.9))
    deck = _deck([_text("a", "очень много текста", box)])
    assert _ids(limits.slide_too_full(deck, max_fill=0.75)) == ["density.slide_too_full"]


def test_a_normally_filled_slide_is_quiet():
    box = Box(x=0, y=0, w=WIDTH, h=HEIGHT // 2)
    deck = _deck([_text("a", "в меру", box)])
    assert not limits.slide_too_full(deck, max_fill=0.75)
    assert not limits.slide_too_empty(deck, min_fill=0.25)


def test_an_empty_slide_is_caught():
    box = Box(x=0, y=0, w=WIDTH // 10, h=HEIGHT // 10)
    deck = _deck([_text("a", "мало", box)])
    assert _ids(limits.slide_too_empty(deck, min_fill=0.25)) == ["density.slide_too_empty"]


# ── таблица больше 7 строк или 5 колонок ────────────────────────────────


def _table(rows: int, columns: int) -> Element:
    return Element(
        id="t1",
        kind=ElementKind.TABLE,
        role=SlotRole.TABLE,
        box=Box(x=0, y=0, w=WIDTH // 2, h=HEIGHT // 2),
        table=TableContent(
            header=[f"Колонка {n}" for n in range(columns)],
            rows=[[f"{r}-{c}" for c in range(columns)] for r in range(rows)],
        ),
        provenance=WHENCE,
    )


def test_a_table_over_the_limit_is_caught():
    assert _ids(limits.table_too_big(_deck([_table(8, 3)]))) == ["density.table_too_big"]
    assert _ids(limits.table_too_big(_deck([_table(3, 6)]))) == ["density.table_too_big"]


def test_a_table_at_the_limit_is_quiet():
    """Семь строк и пять колонок — ровно предел, а не нарушение."""
    assert not limits.table_too_big(_deck([_table(7, 5)]))


# ── больше пяти серий ───────────────────────────────────────────────────


def _chart(series_count: int, **extra) -> Element:
    categories = ["Янв", "Фев", "Мар"]
    fields = {
        "chart_kind": ChartKind.COLUMN,
        "categories": categories,
        "series": [
            ChartSeries(name=f"Ряд {n}", values=[1.0, 2.0, 3.0], color=BLACK)
            for n in range(series_count)
        ],
        "axis_title_x": "Месяц",
        "axis_title_y": "Минуты",
        "unit": "мин",
        "has_legend": True,
    }
    fields.update(extra)
    return Element(
        id="c1",
        kind=ElementKind.CHART,
        role=SlotRole.CHART,
        box=Box(x=0, y=0, w=WIDTH // 2, h=HEIGHT // 2),
        chart=ChartContent(**fields),
        provenance=WHENCE,
    )


def test_a_chart_over_five_series_is_caught():
    assert _ids(limits.too_many_series(_deck([_chart(6)]))) == ["density.too_many_series"]


def test_a_chart_at_five_series_is_quiet():
    assert not limits.too_many_series(_deck([_chart(5)]))


# ── текст-заглушка ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Lorem ipsum dolor sit amet",
        "TODO: дописать вывод",
        "Показатель XXX за квартал",
        "Вставьте текст сюда",
        "ХХХ рублей выручки",
    ],
)
def test_placeholder_text_is_caught(text):
    deck = _deck([_text("a", text, Box(x=0, y=0, w=100, h=100))])
    assert _ids(limits.placeholder_text(deck)) == ["content.placeholder_left"]


@pytest.mark.parametrize(
    "text",
    [
        "Размер XXXL остался на складе",
        "Выручка выросла втрое за квартал",
        "Отгрузка в Bruxxxelles задержана",
        "Todoschi — фамилия клиента",
    ],
)
def test_a_word_that_merely_looks_like_a_placeholder_is_quiet(text):
    """`XXX` внутри слова — часть слова. Ложная находка хуже пропущенной."""
    assert not limits.placeholder_text(_deck([_text("a", text, Box(x=0, y=0, w=100, h=100))]))


def test_placeholder_inside_a_table_is_caught():
    element = _table(2, 2)
    element.table.rows[0][0] = "TODO"
    assert _ids(limits.placeholder_text(_deck([element]))) == ["content.placeholder_left"]


# ── подписи диаграммы ───────────────────────────────────────────────────


def test_a_chart_without_axis_titles_is_caught():
    element = _chart(2, axis_title_x="", axis_title_y="")
    found = limits.chart_labels(_deck([element]))
    assert _ids(found) == ["integrity.chart_unlabelled"]
    assert "подписей осей" in found[0].message


def test_a_chart_without_legend_is_caught_when_series_are_many():
    element = _chart(2, has_legend=False)
    found = limits.chart_labels(_deck([element]))
    assert _ids(found) == ["integrity.chart_unlabelled"]
    assert "легенды" in found[0].message


def test_a_single_series_chart_needs_no_legend():
    """Легенда из одного имени ряда повторяет его и только."""
    assert not limits.chart_labels(_deck([_chart(1, has_legend=False)]))


def test_a_pie_chart_is_not_asked_for_axis_titles():
    """У круга осей нет по построению: требовать их — ложная находка."""
    element = _chart(
        1, chart_kind=ChartKind.PIE, axis_title_x="", axis_title_y="", has_legend=True
    )
    assert not limits.chart_labels(_deck([element]))


def test_a_pie_chart_without_legend_is_caught():
    element = _chart(
        1, chart_kind=ChartKind.PIE, axis_title_x="", axis_title_y="", has_legend=False
    )
    assert _ids(limits.chart_labels(_deck([element]))) == ["integrity.chart_unlabelled"]


def test_units_in_the_chart_title_are_enough():
    element = _chart(2, unit="", title="Время обнаружения, минуты")
    assert not limits.chart_labels(_deck([element]))


def test_a_fully_labelled_chart_is_quiet():
    assert not limits.chart_labels(_deck([_chart(2)]))


# ── больше двух гарнитур ────────────────────────────────────────────────


def test_more_than_two_font_families_are_caught():
    deck = _deck(
        [
            _text("a", "раз", Box(x=0, y=0, w=100, h=100), family="Play"),
            _text("b", "два", Box(x=0, y=0, w=100, h=100), family="Arial"),
            _text("c", "три", Box(x=0, y=0, w=100, h=100), family="Georgia"),
        ]
    )
    found = limits.too_many_fonts(deck)
    assert _ids(found) == ["template.too_many_fonts"]
    assert "Georgia" in found[0].message


def test_two_font_families_are_quiet():
    deck = _deck(
        [
            _text("a", "раз", Box(x=0, y=0, w=100, h=100), family="Play"),
            _text("b", "два", Box(x=0, y=0, w=100, h=100), family="Arial"),
        ]
    )
    assert not limits.too_many_fonts(deck)


def test_families_are_counted_across_the_whole_deck():
    """Две гарнитуры тут и две там — это четыре в презентации."""
    deck = DeckIR(
        variant="проба",
        template_sha256="0" * 64,
        slide_width_emu=WIDTH,
        slide_height_emu=HEIGHT,
        slides=[
            SlideIR(
                index=1,
                elements=[
                    _text("a", "раз", Box(x=0, y=0, w=100, h=100), family="Play"),
                    _text("b", "два", Box(x=0, y=0, w=100, h=100), family="Arial"),
                ],
            ),
            SlideIR(
                index=2,
                elements=[
                    _text("c", "три", Box(x=0, y=0, w=100, h=100), family="Georgia"),
                    _text("d", "четыре", Box(x=0, y=0, w=100, h=100), family="Verdana"),
                ],
            ),
        ],
    )
    assert _ids(limits.too_many_fonts(deck)) == ["template.too_many_fonts"]


# ── контраст: непроверенное не выдаётся за проверенное ──────────────────


def test_contrast_over_imagery_is_reported_as_unverified():
    """Под текстом фотография макета: по одному цвету контраст не считается.

    Молчание тут было бы хуже находки — отчёт врал бы про покрытие: «нарушений
    не найдено» при непроверенном тексте.
    """
    from deckwright.audit.deterministic import template_fidelity
    from deckwright.schemas import (
        LayoutSpec,
        Pattern,
        PatternClass,
        Slot,
        TemplateSpec,
    )

    whole = Box(x=0, y=0, w=WIDTH, h=HEIGHT)
    element = _text("a", "Заголовок на фотографии", whole)
    slide = SlideIR(index=1, pattern_id="p1", elements=[element], background=BLACK)
    spec = TemplateSpec(
        source_name="проба.pptx",
        template_sha256="0" * 64,
        slide_width_emu=WIDTH,
        slide_height_emu=HEIGHT,
        layouts=[LayoutSpec(id="l1", name="проба", master_id="m1")],
        patterns=[
            Pattern(
                id="p1",
                layout_id="l1",
                pattern_class=PatternClass.STATEMENT,
                donor_slide_index=1,
                content_area=whole,
                layout_obstacles=[whole],
                slots=[
                    Slot(
                        id="s1",
                        role=SlotRole.TITLE,
                        box=whole,
                        provenance=WHENCE,
                    )
                ],
                provenance=WHENCE,
            )
        ],
    )
    found = template_fidelity.contrast(slide, min_ratio=4.5, spec=spec)
    assert _ids(found) == ["template.contrast_unverified"]


def test_contrast_on_a_plain_backdrop_is_measured():
    """Ровная подложка — контраст считается как раньше, без «не измерено»."""
    from deckwright.audit.deterministic import template_fidelity

    element = _text("a", "Тёмный текст", Box(x=0, y=0, w=WIDTH // 2, h=HEIGHT // 2))
    element.backdrop = Color(rgb="111111")
    slide = SlideIR(index=1, elements=[element], background=Color(rgb="FFFFFF"))
    found = template_fidelity.contrast(slide, min_ratio=4.5)
    assert _ids(found) == ["template.low_contrast"], "чёрное по чёрному не поймано"


# ── покрытие названо честно ─────────────────────────────────────────────


def test_coverage_names_the_denominator():
    """«Находок нет» без знаменателя читается как «всё проверено»."""
    from deckwright.audit.registry import CHECKS
    from deckwright.schemas import AuditReport

    report = AuditReport(
        variant="balanced",
        issues=[],
        skipped_checks={"content.has_content": "модель со зрением недоступна"},
        checks_total=len(CHECKS),
    )
    assert report.checks_run == len(CHECKS) - 1
    assert str(len(CHECKS)) in report.coverage
    assert "пропущено 1" in report.coverage


def test_coverage_without_a_denominator_says_so():
    """Старый отчёт без знаменателя не притворяется полным."""
    from deckwright.schemas import AuditReport

    assert AuditReport(variant="x").coverage == "покрытие неизвестно"
