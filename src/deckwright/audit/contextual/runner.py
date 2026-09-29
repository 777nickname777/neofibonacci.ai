"""Контекстные проверки: вопросы Приложения 1, на которые отвечает модель.

Два прохода, и разделение между ними не вкусовое.

**По картинке слайда** — всё, что видно только глазами: отвечает ли содержание
заголовку, есть ли на слайде содержание вообще, к месту ли картинки, не
осталось ли подсказок шаблона. Текст `SlideIR` показывает намерение, а не
результат: обрезанный край, наложившиеся блоки и чужая иконка в нём не видны.
Этот проход обязателен и сокращению не подлежит.

**По тексту колоды** — то, где картинка не добавляет ничего: опечатки, единый
язык, происхождение чисел, связность соседей. Эти вопросы не зависят от
варианта вёрстки — содержание у трёх вариантов одно, — и задаются раз на
колоду вместо трёх раз с картинкой.

Расход держится двумя рычагами: разрешением картинки и пропуском слайдов, не
изменившихся после исправления. Вторая итерация трогает два-три слайда из
тридцати, а без пропуска стоила бы как первая.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from deckwright.audit.registry import check
from deckwright.config import agent_prompt
from deckwright.plan.planner import load_prompt
from deckwright.schemas import (
    Box,
    CheckKind,
    DeckIR,
    DeckPlan,
    FixKind,
    Issue,
    ProposedFix,
    SlideIntent,
    SlideIR,
)


class Answer(BaseModel):
    """Ответ модели на один вопрос."""

    check_id: str
    passed: bool
    reason: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    slide_index: int = Field(default=0, ge=0)


class SlideAnswers(BaseModel):
    answers: list[Answer] = Field(default_factory=list)


class DeckAnswers(BaseModel):
    answers: list[Answer] = Field(default_factory=list)


@dataclass
class ContextualResult:
    """Находки и честный список того, что выполнить не удалось."""

    issues: list[Issue] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    # Текстовый проход не задавался заново: ответы принесены с другого
    # варианта. Не то же самое, что «пропущен»: вопрос задан, просто один раз.
    text_reused: bool = False


class SharedAudit:
    """Вопросы модели, общие для вариантов одной колоды, идущих параллельно.

    Текстовый проход идёт по плану, а план у вариантов один: вариант,
    пришедший первым, задаёт вопрос, остальные ждут его ответа на блокировке
    — их проход по картинкам тем временем идёт.

    Картинки у вариантов часто одинаковые: титул, финал, слайды одной
    композиции (живой прогон `zelenie_investicii`: 26 разных страниц из 39).
    Одна и та же картинка с тем же текстом вопроса — один вызов: лимит 40 000
    токенов в минуту на аккаунт, и проход по картинкам трёх вариантов упирался
    в него (≈140 с аудита на вариант).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._value: tuple[list[Issue], str] = ([], "")
        self._slides: dict[str, Future] = {}

    def get(self, compute: Callable[[], tuple[list[Issue], str]]) -> tuple[list[Issue], str, bool]:
        """Ответ текстового прохода и признак «получен другим вариантом»."""
        with self._lock:
            if self._done:
                return (*self._value, True)
            self._value = compute()
            self._done = True
            return (*self._value, False)

    def slide(self, key: str, compute: Callable[[], SlideAnswers]) -> SlideAnswers:
        """Ответы по картинке: первый спросивший спрашивает, остальные ждут.

        Отказ модели не разделяется: ждавший вариант спрашивает сам — иначе
        одна сетевая ошибка стоила бы находок трёх вариантов.
        """
        with self._lock:
            future = self._slides.get(key)
            owner = future is None
            if owner:
                future = self._slides[key] = Future()
        if owner:
            try:
                future.set_result(compute())
            except Exception as failure:
                future.set_exception(failure)
                raise
            return future.result()
        try:
            return future.result()
        except Exception:
            return compute()


_BARE_INTENTS = (SlideIntent.TITLE, SlideIntent.SECTION)


def _questions(check_ids: list[str]) -> str:
    return "\n".join(f"- {cid}: {check(cid).title}?" for cid in check_ids)


def _slide_body(slide: SlideIR) -> str:
    lines = [
        paragraph.text
        for element in slide.all_elements()
        if element.text is not None
        for paragraph in element.text.paragraphs
        if paragraph.text.strip()
    ]
    return "\n".join(lines) or "(текста нет)"


