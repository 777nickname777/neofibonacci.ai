"""Шесть дефектов вёрстки, найденных на листах пробы 4×4 и приёмки сентября.

Каждый тест собирает колоду по записанному живому плану (или по плану,
который воспроизводит ситуацию) и проверяет то, что было видно на листе:

* `vk_tech`: слайд с одним показателем — одна мелкая цифра под заголовком;
* `vk_tech` dense: таблица на тёмной обложке раздела;
* `zelenie_investicii`: сводная таблица кеглем 12 в верхней трети, «412.0»;
* `vk_workspace`: показатель на заголовке, ячейки таблицы голубые;
* шаблон экзаменов: три пункта 2 + 1 по карточкам «две + широкая»;
* синтетический шаблон с фото: одни и те же карточки на девяти слайдах.
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from deckwright.config import load_config
from deckwright.layout import strategy as layout_strategy
from deckwright.llm.fake import RecordedClient
from deckwright.parse.opener import parse_template
from deckwright.pipeline import lay_out_variant
from deckwright.schemas import BlockKind, ContentPack, ElementKind, SlotRole

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from make_photo_template import build as build_photo_template

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "config.yaml"
FIXTURES = Path(__file__).parent / "fixtures"
VK_TECH = ROOT / "data" / "templates" / "vk_tech.pptx"
VK_WORKSPACE = ROOT / "data" / "templates" / "vk_workspace.pptx"
ZELENIE = ROOT / "data" / "holdout" / "zelenie_investicii.pptx"
EXAM = ROOT / "data" / "holdout" / "podgotovka_k_ekzamenam.pptx"
VARIANTS = ("dense", "balanced", "airy")


def _deck(template: Path, source: Path, variant: str, out: Path, recorded: Path | None = None):
    if not template.exists():
        pytest.skip(f"нет шаблона {template.name}")
    pack = ContentPack.model_validate(json.loads((source / "pack.json").read_text("utf-8")))
    return lay_out_variant(
        template,
        pack,
        load_config(CONFIG),
        RecordedClient(recorded or source / "recorded"),
        variant,
        out / variant,
        fix_mode="off",
    ).deck


def _texts(slide) -> list:
    return [e for e in slide.all_elements() if e.text is not None and e.text.paragraphs]


def _slide_titled(deck, title: str):
    return next(
        slide
        for slide in deck.slides
        if any(p.text == title for e in _texts(slide) for p in e.text.paragraphs)
    )


# ── 1. vk_tech: один показатель на слайде — крупно ──────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_single_figure_is_not_smaller_than_the_title(tmp_path, variant):
    """«23 %», «27 %», «2 руб» — не строкой 16 pt под заголовком 24 pt.

    Живой план pdf × vk_tech (проба 4×4): три слайда подряд с одним
    показателем. Число — не мельче типичного заголовка шаблона.
    """
    deck = _deck(VK_TECH, FIXTURES / "vk_tech_pdf", variant, tmp_path)
    spec = parse_template(VK_TECH)
    title = layout_strategy.role_typical(spec, SlotRole.TITLE)
    for text in ("23 %", "27 %", "2 руб"):
        element = next(
            e
            for slide in deck.slides
            for e in _texts(slide)
            if e.role is not SlotRole.TITLE and e.text.paragraphs[0].text == text
        )
        assert element.text.paragraphs[0].style.size_pt >= title, (text, element.box)


# ── 2. vk_tech: таблица — на основном фоне, не на обложке раздела ───────────


def test_tables_stay_off_the_accent_background(tmp_path, monkeypatch):
    """Шесть таблиц на `vk_tech`: ни одна не садится на тёмную обложку.

    Мест под таблицу у `vk_tech` нет ни в одной композиции; когда светлые
    кончались, подбор по разнообразию уводил таблицу на тёмный раздел (dense,
    слайд 7 пробы). Шесть таблиц — план pdf × vk_tech с тремя повторёнными
    слайдами-рядами, ряды показаны таблицей.
    """
    plan = json.loads((FIXTURES / "vk_tech_pdf" / "recorded" / "plan_deck.json").read_text())
    slides = plan["slides"]
    extra = copy.deepcopy(slides[4:7])
    for slide in extra:
        for block in slide["blocks"]:
            block["id"] += "x"
    plan["slides"] = slides[:7] + extra + slides[7:]
    for number, slide in enumerate(plan["slides"], start=1):
        slide["index"] = number
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "plan_deck.json").write_text(json.dumps(plan, ensure_ascii=False), "utf-8")

    original = layout_strategy.Strategy.role_for

    def as_table(self, block):
        if block.kind is BlockKind.SERIES:
            return SlotRole.TABLE
        return original(self, block)

    monkeypatch.setattr(layout_strategy.Strategy, "role_for", as_table)
    deck = _deck(VK_TECH, FIXTURES / "vk_tech_pdf", "dense", tmp_path, recorded)
    with_tables = [
        slide
        for slide in deck.slides
        if any(e.kind is ElementKind.TABLE for e in slide.all_elements())
    ]
    assert len(with_tables) == 6
    assert [slide.index for slide in with_tables if slide.is_dark] == []


# ── 3. zelenie_investicii: сводная таблица читается ─────────────────────────


@pytest.mark.parametrize("variant", VARIANTS)
def test_summary_table_is_set_at_template_table_size(tmp_path, variant):
    """Сводная таблица — кеглем таблиц шаблона (18), числа — записью колоды.

    Ширина мерилась самой длинной ячейкой на все четыре колонки, и таблица
    выходила кеглем 12 в верхней трети слайда; ячейки «412.0» и «-2.0»
    модель писала по-программистски.
    """
    deck = _deck(ZELENIE, FIXTURES / "zelenie_pdf", variant, tmp_path)
    slide = _slide_titled(deck, "Итоговые показатели эффективности пилота")
    table = next(e for e in slide.all_elements() if e.kind is ElementKind.TABLE)
    assert table.table.cell_style.size_pt >= 18
    assert table.box.h >= deck.slide_height_emu // 3
    cells = [cell for row in table.table.rows for cell in row]
    assert "412" in cells and "318" in cells
    assert not [cell for cell in cells if cell.endswith(".0")]


# ── 4. vk_workspace: показатель не на заголовке, ячейки как у шаблона ───────


@pytest.mark.parametrize("variant", VARIANTS)
def test_figure_clears_an_overflowing_title(tmp_path, variant):
    """Заголовок в четыре строки при рамке на две — показатель ниже него.

    Место показателя донора заходит в низ рамки заголовка на 7 % своей
    площади, в пределах допуска; переполненный заголовок занимает и строки
    под рамкой. План — записанный `tests/fixtures/recorded` на пакете
    `content_pack.json`, слайд 6 на `vk_workspace`.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.json").write_text((FIXTURES / "content_pack.json").read_text("utf-8"))
    deck = _deck(VK_WORKSPACE, source, variant, tmp_path, FIXTURES / "recorded")
    slide = _slide_titled(
        deck, "Пилот подтвердил эффективность: время обнаружения сократилось в 4.6 раза"
    )
    title = next(e for e in slide.elements if e.role is SlotRole.TITLE)
    used = title.text.used_lines * title.text.paragraphs[0].style.size_pt * 1.2 * 12_700
    title_bottom = title.box.y + max(title.box.h, round(used))
    for element in slide.all_elements():
        if element is title:
            continue
        beside = element.box.x >= title.box.right or element.box.right <= title.box.x
        assert beside or element.box.y >= title_bottom, (element.id, element.box)
        if element.chart is not None:
            assert element.box.h >= deck.slide_height_emu // 4, element.box
    figure = next(e for e in _texts(slide) if "9 минут" in e.text.paragraphs[0].text)
    assert figure.text.paragraphs[0].text.startswith("9 минут"), "число — вперёд подписи"


