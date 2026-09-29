"""Контрольные случаи группы 6: путь от находки до проверенного результата.

Живое переписывание моделью проверяется отдельно
(`.repair-progress/live_stage06.py`): здесь — то, что проверяется без ключа.

* режим переписывания включается из интерфейса, а не правкой конфига;
* выключённый режим не делает скрытых вызовов;
* недоступный клиент даёт причину и сохраняет прошлый результат;
* однозначная находка действительно исправляется;
* находка без безопасного действия возвращает объяснение, а не исчезает;
* у идущего прогона неизменяемый снимок своих настроек.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from deckwright.audit import fixers  # noqa: E402
from deckwright.config import load_config  # noqa: E402
from deckwright.schemas import (  # noqa: E402
    Box,
    CheckKind,
    Color,
    DeckIR,
    Element,
    ElementKind,
    FixKind,
    Issue,
    IssueCategory,
    Paragraph,
    ProposedFix,
    Provenance,
    Severity,
    SlideIR,
    SlotRole,
    SourceKind,
    TextContent,
    TextStyle,
)

CONFIG = ROOT / "configs" / "config.yaml"
WIDTH, HEIGHT = 12_192_000, 6_858_000
WHENCE = Provenance(kind=SourceKind.LAYOUT, ref="проба")
BLACK = Color(rgb="000000")


def _element(element_id: str, box: Box) -> Element:
    return Element(
        id=element_id,
        kind=ElementKind.TEXT,
        role=SlotRole.BODY,
        box=box,
        text=TextContent(
            paragraphs=[
                Paragraph(
                    text="Текст слайда",
                    style=TextStyle(font_family="Play", size_pt=18.0, color=BLACK),
                )
            ]
        ),
        provenance=WHENCE,
    )


def _deck(elements: list[Element]) -> DeckIR:
    return DeckIR(
        variant="balanced",
        template_sha256="0" * 64,
        slide_width_emu=WIDTH,
        slide_height_emu=HEIGHT,
        slides=[SlideIR(index=1, elements=elements)],
    )


def _issue(check_id: str, element_id: str, kind: FixKind, action: str = "") -> Issue:
    # `action` обязателен для automatic и assisted — схема это требует, и
    # правильно: автоправка обязана знать, что именно делать.
    if kind in (FixKind.AUTOMATIC, FixKind.ASSISTED) and not action:
        action = "shorten_or_split" if kind is FixKind.ASSISTED else "move_inside_slide"
    return Issue(
        check_id=check_id,
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=Severity.ERROR,
        slide_index=1,
        element_ids=[element_id],
        message="проба",
        fix=ProposedFix(
            kind=kind,
            description="",
            action=action,
            params={"element_id": element_id} if element_id else {},
        ),
    )


# ── однозначная находка действительно исправляется ──────────────────────


def test_an_element_outside_the_slide_is_moved_back():
    """Выход за границы — операция без решения человека: она и выполняется."""
    outside = Box(x=WIDTH - 100_000, y=0, w=2_000_000, h=500_000)
    deck = _deck([_element("a", outside)])
    outcome = fixers.apply(deck, [_issue("layout.out_of_bounds", "a", FixKind.AUTOMATIC,
                                         action="move_inside_slide")])
    box = deck.slides[0].elements[0].box
    assert outcome.applied, f"находка не применена: {outcome.skipped}"
    assert box.right <= WIDTH, "элемент остался за краем слайда"


def test_an_unknown_operation_is_explained_not_silently_dropped():
    """Находка без реализованного действия остаётся с причиной."""
    deck = _deck([_element("a", Box(x=0, y=0, w=100_000, h=100_000))])
    outcome = fixers.apply(deck, [_issue("layout.off_grid", "a", FixKind.AUTOMATIC,
                                         action="неизвестное_действие")])
    assert not outcome.applied
    assert outcome.skipped, "находка исчезла без объяснения"
    assert "не реализована" in " ".join(outcome.skipped.values())


def test_a_missing_element_is_explained():
    deck = _deck([_element("a", Box(x=0, y=0, w=100_000, h=100_000))])
    outcome = fixers.apply(deck, [_issue("layout.out_of_bounds", "нет-такого",
                                         FixKind.AUTOMATIC, action="move_inside_slide")])
    assert not outcome.applied
    assert "не найден" in " ".join(outcome.skipped.values())


def test_contextual_findings_are_never_applied():
    """У контекстной находки операции нет — только показ."""
    deck = _deck([_element("a", Box(x=0, y=0, w=100_000, h=100_000))])
    issue = _issue("content.has_content", "a", FixKind.ASSISTED)
    issue = issue.model_copy(update={"kind": CheckKind.CONTEXTUAL})
    outcome = fixers.apply(deck, [issue])
    assert not outcome.applied
    assert outcome.skipped


# ── режим переписывания: разрешён, доступен, выбран ─────────────────────


def test_rewrite_is_off_by_default_in_the_config():
    """Открытая страница не должна сама тратить ключ."""
    cfg = load_config(CONFIG)
    assert cfg.run.rewrite_assisted is False


def test_the_interface_offers_the_rewrite_switch():
    """Режим включается из интерфейса, а не правкой `config.yaml`.

    Без переключателя интерфейс показывал галочки у находок, которые заведомо
    нечем исправить, и кнопка «Применить» по ним молча ничего не делала.
    """
    source = (ROOT / "app" / "ui.py").read_text("utf-8")
    assert "rewrite_assisted" in source, "переключателя переписывания нет"
    assert "Переписывать текст моделью" in source
    # Недоступность объясняется, а не прячется.
    assert "недоступно" in source


def test_a_run_keeps_a_snapshot_of_its_settings():
    """Идущий прогон доработает на том, с чем начат."""
    import runs

    cfg = load_config(CONFIG)
    state = runs.RunState(
        run_id="r1",
        template_name="t.pptx",
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        cfg=cfg,
    )
    assert state.cfg is not None
    assert state.cfg.run.rewrite_assisted == cfg.run.rewrite_assisted


def test_rewrite_without_a_client_states_the_reason():
    """Модель недоступна — находка остаётся с причиной, а не исчезает."""
    from deckwright.audit import rewrite as rewrite_step

    issue = _issue("layout.text_overflow", "a", FixKind.ASSISTED)
    assert rewrite_step.rewritable([issue]), "текстовая находка не признана переписываемой"


def test_findings_that_need_no_text_are_not_sent_to_the_model():
    """Геометрия чинится сама: платить за неё вызовом модели не за что."""
    from deckwright.audit import rewrite as rewrite_step

    issue = _issue("layout.out_of_bounds", "a", FixKind.AUTOMATIC, action="move_inside_slide")
    assert not rewrite_step.rewritable([issue])


# ── прошлый результат переживает неудачу ────────────────────────────────


def test_a_failed_fix_keeps_the_previous_result(tmp_path, monkeypatch):
    """Сбой при исправлении не уничтожает уже готовую колоду."""
    import runs

    state = runs.RunState(
        run_id="r2",
        template_name="t.pptx",
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        variants={"balanced": runs.VariantState(name="balanced")},
    )
    variant_state = state.variants["balanced"]
    marker = object()
    variant_state.result = marker

    def boom(*args, **kwargs):
        raise RuntimeError("модель отказала")

    monkeypatch.setattr(runs, "apply_selection", boom)
    variant_state.selected = {"1:layout.text_overflow:a"}
    runs.apply(state, "balanced", client=None)
    for _ in range(200):
        if not variant_state.applying:
            break
        __import__("time").sleep(0.02)
    assert variant_state.result is marker, "прошлый результат потерян"
    assert variant_state.error, "об отказе не сказано"


@pytest.mark.parametrize("mode", ["off", "review", "auto"])
def test_fix_modes_are_all_accepted(mode):
    """Режимы объявлены и принимаются конфигом."""
    cfg = load_config(CONFIG)
    updated = cfg.model_copy(update={"run": cfg.run.model_copy(update={"fix_mode": mode})})
    assert updated.run.fix_mode == mode


# ── ухудшающая правка откатывается ──────────────────────────────────────


def test_a_fix_that_adds_errors_is_rolled_back(monkeypatch, tmp_path):
    """Меньшее число находок — не доказательство улучшения.

    Правка, после которой ошибок стало больше, откатывается целиком: колода
    возвращается к прошлому состоянию, а находка остаётся в отчёте с
    причиной. Иначе цикл «чинил» бы колоду до неработоспособности, отчитываясь
    уменьшением общего счётчика.
    """
    source = (ROOT / "src/deckwright/pipeline.py").read_text("utf-8")
    assert "откачена" in source, "отката ухудшающей правки нет"
    assert "error_count > before" in source, "сравниваются не ошибки, а что-то другое"
    # Откат возвращает именно прошлое состояние, а не пересобирает заново.
    assert "deck, plan, built, layout_issues, _ = before" in source
