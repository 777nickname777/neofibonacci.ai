"""Построение `DeckPlan`. Скелетная версия слоя plan.

План описывает, что сказать и в каком порядке, и не знает про шаблон ничего:
ни координат, ни кеглей, ни имён layout'ов. Поэтому один и тот же план
раскладывается тремя вариантами вёрстки и ложится на любой шаблон.

Промпт живёт отдельным версионируемым файлом в `prompts/` — в коде его нет.

Контент-пакет приходит готовым: разбор входа — слой `content`.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from deckwright.llm.base import StructuredClient
from deckwright.plan.budget import LengthBudget, compute_budget
from deckwright.schemas import BlockKind, ContentPack, DeckPlan, PromptVersion, TemplateSpec

PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


class Prompt(BaseModel):
    """Промпт, загруженный из файла, вместе со своей версией."""

    name: str
    version: str
    step: str
    template: str
    description: str = ""
    sha256: str = Field(default="")

    def render(self, **values: object) -> str:
        return self.template.format(**values)

    def as_manifest_entry(self) -> PromptVersion:
        return PromptVersion(name=self.name, version=self.version, sha256=self.sha256)


def load_prompt(name: str, prompts_dir: str | Path | None = None) -> Prompt:
    """Читает промпт из `prompts/<name>.yaml` и считает его хэш.

    Хэш идёт в манифест: он гарантирует, что прогон воспроизводится именно тем
    текстом промпта, а не его более поздней правкой.
    """
    directory = Path(prompts_dir) if prompts_dir else PROMPTS_DIR
    path = directory / f"{name}.yaml"
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    return Prompt(**data, sha256=hashlib.sha256(raw).hexdigest())


def _format_facts(pack: ContentPack) -> str:
    """Факты с их числовыми значениями.

    Значение показывается отдельно, чтобы модель могла сослаться на него в
    формуле выведенного числа: без этого она не знает, что `f1` — это 42.
    """
    if not pack.facts:
        return "(фактов не предоставлено)"
    lines = []
    for fact in pack.facts:
        numeric = ""
        if fact.value is not None:
            unit = f" {fact.unit}" if fact.unit else ""
            numeric = f"  (значение: {fact.value}{unit})"
        lines.append(f"- [{fact.id}] {fact.text}{numeric}")
    return "\n".join(lines)


def _format_series(pack: ContentPack) -> str:
    if not pack.series:
        return "(числовых рядов не предоставлено)"
    lines = []
    for series in pack.series:
        points = ", ".join(f"{p.label}={p.value}" for p in series.points)
        unit = f" ({series.unit})" if series.unit else ""
        lines.append(f"- [{series.id}] {series.name}{unit}: {points}")
    return "\n".join(lines)


def _format_quotes(pack: ContentPack) -> str:
    if not pack.quotes:
        return "(цитат не предоставлено)"
    lines = []
    for quote in pack.quotes:
        who = ", ".join(part for part in (quote.author, quote.role) if part)
        lines.append(f"- [{quote.id}] «{quote.text}»" + (f" — {who}" if who else ""))
    return "\n".join(lines)


def slide_count_text(slide_count: int | None, min_slides: int, max_slides: int) -> str:
    """Сколько слайдов просить: заданное число или диапазон ТЗ на выбор модели.

    Число, заданное пользователем, — закон. Не задано — модель выбирает сама
    в границах ТЗ (10–15) по объёму материала: брифу в две фразы хватит
    нижней границы, отчёту на десять страниц нужна верхняя.
    """
    if slide_count:
        return str(slide_count)
    return (
        f"от {min_slides} до {max_slides} (число выбери сам: чем меньше материала, "
        f"тем ближе к {min_slides}; первый и последний слайды входят в счёт)"
    )


def build_plan(
    pack: ContentPack,
    client: StructuredClient,
    slide_count: int | str,
    spec: TemplateSpec | None = None,
    max_bullets: int = 6,
    max_words_per_bullet: int = 15,
    substitution_slack: float = 0.8,
    prompts_dir: str | Path | None = None,
    block_limits: tuple[tuple[str, int, int], ...] = (),
) -> tuple[DeckPlan, Prompt, LengthBudget | None]:
    """План, использованный промпт и бюджеты длины.

    Бюджеты считаются по шаблону, если он передан: у каждого шаблона своя
    заголовочная рамка и свой кегль, и «слишком длинно» у них разное. Без
    шаблона модель работает по одним порогам плотности из ТЗ — план тогда
    может не влезть, и разбираться с этим придётся фиттеру.
    """
    prompt = load_prompt("plan_deck.v5", prompts_dir)
    budget = (
        compute_budget(
            spec, max_bullets, max_words_per_bullet, substitution_slack=substitution_slack
        )
        if spec is not None
        else None
    )
    if budget is not None and block_limits:
        budget = replace(budget, block_limits=block_limits)
    limits = (
        budget.as_prompt_lines()
        if budget is not None
        else (
            f"- пунктов на слайде: не больше {max_bullets}\n"
            f"- пункт списка: не длиннее {max_words_per_bullet} слов"
        )
    )

    brief = pack.brief
    text = prompt.render(
        topic=brief.topic,
        purpose=brief.purpose.value,
        audience=brief.audience or "не указана",
        goal=brief.goal or "не указана",
        language=brief.language,
        extra_instructions=brief.extra_instructions or "нет",
        request=brief.request or "(не передан: материалы — документы)",
        quotes=_format_quotes(pack),
        facts=_format_facts(pack),
        series=_format_series(pack),
        slide_count=slide_count,
        length_limits=limits,
    )
    plan = client.complete(step=prompt.step, prompt=text, schema=DeckPlan)
    return resolve_facts(resolve_quotes(plan, pack), pack), prompt, budget


def resolve_quotes(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Блок цитаты получает её текст и автора из контент-пакета.

    Модель ссылается на цитату идентификатором и текст не переписывает — так
    цитата остаётся дословной. Но вёрстка кладёт на слайд строки блока, а у
    блока с одним `quote_id` их нет: слайд с цитатой выходил пустым (живой
    прогон на docx-отчёте, все три варианта).
    """
    quotes = {quote.id: quote for quote in pack.quotes}
    plan = plan.model_copy(deep=True)
    for slide in plan.slides:
        for block in slide.blocks:
            quote = quotes.get(block.quote_id or "")
            if quote is None or block.items:
                continue
            who = ", ".join(part for part in (quote.author, quote.role) if part)
            block.items = [f"«{quote.text}»"] + ([f"— {who}"] if who else [])
    return plan


# Блоки, чей текст — строки пунктов. У графика, таблицы, KPI и картинки
# содержимое другое, и текст фактов в них подставлять нельзя.
_TEXT_BLOCKS = (BlockKind.PARAGRAPH, BlockKind.BULLETS, BlockKind.STEPS)


def resolve_facts(plan: DeckPlan, pack: ContentPack) -> DeckPlan:
    """Текстовый блок, где у модели есть только `fact_ids`, получает текст фактов.

    Живой прогон pdf × vk_tech: у слайда-решения один абзац с `fact_ids` и
    без строк — слайд выходил пустым во всех трёх вариантах. Текст факта
    взят из входа и уже сверен, так что подставлять его безопасно.
    """
    facts = {fact.id: fact for fact in pack.facts}
    plan = plan.model_copy(deep=True)
    for slide in plan.slides:
        for block in slide.blocks:
            if block.kind not in _TEXT_BLOCKS or block.items:
                continue
            block.items = [facts[fact_id].text for fact_id in block.fact_ids if fact_id in facts]
    return plan
