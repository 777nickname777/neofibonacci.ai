"""Контракты слоёв: round-trip и те инварианты, ради которых валидаторы написаны.

Тривиальные поля не проверяются — Pydantic и так их держит. Здесь только то,
где неверная модель пропустила бы ошибку дальше по пайплайну.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import fixtures as fx
from deckwright.schemas import (
    BlockKind,
    Box,
    ChartContent,
    ChartKind,
    ChartSeries,
    CheckKind,
    Color,
    ContentBlock,
    ContentPack,
    DeckIR,
    DeckPlan,
    Element,
    ElementKind,
    Fact,
    FixKind,
    FontToken,
    Issue,
    IssueCategory,
    Pattern,
    PatternClass,
    Provenance,
    Repeater,
    RunManifest,
    Severity,
    SlideIntent,
    SlidePlan,
    SlotRole,
    SourceKind,
    TableContent,
    TemplateSpec,
)

# ── round-trip: контракт переживает сериализацию ──────────────────────────────

@pytest.mark.parametrize(
    ("factory", "model"),
    [
        (fx.template_spec, TemplateSpec),
        (fx.content_pack, ContentPack),
        (fx.deck_plan, DeckPlan),
        (fx.deck_ir, DeckIR),
        (fx.run_manifest, RunManifest),
    ],
)
def test_round_trip(factory, model):
    original = factory()
    restored = model.model_validate_json(original.model_dump_json())
    assert restored == original


# ── геометрия ─────────────────────────────────────────────────────────────────

def test_boxes_that_touch_do_not_overlap():
    """Смежные блоки не должны считаться наложившимися — иначе аудит завалит
    ложными находками любую сетку, где карточки стоят вплотную."""
    left = Box(x=0, y=0, w=100, h=100)
    right = Box(x=100, y=0, w=100, h=100)
    assert left.intersection(right) is None
    assert left.intersection(Box(x=99, y=0, w=100, h=100)) is not None


def test_contrast_ratio_matches_wcag():
    black, white = Color(rgb="000000"), Color(rgb="FFFFFF")
    assert black.contrast_ratio(white) == pytest.approx(21.0, abs=0.01)
    assert white.contrast_ratio(black) == pytest.approx(21.0, abs=0.01)


def test_color_accepts_hash_and_lowercase():
    assert Color(rgb="#0077ff").rgb == "0077FF"


# ── TemplateSpec ──────────────────────────────────────────────────────────────

def test_pattern_referencing_unknown_layout_is_rejected():
    spec = fx.template_spec()
    broken = spec.model_copy(
        update={"patterns": [spec.patterns[0].model_copy(update={"layout_id": "нет-такого"})]}
    )
    with pytest.raises(ValidationError, match="неизвестный layout"):
        TemplateSpec.model_validate(broken.model_dump())


def test_type_scale_must_be_sorted_and_unique():
    data = fx.template_spec().model_dump()
    data["type_scale_pt"] = [18.0, 12.0, 12.0]
    with pytest.raises(ValidationError, match="отсортирована"):
        TemplateSpec.model_validate(data)


def test_pattern_without_anything_to_fill_is_rejected():
    with pytest.raises(ValidationError, match="нечем заполнять"):
        Pattern(
            id="empty",
            pattern_class=PatternClass.GRID,
            donor_slide_index=1,
            content_area=Box(x=0, y=0, w=10, h=10),
            provenance=fx.FROM_SLIDE,
        )


def test_repeater_capacity_bounds_are_consistent():
    item = fx.slot("i", SlotRole.BODY, Box(x=0, y=0, w=100, h=100))
    with pytest.raises(ValidationError, match="вне границ"):
        Repeater(
            id="r",
            item_slots=[item],
            item_box=Box(x=0, y=0, w=100, h=100),
            observed_count=9,
            min_count=1,
            max_count=4,
            pitch_emu=120,
            gutter_emu=20,
            provenance=fx.FROM_SLIDE,
        )


def test_pattern_capacity_counts_repeated_slots():
    """Ёмкость паттерна — то, по чему матчер выбирает макет под объём контента."""
    item = fx.slot("i", SlotRole.BODY, Box(x=0, y=0, w=100, h=100))
    pattern = Pattern(
        id="grid",
        pattern_class=PatternClass.GRID,
        donor_slide_index=21,
        slots=[fx.slot("t", SlotRole.TITLE, Box(x=0, y=0, w=100, h=50))],
        repeaters=[
            Repeater(
                id="cards",
                item_slots=[item],
                item_box=Box(x=0, y=0, w=100, h=100),
                observed_count=4,
                min_count=2,
                max_count=6,
                pitch_emu=120,
                gutter_emu=20,
                provenance=fx.FROM_SLIDE,
            )
        ],
        content_area=Box(x=0, y=0, w=1000, h=500),
        provenance=fx.FROM_SLIDE,
    )
    assert pattern.capacity == 1 + 4


def test_extracted_font_cannot_also_claim_a_substitution():
    with pytest.raises(ValidationError, match="подстановка не нужна"):
        FontToken(family="Play", usage_count=1, embedded=True, substituted_with="DejaVu Sans")


def test_fallback_provenance_cannot_be_fully_confident():
    with pytest.raises(ValidationError, match="confidence"):
        Provenance(kind=SourceKind.FALLBACK, confidence=1.0)


# ── ContentPack ───────────────────────────────────────────────────────────────

def test_fact_pointing_at_missing_document_is_rejected():
    """Без этого проверка «цифры со слайда есть в материалах» сверяла бы с пустотой."""
    data = fx.content_pack().model_dump()
    data["facts"].append(Fact(id="f9", text="что-то", source_doc_id="нет").model_dump())
    with pytest.raises(ValidationError, match="несуществующие документы"):
        ContentPack.model_validate(data)


# ── DeckPlan ──────────────────────────────────────────────────────────────────

def test_body_slide_without_blocks_is_rejected():
    """Слайд с одним заголовком — находка аудита; в план он попадать не должен."""
    with pytest.raises(ValidationError, match="без блоков"):
        SlidePlan(index=1, intent=SlideIntent.EVIDENCE, takeaway_title="Тема")


def test_title_and_closing_may_be_bare():
    for intent in (SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING):
        SlidePlan(index=1, intent=intent, takeaway_title="Заголовок")


def test_block_declared_as_series_must_name_one():
    with pytest.raises(ValidationError, match="рядов не указано"):
        ContentBlock(id="b", kind=BlockKind.SERIES, heading="Динамика")


def test_slide_indices_must_be_contiguous():
    data = fx.deck_plan().model_dump()
    data["slides"][1]["index"] = 7
    with pytest.raises(ValidationError, match=r"1\.\.N подряд"):
        DeckPlan.model_validate(data)


# ── SlideIR ───────────────────────────────────────────────────────────────────

def test_element_kind_must_match_its_payload():
    with pytest.raises(ValidationError, match="содержимого нет"):
        Element(
            id="e",
            kind=ElementKind.CHART,
            role=SlotRole.CHART,
            box=Box(x=0, y=0, w=10, h=10),
            provenance=fx.FROM_SLIDE,
        )


def test_duplicate_element_ids_are_rejected():
    """Аудит адресует находки по id элемента — дубликат сломал бы подсветку в UI."""
    deck = fx.deck_ir()
    slide = deck.slides[0]
    twin = slide.elements[0].model_copy()
    data = deck.model_dump()
    data["slides"][0]["elements"].append(twin.model_dump())
    with pytest.raises(ValidationError, match="повторяются id"):
        DeckIR.model_validate(data)


def test_chart_series_must_align_with_categories():
    with pytest.raises(ValidationError, match="числом значений"):
        ChartContent(
            chart_kind=ChartKind.COLUMN,
            categories=["Q1", "Q2", "Q3"],
            series=[ChartSeries(name="Выручка", values=[1.0, 2.0], color=fx.ACCENT)],
        )


def test_table_rows_must_match_header_width():
    with pytest.raises(ValidationError, match="по ширине заголовка"):
        TableContent(header=["Метрика", "Значение"], rows=[["Выручка"]])


def test_walk_returns_nested_elements():
    child = Element(
        id="child",
        kind=ElementKind.TEXT,
        role=SlotRole.BODY,
        box=Box(x=0, y=0, w=10, h=10),
        provenance=fx.FROM_SLIDE,
        text=fx.deck_ir().slides[0].elements[0].text,
    )
    group = Element(
        id="group",
        kind=ElementKind.GROUP,
        role=SlotRole.DECOR,
        box=Box(x=0, y=0, w=20, h=20),
        provenance=fx.FROM_SLIDE,
        children=[child],
    )
    assert [e.id for e in group.walk()] == ["group", "child"]


# ── Issue ─────────────────────────────────────────────────────────────────────

def test_deterministic_issue_cannot_be_uncertain():
    with pytest.raises(ValidationError, match="не может быть"):
        Issue(
            check_id="layout.out_of_bounds",
            kind=CheckKind.DETERMINISTIC,
            category=IssueCategory.LAYOUT,
            severity=Severity.ERROR,
            slide_index=1,
            message="элемент вышел за границы слайда",
            confidence=0.8,
        )


def test_contextual_issue_may_be_uncertain():
    issue = Issue(
        check_id="content.title_is_a_takeaway",
        kind=CheckKind.CONTEXTUAL,
        category=IssueCategory.CONTENT,
        severity=Severity.WARNING,
        slide_index=2,
        message="заголовок называет тему, а не содержит вывод",
        confidence=0.7,
    )
    assert issue.confidence == 0.7


def test_automatic_fix_must_name_an_action():
    from deckwright.schemas import ProposedFix

    with pytest.raises(ValidationError, match="без имени операции"):
        ProposedFix(kind=FixKind.AUTOMATIC, description="подвинуть логотип")


# ── RunManifest ───────────────────────────────────────────────────────────────

def test_manifest_reports_time_budget():
    from deckwright.schemas import StageTiming

    manifest = fx.run_manifest().model_copy(
        update={
            "timings": [
                StageTiming(stage="parse", seconds=1.5),
                StageTiming(stage="render", seconds=4.0),
            ]
        }
    )
    assert manifest.total_seconds == 5.5
    # Разбор входа в бюджет не входит (A18): сверяется только генерация.
    assert manifest.parse_seconds == 1.5
    assert manifest.generation_seconds == 4.0
    assert manifest.within_budget(300) is True
    assert manifest.within_budget(5) is True
    assert manifest.within_budget(3) is False


def test_run_summary_counts_input_parsing_in_the_budget():
    """300 с — на всю колоду: разбор входа входит в бюджет вместе с генерацией.

    Разбор входа теперь вызов модели (приведение любого входа к контент-
    пакету), и заказчик считает его частью времени на колоду.
    """
    from datetime import UTC, datetime

    from deckwright.schemas import RunSummary

    def summary(parse: float, generation: float) -> RunSummary:
        now = datetime.now(UTC)
        return RunSummary(
            run_id="r",
            template_name="t.pptx",
            started_at=now,
            finished_at=now,
            variant_seconds={"dense": 90.0, "balanced": 90.0, "airy": 90.0},
            parse_seconds=parse,
            generation_seconds=generation,
            total_seconds=parse + generation,
            budget_seconds=300,
        )

    assert summary(parse=40.0, generation=250.0).within_budget is True
    # Генерация в бюджете, но вместе с разбором входа — нет.
    assert summary(parse=120.0, generation=290.0).within_budget is False
    assert summary(parse=0.1, generation=301.0).within_budget is False
