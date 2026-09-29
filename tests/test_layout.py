"""Вёрстка: выбор композиции, закон фиттера и три варианта из одного кода.

Проверяется то, что ломается молча. Слайд с текстом, уехавшим за край, всё
равно соберётся, откроется и пройдёт проверку целостности — увидеть это можно
только измерением.
"""

from __future__ import annotations

import json
import sys
import typing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from make_holdout import build_all

from deckwright.config import load_config
from deckwright.layout.fitter import fit_paragraphs, fit_size, split_blocks
from deckwright.layout.matcher import (
    MIN_CONTRAST,
    _overlaps,
    build_deck_ir,
    needed_profile,
)
from deckwright.layout.strategy import (
    OverflowAction,
    Strategy,
    ladder_for_role,
    role_floor,
    scale_ladder,
)
from deckwright.layout.text_metrics import metrics_for_spec
from deckwright.parse.opener import parse_template
from deckwright.schemas import Box, Color, DeckPlan, SlotRole, required_contrast

CONFIG = "configs/config.yaml"
VARIANTS = ("airy", "balanced", "dense")
REAL_HOLDOUT_DIR = Path(__file__).resolve().parents[1] / "data" / "holdout"


@pytest.fixture(scope="module")
def plan() -> DeckPlan:
    path = Path(__file__).parent / "fixtures" / "recorded" / "plan_deck.json"
    return DeckPlan.model_validate(json.loads(path.read_text("utf-8")))


@pytest.fixture(scope="module")
def specs(tmp_path_factory, template_paths):
    """Все шаблоны, до каких дотянулись: синтетика, датасет и чужой."""
    paths = list(template_paths)
    paths += sorted(build_all(tmp_path_factory.mktemp("layout-holdout")))
    if REAL_HOLDOUT_DIR.is_dir():
        paths += sorted(REAL_HOLDOUT_DIR.glob("*.pptx"))
    return [(path.stem, parse_template(path)) for path in paths]


# ── Готовность фазы ──────────────────────────────────────────────────────────


def test_nothing_lands_outside_the_slide(specs, plan):
    """Критерий готовности фазы: ни один элемент не выходит за слайд.

    Пятая карточка повторителя уезжала на полтора дюйма за правый край:
    `max_count` — оценка вместимости сетки, и она бывает щедрее слайда.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    box = element.box
                    assert box.x >= 0 and box.y >= 0, f"{name}/{variant}: {element.id}"
                    assert box.right <= deck.slide_width_emu, (
                        f"{name}/{variant}: {element.id} уехал за правый край на "
                        f"{(box.right - deck.slide_width_emu) / 914400:.2f} дюйма"
                    )
                    assert box.bottom <= deck.slide_height_emu, (
                        f"{name}/{variant}: {element.id} уехал за нижний край на "
                        f"{(box.bottom - deck.slide_height_emu) / 914400:.2f} дюйма"
                    )


def test_every_size_comes_from_the_template_scale(specs, plan):
    """Кегль — только ступень шкалы шаблона.

    Промежуточное значение проверка «кегль не из типографической шкалы»
    найдёт справедливо, и чинить придётся то же самое, но позже и дороже.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        ladder = set(scale_ladder(spec))
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            used = {
                paragraph.style.size_pt
                for slide in deck.slides
                for element in slide.all_elements()
                if element.text is not None
                for paragraph in element.text.paragraphs
            }
            assert used <= ladder, f"{name}/{variant}: не из шкалы {sorted(used - ladder)}"


def test_the_three_variants_are_actually_different(specs, plan):
    """Три варианта — это параметр, а не три ветки кода. Но и не один результат."""
    cfg = load_config(CONFIG)
    differing = 0
    for _, spec in specs:
        shapes = set()
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            sizes = tuple(
                sorted(
                    {
                        paragraph.style.size_pt
                        for slide in deck.slides
                        for element in slide.all_elements()
                        if element.text is not None
                        for paragraph in element.text.paragraphs
                    }
                )
            )
            shapes.add(
                (
                    len(deck.slides),
                    tuple(sorted(s.pattern_id or s.layout_id or "" for s in deck.slides)),
                    sizes,
                )
            )
        if len(shapes) > 1:
            differing += 1
    assert differing, "все варианты дали одинаковую колоду на каждом шаблоне"


def test_layout_uses_more_than_one_composition(specs, plan):
    """Колода из десяти одинаковых слайдов формально верна и бесполезна."""
    cfg = load_config(CONFIG)
    rich = [
        (name, spec) for name, spec in specs if len(spec.patterns) >= len(VARIANTS) * 2
    ]
    if not rich:
        pytest.skip("нет шаблона, у которого композиций заметно больше числа вариантов")
    for name, spec in rich:
        deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"))
        used = {slide.pattern_id for slide in deck.slides if slide.pattern_id}
        assert len(used) > 1, f"{name}: вся колода легла в одну композицию {used}"


# ── Закон фиттера ────────────────────────────────────────────────────────────


def test_fitter_steps_down_the_ladder_and_stops_at_its_bottom():
    """Шаг вниз — только по ступеням, и ниже последней фиттер не идёт."""
    metrics = _metrics()
    ladder = [10.0, 14.0, 18.0, 24.0]
    tight = Box(x=0, y=0, w=914_400, h=300_000)

    result = fit_size("Короткий", metrics, Box(x=0, y=0, w=4_572_000, h=1_828_800),
                      ladder, 24.0)
    assert result.fits
    assert result.size_pt in ladder

    huge = " ".join(["длинноеслово"] * 60)
    result = fit_size(huge, metrics, tight, ladder, 24.0)
    assert not result.fits, "в рамку размером с марку такой текст влезть не может"
    assert result.size_pt == min(ladder), "фиттер обязан остановиться на нижней ступени"
    assert result.overflow_emu > 0, "переполнение обязано быть измерено, а не угадано"


def test_fitter_never_rises_above_the_size_the_template_uses():
    """Выше стартового кегля подниматься нельзя: это уже не типографика шаблона."""
    metrics = _metrics()
    ladder = [10.0, 14.0, 18.0, 24.0, 40.0]
    roomy = Box(x=0, y=0, w=9_144_000, h=4_572_000)
    result = fit_size("Два слова", metrics, roomy, ladder, 14.0)
    assert result.size_pt <= 14.0


def test_paragraphs_of_one_list_share_a_single_size():
    """Разный кегль у соседних пунктов списка — авария, а не вёрстка."""
    metrics = _metrics()
    ladder = [8.0, 12.0, 16.0, 20.0]
    box = Box(x=0, y=0, w=4_572_000, h=1_371_600)
    lines = ["Первый пункт", "Второй пункт подлиннее", "Третий"]
    result = fit_paragraphs(lines, metrics, box, ladder, 20.0)
    assert result.size_pt in ladder
    # То же содержание одной строкой не должно требовать более крупного кегля.
    assert result.size_pt <= fit_size(lines[0], metrics, box, ladder, 20.0).size_pt


