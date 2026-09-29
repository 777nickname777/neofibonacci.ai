"""Контракты планирования: бюджеты длины и происхождение чисел.

Обе вещи вскрыл живой прогон. Модель написала «сократилось в 4.6 раза» —
числа, которого в материалах нет, — и сложила таблицу в строки с
разделителем, потому что деть её было некуда. Здесь проверяется, что контракт
это теперь ловит, а проверка остаётся детерминированной.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from deckwright.layout.text_metrics import characters_that_fit, load_metrics, wrap
from deckwright.parse.opener import parse_template
from deckwright.plan.budget import compute_budget
from deckwright.plan.figures import FormulaError, evaluate_formula, parse_number, verify
from deckwright.schemas import (
    BlockKind,
    ContentBlock,
    ContentPack,
    DeckPlan,
    Fact,
    Figure,
    FigureKind,
    TableData,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def pack() -> ContentPack:
    return ContentPack.model_validate(
        json.loads((FIXTURES / "content_pack.json").read_text("utf-8"))
    )


@pytest.fixture(scope="module")
def recorded_plan() -> DeckPlan:
    return DeckPlan.model_validate(
        json.loads((FIXTURES / "recorded" / "plan_deck.json").read_text("utf-8"))
    )


# ── Таблица как структура ────────────────────────────────────────────────────

def test_table_block_must_carry_a_table():
    """Строки с разделителем таблицей не считаются.

    Иначе вёрстке пришлось бы разбирать текст обратно в структуру, а контракт
    обязан нести структуру.
    """
    with pytest.raises(ValidationError, match="таблицы не приложено"):
        ContentBlock(
            id="b",
            kind=BlockKind.TABLE,
            items=["Метрика | До | После", "MTTD | 42 | 9"],
        )


def test_table_rows_must_match_columns():
    with pytest.raises(ValidationError, match="по ширине заголовка"):
        TableData(columns=["Метрика", "До", "После"], rows=[["MTTD", "42"]])


def test_recorded_plan_carries_a_structured_table(recorded_plan):
    tables = [
        block.table
        for slide in recorded_plan.slides
        for block in slide.blocks
        if block.table
    ]
    assert tables, "в записанном плане нет ни одной таблицы"
    assert all(len(row) == len(table.columns) for table in tables for row in table.rows)


# ── Происхождение чисел ──────────────────────────────────────────────────────

def test_derived_figure_without_a_formula_is_rejected():
    with pytest.raises(ValidationError, match="без формулы"):
        Figure(text="в 4.6 раза", kind=FigureKind.DERIVED, fact_ids=["f1", "f3"])


def test_cited_figure_with_a_formula_is_rejected():
    """Число либо взято из материалов, либо выведено. Третьего нет."""
    with pytest.raises(ValidationError, match="с формулой"):
        Figure(text="42", kind=FigureKind.CITED, fact_ids=["f1"], formula="f1 * 1")


def test_derived_figure_is_recomputed_not_searched(pack):
    """«4.6» в материалах не встречается ни разу, и это не выдумка."""
    figure = Figure(
        text="в 4.6 раза", kind=FigureKind.DERIVED, fact_ids=["f1", "f3"], formula="f1 / f3"
    )
    assert verify(figure, pack) is None


def test_wrong_derived_figure_is_caught(pack):
    figure = Figure(
        text="в 9.9 раза", kind=FigureKind.DERIVED, fact_ids=["f1", "f3"], formula="f1 / f3"
    )
    problem = verify(figure, pack)
    assert problem and "не сходится" in problem


def test_cited_figure_that_is_not_in_the_facts_is_caught(pack):
    figure = Figure(text="77 минут", kind=FigureKind.CITED, fact_ids=["f1"])
    problem = verify(figure, pack)
    assert problem and "таких значений нет" in problem


def test_figure_referring_to_a_missing_fact_is_caught(pack):
    figure = Figure(text="5", kind=FigureKind.CITED, fact_ids=["нет-такого"])
    problem = verify(figure, pack)
    assert problem and "отсутствующие факты" in problem


def test_non_numeric_fact_is_not_declared_a_fabrication(pack):
    """Не каждый факт числовой. Проверить такое число нечем, и проверка
    обязана молчать, а не объявлять его выдуманным."""
    pack = pack.model_copy(
        update={"facts": [*pack.facts, Fact(id="fx", text="Команда выросла", source_doc_id="d1")]}
    )
    assert verify(Figure(text="7", kind=FigureKind.CITED, fact_ids=["fx"]), pack) is None


def test_formula_cannot_execute_code():
    """На вход приходит текст от модели; выполнять его как код нельзя."""
    for hostile in (
        "__import__('os').system('ls')",
        "open('/etc/passwd').read()",
        "f1 ** 999999",
    ):
        with pytest.raises(FormulaError):
            evaluate_formula(hostile, {"f1": 2.0})


def test_formula_division_by_zero_is_reported_not_raised():
    with pytest.raises(FormulaError, match="ноль"):
        evaluate_formula("f1 / f2", {"f1": 1.0, "f2": 0.0})


def test_number_is_parsed_out_of_prose():
    assert parse_number("в 4.6 раза") == pytest.approx(4.6)
    assert parse_number("34 %") == pytest.approx(34)
    assert parse_number("4,6 раза") == pytest.approx(4.6)
    assert parse_number("без чисел") is None


def test_every_figure_in_the_recorded_plan_checks_out(recorded_plan, pack):
    problems = [
        verify(figure, pack)
        for slide in recorded_plan.slides
        for figure in slide.figures
        if verify(figure, pack)
    ]
    assert not problems, problems


# ── Бюджеты длины ────────────────────────────────────────────────────────────

def test_budget_is_derived_from_the_template(template_paths):
    """У каждого шаблона свой бюджет: своя рамка и свой кегль."""
    budgets = [
        compute_budget(parse_template(path), max_bullets=6, max_words_per_bullet=15)
        for path in template_paths
    ]
    for budget in budgets:
        assert budget.title_chars > 0
        assert budget.bullet_chars > 0
        assert budget.max_bullets == 6
        assert budget.max_words_per_bullet == 15
        assert budget.measured_with, "не сказано, чем мерили"

    if len(budgets) > 1:
        assert len({b.title_chars for b in budgets}) > 1, (
            "бюджеты одинаковы у разных шаблонов — значит считаются не по шаблону"
        )


def test_budget_reaches_the_prompt(template_paths):
    budget = compute_budget(parse_template(template_paths[0]), 6, 15)
    lines = budget.as_prompt_lines()
    assert str(budget.title_chars) in lines
    assert str(budget.max_bullets) in lines
    assert "15 слов" in lines


def _empty_spec():
    from deckwright.schemas import TemplateSpec

    return TemplateSpec(
        template_sha256="0" * 64,
        source_name="empty.pptx",
        slide_width_emu=12_192_000,
        slide_height_emu=6_858_000,
    )


def test_budget_names_the_substitution_it_measured_with():
    """Шаблон без встроенных шрифтов меряется подставленным — и обязан об
    этом сказать: расчёт на чужих метриках расходится с тем, что увидит
    человек."""
    budget = compute_budget(_empty_spec(), 6, 15)
    assert budget.title_chars > 0
    assert "подстановка" in budget.measured_with


def test_budget_without_any_font_states_no_lengths(monkeypatch):
    """Если шрифтов нет вовсе, длины неизвестны — и выдумывать их нельзя.

    Порог плотности из ТЗ при этом остаётся: он от шрифта не зависит.
    """
    monkeypatch.setattr("deckwright.layout.text_metrics._system_font", lambda: None)
    budget = compute_budget(_empty_spec(), 6, 15)
    assert budget.title_chars == 0
    assert budget.max_bullets == 6
    assert budget.max_words_per_bullet == 15


# ── Измерение текста ─────────────────────────────────────────────────────────

def test_wrapping_matches_measured_width():
    """Строка шире бокса обязана переноситься, а не считаться поместившейся."""
    from deckwright.layout.text_metrics import _system_font

    path = _system_font()
    if path is None:
        pytest.skip("в системе нет шрифтов")
    metrics = load_metrics(path)

    narrow = 914_400  # один дюйм
    lines = wrap("Пилот сократил время обнаружения инцидентов", metrics, 18, narrow)
    assert len(lines) > 1


def test_more_characters_fit_at_a_smaller_size():
    from deckwright.layout.text_metrics import _system_font

    path = _system_font()
    if path is None:
        pytest.skip("в системе нет шрифтов")
    metrics = load_metrics(path)
    box_w, box_h = 8 * 914_400, 914_400
    assert characters_that_fit(metrics, 18, box_w, box_h) > characters_that_fit(
        metrics, 36, box_w, box_h
    )


def test_planner_is_told_how_long_the_cover_and_closing_may_be():
    """Обложку и финал меряют их рамками и шаблонным кеглем, как и остальное."""
    from deckwright.plan.budget import LengthBudget

    budget = LengthBudget(
        title_chars=40, subtitle_chars=30, bullet_chars=50, max_bullets=6,
        max_words_per_bullet=15, measured_with="test",
        block_limits=(
            ("cover_title", 1, 60), ("cover_text", 0, 0), ("closing_text", 1, 90),
        ),
    )
    lines = budget.as_prompt_lines()
    assert "титульный слайд: заголовок не длиннее 60 символов" in lines
    assert "места под подзаголовок нет — только заголовок" in lines
    assert "завершающий слайд: подзаголовок под ним — один абзац не длиннее 90" in lines


def test_figure_citing_a_series_point_is_checked_against_the_series(pack):
    """Живой план: число с графика ссылается на ряд, а не на факт.

    Раньше такое число объявлялось ссылкой «на отсутствующие факты», хотя
    точка ряда во входе есть.
    """
    assert verify(Figure(text="21 мин", kind=FigureKind.CITED, fact_ids=["ser_mttd"]), pack) is None
    problem = verify(Figure(text="30 мин", kind=FigureKind.CITED, fact_ids=["ser_mttd"]), pack)
    assert problem and "таких значений нет" in problem
    derived = Figure(
        text="33 мин", kind=FigureKind.DERIVED, fact_ids=["ser_mttd"], formula="42 - 9"
    )
    assert verify(derived, pack) is None


def test_cited_figure_sign_is_direction_not_value(pack):
    """«−23 %» на слайде для факта «снизилось на 23 %» — то же число."""
    fact = pack.facts[0]
    figure = Figure(text=f"-{fact.value:g}", kind=FigureKind.CITED, fact_ids=[fact.id])
    assert verify(figure, pack) is None


def test_cited_figure_may_be_any_number_of_the_fact_text(pack):
    """«Бюджет 48 млн, освоено 31 млн»: value 48, но «31» — тоже цитата факта."""
    from deckwright.schemas import ContentPack

    two = ContentPack.model_validate(
        {
            **pack.model_dump(),
            "facts": [{"id": "f1", "text": "Бюджет 48 млн рублей, освоено 31 млн",
                       "source_doc_id": pack.facts[0].source_doc_id, "value": 48}],
        }
    )
    assert verify(Figure(text="31 млн", kind=FigureKind.CITED, fact_ids=["f1"]), two) is None
    problem = verify(Figure(text="35 млн", kind=FigureKind.CITED, fact_ids=["f1"]), two)
    assert problem and "таких значений нет" in problem


def test_figure_without_kind_is_cited():
    """plan_deck.v6: у процитированного числа модель `kind` не пишет."""
    figure = Figure.model_validate({"text": "26", "fact_ids": ["f3"]})
    assert figure.kind is FigureKind.CITED
    # С формулой и без `kind` — выведенное: иначе план уходил на повтор.
    derived = Figure.model_validate({"text": "в 3 раза", "fact_ids": ["f3"], "formula": "f3 / 3"})
    assert derived.kind is FigureKind.DERIVED
    with pytest.raises(ValidationError, match="без формулы"):
        Figure.model_validate({"text": "в 3 раза", "kind": "derived", "fact_ids": ["f3"]})


def test_stray_deck_level_figures_do_not_cost_a_retry():
    """Пустое `figures` сверху — не повод переспрашивать модель.

    Живой прогон на выложенном сервисе: план пришёл с `figures: []` на
    уровне колоды, схема отвергла его как лишнее поле, и шаг ушёл на четыре
    повтора по 120 с — прогон сняли по времени. Пустой список не несёт
    ничего, и терять на нём бюджет незачем.
    """
    raw = {
        "title": "Колода",
        "purpose": "project",
        "figures": [],
        "slides": [
            {"index": 1, "intent": "title", "takeaway_title": "Заголовок"},
            {"index": 2, "intent": "closing", "takeaway_title": "Вывод"},
        ],
    }
    plan = DeckPlan.model_validate(raw)

    assert plan.slide_count == 2
    assert not hasattr(plan, "figures"), "figures колоде не принадлежит"
    assert plan.slides[0].figures == []


def test_figures_with_numbers_at_deck_level_are_named_not_swallowed():
    """Непустое `figures` сверху — числа без слайда: ответ невалиден.

    Молча выбросить их нельзя: по ним аудит проверяет происхождение чисел.
    Ошибка говорит, куда их класть, — её текст уходит модели на повтор, и
    это лучше, чем «Extra inputs are not permitted».
    """
    raw = {
        "title": "Колода",
        "purpose": "project",
        "figures": [{"text": "26", "fact_ids": ["f3"]}],
        "slides": [{"index": 1, "intent": "title", "takeaway_title": "Заголовок"}],
    }
    with pytest.raises(ValidationError, match="принадлежит слайду"):
        DeckPlan.model_validate(raw)
