"""Сборка отчёта аудита: детерминированные проверки плюс контекстные.

Отчёт обязан быть честным в обе стороны. Находка — это найденная проблема, а
`skipped_checks` — проверка, которая **не выполнялась**, и почему. Пустой
список находок при недоступной модели означал бы «всё хорошо», хотя половина
вопросов даже не была задана.
"""

from __future__ import annotations

from pathlib import Path

from deckwright.audit.contextual import runner as contextual
from deckwright.audit.deterministic import content as content_checks
from deckwright.audit.deterministic import geometry, limits, template_fidelity
from deckwright.audit.registry import CHECKS, contextual_ids
from deckwright.schemas import AuditReport, DeckIR, DeckPlan, Severity, TemplateSpec


def audit_deck(
    deck: DeckIR,
    spec: TemplateSpec,
    plan: DeckPlan,
    pack,
    cfg,
    pptx_path: str | Path | None = None,
    pages: list[Path] | None = None,
    client=None,
    only_slides: set[int] | None = None,
    prompts_dir: str | Path | None = None,
    text_findings: list | None = None,
) -> AuditReport:
    """Полный аудит одного варианта колоды.

    `text_findings` — находки текстового прохода с другого варианта: вопросы
    этого прохода задаются по плану, а план у трёх вариантов один.
    """
    issues = []
    issues.extend(geometry.run(deck, spec))
    issues.extend(
        limits.run(deck, cfg.audit.min_fill_ratio, cfg.audit.max_fill_ratio)
    )
    issues.extend(template_fidelity.run(deck, spec, cfg.audit.contrast_min_ratio))
    issues.extend(
        content_checks.run(
            deck,
            plan,
            pack,
            pptx_path,
            max_bullets=cfg.audit.max_bullets_per_slide,
            max_words=cfg.audit.max_words_per_bullet,
            min_fill=cfg.audit.min_fill_ratio,
            spec=spec,
        )
    )

    skipped: dict[str, str] = {}
    if pages is None:
        for check_id in contextual_ids():
            skipped[check_id] = "картинок слайдов нет: контекстный проход не запускался"
    else:
        outcome = contextual.run(
            deck, plan, pages, client, cfg, only_slides, prompts_dir, text_findings
        )
        issues.extend(outcome.issues)
        skipped.update(outcome.skipped)

    # Порядок находок — от серьёзных к мелким и по слайдам: так их читает
    # человек, и так же их покажет интерфейс.
    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(key=lambda issue: (order[issue.severity], issue.slide_index, issue.check_id))

    return AuditReport(
        variant=deck.variant,
        issues=_without_repeats(issues),
        skipped_checks=skipped,
        checks_total=len(CHECKS),
    )


def _without_repeats(issues: list) -> list:
    """Схлопывает находки, неотличимые друг от друга.

    Проверка цвета обходит абзацы, и на элементе из пяти абзацев одного цвета
    она даёт пять находок с одинаковым текстом, одинаковой рамкой и одинаковым
    ключом. Человеку это пять одинаковых строк, интерфейсу — пять рамок с
    одним номером: выбрать по номеру нельзя, потому что номер не единственный.
    Обнаружено при живом прогоне интерфейса, а не рассуждением.

    Схлопывается только полностью совпадающее: текст находки несёт и номер
    абзаца, и имя элемента, поэтому разные проблемы остаются разными.
    """
    seen: set[tuple] = set()
    unique = []
    for issue in issues:
        mark = (
            issue.check_id,
            issue.slide_index,
            tuple(issue.element_ids),
            issue.message,
            issue.bbox.model_dump_json() if issue.bbox else "",
        )
        if mark in seen:
            continue
        seen.add(mark)
        unique.append(issue)
    return unique


def coverage() -> dict[str, int]:
    """Сколько проверок какого рода объявлено. Нужно `docs/AUDIT.md` и тесту."""
    counts: dict[str, int] = {}
    for item in CHECKS:
        counts[item.kind.value] = counts.get(item.kind.value, 0) + 1
    return counts