# Отказы провайдера, которые повтор не лечит: модель отключена, ключ её не
# открывает, такой модели нет. Замечаются один раз на прогон и больше не
# повторяются — иначе аудит тратит минуты на заведомые отказы по каждому
# слайду каждого варианта.
_PERMANENT_MARKERS = (
    "model disabled",
    "permissiondenied",
    "403",
    "401",
    "not found",
    "does not exist",
)
#: Клиенты, отказавшие насовсем, и текст их отказа. Ключ — `id(client)`:
#: у каждого прогона свой клиент, и чужой отказ на него не распространяется.
_permanent: dict[int, str] = {}


def _is_permanent(failure: BaseException) -> bool:
    text = f"{type(failure).__name__} {failure}".lower()
    return any(marker in text for marker in _PERMANENT_MARKERS)


def _to_issue(answer: Answer, slide_index: int, bbox: Box | None = None) -> Issue | None:
    """Ответ «нет» становится находкой. Ответ «да» не становится ничем.

    Уверенность модели переносится в находку как есть: контекстная проверка не
    имеет права притворяться детерминированной.
    """
    if answer.passed:
        return None
    try:
        spec = check(answer.check_id)
    except KeyError:
        # Модель назвала вопрос, которого нет в реестре. Выдумывать под него
        # паспорт нельзя: находка без паспорта не попадёт ни в документацию,
        # ни в UI.
        return None
    if spec.kind is not CheckKind.CONTEXTUAL:
        return None
    return Issue(
        check_id=answer.check_id,
        kind=CheckKind.CONTEXTUAL,
        category=spec.category,
        severity=spec.severity,
        slide_index=max(1, slide_index),
        bbox=bbox,
        message=answer.reason.strip() or spec.title,
        confidence=answer.confidence,
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="решение за человеком: ответ модели на повторе может отличаться",
            action="review_contextual_finding",
            params={"check_id": answer.check_id, "slide_index": slide_index},
        ),
    )