def test_overflow_at_the_bottom_step_becomes_a_finding(specs, plan):
    """Четвёртый шаг закона: молча обрезанный текст хуже честно помеченного."""
    cfg = load_config(CONFIG)
    found = []
    for _, spec in specs:
        for variant in VARIANTS:
            _, issues = build_deck_ir(spec, plan, cfg.variant(variant))
            found.extend(issues)
    if not found:
        pytest.skip("ни на одном шаблоне текст не переполнился — проверять нечего")
    for issue in found:
        if issue.check_id != "layout.text_overflow":
            continue
        assert issue.fix.action, "находка без предложенного исправления бесполезна"
        assert issue.bbox is not None, "UI рисует рамку вокруг находки — bbox обязателен"


def test_splitting_never_tears_a_block_in_half():
    """Блок — смысловая единица: половина списка на слайде хуже, чем два слайда."""
    blocks = ["a", "b", "c", "d", "e"]
    parts = split_blocks(blocks)
    assert sum(len(part) for part in parts) == len(blocks)
    assert [item for part in parts for item in part] == blocks
    # Делить нечего, когда блок один.
    assert split_blocks(["a"]) == [["a"]]


# ── Стратегия ────────────────────────────────────────────────────────────────


def test_role_floor_comes_from_the_template_not_from_the_deck_wide_scale(specs):
    """Предел «мельче нельзя» — свой у каждой роли.

    Шкала целиком для этого не годится: она вбирает декор. У `vk_tech` в ней
    есть ступень 4.14 pt, и фиттер, честно спустившийся до неё, выдал бы
    нечитаемый слайд, формально соблюдя закон.
    """
    for name, spec in specs:
        floor = role_floor(spec, SlotRole.BODY)
        if floor <= 0:
            continue
        ladder = ladder_for_role(spec, SlotRole.BODY)
        assert min(ladder) >= floor, f"{name}: лестница уходит ниже предела роли"
        assert floor in set(scale_ladder(spec)), (
            f"{name}: предел роли должен быть ступенью шкалы шаблона"
        )


def test_data_mode_decides_what_a_series_becomes():
    """Один и тот же ряд у плотного варианта — таблица, у воздушного — число."""
    cfg = load_config(CONFIG)
    dense = Strategy.from_config(cfg.variant("dense"))
    airy = Strategy.from_config(cfg.variant("airy"))
    assert dense.data_viz_mode != airy.data_viz_mode
    assert dense.on_overflow in tuple(OverflowAction)


def test_signature_counts_the_title_and_every_block(plan):
    """Сигнатура слайда строится из плана, а не из текста."""
    cfg = load_config(CONFIG)
    strategy = Strategy.from_config(cfg.variant("balanced"))
    slide = plan.slides[0]
    profile = needed_profile(slide, strategy)
    assert profile[SlotRole.TITLE] == 1
    assert sum(profile.values()) == 1 + len(slide.blocks)


def _metrics():
    """Метрики какого-нибудь настоящего шрифта: тесты меряют, а не гадают."""

    class _Spec:
        fonts: typing.ClassVar[list] = []

    source = metrics_for_spec(_Spec())
    if source.metrics is None:
        pytest.skip("в системе нет ни одного шрифта: мерить нечем")
    return source.metrics


def test_no_two_texts_land_on_top_of_each_other(specs, plan):
    """Композиция снимается со слайда-примера, и её слоты там перекрываются.

    Положить в такие два разных текста — выдать кашу: на `zelenie_investicii`
    выходило до шести наложений на колоду, на `theme_only` — шесть.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                texts = [e for e in slide.all_elements() if e.text is not None]
                for first in range(len(texts)):
                    for second in range(first + 1, len(texts)):
                        a, b = texts[first], texts[second]
                        assert not _overlaps(a.box, b.box), (
                            f"{name}/{variant}: слайд {slide.index}, "
                            f"{a.id} налез на {b.id}"
                        )


def test_text_is_readable_on_the_background_it_lands_on(specs, plan):
    """Цвет шаблона проверяется контрастом, а не принимается на веру.

    Композиция приезжает со светлого слайда-примера на тёмный фон layout'а, и
    чёрный текст шаблона оказывается чёрным по чёрному. Это законно разные
    слайды, и поймать расхождение можно только измерением.
    """
    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    # Фон, на котором текст лежит: подложка донора, если она
                    # есть (карточка, тёмная панель), иначе фон слайда.
                    backdrop = element.backdrop or slide.background
                    if element.text is None or backdrop is None:
                        continue
                    # Бывает подложка, на которой порога не даёт никакой
                    # цвет: на фиолетовой карточке синтетического тёмного
                    # шаблона у белого 4.35, у чёрного 4.4. Тогда требуется
                    # лучший достижимый контраст, а не невозможный.
                    best = max(
                        Color(rgb=rgb).contrast_ratio(backdrop) for rgb in ("FFFFFF", "111111")
                    )
                    for paragraph in element.text.paragraphs:
                        # Порог — по кеглю (крупному тексту WCAG хватает 3:1)
                        # и 3:1 для пары, которой пишет сам шаблон.
                        style = paragraph.style
                        needed = required_contrast(style.size_pt, style.bold, MIN_CONTRAST)
                        if spec.writes_on(style.color, backdrop):
                            needed = min(needed, required_contrast(18.0, False, MIN_CONTRAST))
                        reachable = min(needed, best)
                        ratio = style.color.contrast_ratio(backdrop)
                        assert ratio >= reachable - 0.01, (
                            f"{name}/{variant}: слайд {slide.index}, {element.id} — "
                            f"контраст {ratio:.2f} при достижимом {reachable:.2f}"
                        )


def test_deck_is_set_in_the_templates_own_colours(template_paths):
    """Цвет текста берётся из палитры шаблона, а не придумывается.

    Раньше, когда цвет донора на нашем фоне не читался (или шаблон не сказал
    цвета вовсе), подставлялся `#111111` — цвет, которого в шаблоне нет. Он
    давал 24 находки `template.color_not_in_palette` из 28 на holdout и 30 из
    30 на синтетическом шаблоне, хотя в палитре обоих лежит `#000000` с ролью
    `text`. Аудит был прав, а виновата была вёрстка.
    """
    import json
    from pathlib import Path

    from deckwright.config import load_config
    from deckwright.llm.fake import RecordedClient
    from deckwright.pipeline import run_variant
    from deckwright.schemas import ContentPack

    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "configs" / "config.yaml")
    pack = ContentPack.model_validate(
        json.loads((root / "tests" / "fixtures" / "content_pack.json").read_text("utf-8"))
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = run_variant(
            template_path=template_paths[0],
            pack=pack,
            cfg=cfg,
            client=RecordedClient(root / "tests" / "fixtures" / "recorded"),
            variant="balanced",
            output_dir=tmp,
            fix_mode="review",
        )

    palette = {token.color.rgb for token in result.spec.palette}
    invented = {
        paragraph.style.color.rgb
        for slide in result.deck.slides
        for element in slide.all_elements()
        if element.text
        for paragraph in element.text.paragraphs
        if paragraph.style.color.rgb not in palette
    }
    assert not invented, (
        f"колода набрана цветами, которых в шаблоне нет: {sorted(invented)}; "
        f"палитра шаблона: {sorted(palette)}"
    )


# ── Ёмкость композиции (шаг 1: бюджет длины по макету) ──────────────────────


@pytest.fixture(scope="module")
def live_plan() -> DeckPlan:
    """Живой план из LLM probe #12 на `vk_tech`: то, что увидит жюри."""
    path = Path(__file__).parent / "fixtures" / "recorded_live" / "plan_deck.json"
    return DeckPlan.model_validate(json.loads(path.read_text("utf-8")))


