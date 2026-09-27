"""Живой прогон контекстного аудита: замер вместо оценки.

Оценка «≈1180 токенов на картинку слайда при 96 dpi» взялась из умножения
площади на коэффициент и ни на чём больше не стояла. Здесь она заменяется
измерением, а заодно проверяется то, что оценкой не проверишь.

Четыре вопроса, на которые отвечает прогон:

1. **Сколько токенов на самом деле стоит картинка слайда.** Меряется разностью:
   один и тот же запрос уходит с картинкой и без неё, разница входных токенов
   и есть цена картинки. Косвенных оценок тут не нужно.
2. **Держит ли модель формат на одиннадцати вопросах.** Считаются повторы
   из-за невалидного JSON и отброшенные параметры.
3. **Сколько это занимает при ограничителе.** Время на слайд и на колоду.
4. **Видит ли аудит проблемы или отвечает «да» на всё.** Тот же проход идёт по
   заведомо испорченной колоде: если на ней «да» везде, аудит бесполезен, как
   бы хорошо он ни выглядел на чистой.

Четвёртый вопрос — главный. Аудит, который всегда доволен, проходит все тесты.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from deckwright.audit.contextual.runner import SlideAnswers, _questions, _slide_body
from deckwright.audit.spoil import Spoilage
from deckwright.plan.planner import load_prompt
from deckwright.schemas import DeckIR, DeckPlan


@dataclass
class SlideMeasurement:
    """Что стоил один слайд."""

    index: int
    seconds: float
    prompt_tokens: int
    completion_tokens: int
    retries: int
    answers: int
    negatives: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ProbeResult:
    slides: list[SlideMeasurement] = field(default_factory=list)
    image_token_cost: int | None = None
    text_only_prompt_tokens: int | None = None
    with_image_prompt_tokens: int | None = None
    # Проход по тексту колоды: шесть вопросов из одиннадцати задаются им.
    deck_pass: SlideMeasurement | None = None

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.slides)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.slides)

    @property
    def total_completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.slides)

    @property
    def negatives(self) -> list[str]:
        return [name for slide in self.slides for name in slide.negatives]


def _counters(client) -> tuple[int, int, int]:
    return (
        getattr(client, "prompt_tokens", 0),
        getattr(client, "completion_tokens", 0),
        getattr(client, "retries", 0),
    )


def _ask_slide(
    client, prompt, questions: str, deck: DeckIR, plan: DeckPlan, slide, page: Path | None
) -> SlideMeasurement:
    planned = next((s for s in plan.slides if s.index == slide.index), None)
    text = prompt.template.format(
        topic=plan.title,
        title=planned.takeaway_title if planned else "",
        intent=planned.intent.value if planned else "",
        body=_slide_body(slide),
        questions=questions,
    )
    before = _counters(client)
    started = time.monotonic()
    try:
        images = [page.read_bytes()] if page is not None and page.exists() else None
        answers = client.complete("audit_slide", text, SlideAnswers, images=images)
    except Exception as failure:
        after = _counters(client)
        return SlideMeasurement(
            index=slide.index,
            seconds=time.monotonic() - started,
            prompt_tokens=after[0] - before[0],
            completion_tokens=after[1] - before[1],
            retries=after[2] - before[2],
            answers=0,
            error=str(failure)[:200],
        )
    elapsed = time.monotonic() - started
    after = _counters(client)
    return SlideMeasurement(
        index=slide.index,
        seconds=elapsed,
        prompt_tokens=after[0] - before[0],
        completion_tokens=after[1] - before[1],
        retries=after[2] - before[2],
        answers=len(answers.answers),
        negatives=[a.check_id for a in answers.answers if not a.passed],
    )


def measure_image_cost(
    client, prompt, questions: str, deck: DeckIR, plan: DeckPlan, page: Path
) -> tuple[int, int, int]:
    """Цена картинки в токенах: разность одного и того же запроса с ней и без.

    Прямее не измерить: провайдер не раскладывает входные токены по частям
    запроса, а два вызова с одинаковым текстом отличаются ровно картинкой.
    """
    slide = deck.slides[0]
    without = _ask_slide(client, prompt, questions, deck, plan, slide, None)
    with_image = _ask_slide(client, prompt, questions, deck, plan, slide, page)
    return (
        with_image.prompt_tokens - without.prompt_tokens,
        without.prompt_tokens,
        with_image.prompt_tokens,
    )


def run(
    client,
    deck: DeckIR,
    plan: DeckPlan,
    pages: list[Path],
    check_ids: list[str],
    limit: int = 0,
    measure_cost: bool = True,
    prompts_dir: str | Path | None = None,
) -> ProbeResult:
    """Прогоняет картиночный аудит и возвращает замеры."""
    prompt = load_prompt("audit_slide.v3", prompts_dir)
    questions = _questions(check_ids)
    result = ProbeResult()

    if measure_cost and pages:
        cost, without, with_image = measure_image_cost(
            client, prompt, questions, deck, plan, Path(pages[0])
        )
        result.image_token_cost = cost
        result.text_only_prompt_tokens = without
        result.with_image_prompt_tokens = with_image

    targets = deck.slides[:limit] if limit else deck.slides
    for slide in targets:
        page = Path(pages[slide.index - 1]) if slide.index - 1 < len(pages) else None
        result.slides.append(
            _ask_slide(client, prompt, questions, deck, plan, slide, page)
        )
    return result


def run_text_pass(
    client, plan: DeckPlan, check_ids: list[str], prompts_dir: str | Path | None = None
) -> SlideMeasurement:
    """Проход по тексту колоды: шесть вопросов из одиннадцати задаются им.

    Меряется отдельно: он идёт раз на колоду, а не раз на слайд, и смешивать
    его цену с картиночной значит получить среднее, которого не бывает.
    """
    from deckwright.audit.contextual.runner import DeckAnswers

    prompt = load_prompt("audit_deck.v1", prompts_dir)
    slides = "\n\n".join(
        f"[{slide.index}] {slide.takeaway_title}\n"
        + "\n".join(f"  - {item}" for block in slide.blocks for item in block.items)
        for slide in plan.slides
    )
    figures = "\n".join(
        f"- {figure.text} ({figure.kind.value}"
        + (f", формула {figure.formula}" if figure.formula else "")
        + f", факты {', '.join(figure.fact_ids)})"
        for slide in plan.slides
        for figure in slide.figures
    ) or "(чисел не заявлено)"
    text = prompt.template.format(
        topic=plan.title,
        language=plan.language,
        slides=slides,
        figures=figures,
        questions=_questions(check_ids),
    )

    before = _counters(client)
    started = time.monotonic()
    try:
        answers = client.complete("audit_deck", text, DeckAnswers)
    except Exception as failure:
        after = _counters(client)
        return SlideMeasurement(
            index=0,
            seconds=time.monotonic() - started,
            prompt_tokens=after[0] - before[0],
            completion_tokens=after[1] - before[1],
            retries=after[2] - before[2],
            answers=0,
            error=str(failure)[:200],
        )
    elapsed = time.monotonic() - started
    after = _counters(client)
    return SlideMeasurement(
        index=0,
        seconds=elapsed,
        prompt_tokens=after[0] - before[0],
        completion_tokens=after[1] - before[1],
        retries=after[2] - before[2],
        answers=len(answers.answers),
        negatives=[a.check_id for a in answers.answers if not a.passed],
    )


def format_report(
    clean: ProbeResult,
    spoiled: ProbeResult | None,
    damage: list[Spoilage],
    check_ids: list[str],
    dpi: int,
    text_check_ids: list[str] | None = None,
) -> str:
    """Отчёт прогона в том виде, в каком он читается человеком."""
    lines: list[str] = []
    lines.append(f"вопросов по картинке : {len(check_ids)}")
    lines.append(f"разрешение картинки  : {dpi} dpi")

    if clean.image_token_cost is not None:
        lines.append("")
        lines.append("цена картинки слайда (разность запросов с ней и без):")
        lines.append(f"  без картинки       : {clean.text_only_prompt_tokens} вход. токенов")
        lines.append(f"  с картинкой        : {clean.with_image_prompt_tokens} вход. токенов")
        lines.append(f"  сама картинка      : {clean.image_token_cost} токенов")

    lines.append("")
    lines.append("  слайд  время,с  вход  выход  повторов  ответов  «нет»")
    for slide in clean.slides:
        mark = slide.error[:28] if slide.error else ""
        lines.append(
            f"  {slide.index:5}  {slide.seconds:7.1f}  {slide.prompt_tokens:5}"
            f"  {slide.completion_tokens:5}  {slide.retries:8}  {slide.answers:7}"
            f"  {len(slide.negatives):5}  {mark}"
        )

    if clean.slides:
        times = sorted(s.seconds for s in clean.slides)
        lines.append("")
        lines.append(
            f"время на слайд       : мин {times[0]:.1f}  "
            f"медиана {times[len(times) // 2]:.1f}  макс {times[-1]:.1f} с"
        )
        # Проба спрашивает слайды подряд, а пайплайн — параллельно, восемью
        # вызовами. Назвать эту сумму «всей колодой» значит выдать худший
        # случай за время прогона: прогон делит её на число потоков, пока
        # хватает минутного лимита по токенам.
        lines.append(
            f"сумма по {len(clean.slides)} слайдам : {clean.total_seconds:.1f} с "
            "(последовательно; в прогоне вызовы идут параллельно, "
            "см. vlm.max_concurrent_calls)"
        )
        lines.append(
            f"токенов              : вход {clean.total_prompt_tokens}, "
            f"выход {clean.total_completion_tokens}"
        )
        expected = len(check_ids) * len(clean.slides)
        got = sum(s.answers for s in clean.slides)
        lines.append(
            f"формат ответа        : {got} ответов из {expected} ожидаемых, "
            f"повторов {sum(s.retries for s in clean.slides)}"
        )

    if clean.deck_pass is not None:
        deck_pass = clean.deck_pass
        wanted = len(text_check_ids or [])
        lines.append("")
        lines.append("проход по тексту колоды (раз на колоду, без картинки):")
        lines.append(
            f"  время {deck_pass.seconds:.1f} с, вход {deck_pass.prompt_tokens}, "
            f"выход {deck_pass.completion_tokens}, повторов {deck_pass.retries}"
        )
        lines.append(
            f"  ответов {deck_pass.answers} из {wanted} ожидаемых, "
            f"«нет» {len(deck_pass.negatives)}"
        )
        if deck_pass.error:
            lines.append(f"  ОШИБКА: {deck_pass.error}")

    if spoiled is not None:
        lines.append("")
        lines.append("── заведомо испорченная колода ──")
        for item in damage:
            caught = any(
                item.expect_check in slide.negatives
                for slide in spoiled.slides
                if slide.index == item.slide_index
            )
            mark = "поймано" if caught else "ПРОПУЩЕНО"
            lines.append(
                f"  слайд {item.slide_index}: {item.kind} → "
                f"{item.expect_check}: {mark}"
            )
        total_negatives = len(spoiled.negatives)
        lines.append(
            f"  ответов «нет» на испорченной: {total_negatives}, "
            f"на чистой: {len(clean.negatives)}"
        )
        if total_negatives <= len(clean.negatives):
            lines.append(
                "  ВНИМАНИЕ: на испорченной колоде замечаний не больше, чем на "
                "чистой — аудит не различает их и бесполезен"
            )

    return "\n".join(lines)


# ── Разрешение картинок аудита: те же вопросы при разных dpi ────────────────


@dataclass
class ResolutionPass:
    """Один проход картиночного аудита по колоде при одном разрешении."""

    dpi: int
    repeat: int
    findings: dict[tuple[int, str], str] = field(default_factory=dict)
    prompt_tokens: int = 0
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)


def compare_resolutions(
    client,
    deck: DeckIR,
    plan: DeckPlan,
    pages: list[Path],
    check_ids: list[str],
    base_dpi: int,
    dpis: list[int],
    workers: int,
    repeats_at_base: int = 2,
) -> list[ResolutionPass]:
    """Картиночный аудит одной колоды при каждом разрешении из `dpis`.

    Картинка — PNG рендера при `base_dpi`, уменьшенный так же, как в прогоне
    (`runner.scaled_page`). Базовое разрешение спрашивается дважды: модель
    отвечает не детерминированно, и расхождение двух одинаковых проходов —
    мерка шума, с которой сравниваются меньшие разрешения.
    """
    from deckwright.audit.contextual.runner import _image_pass

    passes: list[ResolutionPass] = []
    for dpi in dpis:
        for repeat in range(repeats_at_base if dpi == base_dpi else 1):
            before = getattr(client, "prompt_tokens", 0)
            started = time.monotonic()
            issues, problems = _image_pass(
                deck,
                plan,
                pages,
                client,
                check_ids,
                workers,
                None,
                None,
                scale=min(1.0, dpi / base_dpi),
            )
            passes.append(
                ResolutionPass(
                    dpi=dpi,
                    repeat=repeat + 1,
                    findings={(i.slide_index, i.check_id): i.message for i in issues},
                    prompt_tokens=getattr(client, "prompt_tokens", 0) - before,
                    seconds=round(time.monotonic() - started, 1),
                    problems=problems,
                )
            )
    return passes


def format_resolution_report(name: str, passes: list[ResolutionPass]) -> str:
    """Таблица: токены, время, находки и их совпадение с первым базовым проходом."""
    base = passes[0]
    lines = [
        f"### {name}",
        "",
        "| dpi | проход | токенов на входе | с | находок | как в базе | потеряно | новых |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in passes:
        same = set(item.findings) & set(base.findings)
        lost = sorted(set(base.findings) - set(item.findings))
        new = sorted(set(item.findings) - set(base.findings))
        lines.append(
            f"| {item.dpi} | {item.repeat} | {item.prompt_tokens} | {item.seconds} | "
            f"{len(item.findings)} | {len(same)} | {len(lost)} | {len(new)} |"
        )
    for item in passes[1:]:
        lost = sorted(set(base.findings) - set(item.findings))
        new = sorted(set(item.findings) - set(base.findings))
        mark = f"- {item.dpi} dpi/{item.repeat}:"
        for key in lost:
            lines.append(f"{mark} нет {key[0]}:{key[1]} — {base.findings[key]}")
        for key in new:
            lines.append(f"{mark} новое {key[0]}:{key[1]} — {item.findings[key]}")
        for problem in item.problems:
            lines.append(f"{mark} не спрошено — {problem}")
    return "\n".join(lines)
