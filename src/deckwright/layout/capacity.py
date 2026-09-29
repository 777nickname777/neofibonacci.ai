"""Сколько текста шаблон реально вмещает — для бюджета планировщика.

Планировщик работает до вёрстки и не знает, в какой макет ляжет слайд.
Общий бюджет шаблона («до 6 пунктов по 34 символа») для этого не годится:
на живом плане #12 модель ему подчинилась, а переполнено было 37 слайдов из
90 — рамки конкретных макетов вмещали от одной до четырёх строк.

Здесь бюджет считается тем же предсказателем, по которому вёрстка потом
выбирает макет (`matcher._blocks_fit`): для каждого вида текстового блока —
сколько пунктов какой длины помещается **читаемым кеглем** хотя бы в один
макет шаблона. Считается для каждого варианта, и берётся минимум: план один
на три варианта, и влезать он обязан во все три.

Ёмкость — кривая, а не одно число: на `vk_workspace` помещается три пункта
по 70 символов, пять по 50 или шесть по 25. Одна пара «шесть по 22»
заставляла модель резать типичный список из четырёх пунктов втрое; кривая
оставляет выбор ей.

Расчёт детерминирован и не зовёт модель; планировщику уходят только числа.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.layout.fitter import fit_paragraphs
from deckwright.layout.matcher import _blocks_fit, _title_fits, _usable_slots
from deckwright.layout.strategy import Strategy, ladder_for_role, role_typical
from deckwright.layout.text_metrics import Fonts
from deckwright.schemas import (
    BlockKind,
    ContentBlock,
    SlideIntent,
    SlidePlan,
    SlotRole,
    TemplateSpec,
)

# Виды блоков, длину которых пишет планировщик. Ряд, таблица и картинка
# мерятся площадью, цитата приходит из контент-пакета готовым текстом.
TEXT_KINDS = (BlockKind.BULLETS, BlockKind.STEPS, BlockKind.PARAGRAPH)

# Длины пункта, которые пробуются от бюджета шаблона вниз: если пять пунктов
# полной длины не влезают никуда, три коротких могут влезть.
# Модельное слово: средняя длина русского слова около семи букв.
_WORD = "абвгдеж"

# Предел длины пункта при поиске: порог ТЗ — пятнадцать слов, это около
# ста десяти символов русского текста.
_MAX_ITEM_CHARS = 110
_MIN_ITEM_CHARS = 10


@dataclass(frozen=True)
class BlockCapacity:
    """Сколько пунктов какой длины помещается в блок этого вида."""

    items: int
    chars: int


def _text(length: int) -> str:
    """Строка из модельных слов заданной длины: перенос считается по словам."""
    words = []
    while len(" ".join(words)) < length:
        words.append(_WORD)
    return " ".join(words)[:length].rstrip()


def _probe(kind: BlockKind, items: int, chars: int, title: str) -> SlidePlan:
    return SlidePlan(
        index=1,
        intent=SlideIntent.SOLUTION,
        takeaway_title=title,
        blocks=[
            ContentBlock(id="probe", kind=kind, items=[_text(chars)] * items),
        ],
    )


def achievable(
    spec: TemplateSpec,
    strategies: list[Strategy],
    item_chars: int,
    title_chars: int,
    max_items: int,
) -> dict[BlockKind, list[BlockCapacity]]:
    """Кривая ёмкости по видам текстовых блоков, общая для всех вариантов.

    Для каждого числа пунктов от одного до порога ТЗ — наибольшая длина
    пункта, при которой блок влезает во всех вариантах. Из кривой остаются
    точки, где длина падает: «три по 70, пять по 50, шесть по 25».

    `item_chars` — бюджет шаблона: он нужен абзацу и как нижняя граница,
    если читаемым кеглем не влезает ничего.
    """
    fonts = Fonts(spec)
    metrics = fonts if fonts.usable else None
    if metrics is None or not strategies:
        return {}
    ladders = {role: ladder_for_role(spec, role) for role in SlotRole}
    typical = {role: role_typical(spec, role) for role in SlotRole}
    title = _text(max(1, round(title_chars * 0.7)))
    usable = [
        pattern
        for pattern in spec.content_patterns
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]

    def fits(kind, items, chars, strict) -> bool:
        slide = _probe(kind, items, chars, title)
        return all(
            any(
                _title_fits(pattern, slide, strategy, metrics, ladders[SlotRole.TITLE])
                and _blocks_fit(pattern, spec, slide, strategy, metrics, ladders, strict)
                for pattern in usable
            )
            for strategy in strategies
        )

    def longest(kind, items, strict, ceiling) -> int:
        """Наибольшая влезающая длина — двоичным поиском: ёмкость монотонна."""
        low, high, found = _MIN_ITEM_CHARS, ceiling, 0
        while low <= high:
            middle = (low + high) // 2
            if fits(kind, items, middle, strict):
                found, low = middle, middle + 1
            else:
                high = middle - 1
        return found

    def curve(kind: BlockKind, strict) -> list[BlockCapacity]:
        if kind is BlockKind.PARAGRAPH:
            chars = longest(kind, 1, strict, max(_MAX_ITEM_CHARS, item_chars * max_items))
            return [BlockCapacity(items=1, chars=chars)] if chars else []
        points: list[BlockCapacity] = []
        ceiling = _MAX_ITEM_CHARS
        for count in range(1, max_items + 1):
            # Больше пунктов — длина не растёт. Сначала проверяется прежняя:
            # у просторного шаблона кривая плоская, и поиск не нужен вовсе.
            if points and fits(kind, count, ceiling, strict):
                chars = ceiling
            else:
                chars = longest(kind, count, strict, ceiling - 1 if points else ceiling)
            if not chars:
                break
            ceiling = chars
            # Пока длина не падает, больше пунктов — даром: точку заменяем.
            if points and points[-1].chars == chars:
                points[-1] = BlockCapacity(items=count, chars=chars)
            else:
                points.append(BlockCapacity(items=count, chars=chars))
        return points

    result: dict[BlockKind, list[BlockCapacity]] = {}
    by_roles: dict[tuple, list[BlockCapacity]] = {}
    for kind in TEXT_KINDS:
        # Виды блоков, которые во всех вариантах ложатся в одну роль слота,
        # имеют одну кривую: считать её дважды — лишние секунды.
        roles = tuple(
            strategy.role_for(ContentBlock(id="k", kind=kind, items=["x"]))
            for strategy in strategies
        ) + ((kind,) if kind is BlockKind.PARAGRAPH else ())
        if roles not in by_roles:
            # Сначала — читаемым кеглем; если так не влезает ничего, — хоть
            # как-то. Пустая кривая остаётся пустой: тогда в промпт уходит
            # общий бюджет шаблона.
            by_roles[roles] = curve(kind, typical) or curve(kind, None)
        result[kind] = by_roles[roles]
    return result


# Виды ограничений для служебных слайдов: {намерение}_{место}.
BOOKEND_KINDS = ("cover_title", "cover_text", "closing_title", "closing_text")


def bookend_limits(spec: TemplateSpec) -> list[tuple[str, int, int]]:
    """Сколько символов вмещают заголовок и текст обложки и финала шаблона.

    Обложку и финал колода берёт целиком, с кеглем шаблона, поэтому и мерить
    их надо шаблонным кеглем, а не шкалой контентных макетов: подзаголовок в
    130 символов садился в подпись спикера обложки `vk_workspace` кеглем 12.
    Мерятся первые места под текст в том порядке, в каком их заполняет
    вёрстка: заголовок и первое место под ним.
    """
    fonts = Fonts(spec)
    metrics = fonts if fonts.usable else None
    if metrics is None:
        return []
    by_id = {pattern.id: pattern for pattern in spec.patterns}
    limits: list[tuple[str, int, int]] = []
    for prefix, pattern_id in (
        ("cover", spec.cover_pattern_id),
        ("closing", spec.closing_pattern_id or spec.cover_pattern_id),
    ):
        pattern = by_id.get(pattern_id) if pattern_id else None
        if pattern is None:
            continue
        title = next((s for s in pattern.slots if s.role is SlotRole.TITLE), None)
        # Место под текст — только подзаголовок: подпись спикера заполняется
        # данными о выступающем, а места над заголовком — оформление.
        text = next((s for s in pattern.slots if s.role is SlotRole.BODY), None)
        if text is None:
            # Места под текст нет: планировщику так и говорится — «только
            # заголовок», а не «текст до N символов».
            limits.append((f"{prefix}_text", 0, 0))
        for place, slot in (("title", title), ("text", text)):
            if slot is None:
                continue
            declared = (
                slot.style.size_pt
                if slot.style is not None
                else role_typical(spec, slot.role) or 14.0
            )
            # Заголовок вёрстка уменьшает по шкале заголовков — так и мерим.
            # Текст — не мельче типичного для роли: мелкий текст на обложке и
            # был дефектом.
            floor = (
                min(ladder_for_role(spec, SlotRole.TITLE), default=declared)
                if place == "title"
                else min(declared, role_typical(spec, slot.role) or declared)
            )
            ladder = sorted(
                {declared, floor}
                | {
                    size
                    for size in ladder_for_role(spec, slot.role)
                    if floor <= size <= declared
                }
            )
            # Бюджет длины считается тем начертанием, которым место и
            # будет набрано: полужирный заголовок шире обычного.
            chars = _longest_at(
                fonts.for_slot(slot), slot.box, ladder, declared
            )
            if chars:
                limits.append((f"{prefix}_{place}", 1, chars))
    return limits


def _longest_at(metrics, box, ladder: list[float], start: float) -> int:
    """Наибольшая длина модельного текста, влезающая в рамку по этой шкале."""
    low, high, found = _MIN_ITEM_CHARS, 400, 0
    while low <= high:
        middle = (low + high) // 2
        if fit_paragraphs([_text(middle)], metrics, box, ladder, start).fits:
            found, low = middle, middle + 1
        else:
            high = middle - 1
    return found