def _overflowing(issues) -> set[int]:
    return {issue.slide_index for issue in issues if issue.check_id == "layout.text_overflow"}


def test_composition_is_chosen_where_the_text_fits(specs, live_plan):
    """Если в шаблоне есть композиция, куда слайд влезает, он туда и ложится.

    До этого выбор мерил только заголовок: на живом плане #12 повестка из
    пяти пунктов шла в рамку на две строки, хотя рядом были рамки на пять —
    37 переполненных слайдов из 90 на трёх шаблонах датасета.
    """
    from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots

    cfg = load_config(CONFIG)
    for name, spec in specs:
        metrics = metrics_for_spec(spec).metrics
        ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
        for variant in VARIANTS:
            preset = cfg.variant(variant)
            strategy = Strategy.from_config(preset)
            deck, issues = build_deck_ir(spec, live_plan, preset)
            if len(deck.slides) != len(live_plan.slides):
                continue  # деление сдвинуло номера — сверять не с чем
            over = _overflowing(issues)
            for slide in live_plan.slides:
                could_fit = any(
                    _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
                    and _title_fits(pattern, slide, strategy, metrics, ladders[SlotRole.TITLE])
                    and _blocks_fit(pattern, spec, slide, strategy, metrics, ladders)
                    for pattern in spec.patterns
                )
                if could_fit:
                    assert slide.index not in over, (
                        f"{name}/{variant}: слайд {slide.index} переполнен, "
                        "хотя в шаблоне есть композиция, куда он влезает"
                    )


def test_chart_never_lands_in_a_caption_sized_frame(specs, live_plan):
    """График в рамке подписи «влезает» по тексту и не виден на слайде."""
    from deckwright.schemas import ElementKind

    cfg = load_config(CONFIG)
    for name, spec in specs:
        for variant in VARIANTS:
            deck, _ = build_deck_ir(spec, live_plan, cfg.variant(variant))
            for slide in deck.slides:
                for element in slide.all_elements():
                    if element.kind is ElementKind.CHART:
                        assert element.box.h >= deck.slide_height_emu * 0.25, (
                            f"{name}/{variant}: график на слайде {slide.index} "
                            f"высотой {element.box.h / 914400:.2f} дюйма"
                        )


def test_variants_avoid_each_others_compositions(specs, live_plan):
    """A12: общий реестр не даёт вариантам съехаться на одни композиции.

    Сравнение — с независимой сборкой тех же вариантов: с реестром
    совпадений на тех же местах не больше, чем без него.
    """
    cfg = load_config(CONFIG)

    def same_places(spec, siblings) -> int:
        decks = [
            build_deck_ir(spec, live_plan, cfg.variant(v), siblings=siblings)[0]
            for v in VARIANTS
        ]
        ids = [[s.pattern_id for s in deck.slides] for deck in decks]
        return sum(
            1
            for a in range(len(ids))
            for b in range(a + 1, len(ids))
            for x, y in zip(ids[a], ids[b], strict=False)
            if x == y
        )

    for name, spec in specs:
        assert same_places(spec, {}) <= same_places(spec, None), name


def test_rebuild_keeps_its_own_compositions(specs, live_plan):
    """Пересборка в цикле исправления не избегает собственного выбора."""
    cfg = load_config(CONFIG)
    for name, spec in specs:
        siblings: dict = {}
        first = [
            build_deck_ir(spec, live_plan, cfg.variant(v), siblings=siblings)[0]
            for v in VARIANTS
        ]
        again, _ = build_deck_ir(spec, live_plan, cfg.variant(VARIANTS[-1]), siblings=siblings)
        assert [s.pattern_id for s in again.slides] == [
            s.pattern_id for s in first[-1].slides
        ], name


def test_word_wider_than_the_line_does_not_fit():
    """Рендер режет такое слово посередине — «обнаруже / ния»."""
    metrics = _metrics()
    word = "обнаружения"
    size = 24.0
    width = metrics.width_emu(word, size)
    box = Box(x=0, y=0, w=int(width * 0.8), h=914400 * 5)
    assert not fit_size(word, metrics, box, [size], size).fits
    assert not fit_paragraphs([word], metrics, box, [size], size).fits


def test_lists_are_read_at_the_body_size_of_the_template(specs):
    """У списков нет своих слотов — читаться они обязаны как тело текста."""
    from deckwright.layout.strategy import role_typical

    for name, spec in specs:
        if role_typical(spec, SlotRole.BODY):
            assert role_typical(spec, SlotRole.BULLETS) > 0, name


# ── Шаг 2: ёмкость для планировщика ─────────────────────────────────────────


def test_declared_capacity_really_fits_in_every_variant(specs):
    """Каждая точка кривой ёмкости обязана укладываться во всех трёх вариантах.

    И кривая монотонна: больше пунктов — пункт не длиннее.
    """
    from deckwright.layout.capacity import _probe, _text, achievable
    from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots

    cfg = load_config(CONFIG)
    strategies = [Strategy.from_config(cfg.variant(v)) for v in VARIANTS]
    for name, spec in specs:
        metrics = metrics_for_spec(spec).metrics
        if metrics is None:
            continue
        ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
        capacity = achievable(spec, strategies, item_chars=34, title_chars=34, max_items=6)
        for kind, curve in capacity.items():
            assert all(point.items <= 6 for point in curve), f"{name}/{kind}: больше порога ТЗ"
            lengths = [point.chars for point in curve]
            assert lengths == sorted(lengths, reverse=True), f"{name}/{kind}: {curve}"
            for point in curve:
                slide = _probe(kind, point.items, point.chars, _text(24))
                for strategy in strategies:
                    assert any(
                        _usable_slots(p, spec.slide_width_emu, spec.slide_height_emu)
                        and _title_fits(p, slide, strategy, metrics, ladders[SlotRole.TITLE])
                        and _blocks_fit(p, spec, slide, strategy, metrics, ladders)
                        for p in spec.patterns
                    ), f"{name}/{strategy.name}: {kind} {point.items}×{point.chars} не влезает"


def test_planner_is_told_the_capacity_not_the_generic_threshold():
    """В промпт уходит кривая ёмкости, а не общий порог «до 6»."""
    from deckwright.plan.budget import LengthBudget

    budget = LengthBudget(
        title_chars=34,
        subtitle_chars=27,
        bullet_chars=34,
        max_bullets=6,
        max_words_per_bullet=15,
        measured_with="тест",
        block_limits=(
            ("bullets", 2, 110),
            ("bullets", 3, 74),
            ("bullets", 5, 50),
            ("bullets", 6, 26),
            ("paragraph", 1, 132),
        ),
    )
    lines = budget.as_prompt_lines()
    assert (
        "1–2 пункта до 110 символов; 3 пункта до 74 символов; "
        "4–5 пунктов до 50 символов; 6 пунктов до 26 символов"
    ) in lines
    assert "абзац (paragraph): не длиннее 132" in lines
    assert "не больше 6" not in lines


