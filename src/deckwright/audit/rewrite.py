"""Переписывание текста слайда по выбранной находке — шаг с моделью.

Находки «сократить формулировку», «разнести пункты», «текст не помещается»
помечены `ASSISTED` не из осторожности: механической операции у них нет.
Выкинуть половину пункта можно только зная, что в нём важно, а это решение о
содержании. Поэтому шаг запускается **только по явному выбору человека** и
только при включённом `run.rewrite_assisted` — без флага выбранная находка
остаётся помеченной «требует редактирования», и прогон в модель не ходит.

Переписывание идёт в тех же рамках, что и планирование, и это условие
сходимости цикла, а не вежливость. Текст, переписанный без ограничений длины,
снова не влезет в рамку, и следующая итерация принесёт ту же находку. Поэтому
ответ модели принимается, только если он:

* укладывается в бюджет длины, снятый с шаблона;
* не вводит чисел, которых на слайде не было;
* не трогает состав блоков — идентификаторы те же, новых нет.

Не прошедший проверку ответ отклоняется целиком по слайду, находка остаётся, и
причина называется вслух. Молча принять текст с выдуманным числом нельзя:
происхождение фактов проверяется детерминированно, и его сохранность — это
то, ради чего проверка вообще существует.

Правка применяется к `DeckPlan`, а не к `SlideIR`: текст — это содержание, а
содержание живёт в плане. Колода после правки пересобирается вёрсткой заново,
и фиттер получает шанс уложить текст без уменьшения кегля.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from deckwright.config import agent_prompt
from deckwright.plan.budget import LengthBudget
from deckwright.plan.figures import numbers_in
from deckwright.plan.planner import Prompt, load_prompt
from deckwright.schemas import DeckPlan, Issue, SlidePlan

# Находки, у которых переписывание текста — осмысленный ответ. Остальные
# ASSISTED (в частности `review_contextual_finding`) применить нечем: там
# человек смотрит и решает сам.
REWRITABLE_ACTIONS = frozenset({"shorten_paragraph", "shorten_or_split", "split_bullets"})


class RewrittenBlock(BaseModel):
    """Блок плана с переписанным текстом."""

    id: str
    heading: str = ""
    items: list[str] = Field(default_factory=list)


class RewrittenSlide(BaseModel):
    """Ответ модели на один слайд."""

    takeaway_title: str = ""
    blocks: list[RewrittenBlock] = Field(default_factory=list)


@dataclass
class RewriteOutcome:
    """План после правки и честный список того, что не применилось."""

    plan: DeckPlan
    rewritten: list[int] = field(default_factory=list)
    # {номер слайда: причина отказа}
    rejected: dict[int, str] = field(default_factory=dict)
    prompt: Prompt | None = None


def rewritable(issues: list[Issue]) -> list[Issue]:
    """Находки, которые этот шаг умеет отработать."""
    return [issue for issue in issues if issue.fix.action in REWRITABLE_ACTIONS]


def _slide_text(slide: SlidePlan) -> str:
    parts = [slide.takeaway_title, slide.subtitle]
    for block in slide.blocks:
        parts.append(block.heading)
        parts.extend(block.items)
    return "\n".join(part for part in parts if part)


def _format_blocks(slide: SlidePlan) -> str:
    if not slide.blocks:
        return "(блоков нет)"
    lines = []
    for block in slide.blocks:
        head = f" — {block.heading}" if block.heading else ""
        lines.append(f"[{block.id}] {block.kind.value}{head}")
        lines.extend(f"  - {item}" for item in block.items)
    return "\n".join(lines)


def _format_findings(issues: list[Issue]) -> str:
    return "\n".join(f"- {issue.message} ({issue.fix.description})" for issue in issues)


def _capacity(issues: list[Issue]) -> int:
    """Во сколько строк надо уложиться на этом слайде.

    Берётся из находки: фиттер померил рамку и знает точное число. Без него
    промпт говорит «покороче», и живой прогон показал, чем это кончается —
    модель сократила каждую строку втрое, число абзацев не тронула, и
    переполнение осталось: в рамку помещается две строки, а абзацев было пять.
    """
    limits = [
        int(issue.fix.params.get("capacity_lines") or 0)
        for issue in issues
        if issue.fix.params.get("capacity_lines")
    ]
    return min(limits) if limits else 0


def _too_long(text: str, limit: int) -> bool:
    return limit > 0 and len(text) > limit


def _check(
    slide: SlidePlan,
    answer: RewrittenSlide,
    budget: LengthBudget | None,
    capacity: int = 0,
) -> str | None:
    """Причина отказа или `None`, если правку можно принять.

    `capacity` — сколько строк помещается в рамку. Ответ, который в неё не
    влезет, принимать бессмысленно: следующий аудит вернёт ту же находку, а
    итерации кончатся.
    """
    known = {block.id for block in slide.blocks}
    unknown = [block.id for block in answer.blocks if block.id not in known]
    if unknown:
        return f"ответ вводит блоки, которых на слайде нет: {', '.join(unknown)}"

    title = answer.takeaway_title.strip()
    if budget is not None:
        if _too_long(title, budget.title_chars):
            return (
                f"заголовок {len(title)} символов при бюджете {budget.title_chars}: "
                "переписанный текст снова не влезет в рамку"
            )
        for block in answer.blocks:
            if len(block.items) > budget.max_bullets:
                return (
                    f"блок {block.id}: пунктов {len(block.items)} при пороге "
                    f"{budget.max_bullets}"
                )
            for item in block.items:
                if _too_long(item, budget.bullet_chars):
                    return (
                        f"блок {block.id}: пункт {len(item)} символов при бюджете "
                        f"{budget.bullet_chars}"
                    )
                words = len(item.split())
                if words > budget.max_words_per_bullet:
                    return (
                        f"блок {block.id}: пункт из {words} слов при пороге "
                        f"{budget.max_words_per_bullet}"
                    )

    if capacity:
        headings = {block.id: block.heading for block in slide.blocks}
        for block in answer.blocks:
            # Заголовок блока занимает строку наравне с пунктами, и остаётся
            # от плана, если модель своего не прислала: правка не имеет права
            # его выбрасывать.
            heading = block.heading or headings.get(block.id, "")
            occupied = len(block.items) + (1 if heading else 0)
            if occupied > capacity:
                return (
                    f"блок {block.id}: {occupied} строк при ёмкости рамки "
                    f"{capacity} — текст снова не поместится"
                )

    # Числа проверяются по всему слайду, а не по блоку: пункт мог переехать
    # в соседний блок, и это законно, а вот появиться из ниоткуда — нет.
    was = numbers_in(_slide_text(slide))
    became = numbers_in(
        "\n".join(
            [title, *(part for block in answer.blocks for part in [block.heading, *block.items])]
        )
    )
    invented = sorted(became - was)
    if invented:
        shown = ", ".join(f"{value:g}" for value in invented[:5])
        return f"переписывание ввело числа, которых на слайде не было: {shown}"
    return None


def _apply(slide: SlidePlan, answer: RewrittenSlide) -> SlidePlan:
    """Новый `SlidePlan` с переписанным текстом. Состав блоков не меняется."""
    changed = {block.id: block for block in answer.blocks}
    updated = slide.model_copy(deep=True)
    title = answer.takeaway_title.strip()
    if title:
        updated.takeaway_title = title
    for block in updated.blocks:
        replacement = changed.get(block.id)
        if replacement is None:
            continue
        if replacement.heading:
            block.heading = replacement.heading
        if replacement.items:
            block.items = list(replacement.items)
    return updated


def rewrite(
    plan: DeckPlan,
    issues: list[Issue],
    client,
    budget: LengthBudget | None = None,
    length_limits: str = "",
    prompts_dir: str | Path | None = None,
) -> RewriteOutcome:
    """Переписывает слайды, на которых есть выбранные текстовые находки.

    Вызов на слайд, а не на находку: две находки на одном слайде — это один
    текст, и просить модель переписать его дважды значит получить два разных
    текста и выбирать между ними монеткой.
    """
    targets: dict[int, list[Issue]] = {}
    for issue in rewritable(issues):
        targets.setdefault(issue.slide_index, []).append(issue)
    if not targets:
        return RewriteOutcome(plan=plan)

    prompt = load_prompt(agent_prompt("shorten_text"), prompts_dir)
    limits = length_limits or (
        budget.as_prompt_lines() if budget is not None else "(ограничений не задано)"
    )
    updated = plan.model_copy(deep=True)
    by_index = {slide.index: position for position, slide in enumerate(updated.slides)}

    outcome = RewriteOutcome(plan=updated, prompt=prompt)
    for index in sorted(targets):
        position = by_index.get(index)
        if position is None:
            outcome.rejected[index] = "слайда с таким номером в плане нет"
            continue
        slide = updated.slides[position]
        capacity = _capacity(targets[index])
        slide_limits = limits
        if capacity:
            slide_limits = (
                f"- В рамку этого слайда помещается {capacity} строк текста, "
                f"и это главное ограничение: сократить каждую строку, не "
                f"сократив их число, не поможет. Заголовок блока занимает "
                f"строку наравне с пунктом, то есть при заголовке пунктов "
                f"остаётся {max(0, capacity - 1)}.\n" + limits
            )
        text = prompt.render(
            language=plan.language,
            title=slide.takeaway_title,
            intent=slide.intent.value,
            blocks=_format_blocks(slide),
            findings=_format_findings(targets[index]),
            length_limits=slide_limits,
        )
        try:
            answer = client.complete(prompt.step, text, RewrittenSlide)
        except Exception as failure:  # отказ модели не отменяет остальную правку
            outcome.rejected[index] = f"модель не ответила: {failure}"
            continue

        problem = _check(slide, answer, budget, capacity)
        if problem is not None:
            outcome.rejected[index] = problem
            continue

        rewritten = _apply(slide, answer)
        if rewritten == slide:
            outcome.rejected[index] = "модель вернула тот же текст"
            continue
        updated.slides[position] = rewritten
        outcome.rewritten.append(index)

    if not outcome.rewritten:
        return outcome

    # План после правки обязан оставаться валидным по своей же схеме.
    # Присваивание в список Pydantic не проверяет, а дальше по нему поедет
    # вёрстка — пусть падает здесь, а не в рендере.
    try:
        outcome.plan = DeckPlan.model_validate(updated.model_dump())
    except ValidationError as failure:
        for index in outcome.rewritten:
            outcome.rejected[index] = f"план после правки не проходит схему: {failure}"
        outcome.plan = plan
        outcome.rewritten = []

    return outcome