def scaled_page(page: Path, scale: float) -> bytes:
    """Картинка слайда для модели: PNG рендера, уменьшенный до `contextual_dpi`.

    Токены картинки растут как её площадь, а лимит аккаунта — 40 000 токенов
    в минуту на всё: аудит по картинкам упирается в него и занимает 80–111 с
    прогона. Картинки для человека (`render.png_dpi`) при этом не трогаются.
    """
    data = page.read_bytes()
    if scale >= 1.0:
        return data
    import io

    from PIL import Image

    image = Image.open(io.BytesIO(data))
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    out = io.BytesIO()
    image.convert("RGB").resize(size, Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def _image_pass(
    deck: DeckIR,
    plan: DeckPlan,
    pages: list[Path],
    client,
    check_ids: list[str],
    workers: int,
    prompts_dir: str | Path | None,
    only_slides: set[int] | None,
    shared: SharedAudit | None = None,
    scale: float = 1.0,
) -> tuple[list[Issue], list[str]]:
    prompt = load_prompt(agent_prompt("audit_slide"), prompts_dir)
    questions = _questions(check_ids)
    # Титул и разделитель по замыслу состоят из заголовка: вопрос «есть ли
    # на слайде содержание» для них ложный. Живой прогон 4×4: модель
    # отвечала «нет» на титуле в половине колод.
    bare_questions = _questions([cid for cid in check_ids if cid != "content.has_content"])
    by_index = {slide.index: slide for slide in plan.slides}

    def ask(slide: SlideIR) -> tuple[list[Issue], str]:
        page = pages[slide.index - 1] if slide.index - 1 < len(pages) else None
        if page is None or not Path(page).exists():
            return [], f"слайд {slide.index}: картинки нет, вопрос не задан"
        # По замыслу слайда, а не по его месту в колоде: после деления
        # переполненного слайда и переноса текста обложки позиции сдвинуты, и
        # поиск по позиции подставлял модели чужой заголовок либо пустой.
        planned = by_index.get(slide.plan_index or slide.index)
        text = prompt.template.format(
            topic=plan.title,
            title=planned.takeaway_title if planned else "",
            intent=planned.intent.value if planned else "",
            body=_slide_body(slide),
            questions=(
                bare_questions
                if planned and planned.intent in _BARE_INTENTS
                else questions
            ),
        )
        image = scaled_page(Path(page), scale)

        def call() -> SlideAnswers:
            return client.complete("audit_slide", text, SlideAnswers, images=[image])

        if _permanent.get(id(client)):
            # Провайдер уже отказал так, что повтор ничего не изменит: модель
            # отключена или ключ её не открывает. Спрашивать про каждый
            # следующий слайд — тратить время прогона на заведомый отказ.
            return [], f"слайд {slide.index}: {_permanent[id(client)]}"
        try:
            if shared is None:
                answers = call()
            else:
                # Ключ — всё, что видит модель: картинка и текст вопроса.
                key = hashlib.sha256(image + text.encode("utf-8")).hexdigest()
                answers = shared.slide(key, call)
        except Exception as failure:  # отказ модели не должен ронять аудит
            if _is_permanent(failure):
                _permanent[id(client)] = str(failure)
            return [], f"слайд {slide.index}: {failure}"
        found = [
            issue
            for issue in (_to_issue(a, slide.index) for a in answers.answers)
            if issue is not None
        ]
        return found, ""

    targets = [
        slide
        for slide in deck.slides
        if only_slides is None or slide.index in only_slides
    ]
    if not targets:
        return [], []

    # Параллельность здесь — условие выполнимости, а не оптимизация: вызов на
    # слайд при десяти слайдах и трёх вариантах последовательно не влезает в
    # бюджет пяти минут. Окно по токенам держит `RateLimiter` внутри клиента.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(ask, targets))

    issues = [issue for found, _ in results for issue in found]
    problems = [note for _, note in results if note]
    return issues, problems


def _text_pass(
    plan: DeckPlan,
    client,
    check_ids: list[str],
    prompts_dir: str | Path | None,
) -> tuple[list[Issue], str]:
    prompt = load_prompt(agent_prompt("audit_deck"), prompts_dir)
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
    try:
        answers = client.complete("audit_deck", text, DeckAnswers)
    except Exception as failure:
        return [], str(failure)
    found = [
        issue
        for issue in (_to_issue(a, a.slide_index or 1) for a in answers.answers)
        if issue is not None
    ]
    return found, ""


def run(
    deck: DeckIR,
    plan: DeckPlan,
    pages: list[Path],
    client,
    cfg,
    only_slides: set[int] | None = None,
    prompts_dir: str | Path | None = None,
    text_findings: list[Issue] | SharedAudit | None = None,
) -> ContextualResult:
    """Оба контекстных прохода. Невыполненное честно перечисляется.

    `only_slides` — номера слайдов, изменившихся после исправления. Вторая
    итерация трогает два-три слайда из тридцати, и переспрашивать всю колоду
    значит платить за неё дважды.

    `text_findings` — ответы текстового прохода, уже полученные на другом
    варианте. Вопросы этого прохода задаются по плану, а план у трёх
    вариантов один: опечатки, единый язык и происхождение чисел от вёрстки не
    зависят. Управляется `audit.text_checks_once_per_deck`.
    """
    result = ContextualResult()
    audit = cfg.audit

    if not audit.contextual_enabled:
        for check_id in audit.checks_by_mode("image") + audit.checks_by_mode("text"):
            result.skipped[check_id] = "контекстные проверки выключены в конфиге"
        return result

    if client is None:
        for check_id in audit.checks_by_mode("image") + audit.checks_by_mode("text"):
            result.skipped[check_id] = "модель недоступна: вопрос не задан"
        return result

    # Текстовый проход — один вызов по плану, от картинок не зависит. Он идёт
    # одновременно с проходом по картинкам, а не после него: по очереди это
    # сумма двух самых долгих вызовов аудита вместо большего из них.
    text_ids = audit.checks_by_mode("text")

    def compute() -> tuple[list[Issue], str]:
        return _text_pass(plan, client, text_ids, prompts_dir)

    def text_part() -> tuple[list[Issue], str, bool]:
        if audit.text_checks_once_per_deck and isinstance(text_findings, list):
            return text_findings, "", True
        if audit.text_checks_once_per_deck and isinstance(text_findings, SharedAudit):
            return text_findings.get(compute)
        return (*compute(), False)

    text_pool = ThreadPoolExecutor(max_workers=1) if text_ids else None
    text_job = text_pool.submit(text_part) if text_pool is not None else None

    image_ids = audit.checks_by_mode("image")
    if image_ids:
        issues, problems = _image_pass(
            deck,
            plan,
            pages,
            client,
            image_ids,
            cfg.vlm.max_concurrent_calls,
            prompts_dir,
            only_slides,
            text_findings if isinstance(text_findings, SharedAudit) else None,
            scale=min(1.0, audit.contextual_dpi / cfg.render.png_dpi),
        )
        result.issues.extend(issues)
        if problems:
            for check_id in image_ids:
                result.skipped[check_id] = "; ".join(problems[:3])

    if text_job is not None:
        issues, problem, reused = text_job.result()
        text_pool.shutdown()
        result.issues.extend(issues)
        result.text_reused = reused
        if problem:
            for check_id in text_ids:
                result.skipped[check_id] = problem

    return result