def test_table_cells_are_written_as_the_template_writes_its_table(tmp_path):
    """Ячейки — светло-серым E4E7EA, как таблица шаблона, а не голубым места."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.json").write_text((FIXTURES / "content_pack.json").read_text("utf-8"))
    deck = _deck(VK_WORKSPACE, source, "dense", tmp_path, FIXTURES / "recorded")
    tables = [e.table for s in deck.slides for e in s.all_elements() if e.table is not None]
    assert tables
    assert {table.cell_style.color.rgb for table in tables} == {"E4E7EA"}


# ── 5. Шаблон экзаменов: три пункта — в три карточки ────────────────────────


@pytest.mark.parametrize("variant", ("balanced", "airy"))
def test_three_steps_fill_two_cards_and_the_wide_one(tmp_path, variant):
    """«Две карточки + широкая»: пункт на карточку, широкая — третья.

    Три пункта делились 2 + 1 по верхним карточкам, широкая под ними
    доставалась абзацу, растянутому к левому полю за обводку карточки.
    """
    deck = _deck(EXAM, FIXTURES / "exam_sales", variant, tmp_path)
    slide = _slide_titled(deck, "В IV квартале запускаем программу лояльности в приложении")
    steps = ("Запуск программы лояльности", "Продвижение через приложение",
             "Анализ оттока в Мурманске")
    placed = {
        p.text: e for e in _texts(slide) for p in e.text.paragraphs if p.text in steps
    }
    assert set(placed) == set(steps)
    assert len({id(element) for element in placed.values()}) == 3, "пункт — своя карточка"
    wide = placed["Анализ оттока в Мурманске"]
    top = placed["Запуск программы лояльности"]
    assert wide.box.y > top.box.bottom and wide.box.w > top.box.w
    # Ничто не вылезает левее карточек: фото слева ушло, колонка осталась.
    for element in _texts(slide):
        if element.role is not SlotRole.TITLE:
            assert element.box.x >= top.box.x - deck.slide_width_emu // 50, element.box


# ── 6. Синтетический шаблон с фото: карточки не на полколоды ────────────────


@pytest.fixture(scope="module")
def photo_template(tmp_path_factory) -> Path:
    return build_photo_template(tmp_path_factory.mktemp("photo") / "photo.pptx")


@pytest.mark.parametrize("source", ("roadmap_sales", "vk_tech_sales"))
@pytest.mark.parametrize("variant", VARIANTS)
def test_cards_do_not_take_half_the_deck(photo_template, tmp_path, source, variant):
    """Карточки брали 9–10 слайдов из 14–18: по типичному кеглю тела (24,
    из layout'ов мастера) проходили одни они, а текстовые композиции набраны
    16 и после двух повторов не рассматривались."""
    deck = _deck(photo_template, FIXTURES / source, variant, tmp_path)
    counts = Counter(slide.pattern_id for slide in deck.slides)
    assert max(counts.values()) <= 4, counts
    for slide in deck.slides:
        for element in slide.all_elements():
            if element.chart is not None:
                assert element.box.h >= deck.slide_height_emu // 4, (slide.index, element.box)


# ── 7. Живой прогон run 33: график высотой в 1 EMU ─────────────────────────


@pytest.mark.parametrize(
    ("template", "source"),
    (
        (ROOT / "data" / "templates" / "vk_education.pptx", "observability_education"),
        (VK_TECH, "sales_vk_tech_live"),
    ),
)
def test_charts_never_collapse(tmp_path, template, source):
    """График садится туда, где его видно, во всех трёх вариантах.

    Run 33: на `vk_tech` airy подбор видел место под график свободным, а
    сборка сначала сажала туда подзаголовок; на `vk_education` airy
    заголовок в четыре строки занял место фото. Оба раза график выходил
    высотой в 1 EMU, а слайд с одним блоком не уходил в другую композицию.
    """
    pack = ContentPack.model_validate(
        json.loads((FIXTURES / source / "pack.json").read_text("utf-8"))
    )
    prepared = None
    for variant in VARIANTS:
        laid = lay_out_variant(
            template, pack, load_config(CONFIG), RecordedClient(FIXTURES / source / "recorded"),
            variant, tmp_path / variant, fix_mode="off", prepared=prepared,
        )
        prepared = laid.prepared
        for slide in laid.deck.slides:
            for element in slide.all_elements():
                if element.chart is not None:
                    assert element.box.h >= laid.deck.slide_height_emu // 4, (
                        variant, slide.index, element.box,
                    )
