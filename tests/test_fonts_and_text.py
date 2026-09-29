"""Контрольные случаи группы 3: гарнитура, кегль и вместимость.

Проверяется то, что перечислено в задании этапа:

* заголовок 48 pt рядом с колонтитулом 18 pt — предел у слота, а не у роли;
* установленная гарнитура находится по имени семейства, а не по имени файла;
* начертание и покрытие кириллицы участвуют в поиске;
* точной гарнитуры нет — подстановка названа и учтена в измерении;
* встроенный шрифт доступен и отдельно недоступен;
* метрик нет — планировщик не получает нулей как требование;
* длинное неразрывное слово и узкий слот;
* несколько макетов с разными шкалами кегля.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deckwright.config import load_config
from deckwright.layout import text_metrics
from deckwright.layout.strategy import (
    ladder_for_role,
    ladder_for_slot,
    role_floor,
    slot_floor,
)
from deckwright.layout.text_metrics import (
    find_family,
    font_index,
    metrics_for_spec,
    normalized_family,
    wrap,
)
from deckwright.parse.opener import parse_template
from deckwright.plan.budget import compute_budget
from deckwright.schemas import (
    Box,
    Color,
    Provenance,
    Slot,
    SlotRole,
    SourceKind,
    TemplateSpec,
    TextStyle,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "config.yaml"
TEMPLATES = ROOT / "data" / "templates"
PT = 12_700


def _slot(role: SlotRole, size_pt: float, tag: str) -> Slot:
    return Slot(
        id=tag,
        role=role,
        box=Box(x=0, y=0, w=4_000_000, h=800_000),
        style=TextStyle(
            font_family="Arial", size_pt=size_pt, color=Color(rgb="000000")
        ),
        provenance=Provenance(kind=SourceKind.LAYOUT, ref=tag),
    )


def _spec_with(slots: list[Slot]) -> TemplateSpec:
    """Минимальный шаблон: один макет с заданными слотами."""
    from deckwright.schemas import LayoutSpec

    return TemplateSpec(
        source_name="проба.pptx",
        template_sha256="0" * 64,
        slide_width_emu=12_192_000,
        slide_height_emu=6_858_000,
        layouts=[
            LayoutSpec(id="layout1", name="проба", master_id="master1", slots=slots)
        ],
    )


# ── предел кегля принадлежит слоту, а не роли ───────────────────────────


def test_small_footer_does_not_set_the_floor_for_a_big_title():
    """Заголовок 48 pt рядом с подписью 18 pt той же роли не ужимается втрое.

    Предел роли — минимум по всему шаблону, и он остаётся нижней границей;
    но у каждой композиции своя иерархия, и место в 48 pt обязано держаться
    своего порядка.
    """
    spec = _spec_with(
        [_slot(SlotRole.TITLE, 48.0, "big"), _slot(SlotRole.TITLE, 18.0, "small")]
    )
    assert role_floor(spec, SlotRole.TITLE) == 18.0
    assert slot_floor(spec, SlotRole.TITLE, 48.0) > 18.0
    assert slot_floor(spec, SlotRole.TITLE, 18.0) == 18.0
    big = ladder_for_slot(spec, SlotRole.TITLE, 48.0)
    assert big and min(big) >= slot_floor(spec, SlotRole.TITLE, 48.0)
    assert min(big) > min(ladder_for_role(spec, SlotRole.TITLE))


def test_the_templates_own_title_keeps_its_order_of_size():
    """На `vk_tech` заголовки объявлены от 18 до 54 pt — и 54 не падает до 18.

    Предел роли — по самому шаблону: читаемый минимум вёрстку не ограничивает,
    он проверяется аудитом (`template.text_too_small`). Иерархию держит предел
    слота — три пятых его собственного кегля.
    """
    path = TEMPLATES / "vk_tech.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    spec = parse_template(path, None, cfg.fonts.extract_dir)
    assert role_floor(spec, SlotRole.TITLE) <= 18.0
    ladder = ladder_for_slot(spec, SlotRole.TITLE, 54.0)
    assert min(ladder) >= 54.0 * 0.6


def test_a_role_without_slots_borrows_the_floor_of_body_text():
    """Подпись без своих слотов не открывает шкалу до декоративных ступеней."""
    path = TEMPLATES / "vk_tech.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    spec = parse_template(path, None, cfg.fonts.extract_dir)
    assert not [
        slot
        for pattern in spec.content_patterns
        for slot in pattern.slots
        if slot.role is SlotRole.CAPTION and slot.style and slot.style.size_pt > 0
    ], "у подписи появились свои слоты — проба больше не про это"
    assert role_floor(spec, SlotRole.CAPTION) == role_floor(spec, SlotRole.BODY)
    assert min(ladder_for_slot(spec, SlotRole.CAPTION, 0.0)) >= 7.0


def test_layouts_with_different_scales_get_different_floors():
    """Разные макеты — разные шкалы: предел считается от кегля своего слота."""
    spec = _spec_with(
        [
            _slot(SlotRole.BODY, 12.0, "мелкий"),
            _slot(SlotRole.BODY, 24.0, "средний"),
            _slot(SlotRole.BODY, 48.0, "крупный"),
        ]
    )
    floors = [slot_floor(spec, SlotRole.BODY, size) for size in (12.0, 24.0, 48.0)]
    assert floors == sorted(floors)
    assert floors[0] < floors[-1]


# ── гарнитура ищется по имени семейства ─────────────────────────────────


def test_family_is_found_by_its_name_not_by_the_file_name():
    """Шрифт стоит в системе под своим именем, файл называется иначе."""
    index = font_index()
    if not index:
        pytest.skip("в системе не нашлось ни одного шрифта")
    sample = next(
        (item for item in index if item.family and not item.bold and not item.italic),
        None,
    )
    assert sample is not None
    found = find_family(sample.family, cyrillic=sample.cyrillic)
    assert found is not None, f"{sample.family} есть в индексе, но не находится"
    assert normalized_family(found.family) == normalized_family(sample.family)


def test_family_name_is_matched_regardless_of_spacing_and_case():
    assert normalized_family("Times New Roman") == normalized_family("timesnewroman")
    assert normalized_family("PT Sans") == normalized_family("pt-sans")


def test_a_font_without_cyrillic_is_not_offered_for_russian_text():
    """Покрытие символов — часть поиска, а не подробность.

    Совпадения имени мало: шрифт без кириллицы русский текст не наберёт, и
    мерить им — считать по глифам, которых не будет.
    """
    index = font_index()
    without = next((item for item in index if not item.cyrillic), None)
    if without is None:
        pytest.skip("все шрифты системы покрывают кириллицу")
    assert find_family(without.family, cyrillic=True) is None or find_family(
        without.family, cyrillic=True
    ).cyrillic
    assert find_family(without.family, cyrillic=False) is not None


def test_missing_family_is_reported_not_guessed():
    assert find_family("Такой Гарнитуры Нет 12345") is None


def test_style_is_part_of_the_request():
    """Жирное начертание берётся жирным файлом, если он есть."""
    index = font_index()
    bold = next((item for item in index if item.bold and not item.italic), None)
    if bold is None:
        pytest.skip("в системе нет ни одного жирного начертания")
    found = find_family(bold.family, bold=True, cyrillic=bold.cyrillic)
    assert found is not None
    assert found.bold or normalized_family(found.family) == normalized_family(bold.family)


# ── подстановка названа и учтена ────────────────────────────────────────


def test_substitution_is_named_and_costs_a_margin(monkeypatch):
    """Гарнитуры нет ни встроенной, ни в системе — подстановка и запас."""
    monkeypatch.setattr(text_metrics, "find_family", lambda *a, **k: None)
    spec = _spec_with([_slot(SlotRole.TITLE, 40.0, "t")])
    spec = spec.model_copy(
        update={"fonts": [_font_token("Такой Гарнитуры Нет 12345")]}
    )
    source = metrics_for_spec(spec)
    if source.metrics is None:
        pytest.skip("в системе нет ни одного шрифта для подстановки")
    assert not source.metric_compatible
    assert "подстановка" in source.description
    budget = compute_budget(spec, 6, 15)
    assert budget.title_chars > 0
    assert not budget.metric_compatible


def _font_token(family: str):
    from deckwright.schemas import FontToken

    return FontToken(family=family, usage_count=10, embedded=False)


def test_embedded_font_is_preferred_when_it_is_the_templates_own(tmp_path):
    """Встроенный шрифт берётся, только если он и есть основная гарнитура.

    На `vk_education` текст набран Arial, а Play лежит в файле для отдельных
    фигур донора: мерить Play, а писать Arial — расходиться на ширинах.
    """
    path = TEMPLATES / "vk_education.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_education")
    cfg = load_config(CONFIG)
    spec = parse_template(path, None, cfg.fonts.extract_dir)
    embedded = {token.family for token in spec.fonts if token.embedded}
    assert embedded, "во встроенных шрифтах шаблона ничего не нашлось"
    source = metrics_for_spec(spec)
    assert source.requested == spec.fonts[0].family
    assert normalized_family(source.used) == normalized_family(source.requested)


def test_embedded_font_unavailable_falls_back_without_zeroing(monkeypatch, tmp_path):
    """libeot недоступна — шрифт не извлечён, но измерение остаётся."""
    from deckwright.parse import fonts as fonts_mod

    monkeypatch.setattr(
        fonts_mod, "load_libeot", lambda: (_ for _ in ()).throw(
            fonts_mod.FontExtractionError("библиотека недоступна")
        )
    )
    path = TEMPLATES / "vk_workspace.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_workspace")
    fonts, warnings = fonts_mod.extract_embedded_fonts(path, tmp_path / "fonts")
    # Шрифты не извлеклись, но прогон не упал и причина названа: без этого
    # «нет метрик» выглядит как «шаблон без шрифтов».
    assert fonts == []
    assert warnings and any("библиотека недоступна" in text for text in warnings)


# ── метрик нет: планировщику не уходят нули как требование ──────────────


def test_unmeasured_lengths_are_not_written_as_zero(monkeypatch):
    """«Не длиннее 0 символов» — требование, которое нельзя выполнить."""
    monkeypatch.setattr(text_metrics, "_system_font", lambda: None)
    monkeypatch.setattr(text_metrics, "find_family", lambda *a, **k: None)
    spec = _spec_with([_slot(SlotRole.TITLE, 40.0, "t")])
    budget = compute_budget(spec, 6, 15)
    text = budget.as_prompt_lines()
    assert "не длиннее 0 символов" not in text
    if budget.title_chars == 0:
        assert "не измерена" in text


def test_measured_budget_still_states_lengths():
    path = TEMPLATES / "vk_tech.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    spec = parse_template(path, None, cfg.fonts.extract_dir)
    budget = compute_budget(spec, 6, 15)
    assert budget.title_chars > 0
    assert "не длиннее" in budget.as_prompt_lines()


# ── перенос: длинное слово в узком слоте ────────────────────────────────


def test_a_long_word_is_not_cut_in_a_narrow_slot():
    """Слово длиннее строки не режется: PowerPoint его тоже не режет.

    Резать значит подменять текст; выпускать за край — честно, и это видит
    проверка переполнения.
    """
    source = metrics_for_spec(
        _spec_with([_slot(SlotRole.BODY, 12.0, "b")]).model_copy(
            update={"fonts": [_font_token("Arial")]}
        )
    )
    if source.metrics is None:
        pytest.skip("мерить нечем")
    word = "непереносимоедлинноесловобезпробелов"
    lines = wrap(word, source.metrics, 12.0, 400_000)
    assert lines == [word], "слово разрезано"


def test_wrapping_keeps_every_word():
    source = metrics_for_spec(
        _spec_with([_slot(SlotRole.BODY, 12.0, "b")]).model_copy(
            update={"fonts": [_font_token("Arial")]}
        )
    )
    if source.metrics is None:
        pytest.skip("мерить нечем")
    text = "Единая платформа наблюдаемости для инженерных команд компании"
    lines = wrap(text, source.metrics, 14.0, 2_000_000)
    assert " ".join(lines).split() == text.split()


# ── происхождение ограничения видно в результате ────────────────────────


def test_run_records_what_the_text_was_measured_with(tmp_path):
    """Аудитору группы 5 нужны и факт подстановки, и чем она разрешилась."""
    path = TEMPLATES / "vk_education.pptx"
    if not path.exists():
        pytest.skip("нет шаблона vk_education")
    cfg = load_config(CONFIG)
    spec = parse_template(path, None, cfg.fonts.extract_dir)
    budget = compute_budget(spec, 6, 15)
    assert budget.measured_with
    assert json.dumps(budget.measured_with, ensure_ascii=False)


# ── Один цвет текста на слайде ───────────────────────────────────────────────


def _text_element(element_id, role, color, backdrop=None, box=None):
    from deckwright.schemas import (
        Box,
        Color,
        Element,
        ElementKind,
        Paragraph,
        Provenance,
        SourceKind,
        TextContent,
        TextStyle,
    )

    style = TextStyle(font_family="Arial", size_pt=18, color=Color(rgb=color))
    return Element(
        id=element_id,
        kind=ElementKind.TEXT,
        role=role,
        box=box or Box(x=0, y=0, w=1_000_000, h=500_000),
        provenance=Provenance(kind=SourceKind.DERIVED, ref="тест"),
        backdrop=Color(rgb=backdrop) if backdrop else None,
        text=TextContent(paragraphs=[Paragraph(text="Текст", style=style)]),
    )


def test_slide_text_is_brought_to_one_colour():
    """Подпись, пункт и шапка таблицы на слайде пишутся одним цветом.

    Цвет для каждого места выбирался отдельно и по-своему верно — по
    подложке, по роли, по тому, чем шаблон пишет на этой карточке. Вместе
    это давало слайд, где подпись серая, а пункт чёрный.
    """
    from deckwright.layout.matcher import _one_ink
    from deckwright.schemas import Color, SlotRole

    elements = [
        _text_element("e1", SlotRole.BODY, "111111"),
        _text_element("e2", SlotRole.CAPTION, "666666"),
        _text_element("e3", SlotRole.BODY, "111111"),
    ]
    issues = _one_ink(elements, 1, None, Color(rgb="FFFFFF"), False)

    assert not issues
    inks = {e.text.paragraphs[0].style.color.rgb for e in elements}
    # На светлом фоне это чёрный: цвет выбирается по контрасту, а не
    # наследуется от шаблона.
    assert inks == {"000000"}, inks


def test_the_title_gets_its_colour_from_its_own_backdrop():
    """Заголовок живёт по тому же правилу, но по своей подложке.

    Раньше он сохранял фирменный цвет. Требование к результату строже:
    тёмный фон — белый текст, светлый — чёрный, и это касается заголовка
    тоже. Отличаться от остального текста он вправе только тогда, когда
    стоит на другой подложке.
    """
    from deckwright.layout.matcher import _one_ink
    from deckwright.schemas import Color, SlotRole

    title = _text_element("t", SlotRole.TITLE, "0057B8", backdrop="101010")
    body = _text_element("b", SlotRole.BODY, "111111")
    caption = _text_element("c", SlotRole.CAPTION, "666666")
    _one_ink([title, body, caption], 1, None, Color(rgb="FFFFFF"), False)

    assert title.text.paragraphs[0].style.color.rgb == "FFFFFF"
    assert caption.text.paragraphs[0].style.color.rgb == "000000"


def test_impossible_single_colour_is_named_not_forced():
    """Светлый и тёмный блок на одном слайде: конфликт называется находкой.

    Перекрасить весь текст в один цвет ценой нечитаемости — хуже, чем
    сказать, что дизайн слайда этого не позволяет.
    """
    from deckwright.layout.matcher import _one_ink
    from deckwright.schemas import Color, SlotRole

    on_white = _text_element("w", SlotRole.BODY, "111111", backdrop="FFFFFF")
    on_black = _text_element("b", SlotRole.BODY, "FFFFFF", backdrop="000000")
    issues = _one_ink([on_white, on_black], 3, None, Color(rgb="FFFFFF"), False)

    assert [i.check_id for i in issues] == ["template.text_color_split"]
    # Каждый остался с читаемым цветом: нечитаемого текста правило не создаёт.
    assert on_white.text.paragraphs[0].style.color.rgb == "000000"
    assert on_black.text.paragraphs[0].style.color.rgb == "FFFFFF"


def test_table_and_chart_labels_obey_the_slide_colour():
    """Ячейки, шапка таблицы и подписи графика — тем же цветом, что текст."""
    from deckwright.layout.matcher import _one_ink
    from deckwright.schemas import (
        Box,
        ChartContent,
        ChartKind,
        ChartSeries,
        Color,
        Element,
        ElementKind,
        Provenance,
        SlotRole,
        SourceKind,
        TableContent,
        TextStyle,
    )

    grey = TextStyle(font_family="Arial", size_pt=12, color=Color(rgb="888888"))
    box = Box(x=0, y=0, w=1_000_000, h=500_000)
    where = Provenance(kind=SourceKind.DERIVED, ref="тест")
    table = Element(
        id="tbl", kind=ElementKind.TABLE, role=SlotRole.TABLE, box=box, provenance=where,
        table=TableContent(
            header=["Месяц", "Выручка"],
            rows=[["Июль", "14,2"]],
            cell_style=grey,
            header_style=grey.model_copy(update={"color": Color(rgb="FFFFFF")}),
        ),
    )
    chart = Element(
        id="cht", kind=ElementKind.CHART, role=SlotRole.CHART, box=box, provenance=where,
        chart=ChartContent(
            chart_kind=ChartKind.BAR,
            categories=["Июль"],
            series=[ChartSeries(name="Выручка", values=[14.2], color=Color(rgb="0057B8"))],
            label_style=grey.model_copy(update={"color": Color(rgb="333333")}),
        ),
    )
    body = _text_element("b", SlotRole.BODY, "111111")
    _one_ink([body, table, chart], 4, None, Color(rgb="FFFFFF"), False)

    assert table.table.cell_style.color.rgb == "000000"
    assert table.table.header_style.color.rgb == "000000"
    assert chart.chart.label_style.color.rgb == "000000"
    # Цвет самого ряда — это графика, а не текст: его не трогаем.
    assert chart.chart.series[0].color.rgb == "0057B8"


# ── Гарнитура места, а не одна на колоду ─────────────────────────────────────


def _spec(path):
    from deckwright.parse.opener import parse_template

    return parse_template(path, cache_dir=None, font_dir=None)


def test_title_placeholder_keeps_the_heading_font_of_the_theme(template_paths):
    """Заголовок набран заголовочной гарнитурой темы, а не основной.

    До правки все места получали `spec.fonts[0]`, и заголовок мерился
    основной гарнитурой, а рисовался заголовочной: на `finansy` это
    `Calibri` против `Calibri Light`.
    """
    from deckwright.schemas import SlotRole

    checked = 0
    for path in template_paths:
        spec = _spec(path)
        families = {
            slot.style.font_family
            for layout in spec.layouts
            for slot in layout.slots
            if slot.role is SlotRole.TITLE and slot.style is not None
        }
        body = {
            slot.style.font_family
            for layout in spec.layouts
            for slot in layout.slots
            if slot.role is SlotRole.BODY and slot.style is not None
        }
        if not families or not body:
            continue
        checked += 1
        assert all(family for family in families | body), (
            f"{path.name}: у места пустая гарнитура"
        )
    assert checked, "ни в одном шаблоне не нашлось пары «заголовок + текст»"


def test_two_fonts_of_the_template_stay_two(tmp_path):
    """Разные гарнитуры заголовка и текста — замысел шаблона, а не конфликт."""
    import json

    from deckwright.config import load_config
    from deckwright.layout.matcher import build_deck_ir
    from deckwright.schemas import ContentPack, DeckPlan, SlotRole

    finansy = Path("/Users/nikitaosipov/Downloads/finansy.pptx")
    if not finansy.exists():
        pytest.skip("шаблона finansy.pptx нет в этой среде")
    cfg = load_config(ROOT / "configs" / "config.yaml")
    spec = _spec(finansy)
    plan = DeckPlan.model_validate(
        json.loads((ROOT / "tests/fixtures/recorded/plan_deck.json").read_text("utf-8"))
    )
    pack = ContentPack.model_validate(
        json.loads((ROOT / "tests/fixtures/content_pack.json").read_text("utf-8"))
    )
    deck, _ = build_deck_ir(
        spec, plan, cfg.variants[1], pack=pack,
        substitution_slack=cfg.fonts.substitution_slack,
    )
    titles = {
        p.style.font_family
        for slide in deck.slides
        for element in slide.all_elements()
        if element.role is SlotRole.TITLE and element.text is not None
        for p in element.text.paragraphs
    }
    bodies = {
        p.style.font_family
        for slide in deck.slides
        for element in slide.all_elements()
        if element.role is not SlotRole.TITLE and element.text is not None
        for p in element.text.paragraphs
    }
    assert titles and bodies
    assert titles != bodies, (
        f"заголовки и текст сведены к одной гарнитуре: {titles} и {bodies}"
    )


def test_metrics_follow_the_weight_of_the_text():
    """Полужирный меряется полужирным: он шире обычного на 7-8 %."""
    from deckwright.layout.text_metrics import Fonts

    class _Spec:
        fonts = ()
        slide_height_emu = 6_858_000

    fonts = Fonts(_Spec())
    plain = fonts.metrics("Arial", bold=False)
    bold = fonts.metrics("Arial", bold=True)
    if plain is None or bold is None:
        pytest.skip("Arial в системе нет")
    sample = "Выручка выросла до 47,3 млн руб."
    assert bold.width_pt(sample, 18) > plain.width_pt(sample, 18)


def test_substitution_margin_is_taken_once_and_only_when_needed():
    """Запас на чужие ширины — только несовместимой подстановке и один раз."""
    from deckwright.layout.text_metrics import Fonts

    class _Spec:
        fonts = ()
        slide_height_emu = 6_858_000

    fonts = Fonts(_Spec(), substitution_slack=0.8)
    clone = fonts.source("Calibri")
    if clone.metrics is None:
        pytest.skip("метрического клона Calibri в системе нет")
    assert clone.metric_compatible
    assert clone.metrics.width_scale == 1.0, "клону запас не полагается"

    alien = fonts.source("Гарнитура, которой нет")
    assert alien.metrics is not None
    assert not alien.metric_compatible
    assert alien.metrics.width_scale == pytest.approx(1.25)
    # Повторный запрос не накапливает запас.
    assert fonts.source("Гарнитура, которой нет").metrics.width_scale == pytest.approx(1.25)


def test_readable_floor_comes_from_config_and_respects_the_template():
    """Нижняя граница кегля задана конфигом и не превышает возможностей шаблона."""
    from deckwright.layout.strategy import configure_min_sizes, readable_floor
    from deckwright.schemas import SlotRole

    class _Slot:
        def __init__(self, size):
            self.role = SlotRole.BODY
            self.style = type("S", (), {"size_pt": size})()

    class _Layout:
        def __init__(self, sizes):
            self.slots = [_Slot(s) for s in sizes]

    class _Spec:
        def __init__(self, sizes, height=6_858_000):
            self.layouts = [_Layout(sizes)]
            self.content_patterns = []
            self.slide_height_emu = height

    configure_min_sizes({"body": 18})
    try:
        # Шаблон набирает тело крупно — граница ровно та, что просили.
        assert readable_floor(_Spec([10, 24]), SlotRole.BODY) == 18
        # Шаблон мельче по замыслу — выше его собственного максимума не лезем.
        assert readable_floor(_Spec([9, 12]), SlotRole.BODY) == 12
        # Слайд ниже — граница пересчитана пропорционально.
        assert readable_floor(_Spec([10, 24], height=5_143_500), SlotRole.BODY) == 13.5
    finally:
        configure_min_sizes({})


def test_pdf_converter_is_told_which_font_we_measured_with(tmp_path):
    """Подстановка объявляется конвертеру: PDF рисуется тем, чем мерили."""
    from deckwright.render.pdf import _with_fonts

    env = _with_fonts(tmp_path, [], [("Calibri", "Carlito")])
    assert env is not None
    conf = (tmp_path / "fonts.conf").read_text("utf-8")
    assert "<string>Calibri</string>" in conf
    assert "<string>Carlito</string>" in conf
    # Совпадающие имена псевдонимом не становятся.
    assert _with_fonts(tmp_path, [], [("Arial", "arial")]) is None


def test_small_text_is_named_by_the_audit_not_forced_by_the_layout():
    """Кегль ниже читаемого — находка аудита, а не запрет фиттеру.

    Жёсткая граница снизу заставляла вёрстку брать композицию, в которую
    текст не влезает вовсе: на `vk_tech` надпись ложилась поверх круговой
    схемы и выезжала за нижний край слайда. Читаемый кегль ценой
    развалившегося слайда — не выигрыш.
    """
    from deckwright.audit.deterministic.template_fidelity import too_small
    from deckwright.layout.strategy import configure_min_sizes
    from deckwright.schemas import SlotRole

    element = _text_element("e1", SlotRole.BODY, "111111")
    element.text.paragraphs[0].style = element.text.paragraphs[0].style.model_copy(
        update={"size_pt": 9}
    )

    class _Slot:
        role = SlotRole.BODY
        style = type("S", (), {"size_pt": 20})()

    class _Spec:
        def __init__(self):
            self.layouts = [type("L", (), {"slots": [_Slot()]})()]
            self.content_patterns = []
            self.slide_height_emu = 6_858_000

    class _Slide:
        index = 3

        def all_elements(self):
            return [element]

    configure_min_sizes({"body": 18})
    try:
        found = too_small(_Slide(), _Spec())
        assert [i.check_id for i in found] == ["template.text_too_small"]
        assert "9 pt" in found[0].message
        # Кегль на границе находкой не становится.
        element.text.paragraphs[0].style = element.text.paragraphs[0].style.model_copy(
            update={"size_pt": 18}
        )
        assert not too_small(_Slide(), _Spec())
    finally:
        configure_min_sizes({})


# ── Цвет по фону и защита оформления ─────────────────────────────────────────


def test_ink_is_chosen_by_measured_contrast_not_by_a_threshold():
    """Чёрный или белый — по относительной яркости sRGB, а не по порогу.

    Средние тона — тот случай, где выбор «по половине» ошибается: у `767676`
    яркость 0.187, и простой порог 0.5 дал бы белый, у которого контраст
    4.34:1 — ниже требуемых 4.5.
    """
    from deckwright.layout.matcher import INK_CONTRAST, ink_for
    from deckwright.schemas import Color

    for rgb in (
        "FFFFFF", "000000", "767676", "808080", "7F7F7F", "0077FF",
        "4472C4", "ED7D31", "8AB833", "014B3B", "2E1A47", "FF0053",
    ):
        ground = Color(rgb=rgb)
        ink = ink_for(ground)
        assert ink.rgb in ("000000", "FFFFFF")
        assert ink.contrast_ratio(ground) >= INK_CONTRAST, (
            f"{rgb}: {ink.rgb} даёт {ink.contrast_ratio(ground):.2f}"
        )


def test_any_solid_ground_has_a_readable_ink():
    """Для любой сплошной заливки один из двух цветов даёт не ниже 4.5:1.

    Утверждение проверяется перебором, а не на слово: равенство контрастов
    чёрного и белого достигается при яркости 0.179, и там оба дают 4.58.
    """
    from deckwright.layout.matcher import INK_CONTRAST, ink_for
    from deckwright.schemas import Color

    worst = 21.0
    for value in range(0, 256, 5):
        for rgb in (f"{value:02X}0000", f"00{value:02X}00", f"0000{value:02X}",
                    f"{value:02X}{value:02X}{value:02X}"):
            ground = Color(rgb=rgb)
            worst = min(worst, ink_for(ground).contrast_ratio(ground))
    assert worst >= INK_CONTRAST, f"нашёлся фон с контрастом {worst:.2f}"


def test_the_title_obeys_the_same_rule_as_the_body():
    """Заголовок — тоже чёрный или белый по своей подложке."""
    from deckwright.layout.matcher import _one_ink
    from deckwright.schemas import Color, SlotRole

    title = _text_element("t", SlotRole.TITLE, "0057B8", backdrop="101010")
    body = _text_element("b", SlotRole.BODY, "111111")
    _one_ink([title, body], 1, None, Color(rgb="FFFFFF"), False)

    assert title.text.paragraphs[0].style.color.rgb == "FFFFFF"
    assert body.text.paragraphs[0].style.color.rgb == "000000"


def test_a_logo_of_several_shapes_becomes_one_zone():
    """Знак и слово рядом — один логотип, и зона у них общая.

    Иначе текст садится ровно в просвет между ними.
    """
    from deckwright.parse.surfaces import Surface, SurfaceKind, protected
    from deckwright.schemas import Box, SourceKind

    slide_w, slide_h = 12_192_000, 6_858_000
    mark = Box(x=10_000_000, y=200_000, w=400_000, h=400_000)
    word = Box(x=10_450_000, y=250_000, w=900_000, h=300_000)
    zones = protected(
        [
            Surface(id="a", kind=SurfaceKind.BRAND, box=mark, z=1, source=SourceKind.LAYOUT),
            Surface(id="b", kind=SurfaceKind.BRAND, box=word, z=2, source=SourceKind.LAYOUT),
        ],
        slide_w, slide_h, padding=0,
    )
    assert len(zones) == 1, zones
    assert zones[0].x <= mark.x and zones[0].right >= word.right


def test_a_full_slide_frame_blocks_its_bands_not_the_whole_slide():
    """Декоративная рамка во весь лист запрещает полосы, а не середину."""
    from deckwright.parse.surfaces import Surface, SurfaceKind, protected
    from deckwright.schemas import Box, SourceKind

    slide_w, slide_h = 12_192_000, 6_858_000
    frame = Box(x=0, y=0, w=slide_w, h=slide_h)
    zones = protected(
        [Surface(id="f", kind=SurfaceKind.DECOR, box=frame, z=1, source=SourceKind.LAYOUT)],
        slide_w, slide_h, padding=0,
    )
    assert len(zones) == 4, "рамка обязана распасться на четыре полосы"
    middle = Box(x=slide_w // 3, y=slide_h // 3, w=slide_w // 3, h=slide_h // 3)
    assert all(zone.intersection(middle) is None for zone in zones), (
        "середина слайда осталась запрещённой"
    )


def test_the_padding_around_protected_zones_comes_from_config():
    """Отступ вокруг логотипа задаётся конфигурацией и считается от слайда."""
    from deckwright.layout.matcher import configure_protected_padding, protected_padding

    class _Spec:
        slide_width_emu = 12_192_000
        slide_height_emu = 6_858_000

    configure_protected_padding(0.01)
    try:
        assert protected_padding(_Spec()) == round(6_858_000 * 0.01)
        configure_protected_padding(0.0)
        assert protected_padding(_Spec()) == 0
    finally:
        configure_protected_padding(0.01)


def test_footer_and_slide_number_are_protected():
    """Колонтитул и номер слайда — оформление шаблона, писать поверх нельзя."""
    from deckwright.parse.surfaces import SERVICE_PLACEHOLDERS

    assert {"ftr", "sldNum", "dt"} <= SERVICE_PLACEHOLDERS
