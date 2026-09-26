"""Аудит: на каждую детерминированную проверку позитив и негатив.

Позитив — «на чистом материале находки нет»; негатив — «на подпорченном
находка есть». Только вторая половина доказывает, что проверка вообще
работает: проверка, которая всегда молчит, проходит позитивный тест идеально.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from deckwright.audit import fixers
from deckwright.audit.contextual.runner import Answer, SlideAnswers
from deckwright.audit.deterministic import content as content_checks
from deckwright.audit.deterministic import geometry, template_fidelity
from deckwright.audit.registry import BY_ID, CHECKS, check, deterministic_ids
from deckwright.audit.report import audit_deck
from deckwright.config import load_config
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import run_variant
from deckwright.schemas import (
    Box,
    CheckKind,
    ContentPack,
    FixKind,
    Severity,
)

CONFIG = "configs/config.yaml"
AUDIT_DOC = Path(__file__).resolve().parents[1] / "docs" / "AUDIT.md"


@pytest.fixture(scope="module")
def pack(content_pack_path):
    return ContentPack.model_validate(json.loads(content_pack_path.read_text("utf-8")))


@pytest.fixture(scope="module")
def clean(template_paths, pack, recorded_dir, tmp_path_factory):
    """Колода, собранная пайплайном: материал для позитивных проверок."""
    cfg = load_config(CONFIG)
    return run_variant(
        template_path=template_paths[0],
        pack=pack,
        cfg=cfg,
        client=RecordedClient(recorded_dir),
        variant="balanced",
        output_dir=tmp_path_factory.mktemp("audit"),
    )


# ── Реестр ───────────────────────────────────────────────────────────────────


def test_every_check_is_typed_and_documented():
    """A14: каждая проверка помечена типом. A13: и описана в AUDIT.md."""
    assert AUDIT_DOC.exists(), "docs/AUDIT.md не написан"
    doc = AUDIT_DOC.read_text("utf-8")
    for item in CHECKS:
        assert item.kind in (CheckKind.DETERMINISTIC, CheckKind.CONTEXTUAL)
        assert item.id in doc, f"проверка {item.id} не описана в AUDIT.md"


def test_audit_doc_invents_no_checks():
    """Документация не должна обещать проверок, которых нет в коде.

    Ищутся только имена в пространствах категорий: в тексте есть и параметры
    конфига вроде `audit.contextual_dpi`, и они проверками не являются.
    """
    import re

    from deckwright.schemas import IssueCategory

    prefixes = "|".join(category.value for category in IssueCategory)
    doc = AUDIT_DOC.read_text("utf-8")
    mentioned = set(re.findall(rf"`(({prefixes})\.[a-z0-9_]+)`", doc))
    unknown = {name for name, _ in mentioned if name not in BY_ID}
    assert not unknown, f"в AUDIT.md описаны несуществующие проверки: {sorted(unknown)}"


def test_contextual_findings_are_never_auto_fixed():
    """Ответ модели на повторе может отличаться — молча править по нему нельзя."""
    for item in CHECKS:
        if item.kind is CheckKind.CONTEXTUAL:
            assert item.fix is not FixKind.AUTOMATIC, item.id


def test_unknown_check_id_is_a_loud_error():
    with pytest.raises(KeyError, match="не объявлена"):
        check("layout.нет_такой")


# ── Позитив: на собранной колоде тихо там, где должно быть тихо ──────────────


def test_clean_deck_has_no_errors(clean, pack):
    """Колода, собранная пайплайном, не должна давать находок уровня ERROR.

    Предупреждения допустимы — это переполнение и поля, про которые вёрстка
    честно сообщила. Ошибка означает, что колоду нельзя отдавать.
    """
    cfg = load_config(CONFIG)
    report = audit_deck(
        clean.deck, clean.spec, clean.plan, pack, cfg,
        pptx_path=clean.pptx, pages=clean.pages, client=None,
    )
    errors = [i for i in report.issues if i.severity is Severity.ERROR]
    assert not errors, [f"{i.check_id}: {i.message}" for i in errors[:5]]


def test_every_finding_carries_what_a15_requires(clean, pack):
    """A15: id, тип, серьёзность, слайд, bbox, описание, исправление."""
    cfg = load_config(CONFIG)
    report = audit_deck(
        clean.deck, clean.spec, clean.plan, pack, cfg,
        pptx_path=clean.pptx, pages=clean.pages, client=None,
    )
    for issue in report.issues:
        assert issue.check_id in BY_ID, issue.check_id
        assert issue.slide_index >= 1
        assert issue.message.strip()
        assert issue.fix.kind in tuple(FixKind)
        if issue.fix.kind in (FixKind.AUTOMATIC, FixKind.ASSISTED):
            assert issue.fix.action, f"{issue.check_id}: исправление без операции"


def test_skipped_checks_say_what_was_not_asked(clean, pack):
    """Пустой список находок без модели означал бы «всё хорошо» — а вопрос не задан."""
    cfg = load_config(CONFIG)
    report = audit_deck(
        clean.deck, clean.spec, clean.plan, pack, cfg,
        pptx_path=clean.pptx, pages=clean.pages, client=None,
    )
    from deckwright.audit.registry import contextual_ids

    assert set(report.skipped_checks) >= set(contextual_ids()), (
        "контекстные проверки не выполнялись, но в отчёте об этом не сказано"
    )
    assert all(report.skipped_checks.values()), "причина пропуска не указана"


# ── Негатив: подпорченный материал обязан ловиться ───────────────────────────


def _first_text_element(deck):
    for slide in deck.slides:
        for element in slide.all_elements():
            if element.text is not None:
                return slide, element
    raise AssertionError("в колоде нет ни одного текстового элемента")


def test_out_of_bounds_is_caught(clean):
    slide, element = _first_text_element(clean.deck)
    original = element.box
    element.box = Box(x=original.x, y=clean.deck.slide_height_emu, w=original.w, h=original.h)
    try:
        found = geometry.out_of_bounds(slide, clean.deck)
        assert found and found[0].check_id == "layout.out_of_bounds"
        assert found[0].bbox is not None
    finally:
        element.box = original


def test_overlap_is_caught(clean):
    slide = next(
        s for s in clean.deck.slides
        if len([e for e in s.all_elements() if e.text is not None]) >= 2
    )
    texts = [e for e in slide.all_elements() if e.text is not None]
    original = texts[1].box
    texts[1].box = texts[0].box
    try:
        found = geometry.overlaps(slide)
        assert found and found[0].check_id == "layout.overlap"
    finally:
        texts[1].box = original


def test_margin_violation_is_caught(clean):
    if clean.spec.grid is None:
        pytest.skip("у шаблона нет полей")
    slide, element = _first_text_element(clean.deck)
    original = element.box
    element.box = Box(x=0, y=0, w=original.w, h=original.h)
    try:
        found = geometry.margins(slide, clean.deck, clean.spec)
        assert any(i.check_id == "layout.margin_violation" for i in found)
    finally:
        element.box = original


def test_stretched_image_is_caught(clean):
    from deckwright.schemas import (
        Element,
        ElementKind,
        ImageContent,
        Provenance,
        SlotRole,
        SourceKind,
    )

    slide = clean.deck.slides[0]
    stretched = Element(
        id="test_stretched",
        kind=ElementKind.IMAGE,
        role=SlotRole.IMAGE,
        box=Box(x=0, y=0, w=4_000_000, h=500_000),
        provenance=Provenance(kind=SourceKind.SLIDE),
        image=ImageContent(path="x.png", native_w=800, native_h=800),
    )
    slide.elements.append(stretched)
    try:
        found = geometry.stretched_images(slide)
        assert any(i.check_id == "layout.image_stretched" for i in found)
    finally:
        slide.elements.remove(stretched)


def _restyle(element, **style_updates):
    """Меняет стиль первого абзаца. Стили заморожены, поэтому через копию."""
    paragraph = element.text.paragraphs[0]
    element.text.paragraphs[0] = paragraph.model_copy(
        update={"style": paragraph.style.model_copy(update=style_updates)}
    )


def test_foreign_font_is_caught(clean):
    deck = clean.deck.model_copy(deep=True)
    slide, element = _first_text_element(deck)
    _restyle(element, font_family="Comic Sans MS")
    found = template_fidelity.fonts_and_sizes(slide, clean.spec)
    assert any(i.check_id == "template.font_not_in_template" for i in found)


def test_size_outside_the_scale_is_caught(clean):
    deck = clean.deck.model_copy(deep=True)
    slide, element = _first_text_element(deck)
    _restyle(element, size_pt=13.7371)
    found = template_fidelity.fonts_and_sizes(slide, clean.spec)
    assert any(i.check_id == "template.size_not_in_scale" for i in found)


def test_low_contrast_is_caught(clean):
    deck = clean.deck.model_copy(deep=True)
    slide = next((s for s in deck.slides if s.background is not None), None)
    if slide is None:
        pytest.skip("у колоды нет слайда с известным фоном")
    element = next(e for e in slide.all_elements() if e.text is not None)
    _restyle(element, color=slide.background)
    found = template_fidelity.contrast(slide)
    assert any(i.check_id == "template.low_contrast" for i in found)


def test_color_outside_the_palette_is_caught(clean):
    from deckwright.schemas import Color

    deck = clean.deck.model_copy(deep=True)
    slide, element = _first_text_element(deck)
    _restyle(element, color=Color(rgb="FF00FF"))
    found = template_fidelity.colors(slide, clean.spec)
    assert any(i.check_id == "template.color_not_in_palette" for i in found)


def test_off_grid_element_is_caught(clean):
    if clean.spec.grid is None or not clean.spec.grid.columns:
        pytest.skip("у шаблона нет направляющих")
    deck = clean.deck.model_copy(deep=True)
    slide, element = _first_text_element(deck)
    guides = sorted(set(clean.spec.grid.columns) | {clean.spec.grid.margin_left_emu})
    element.box = Box(x=guides[0] + 200_000, y=element.box.y, w=element.box.w, h=element.box.h)
    found = geometry.off_grid(slide, clean.spec)
    assert any(i.check_id == "layout.off_grid" for i in found)


def test_moved_logo_is_caught(clean):
    from deckwright.schemas import (
        Element,
        ElementKind,
        Provenance,
        RecurringElement,
        SourceKind,
    )
    from deckwright.schemas import SlotRole as Role

    anchor = RecurringElement(
        id="logo1",
        role=Role.LOGO,
        box=Box(x=100_000, y=100_000, w=500_000, h=300_000),
        frequency=1.0,
        provenance=Provenance(kind=SourceKind.SLIDE),
    )
    spec = clean.spec.model_copy(update={"recurring": [anchor]})
    deck = clean.deck.model_copy(deep=True)
    slide = deck.slides[0]
    slide.elements.append(
        Element(
            id="test_logo",
            kind=ElementKind.SHAPE,
            role=Role.LOGO,
            box=Box(x=5_000_000, y=3_000_000, w=500_000, h=300_000),
            provenance=Provenance(kind=SourceKind.SLIDE),
            shape={"preset": "rect"},
        )
    )
    found = template_fidelity.recurring_elements(slide, spec)
    assert any(i.check_id == "template.recurring_element_moved" for i in found)


def test_empty_slide_is_caught(clean, tmp_path):
    """Слайд без содержания: считается по собранному файлу, а не по IR."""
    from pptx import Presentation

    presentation = Presentation(str(clean.pptx))
    for slide in presentation.slides:
        for shape in list(slide.shapes):
            shape._element.getparent().remove(shape._element)
    bare = tmp_path / "bare.pptx"
    presentation.save(str(bare))

    found = content_checks.fill_ratio(bare, clean.deck)
    assert found and found[0].check_id == "density.slide_too_empty"


def test_broken_package_is_caught(clean, tmp_path, template_paths):
    """Битая связь: LibreOffice о ней молчит, PowerPoint требует восстановления.

    Ломаем так же, как это происходит по-настоящему: фигура с картинкой
    ссылается на связь, которой нет. Так выглядит клонирование без переноса
    rel'ов — структурно файл цел, а картинки нет.
    """
    import copy

    from pptx import Presentation

    presentation = Presentation(str(clean.pptx))
    picture = None
    donor = None
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.shape_type == 13:
                picture, donor = shape, slide
                break
        if picture is not None:
            break
    if picture is None:
        # В колоде картинок может не оказаться: вёрстка выбрала композиции
        # без них. Ломать можно и чужую — ссылка всё равно подменяется.
        picture = _template_picture(template_paths)
        donor = presentation.slides[0]
    if picture is None:
        pytest.skip("ни в колоде, ни в шаблонах нет картинок — нечем ломать")

    # Ссылка, которой заведомо нет ни на одном слайде: копирование фигуры
    # само по себе может «повезти» и попасть в существующий идентификатор.
    stray = copy.deepcopy(picture._element)
    for node in stray.iter():
        for attr in list(node.attrib):
            if attr.endswith("}embed") or attr.endswith("}link"):
                node.set(attr, "rIdЗаведомоНетТакой")
    donor.shapes._spTree.append(stray)
    broken = tmp_path / "broken.pptx"
    presentation.save(str(broken))

    found = content_checks.package(broken)
    assert any(i.check_id == "integrity.package_broken" for i in found)


def _template_picture(template_paths):
    """Первая картинка из шаблонов — материал, когда в колоде картинок нет."""
    from pptx import Presentation

    for path in template_paths:
        for slide in Presentation(str(path)).slides:
            for shape in slide.shapes:
                if shape.shape_type == 13:
                    return shape
    return None


def test_slide_that_is_one_picture_is_caught(clean, tmp_path, template_paths):
    """Слайд-картинка ТЗ не засчитывает."""
    from pptx import Presentation
    from pptx.util import Emu

    presentation = Presentation(str(clean.pptx))
    picture = None
    for slide in presentation.slides:
        for shape in slide.shapes:
            if shape.shape_type == 13:
                picture = shape.image.blob
                break
        if picture:
            break
    if picture is None:
        found = _template_picture(template_paths)
        picture = found.image.blob if found is not None else None
    if picture is None:
        pytest.skip("ни в колоде, ни в шаблонах нет картинок")

    import io

    slide = presentation.slides[0]
    for shape in list(slide.shapes):
        shape._element.getparent().remove(shape._element)
    slide.shapes.add_picture(io.BytesIO(picture), Emu(0), Emu(0), Emu(1_000_000), Emu(1_000_000))
    single = tmp_path / "single.pptx"
    presentation.save(str(single))

    found = content_checks.package(single)
    assert any(i.check_id == "integrity.slide_is_single_image" for i in found)


def test_cited_figure_that_is_not_in_sources_is_caught(clean, pack):
    """Цитируемое число сверяется с фактом, а не принимается на веру."""
    from deckwright.schemas import Figure, FigureKind

    plan = clean.plan.model_copy(deep=True)
    fact = next((f for f in pack.facts if f.value is not None), None)
    if fact is None:
        pytest.skip("в пакете нет числовых фактов")
    plan.slides[0].figures = [
        Figure(text="1234567", kind=FigureKind.CITED, fact_ids=[fact.id])
    ]
    found = content_checks.figures(plan, pack)
    assert any(i.check_id == "content.figure_not_in_sources" for i in found)


def test_unknown_layout_is_caught(clean):
    slide = clean.deck.slides[0]
    original = slide.layout_id
    slide.layout_id = "master9/layout99"
    try:
        found = template_fidelity.layout_reference(slide, clean.spec)
        assert found and found[0].check_id == "template.unknown_layout"
    finally:
        slide.layout_id = original


def test_too_many_bullets_is_caught(clean):
    slide, element = _first_text_element(clean.deck)
    original = list(element.text.paragraphs)
    element.text.paragraphs = [
        original[0].model_copy(update={"bullet": True, "text": f"пункт {i}"})
        for i in range(12)
    ]
    try:
        found = content_checks.density(slide, max_bullets=6, max_words=15)
        assert any(i.check_id == "density.too_many_bullets" for i in found)
    finally:
        element.text.paragraphs = original


def test_long_bullet_is_caught(clean):
    slide, element = _first_text_element(clean.deck)
    original = list(element.text.paragraphs)
    element.text.paragraphs = [
        original[0].model_copy(update={"text": " ".join(["слово"] * 30)})
    ]
    try:
        found = content_checks.density(slide, max_bullets=6, max_words=15)
        assert any(i.check_id == "density.bullet_too_long" for i in found)
    finally:
        element.text.paragraphs = original


def test_duplicate_slides_are_caught(clean):
    deck = clean.deck.model_copy(deep=True)
    if len(deck.slides) < 2:
        pytest.skip("в колоде один слайд")
    second = deck.slides[1]
    first = deck.slides[0]
    second.elements = [
        element.model_copy(deep=True, update={"id": f"{element.id}_copy"})
        for element in first.elements
    ]
    found = content_checks.duplicate_slides(deck)
    assert any(i.check_id == "integrity.duplicate_slides" for i in found)


def test_text_overflow_is_caught(clean):
    slide, element = _first_text_element(clean.deck)
    original = element.text.truncated
    element.text.truncated = True
    try:
        found = geometry.text_overflow(slide)
        assert found and found[0].check_id == "layout.text_overflow"
    finally:
        element.text.truncated = original


def test_wrong_derived_figure_is_caught(clean, pack):
    """Производное число пересчитывается, а не принимается на веру."""
    from deckwright.schemas import Figure, FigureKind

    plan = clean.plan.model_copy(deep=True)
    fact = next((f for f in pack.facts if f.value is not None), None)
    if fact is None:
        pytest.skip("в пакете нет числовых фактов")
    plan.slides[0].figures = [
        Figure(text="999", kind=FigureKind.DERIVED, fact_ids=[fact.id], formula=f"{fact.id} * 2")
    ]
    found = content_checks.figures(plan, pack)
    assert any(i.check_id == "content.derived_figure_wrong" for i in found)


# ── Фиксеры ──────────────────────────────────────────────────────────────────


def test_automatic_fix_moves_the_element_back_inside(clean):
    deck = clean.deck.model_copy(deep=True)
    slide = deck.slides[0]
    element = next(e for e in slide.all_elements() if e.text is not None)
    element.box = Box(x=deck.slide_width_emu, y=0, w=element.box.w, h=element.box.h)

    issues = geometry.out_of_bounds(slide, deck)
    outcome = fixers.apply(deck, issues, clean.spec)

    assert outcome.applied, "автоматическое исправление не применилось"
    assert element.box.right <= deck.slide_width_emu
    assert geometry.out_of_bounds(slide, deck) == []
    assert slide.index in outcome.changed_slides


def test_margin_fix_converges_for_an_element_wider_than_the_margins(clean):
    """Элемент шире области полей: одного сдвига мало, нужна подгонка размера.

    Так было на `vk_workspace`: график донора на всю ширину слайда. Сдвинутый
    к левому полю, он вылезал за правое, находка возвращалась, и правка
    «применялась» на каждой итерации впустую.
    """
    if clean.spec.grid is None:
        pytest.skip("у шаблона нет полей")
    deck = clean.deck.model_copy(deep=True)
    slide = deck.slides[0]
    element = next(
        e for e in slide.all_elements() if e.role not in geometry._MARGIN_EXEMPT
    )
    element.box = Box(x=0, y=element.box.y, w=deck.slide_width_emu, h=element.box.h)
    issues = [
        issue
        for issue in geometry.margins(slide, deck, clean.spec)
        if element.id in issue.element_ids
    ]
    assert issues, "проверка полей не заметила элемент на всю ширину"

    outcome = fixers.apply(deck, issues, clean.spec)

    assert outcome.applied
    left_over = [
        issue
        for issue in geometry.margins(slide, deck, clean.spec)
        if element.id in issue.element_ids
    ]
    assert left_over == [], "правка применена, а находка осталась"


def test_assisted_findings_are_left_to_the_human(clean):
    """Пользователь выбирает, что исправить: сокращать текст за него нельзя."""
    deck = clean.deck.model_copy(deep=True)
    slide, element = _first_text_element(deck)
    element.text.truncated = True
    issues = geometry.text_overflow(slide)
    outcome = fixers.apply(deck, issues, clean.spec)
    assert not outcome.applied
    assert outcome.skipped["layout.text_overflow"] == "требует решения человека"


# ── Контекстный проход ───────────────────────────────────────────────────────


def test_contextual_yes_produces_nothing_and_no_produces_a_finding():
    """Ответ «да» — не находка. Ответ «нет» — находка с уверенностью модели."""
    from deckwright.audit.contextual.runner import _to_issue

    assert _to_issue(Answer(check_id="content.has_content", passed=True), 1) is None

    issue = _to_issue(
        Answer(
            check_id="content.has_content",
            passed=False,
            reason="на слайде только заголовок",
            confidence=0.7,
        ),
        3,
    )
    assert issue is not None
    assert issue.kind is CheckKind.CONTEXTUAL
    assert issue.confidence == 0.7
    assert issue.fix.kind is FixKind.ASSISTED


def test_contextual_answer_about_an_unknown_check_is_ignored():
    """Модель может назвать вопрос, которого нет. Выдумывать под него паспорт нельзя."""
    from deckwright.audit.contextual.runner import _to_issue

    assert _to_issue(Answer(check_id="content.выдумка", passed=False), 1) is None


def test_contextual_pass_is_skipped_loudly_without_a_model(clean, pack):
    cfg = load_config(CONFIG)
    report = audit_deck(
        clean.deck, clean.spec, clean.plan, pack, cfg,
        pptx_path=clean.pptx, pages=None, client=None,
    )
    assert report.skipped_checks
    assert all("картин" in reason or "модел" in reason for reason in report.skipped_checks.values())


def test_every_deterministic_check_has_both_tests():
    """Готовность фазы: на каждую детерминированную проверку позитив и негатив.

    Позитив общий — «чистая колода без ошибок»; негатив обязан быть свой у
    каждой. Проверка, которая всегда молчит, позитивный тест проходит идеально.
    """
    source = Path(__file__).read_text("utf-8")
    missing = [
        check_id
        for check_id in deterministic_ids()
        if f'"{check_id}"' not in source
    ]
    assert not missing, f"нет негативного теста на: {missing}"


def test_slide_answers_schema_survives_a_terse_model():
    """Модель имеет право ответить коротко; схема не должна на этом падать."""
    parsed = SlideAnswers.model_validate(
        {"answers": [{"check_id": "content.has_content", "passed": True}]}
    )
    assert parsed.answers[0].confidence == 0.5


def test_text_pass_is_asked_once_for_the_whole_deck(
    template_paths, pack, recorded_dir, tmp_path_factory
):
    """Шесть вопросов по тексту задаются раз на колоду, а не раз на вариант.

    Содержание у трёх вариантов одно: опечатки, единый язык и происхождение
    чисел от вёрстки не зависят. По замеру текстовый проход — 12.2 с и 1.9
    тысячи токенов, и трижды это цена ни за что.
    """
    from deckwright.pipeline import run_variant

    class CountingVlm:
        """Считает вопросы по шагам. Отвечает «да» на всё: находки здесь не важны."""

        mocked = True

        def __init__(self) -> None:
            self.calls = 0
            self.steps: list[str] = []

        def complete(self, step, prompt, schema, images=None):
            self.calls += 1
            self.steps.append(step)
            return schema.model_validate({"answers": []})

    cfg = load_config(CONFIG)
    vlm = CountingVlm()
    root = tmp_path_factory.mktemp("text-pass")

    prepared = None
    text_findings = None
    slides_asked = 0
    for variant in ("dense", "balanced"):
        result = run_variant(
            template_path=template_paths[0],
            pack=pack,
            cfg=cfg,
            client=RecordedClient(recorded_dir),
            variant=variant,
            output_dir=root / variant,
            vlm_client=vlm,
            prepared=prepared,
            text_findings=text_findings,
        )
        prepared = result.prepared
        text_findings = result.text_findings
        # Число слайдов у вариантов бывает разным: фиттер вправе разбить
        # переполненный слайд надвое. Считаем по факту, а не по последнему.
        slides_asked += len(result.deck.slides)

    assert vlm.steps.count("audit_deck") == 1, (
        f"проход по тексту задан {vlm.steps.count('audit_deck')} раза: "
        "он не зависит от варианта вёрстки"
    )
    # Картиночный проход, наоборот, обязан идти по каждому варианту: вёрстка
    # у них разная, и видно это только на картинке.
    assert vlm.steps.count("audit_slide") == slides_asked


def test_identical_findings_are_collapsed(clean, pack):
    """Неотличимые находки схлопываются: иначе по номеру нельзя выбрать.

    Проверка цвета обходит абзацы, и элемент из пяти абзацев одного цвета
    давал пять находок с одинаковым текстом, рамкой и ключом. В интерфейсе это
    пять рамок с номером 5 — нашлось живым прогоном страницы, а не рассуждением.
    """
    report = audit_deck(
        clean.deck,
        clean.spec,
        clean.plan,
        pack,
        load_config(CONFIG),
        pptx_path=clean.pptx,
        pages=None,
    )
    marks = [
        (issue.check_id, issue.slide_index, tuple(issue.element_ids), issue.message)
        for issue in report.issues
    ]
    assert len(marks) == len(set(marks)), "в отчёте остались неотличимые находки"

    # Ключ находки обязан быть единственным: по нему человек выбирает, что
    # чинить, и два одинаковых ключа означают выбор наугад.
    keys = [issue.key for issue in report.issues]
    assert len(keys) == len(set(keys)), "ключи находок повторяются"


def test_text_without_a_place_on_the_cover_is_a_finding(tmp_path):
    """Обложка шаблона — только заголовок; абзац титула некуда положить.

    Класть его в свободную полосу поверх оформления или в подпись спикера
    нельзя: находка `"layout.text_without_place"`, текст не вёрстан.
    """
    import json

    from pptx import Presentation as NewPresentation

    from deckwright.layout.matcher import build_deck_ir
    from deckwright.parse.opener import parse_template
    from deckwright.schemas import DeckPlan

    deck = NewPresentation()
    cover = deck.slides.add_slide(deck.slide_layouts[5])
    cover.shapes.title.text = "Название"
    cover.shapes.title.top = deck.slide_height // 3
    content = deck.slides.add_slide(deck.slide_layouts[1])
    content.shapes.title.text = "Заголовок"
    content.placeholders[1].text_frame.text = "Текст"
    path = tmp_path / "cover_only_title.pptx"
    deck.save(path)
    spec = parse_template(path)
    assert spec.cover_pattern_id is not None

    raw = json.loads(Path("tests/fixtures/recorded/plan_deck.json").read_text("utf-8"))
    raw["slides"] = raw["slides"][:1]
    raw["slides"][0]["blocks"] = [{"id": "p", "kind": "paragraph", "items": ["Подзаголовок"]}]
    plan = DeckPlan.model_validate(raw)

    built, issues = build_deck_ir(spec, plan, "balanced")
    assert [i.check_id for i in issues] == ["layout.text_without_place"]
    assert all(e.role.value == "title" for e in built.slides[0].elements if e.text)


def test_donor_data_left_in_the_deck_is_a_finding(clean, tmp_path):
    """Рыбный график и чужое число в собранном файле — находка.

    `"integrity.donor_data_leftover"`: в IR этих фигур нет, они приезжают
    клонированием донора, и видны только в самом `.pptx`.
    """
    from pptx import Presentation as Open
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Emu

    from deckwright.audit.deterministic.content import donor_data

    assert donor_data(clean.pptx, clean.deck) == []

    presentation = Open(str(clean.pptx))
    slide = presentation.slides[1]
    data = CategoryChartData()
    data.categories = ["Категория 1", "Категория 2"]
    data.add_series("Ряд 1", (1, 2))
    inch = 914400
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Emu(inch), Emu(inch), Emu(3 * inch), Emu(2 * inch), data
    )
    number = slide.shapes.add_textbox(Emu(5 * inch), Emu(inch), Emu(inch), Emu(inch))
    number.text_frame.text = "10%"
    spoiled = tmp_path / "spoiled.pptx"
    presentation.save(spoiled)

    found = donor_data(spoiled, clean.deck)
    assert {issue.check_id for issue in found} == {"integrity.donor_data_leftover"}
    assert len(found) == 2 and all(issue.slide_index == 2 for issue in found)


def test_donor_picture_with_its_figure_is_a_finding(clean, tmp_path):
    """Кольцо «10%» без числа — всё ещё чужая доля: картинка донора с числом."""
    from PIL import Image
    from pptx import Presentation as Open
    from pptx.util import Emu

    from deckwright.audit.deterministic.content import donor_data
    from deckwright.schemas import (
        Box,
        DeckIR,
        Pattern,
        PatternClass,
        Provenance,
        Slot,
        SlotRole,
        SourceKind,
        TemplateSpec,
    )

    inch = 914400
    ring = Box(x=inch, y=inch, w=2 * inch, h=2 * inch)
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    pattern = Pattern(
        id="ring", pattern_class=PatternClass.GRID, donor_slide_index=1,
        slots=[Slot(id="t", role=SlotRole.TITLE, box=Box(x=0, y=0, w=inch, h=inch),
                    provenance=here)],
        content_area=ring, figure_pictures=[ring], provenance=here,
    )
    spec = TemplateSpec.model_construct(patterns=[pattern])
    deck = DeckIR.model_validate(clean.deck.model_dump())
    deck.slides[1].pattern_id = "ring"

    picture = tmp_path / "ring.png"
    Image.new("RGB", (8, 8), "#0077FF").save(picture)
    presentation = Open(str(clean.pptx))
    presentation.slides[1].shapes.add_picture(
        str(picture), Emu(ring.x), Emu(ring.y), Emu(ring.w), Emu(ring.h)
    )
    spoiled = tmp_path / "ring.pptx"
    presentation.save(spoiled)

    found = donor_data(spoiled, deck, spec)
    assert [issue.slide_index for issue in found] == [2]
    assert found[0].check_id == "integrity.donor_data_leftover"


def test_large_text_needs_three_to_one_and_the_templates_own_pair_is_accepted(clean):
    """Крупному тексту WCAG требует 3:1; пара, которой пишет сам шаблон, — тоже.

    Белый по фирменному синему `vk_education` — 4.4:1: мелкий текст не
    проходит 4.5, но шаблон пишет им сам, и это решение бренда — цвет
    остаётся, аудит показывает предупреждение. Пара, которой в шаблоне нет,
    при том же контрасте — ошибка.
    """
    from deckwright.schemas import Color, required_contrast

    assert required_contrast(18.0) == pytest.approx(3.0)
    assert required_contrast(14.0, bold=True) == pytest.approx(3.0)
    assert required_contrast(12.0) == 4.5

    deck = clean.deck.model_copy(deep=True)
    slide = deck.slides[1]
    slide.background = Color(rgb="0077FF")
    element = next(e for e in slide.all_elements() if e.text is not None)
    element.backdrop = None
    _restyle(element, color=Color(rgb="FFFFFF"), size_pt=12.0)

    class Writes:
        def __init__(self, answer: bool):
            self.answer = answer

        def writes_on(self, text, backdrop) -> bool:
            return self.answer

    def found(spec) -> list[str]:
        return [
            issue.check_id
            for issue in template_fidelity.contrast(slide, spec=spec)
            if element.id in issue.element_ids
        ]

    # Не пара шаблона — мелкий текст строго 4.5: ошибка.
    assert found(Writes(False)) == ["template.low_contrast"]
    # Пара шаблона — цвет остаётся, но случай не пропускается молча:
    # предупреждение с числом, одно на элемент.
    assert found(Writes(True)) == ["template.brand_pair_contrast"]
    warning = next(
        issue for issue in template_fidelity.contrast(slide, spec=Writes(True))
        if element.id in issue.element_ids
    )
    assert "пара из шаблона" in warning.message and ":1" in warning.message
    assert warning.severity.value == "warning"
    # Крупный текст: 3:1 по WCAG, находки нет.
    _restyle(element, color=Color(rgb="FFFFFF"), size_pt=24.0)
    assert found(Writes(False)) == []


def test_text_over_the_templates_line_is_caught():
    """Графика донора в IR не попадает — проверка берёт её из разбора шаблона.

    Линия под цифрой `vk_tech` оставалась под нашим текстом, и
    `layout.overlap`, сравнивающий только наши элементы, молчал. Полоса
    текста — по строкам, кеглю и привязке: рамка, прижатая к низу, пуста
    сверху, и линия там — не наложение.
    """
    from deckwright.audit.deterministic import geometry
    from deckwright.schemas import (
        Color,
        Element,
        ElementKind,
        Paragraph,
        Pattern,
        PatternClass,
        Provenance,
        SlideIR,
        Slot,
        SlotRole,
        SourceKind,
        TemplateSpec,
        TextContent,
        TextStyle,
        VAlign,
    )

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    frame = Box(x=inch, y=inch, w=4 * inch, h=2 * inch)

    def slide(valign: VAlign) -> SlideIR:
        style = TextStyle(font_family="Arial", size_pt=20, color=Color(rgb="000000"),
                          valign=valign)
        text = TextContent(paragraphs=[Paragraph(text="42 минуты", style=style)], used_lines=1)
        element = Element(id="e", kind=ElementKind.TEXT, role=SlotRole.BODY, box=frame,
                          provenance=here, text=text)
        return SlideIR(index=2, pattern_id="p", elements=[element])

    def spec(line: Box) -> TemplateSpec:
        slot = Slot(id="s", role=SlotRole.BODY, box=frame, provenance=here)
        pattern = Pattern(id="p", pattern_class=PatternClass.KPI_ROW, donor_slide_index=1,
                          slots=[slot], content_area=frame, decor=[line], provenance=here)
        return TemplateSpec(template_sha256="0" * 64, source_name="t", slide_width_emu=10 * inch,
                            slide_height_emu=6 * inch, patterns=[pattern])

    top_line = Box(x=inch, y=int(1.2 * inch), w=3 * inch, h=9144)
    found = geometry.text_over_decor(slide(VAlign.TOP), spec(top_line))
    assert [issue.check_id for issue in found] == ["layout.text_over_decor"]
    # Текст прижат к низу рамки: линия вверху пустой части — не наложение.
    assert geometry.text_over_decor(slide(VAlign.BOTTOM), spec(top_line)) == []
    # Линия ниже рамки — тоже.
    below = top_line.model_copy(update={"y": frame.bottom + inch // 10})
    assert geometry.text_over_decor(slide(VAlign.TOP), spec(below)) == []


def test_number_written_on_a_slide_but_absent_from_the_input_is_caught(pack):
    """Необъявленное число в пункте, которого во входе нет, — ошибка."""
    from pathlib import Path

    from deckwright.audit.deterministic.content import undeclared_numbers
    from deckwright.schemas import DeckPlan

    plan = DeckPlan.model_validate(
        json.loads(
            (Path(__file__).parent / "fixtures" / "recorded" / "plan_deck.json").read_text("utf-8")
        )
    )
    plan.slides[2].blocks[0].items.append("Рост выручки на 37 % за год")
    found = undeclared_numbers(plan, pack)
    assert [issue.check_id for issue in found] == ["content.undeclared_number"]