def test_list_is_spread_over_the_cards_one_item_each():
    """Список из трёх пунктов на композиции с четырьмя карточками.

    Главный дефект листов #12: весь список в первой карточке при трёх пустых
    рядом, 14 слайдов из 90 на `vk_tech`. Каждой мысли — своя карточка;
    лишняя карточка остаётся свободной, и рендер её уберёт.
    """
    from deckwright.layout.matcher import _seat, _usable_slots
    from deckwright.schemas import (
        BlockKind,
        ContentBlock,
        Pattern,
        PatternClass,
        Provenance,
        Repeater,
        Slot,
        SourceKind,
    )

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    body = Slot(id="b", role=SlotRole.BODY, box=Box(x=inch, y=inch, w=2 * inch, h=2 * inch),
                provenance=here)
    cards = Repeater(
        id="rep",
        item_slots=[body],
        item_box=body.box,
        observed_count=4,
        max_count=4,
        pitch_emu=2 * inch + inch // 4,
        gutter_emu=inch // 4,
        member_offsets=[(n * (2 * inch + inch // 4), 0) for n in range(4)],
        provenance=here,
    )
    title = Slot(id="t", role=SlotRole.TITLE, box=Box(x=inch, y=0, w=8 * inch, h=inch // 2),
                 provenance=here)
    pattern = Pattern(
        id="p", pattern_class=PatternClass.GRID, donor_slide_index=1, slots=[title],
        repeaters=[cards], content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch),
        provenance=here,
    )
    block = ContentBlock(id="b1", kind=BlockKind.BULLETS, items=["один", "два", "три"])
    free = _usable_slots(pattern, 10 * inch, int(5.625 * inch))

    seats = _seat(pattern, block, SlotRole.BULLETS, free, [title.box])

    assert seats is not None and len(seats) == 3
    assert [lines for _, lines in seats] == [["один"], ["два"], ["три"]]
    assert [slot.box.x for slot, _ in seats] == [inch + n * cards.pitch_emu for n in range(3)]
    # Четвёртая карточка не достаётся следующему блоку: иначе он сел бы в
    # неё при пустых второй и третьей.
    assert not any(slot.id.startswith("rep_") for slot in free)

    many = ContentBlock(id="b2", kind=BlockKind.STEPS, items=[str(n) for n in range(6)])
    seats = _seat(pattern, many, SlotRole.BULLETS,
                  _usable_slots(pattern, 10 * inch, int(5.625 * inch)), [title.box])
    assert seats is not None
    assert [len(lines) for _, lines in seats] == [2, 2, 1, 1]


def test_title_and_closing_use_the_templates_own_slides(specs, plan):
    """Первый и последний слайды колоды — обложка и финал шаблона."""
    from deckwright.schemas import SlideIntent

    cfg = load_config(CONFIG)
    for name, spec in specs:
        if spec.cover_pattern_id is None:
            continue
        deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"))
        # Деление переполненного слайда сдвигает номера: сверяются края.
        if plan.slides[0].intent is SlideIntent.TITLE:
            assert deck.slides[0].pattern_id == spec.cover_pattern_id, name
        if plan.slides[-1].intent is SlideIntent.CLOSING:
            wanted = spec.closing_pattern_id or spec.cover_pattern_id
            assert deck.slides[-1].pattern_id == wanted, name


def test_secondary_bars_stay_visible_on_dark_and_light():
    """Приглушённый столбик — 3:1 к фону: на чёрном светлее, на белом бледнее."""
    from deckwright.layout.matcher import GRAPHIC_CONTRAST, _muted

    accent = Color(rgb="0077FF")
    for background in ("000000", "FFFFFF", "F2F0E8"):
        base = Color(rgb=background)
        muted = _muted(accent, base, base.luminance < 0.5)
        assert muted.contrast_ratio(base) >= GRAPHIC_CONTRAST, background
        assert muted.rgb != accent.rgb
    on_black = _muted(accent, Color(rgb="000000"), True)
    assert on_black.luminance > Color(rgb="000000").luminance + 0.1


def test_same_conclusion_is_highlighted_the_same_way():
    """«От 42 до 9» — оба конца, что бы ни объявил план."""
    from deckwright.layout.matcher import _key_points

    assert _key_points([42.0, 21.0, 9.0], None, None) == [0, 2]


def test_data_takes_the_freed_space_but_not_the_neighbours():
    """График растёт в свободную область ниже заголовка, не на соседний блок."""
    from deckwright.layout.matcher import _free_region

    inch = 914400

    class Spec:
        slide_width_emu = 10 * inch
        slide_height_emu = int(5.625 * inch)

    area = Box(x=inch // 2, y=inch, w=9 * inch, h=4 * inch)
    slot = Box(x=6 * inch, y=3 * inch, w=2 * inch, h=inch)
    grown = _free_region(slot, area, int(1.2 * inch), [], Spec)
    assert grown.area > 4 * slot.area and grown.y >= int(1.2 * inch)
    neighbour = Box(x=inch // 2, y=2 * inch, w=3 * inch, h=2 * inch)
    beside = _free_region(slot, area, int(1.2 * inch), [neighbour], Spec)
    assert beside.x >= neighbour.right


def test_value_label_drops_the_unit_rather_than_wrapping(specs):
    """Над узким столбиком «42 мин» не помещается — остаётся «42»."""
    from deckwright.layout.matcher import _value_label

    inch = 914400
    for _, spec in specs[:1]:
        narrow = Box(x=0, y=0, w=int(1.2 * inch), h=2 * inch)
        size, unit = _value_label(narrow, ["a", "b", "c", "d", "e"], [42.0] * 5, "мин", 14.0, spec)
        assert unit == "" and size >= 10.0


def _cards_pattern(count: int, *, baked: bool = False, placeholder: str = ""):
    """Композиция: заголовок и `count` карточек-повторителя в ряд."""
    from deckwright.schemas import Pattern, PatternClass, Provenance, Repeater, Slot, SourceKind

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    body = Slot(id="b", role=SlotRole.BODY, box=Box(x=inch, y=inch, w=2 * inch, h=2 * inch),
                placeholder_text=placeholder, provenance=here)
    pitch = 2 * inch + inch // 4
    cards = Repeater(
        id="rep", item_slots=[body], item_box=body.box, observed_count=count,
        max_count=count, pitch_emu=pitch, gutter_emu=inch // 4,
        member_offsets=[(n * pitch, 0) for n in range(count)], provenance=here,
    )
    title = Slot(id="t", role=SlotRole.TITLE, box=Box(x=inch, y=0, w=8 * inch, h=inch // 2),
                 provenance=here)
    return Pattern(
        id="p", pattern_class=PatternClass.GRID, donor_slide_index=1, slots=[title],
        repeaters=[cards], content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch),
        baked_items=baked, provenance=here,
    )


def _one_slide(items: list[str]):
    from deckwright.schemas import BlockKind, ContentBlock, SlideIntent, SlidePlan

    return SlidePlan(
        index=2, intent=SlideIntent.SOLUTION, takeaway_title="Заголовок",
        blocks=[ContentBlock(id="b1", kind=BlockKind.BULLETS, items=items)],
    )


class _Spec:
    slide_width_emu = 10 * 914400
    slide_height_emu = int(5.625 * 914400)


def test_cards_drawn_in_the_layout_are_filled_or_not_chosen():
    """Карточки «01–04» нарисованы в картинке layout'а: пустую не убрать.

    Три пункта на четыре нарисованные карточки оставляют «04» пустой —
    такую композицию не берут. Четыре пункта — берут. Обычные карточки
    рендер убирает сам, им лишняя не мешает.
    """
    from deckwright.layout.matcher import _unsuitable

    strategy = Strategy.from_config(load_config(CONFIG).variant("balanced"))
    three, four = _one_slide(["а", "б", "в"]), _one_slide(["а", "б", "в", "г"])
    assert _unsuitable(_cards_pattern(4, baked=True), _Spec, three, strategy)
    assert not _unsuitable(_cards_pattern(4, baked=True), _Spec, four, strategy)
    assert not _unsuitable(_cards_pattern(4), _Spec, three, strategy)


def test_list_does_not_go_into_one_of_several_twin_frames():
    """Пять одинаковых рамок, не опознанных повтором: список в одной — нет.

    `vk_tech` dense 9 до этой правки: три пункта мелко в первой карточке,
    четыре таких же рядом пустые.
    """
    from deckwright.layout.matcher import _unsuitable
    from deckwright.schemas import Pattern, PatternClass, Provenance, Slot, SourceKind

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    frames = [
        Slot(id=f"f{n}", role=SlotRole.BODY,
             box=Box(x=inch // 2 + n * 2 * inch, y=inch, w=int(1.8 * inch), h=inch),
             provenance=here)
        for n in range(4)
    ]
    title = Slot(id="t", role=SlotRole.TITLE, box=Box(x=inch, y=0, w=8 * inch, h=inch // 2),
                 provenance=here)
    pattern = Pattern(
        id="p", pattern_class=PatternClass.KPI_ROW, donor_slide_index=1,
        slots=[title, *frames], content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch),
        provenance=here,
    )
    strategy = Strategy.from_config(load_config(CONFIG).variant("dense"))
    assert _unsuitable(pattern, _Spec, _one_slide(["а", "б", "в"]), strategy)
    # Абзац — не список: ему одна рамка законна.
    assert not _unsuitable(pattern, _Spec, _one_slide(["один абзац"]), strategy)


def test_text_on_an_illustration_takes_no_more_lines_than_the_donor():
    """Рамка текста накрывает картинку, а донор писал в ней две строки.

    Ниже двух строк — рисунок (шар `vk_tech`): три пункта легли бы на него.
    """
    from deckwright.layout.matcher import _on_picture
    from deckwright.schemas import Pattern, PatternClass, Provenance, Slot, SourceKind

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    picture = Slot(id="i", role=SlotRole.IMAGE, box=Box(x=0, y=inch, w=4 * inch, h=4 * inch),
                   provenance=here)
    text = Slot(id="x", role=SlotRole.BODY,
                box=Box(x=inch // 4, y=inch + inch // 4, w=3 * inch, h=3 * inch),
                placeholder_text="Тестирование оборудования\nи софта до покупки", provenance=here)
    pattern = Pattern(
        id="p", pattern_class=PatternClass.GRID, donor_slide_index=1, slots=[picture, text],
        content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch), provenance=here,
    )
    assert _on_picture(text, pattern, 3)
    assert not _on_picture(text, pattern, 2)


def test_readable_threshold_is_reachable_in_every_variant(specs):
    """Порог «не ниже типичного» — ступень под типичным, общая для вариантов.

    Ровно типичный кегль плотный вариант не набирает никогда: он стартует
    на ступень ниже. Строгая ступень подбора пустела, и список уходил в
    карточку кеглем 8 pt.
    """
    from deckwright.layout.matcher import _step_below
    from deckwright.layout.strategy import role_typical

    cfg = load_config(CONFIG)
    for name, spec in specs:
        ladder = ladder_for_role(spec, SlotRole.BODY)
        typical = role_typical(spec, SlotRole.BODY)
        if not ladder or not typical:
            continue
        threshold = _step_below(ladder, typical)
        assert threshold <= typical, name
        for variant in VARIANTS:
            strategy = Strategy.from_config(cfg.variant(variant))
            assert strategy.start_size(ladder, typical) >= threshold, (name, variant)


def test_list_gets_a_place_per_item_when_the_template_has_one(specs, live_plan):
    """Список садится туда, где каждый пункт получает своё место.

    Если в шаблоне есть пригодная композиция, где пункты списка разложены по
    местам кеглем не ниже порога, слайд не берёт ту, где они в одной рамке.
    """
    from deckwright.layout.matcher import (
        _blocks_fit,
        _lists_spread,
        _title_fits,
        _unsuitable,
        _usable_slots,
    )
    from deckwright.layout.strategy import role_typical

    cfg = load_config(CONFIG)
    for name, spec in specs:
        metrics = metrics_for_spec(spec).metrics
        ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
        typical = {role: role_typical(spec, role) for role in SlotRole}
        patterns = {pattern.id: pattern for pattern in spec.patterns}
        for variant in VARIANTS:
            strategy = Strategy.from_config(cfg.variant(variant))
            deck, _ = build_deck_ir(spec, live_plan, cfg.variant(variant))
            if len(deck.slides) != len(live_plan.slides):
                continue  # деление сдвинуло номера — сверять не с чем
            for slide, built in zip(live_plan.slides, deck.slides, strict=True):
                chosen = patterns.get(built.pattern_id)
                if chosen is None or _lists_spread(chosen, spec, slide, strategy):
                    continue
                exists = any(
                    _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
                    and pattern.id in {p.id for p in spec.content_patterns}
                    and not _unsuitable(pattern, spec, slide, strategy)
                    and _lists_spread(pattern, spec, slide, strategy)
                    and _title_fits(pattern, slide, strategy, metrics, ladders[SlotRole.TITLE])
                    and _blocks_fit(pattern, spec, slide, strategy, metrics, ladders, typical)
                    for pattern in spec.patterns
                )
                assert not exists, (
                    f"{name}/{variant}: слайд {slide.index} — список в одной рамке, "
                    "хотя есть композиция с местом под каждый пункт"
                )


def test_titles_are_black_or_white_by_their_own_background(specs, plan):
    """Заголовок — чёрный или белый по фактическому фону под ним.

    Прежде здесь проверялось обратное: заголовок писался фирменным цветом
    роли, если тот проходил порог крупного текста. Требование к результату
    сильнее: тёмный фон — белый текст, светлый — чёрный, и порог один на
    весь текст — 4.5:1. Фирменный цвет остаётся у логотипов, заливок и
    рядов данных, но не у букв.
    """
    from deckwright.layout.matcher import INK_CONTRAST, ink_for

    cfg = load_config(CONFIG)
    checked = 0
    for name, spec in specs:
        deck, _ = build_deck_ir(spec, plan, cfg.variant("balanced"))
        for slide in deck.slides:
            if slide.background is None:
                continue
            title = next((e for e in slide.all_elements() if e.role is SlotRole.TITLE), None)
            if title is None or title.text is None:
                continue
            ground = title.backdrop or slide.background
            style = title.text.paragraphs[0].style
            checked += 1
            assert style.color.rgb == ink_for(ground).rgb, (
                f"{name}: заголовок слайда {slide.index} цветом {style.color.rgb}, "
                f"на фоне {ground.rgb} читается {ink_for(ground).rgb}"
            )
            assert style.color.contrast_ratio(ground) >= INK_CONTRAST
    assert checked, "ни одного заголовка не проверили"


def _slot(sid: str, role: SlotRole, x: float, y: float, w: float, h: float, text: str = ""):
    from deckwright.schemas import Provenance, Slot, SourceKind

    inch = 914400
    return Slot(
        id=sid, role=role, placeholder_text=text,
        box=Box(x=int(x * inch), y=int(y * inch), w=int(w * inch), h=int(h * inch)),
        provenance=Provenance(kind=SourceKind.SLIDE, ref="test"),
    )


def _card_pattern(item_slots: list, fixed: list | None = None, count: int = 3):
    from deckwright.schemas import Pattern, PatternClass, Provenance, Repeater, SourceKind

    inch = 914400
    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    pitch = 3 * inch
    cards = Repeater(
        id="rep", item_slots=item_slots, item_box=item_slots[0].box, observed_count=count,
        max_count=count, pitch_emu=pitch, gutter_emu=inch // 4,
        member_offsets=[(n * pitch, 0) for n in range(count)], provenance=here,
    )
    title = _slot("t", SlotRole.TITLE, 0.5, 0.2, 9, 0.6)
    return Pattern(
        id="p", pattern_class=PatternClass.GRID, donor_slide_index=1,
        slots=[title, *(fixed or [])], repeaters=[cards],
        content_area=Box(x=0, y=0, w=10 * inch, h=5 * inch), provenance=here,
    ), title


def test_text_in_a_card_takes_the_whole_text_column():
    """Пункт без подзаголовка занимает колонку карточки целиком.

    `vk_tech`: подзаголовок карточки и описание под ним. Пункт садился в
    описание под пустым местом подзаголовка. Подпись в кружке над карточкой
    (holdout) — другая колонка: с ней не сливается.
    """
    from deckwright.layout.matcher import _seat, _usable_slots
    from deckwright.schemas import BlockKind, ContentBlock

    heading = _slot("h", SlotRole.CAPTION, 0.5, 1.0, 2.5, 0.2, "Безопасность")
    body = _slot("b", SlotRole.BODY, 0.5, 1.3, 2.5, 2.0, "Оцените уровень")
    circle = _slot("c", SlotRole.CAPTION, 1.4, 0.7, 0.7, 0.7, "1")
    pattern, title = _card_pattern([heading, body, circle])
    block = ContentBlock(id="b1", kind=BlockKind.BULLETS, items=["один", "два", "три"])
    seats = _seat(pattern, block, SlotRole.BULLETS,
                  _usable_slots(pattern, 10 * 914400, int(5.625 * 914400)), [title.box])

    assert seats is not None and len(seats) == 3
    first = seats[0][0].box
    assert first.y == heading.box.y and first.bottom == body.box.bottom
    assert first.x == body.box.x and first.w == body.box.w  # кружок не вошёл


def test_paragraph_below_the_card_icon_not_in_the_heading_place():
    """`vk_workspace`: рамка заголовка во всю карточку, иконка, под ней текст.

    Абзац без заголовка начинается с места текста под иконкой; показатель
    остаётся вверху, где шаблон пишет крупно.
    """
    from deckwright.layout.matcher import _seat, _usable_slots
    from deckwright.schemas import BlockKind, ContentBlock

    frame = _slot("f", SlotRole.CAPTION, 0.5, 1.0, 2.6, 3.0, "Заголовок")
    text = _slot("x", SlotRole.CAPTION, 0.6, 2.6, 2.3, 0.3, "Текст")
    icon = _slot("i", SlotRole.IMAGE, 0.6, 2.2, 0.3, 0.3)
    pattern, title = _card_pattern([frame, text], fixed=[icon], count=1)
    free = _usable_slots(pattern, 10 * 914400, int(5.625 * 914400))

    paragraph = ContentBlock(id="b1", kind=BlockKind.PARAGRAPH, items=["Высокое время…"])
    seats = _seat(pattern, paragraph, SlotRole.BODY, list(free), [title.box])
    assert seats is not None
    box = seats[0][0].box
    assert box.y == text.box.y and box.bottom == frame.box.bottom

    figure = ContentBlock(id="b2", kind=BlockKind.KPI, items=["42 минуты"])
    seats = _seat(pattern, figure, SlotRole.KPI_VALUE, list(free), [title.box])
    assert seats is not None and seats[0][0].box.y == frame.box.y


def test_figure_and_its_label_go_to_their_own_places():
    """Число — в место числа, подпись — в место текста рядом, каждое своим кеглем.

    Holdout: число 115 pt и подпись 20 pt. Вместе в месте числа подпись
    обязана была влезть кеглем числа.
    """
    from deckwright.layout.matcher import _split_heading
    from deckwright.schemas import (
        BlockKind,
        ContentBlock,
        Pattern,
        PatternClass,
        Provenance,
        SourceKind,
    )

    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    value = _slot("v", SlotRole.KPI_VALUE, 0.5, 1.5, 8, 2.0, "99%")
    label = _slot("l", SlotRole.BODY, 0.5, 3.7, 7, 0.8, "Описание")
    tag = _slot("g", SlotRole.KPI_LABEL, 8.6, 1.5, 0.8, 0.2, "Заголовок")
    block = ContentBlock(id="b", kind=BlockKind.KPI, heading="42 минуты",
                         items=["Среднее время обнаружения"])

    def pattern(slots):
        return Pattern(id="p", pattern_class=PatternClass.KPI_ROW, donor_slide_index=1,
                       slots=slots, content_area=value.box, provenance=here)

    seats = _split_heading(pattern([value, label, tag]), block, [value, label, tag], [])
    assert [(slot.id, lines) for slot, lines in seats] == [
        ("v", ["42 минуты"]), ("l", ["Среднее время обнаружения"])
    ]
    # Рядом только бирка: рамка числа делится сама — сверху число, снизу
    # подпись, каждое своим кеглем (`vk_tech`: «42 минуты» мелко одним
    # абзацем с подписью в рамке «91%»).
    seats = _split_heading(pattern([value, tag]), block, [value, tag], [])
    (top, figure), (bottom, caption) = seats
    assert figure == ["42 минуты"] and caption == ["Среднее время обнаружения"]
    assert top.role is SlotRole.KPI_VALUE and bottom.role is SlotRole.BODY
    assert top.box.bottom <= bottom.box.y and bottom.box.bottom == value.box.bottom
    # Низкая рамка, где двум строкам не уместиться, — не делится.
    low = _slot("v", SlotRole.KPI_VALUE, 0.5, 1.5, 8, 0.5, "99%")
    assert _split_heading(pattern([low, tag]), block, [low, tag], []) is None


def test_label_below_the_figure_inside_its_frame_is_its_place():
    """Место подписи заходит в низ рамки числа — это его подпись, а не помеха.

    `vk_tech`: рамка «91%» выше самой цифры, и «Описание показателя» лежит в
    её нижней части. Раньше такое место отбрасывалось как наложение, и
    число с подписью писались вместе мелко. Рамка числа кончается там, где
    начинается подпись; линия донора в рамке — граница снизу.
    """
    from deckwright.layout.matcher import _split_heading
    from deckwright.schemas import (
        BlockKind,
        ContentBlock,
        Pattern,
        PatternClass,
        Provenance,
        SourceKind,
    )

    inch = 914400
    value = _slot("v", SlotRole.KPI_VALUE, 0.2, 2.0, 4.3, 1.5, "91%")
    label = _slot("l", SlotRole.BODY, 0.3, 3.4, 3.5, 0.4, "Описание показателя")
    line = Box(x=int(0.3 * inch), y=int(3.9 * inch), w=int(2.6 * inch), h=int(0.03 * inch))
    pattern = Pattern(
        id="p", pattern_class=PatternClass.KPI_ROW, donor_slide_index=1, slots=[value, label],
        content_area=value.box, decor=[line], provenance=Provenance(kind=SourceKind.SLIDE, ref="t"),
    )
    block = ContentBlock(id="b", kind=BlockKind.KPI, heading="42 минуты",
                         items=["Среднее время обнаружения"])
    (figure, _), (caption, _) = _split_heading(pattern, block, [value, label], [])
    assert caption.id == "l"
    assert figure.box.bottom <= label.box.y


def test_text_place_stops_before_the_templates_line():
    """Место текста кончается раньше линии донора, лежащей внутри его рамки."""
    from deckwright.layout.matcher import _DECOR_GAP, _clear_of_decor

    inch = 914400
    frame = Box(x=0, y=2 * inch, w=3 * inch, h=2 * inch)
    line = Box(x=0, y=int(3.9 * inch), w=3 * inch, h=int(0.03 * inch))
    clipped = _clear_of_decor(frame, [line])
    assert clipped.y == frame.y and clipped.bottom == line.y - _DECOR_GAP
    # Подложка во всю рамку — не граница.
    panel = Box(x=-inch, y=inch, w=6 * inch, h=4 * inch)
    assert _clear_of_decor(frame, [panel]) == frame
    # Значок у левого края во всю высоту — граница слева.
    icon = Box(x=0, y=2 * inch, w=inch // 3, h=2 * inch)
    assert _clear_of_decor(frame, [icon]).x == icon.right + _DECOR_GAP
    # Значок в середине высокой рамки — начало текста не двигается, срезается низ.
    middle = Box(x=inch // 4, y=3 * inch, w=inch // 3, h=inch // 3)
    kept = _clear_of_decor(frame, [middle])
    assert (kept.x, kept.y) == (frame.x, frame.y) and kept.bottom == middle.y - _DECOR_GAP


def test_alignment_is_read_from_the_donor():
    """Выравнивание текста — донора: заголовок в круге по центру, подпись справа.

    Раньше выравнивание не читалось вовсе, и писатель ставил каждому абзацу
    левый край: заголовок в круге `vk_education` выходил прижатым влево.
    """
    from lxml import etree

    from deckwright.layout.matcher import _align_of
    from deckwright.parse.patterns import text_align, text_valign
    from deckwright.schemas import Align, VAlign

    def shape(body: str):
        return etree.fromstring(
            '<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<p:txBody>{body}</p:txBody></p:sp>"
        )

    centred = shape('<a:bodyPr anchor="b"/><a:p><a:pPr algn="ctr"/><a:r><a:t>Заголовок</a:t>'
                    "</a:r></a:p>")
    assert text_align(centred) is Align.CENTER
    assert text_valign(centred) is VAlign.BOTTOM
    inherited = shape('<a:bodyPr/><a:lstStyle><a:lvl1pPr algn="r"/></a:lstStyle>'
                      "<a:p><a:r><a:t>Подпись</a:t></a:r></a:p>")
    assert text_align(inherited) is Align.RIGHT
    assert text_align(shape("<a:bodyPr/><a:p><a:r><a:t>Текст</a:t></a:r></a:p>")) is None

    slot = _slot("t", SlotRole.TITLE, 0, 0, 3, 1).model_copy(update={"text_align": Align.CENTER})
    assert _align_of(slot) is Align.CENTER
    assert _align_of(None) is Align.LEFT


def test_circle_title_is_centred_in_the_built_deck(specs, live_plan):
    """Собранная колода: место, которое донор центрирует, пишется по центру."""
    from deckwright.schemas import Align

    cfg = load_config(CONFIG)
    for name, spec in specs:
        patterns = {pattern.id: pattern for pattern in spec.patterns}
        deck, _ = build_deck_ir(spec, live_plan, cfg.variant("balanced"))
        for slide in deck.slides:
            pattern = patterns.get(slide.pattern_id)
            if pattern is None:
                continue
            donor = next((s for s in pattern.slots if s.role is SlotRole.TITLE), None)
            title = next((e for e in slide.all_elements() if e.role is SlotRole.TITLE), None)
            if donor is None or title is None or donor.text_align is not Align.CENTER:
                continue
            assert title.text.paragraphs[0].style.align is Align.CENTER, (name, slide.index)


def test_figure_in_the_donors_ring_is_not_a_split():
    """Число в кольце донора — не крупный показатель, а кольцо с чужой долей.

    Предпочтение «показатель делится» увело слайд с данными `vk_education`
    в композицию с кольцами «10%»: число садилось в кольцо, кольцо
    оставалось и изображало данные, которых нет.
    """
    from deckwright.layout.matcher import _figure_unsplit
    from deckwright.schemas import (
        BlockKind,
        ContentBlock,
        Pattern,
        PatternClass,
        Provenance,
        SlideIntent,
        SlidePlan,
        SourceKind,
    )

    here = Provenance(kind=SourceKind.SLIDE, ref="test")
    value = _slot("v", SlotRole.KPI_VALUE, 1, 1.5, 2.4, 0.8, "10%")
    label = _slot("l", SlotRole.BODY, 1.2, 2.4, 2.0, 0.5, "Подпись")
    title = _slot("t", SlotRole.TITLE, 0.5, 0.2, 9, 0.6)
    ring = Box(x=914400, y=int(1.2 * 914400), w=int(2.4 * 914400), h=int(2.4 * 914400))
    slide = SlidePlan(index=2, intent=SlideIntent.EVIDENCE, takeaway_title="Вывод", blocks=[
        ContentBlock(id="b", kind=BlockKind.KPI, heading="42 минуты", items=["Среднее время"])
    ])
    strategy = Strategy.from_config(load_config(CONFIG).variant("dense"))

    def pattern(figures):
        return Pattern(id="p", pattern_class=PatternClass.KPI_ROW, donor_slide_index=1,
                       slots=[title, value, label], content_area=ring, figure_pictures=figures,
                       provenance=here)

    assert not _figure_unsplit(pattern([]), _Spec, slide, strategy)
    assert _figure_unsplit(pattern([ring]), _Spec, slide, strategy)


# ── Фон под текстом: градиент, двухцветная карточка, фон-картинка ───────────


def _ground(x: float, y: float, w: float, h: float, *colors: str, z: int = 0):
    from deckwright.schemas import Ground

    inch = 914400
    return Ground(
        box=Box(x=int(x * inch), y=int(y * inch), w=int(w * inch), h=int(h * inch)),
        colors=[Color(rgb=value) for value in colors],
        z=z,
    )


def _box(x: float, y: float, w: float, h: float) -> Box:
    inch = 914400
    return Box(x=int(x * inch), y=int(y * inch), w=int(w * inch), h=int(h * inch))


class _Grounded:
    """Композиция, от которой вёрстке нужны только заливки донора."""

    def __init__(self, grounds):
        self.grounds = grounds


def test_gradient_card_is_judged_by_its_hardest_stop():
    """Цвет текста на градиенте держит порог на всей заливке, а не в середине.

    Карточки `vk_workspace` залиты `00AEE8`→`0077FF`. Пока разбор читал
    только `a:solidFill`, подложка выходила `None`, цвет выбирался по
    чёрному фону слайда, и по яркому голубому писалось белым — 2.8:1 на
    отрисованной странице. По обоим опорным цветам читается чёрный.
    """
    from deckwright.layout.matcher import grounds_of, ink_over

    card = _Grounded([_ground(0, 0, 4, 3, "00AEE8", "0077FF")])
    text = _box(0.2, 0.2, 3.6, 0.5)
    grounds = grounds_of(text, card, None, Color(rgb="000000"), True)

    assert {color.rgb for color in grounds} == {"00AEE8", "0077FF"}
    ink = ink_over(grounds)
    assert ink.rgb == "000000"
    assert all(ink.contrast_ratio(ground) >= MIN_CONTRAST for ground in grounds)


def test_text_settles_below_a_two_tone_card_header():
    """Рамка на двух фонах отступает под шапку карточки, а не красится.

    Карточка `vk_workspace` — чёрная с голубой градиентной шапкой. Белым
    первые строки лягут на голубое, чёрным остальные — на чёрное: читаемого
    цвета у такой рамки нет, и решается это геометрией.
    """
    from deckwright.layout.matcher import _settled, readable_ink

    card = _Grounded([
        _ground(0, 2, 4, 4, "000000", z=1),
        _ground(0, 2, 4, 0.8, "00AEE8", "0077FF", z=2),
    ])
    # Шапка занимает больше трети рамки: полоса, пересёкшая её краем, —
    # это декор, и фоном текста она не считается (`_OVER_SHARE`).
    text = _box(0.2, 2.2, 3.6, 1.6)
    assert readable_ink(
        [Color(rgb="000000"), Color(rgb="00AEE8")]
    ) is None, "проба бессмысленна, если фоны не спорят"

    settled, plate = _settled(text, card)

    assert plate is None, "здесь надо отступить, а не растить подложку"
    assert settled.y >= card.grounds[1].box.bottom, "текст остался на шапке"
    assert settled.w == text.w, "ширина места потеряна зря"


def test_a_card_hides_the_background_picture_under_it():
    """Порядок отрисовки решает: карточка поверх фона закрывает его.

    Фон-картинка снимается клетками и лежит ниже всего (`z = -1`). Текст на
    белой карточке поверх тёмной картинки читается по карточке, иначе на
    белом получился бы белый текст.
    """
    from deckwright.layout.matcher import grounds_of, ink_over

    slide = _Grounded([
        _ground(0, 0, 4, 4, "2B053F", z=-1),
        _ground(0, 0, 4, 4, "531275", z=-1),
        _ground(0.5, 1, 3, 2, "FFFFFF", z=3),
    ])
    text = _box(0.7, 1.2, 2.6, 1.6)

    grounds = grounds_of(text, slide, None, None, True)

    assert [color.rgb for color in grounds] == ["FFFFFF"]
    assert ink_over(grounds).rgb == "000000"


def _picture_background(image):
    """Фон-картинка так, как её видит разбор: узел `p:bg` и часть с байтами."""
    import io

    from lxml import etree

    A = "http://schemas.openxmlformats.org/drawingml/2006/main"
    P = "http://schemas.openxmlformats.org/presentationml/2006/main"
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    bg = etree.fromstring(
        f'<p:bg xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:bgPr>'
        f'<a:blipFill><a:blip r:embed="rId1"/></a:blipFill></p:bgPr></p:bg>'
    )
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    blob = raw.getvalue()

    class _Part:
        def related_part(self, rid):
            assert rid == "rId1"
            return type("_Image", (), {"blob": blob})()

    return bg, _Part()


def test_decor_baked_into_the_background_picture_is_protected():
    """Логотип внутри фоновой картинки — тоже защищённая зона.

    У ЛЦТ2026 четыре логотипа партнёров запечены в фоновый градиент: ни
    одна проверка по фигурам их не видит, и заголовок ложился прямо на них.
    Признак — перепад яркости внутри клетки, а не имя и не яркость: ровный
    градиент даёт около нуля, нарисованное — резкий.
    """
    from PIL import Image, ImageDraw

    from deckwright.parse.pictures import busy_regions

    slide_w, slide_h = 12192000, 6858000
    picture = Image.new("RGB", (1280, 720))
    for y in range(720):
        # Плавный градиент: сам по себе он не оформление и защиты не требует.
        for x in range(1280):
            picture.putpixel((x, y), (40 + x // 20, 5, 70 + y // 12))
    draw = ImageDraw.Draw(picture)
    draw.rectangle([1000, 30, 1180, 70], fill=(255, 255, 255))

    zones = busy_regions(*_picture_background(picture), slide_w, slide_h)

    assert zones, "нарисованное в фоне не найдено"
    assert all(zone.bottom <= slide_h * 0.18 for zone in zones), "зоны не у верхнего края"
    assert min(zone.x for zone in zones) >= slide_w * 0.7, "зоны левее рисунка"


def test_a_background_photo_protects_nothing():
    """Фотография во весь фон — не оформление по краю: места она не отнимает.

    Иначе «занят» оказывается весь слайд и текст ставить некуда.
    """
    import random

    from PIL import Image

    from deckwright.parse.pictures import busy_regions

    random.seed(4)
    noisy = Image.new("RGB", (1280, 720))
    noisy.putdata([
        (random.randrange(256), random.randrange(256), random.randrange(256))
        for _ in range(1280 * 720)
    ])

    assert busy_regions(*_picture_background(noisy), 12192000, 6858000) == []


def test_a_smooth_gradient_background_protects_nothing():
    """Ровный фон без рисунка защищать не от чего: текст не теряет места."""
    from deckwright.parse.pictures import busy_regions

    assert busy_regions(None, None, 12192000, 6858000) == []
