"""Вёрстка: план + шаблон → `DeckIR`.

Слайд раскладывается на композицию шаблона, а не рисуется заново. Композиция
(`Pattern`) снята с настоящего слайда-примера и несёт `donor_slide_index` —
то, откуда рендерер потом клонирует поддерево фигур. Так оформление, ради
которого шаблон и берут, достаётся даром; нарисованное заново было бы «похоже
на шаблон», а это не то же самое.

Выбор композиции идёт **по структуре**: у слайда есть набор нужных мест
(«заголовок, список, график»), у композиции — набор имеющихся. Класс
композиции поднимает её в выдаче, но неопознанный класс не исключает её
вовсе: на чужом шаблоне правила не опознают заметную долю композиций, и
выбрасывать их значит добровольно обеднить вёрстку.

Если ни одна композиция не подошла, работает запасной путь по layout'ам —
плейсхолдеры есть даже у шаблона без слайдов-примеров.
"""

from __future__ import annotations

import colorsys
from collections import Counter

from deckwright.layout.fitter import FitResult, fit_paragraphs, fit_size, split_blocks
from deckwright.layout.strategy import Strategy, ladder_for_role, role_typical, scale_ladder
from deckwright.layout.text_metrics import FontMetrics, metrics_for_spec
from deckwright.plan.figures import parse_number
from deckwright.schemas import (
    Align,
    BlockKind,
    Box,
    ChartContent,
    ChartKind,
    CheckKind,
    Color,
    ContentBlock,
    DeckIR,
    Element,
    ElementKind,
    FixKind,
    Issue,
    IssueCategory,
    LayoutSpec,
    Paragraph,
    Pattern,
    PatternClass,
    ProposedFix,
    Provenance,
    SeriesShape,
    Severity,
    SlideIntent,
    SlideIR,
    SlidePlan,
    SlotRole,
    SourceKind,
    TableContent,
    TableData,
    TemplateSpec,
    TextContent,
    TextStyle,
    VAlign,
    readable_text_color,
    required_contrast,
)
from deckwright.schemas.common import LARGE_TEXT_PT
from deckwright.visuals.charts import format_value, series_from_pack, series_unit

# Намерения, которым хватает одного заголовка.
_BARE_INTENTS = frozenset({SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING})

# Роли, куда можно положить содержание. Подпись и подпись к числу входят
# сюда наравне с телом текста: в карточке шаблона содержание живёт именно в
# них, и без них композиция из трёх карточек выглядит для вёрстки пустой.
_CONTENT_ROLES = frozenset(
    {
        SlotRole.BODY,
        SlotRole.BULLETS,
        SlotRole.CAPTION,
        SlotRole.CHART,
        SlotRole.TABLE,
        SlotRole.IMAGE,
        SlotRole.KPI_VALUE,
        SlotRole.KPI_LABEL,
        SlotRole.QUOTE,
    }
)

# Роли, которые годятся под текст, если запрошенной не нашлось. Порядок —
# порядок предпочтения: список охотнее ложится в список, чем в абзац.
_TEXT_FALLBACK: dict[SlotRole, tuple[SlotRole, ...]] = {
    SlotRole.BULLETS: (SlotRole.BODY, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.BODY: (SlotRole.BULLETS, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.CAPTION: (SlotRole.BODY, SlotRole.BULLETS, SlotRole.KPI_LABEL),
    SlotRole.QUOTE: (SlotRole.BODY, SlotRole.BULLETS, SlotRole.CAPTION),
    SlotRole.KPI_VALUE: (SlotRole.BODY, SlotRole.CAPTION, SlotRole.KPI_LABEL),
    SlotRole.CHART: (SlotRole.IMAGE, SlotRole.TABLE, SlotRole.BODY),
    SlotRole.TABLE: (SlotRole.CHART, SlotRole.BODY, SlotRole.BULLETS),
    SlotRole.IMAGE: (SlotRole.CHART,),
}


# Блоки, которые раскладываются по элементам повторителя, по пункту в элемент.
_SPREAD_KINDS = frozenset({BlockKind.BULLETS, BlockKind.STEPS})


class LayoutError(RuntimeError):
    """Шаблон не даёт ни одного места, куда положить содержание."""


# ── Подбор места ─────────────────────────────────────────────────────────────


def needed_profile(plan_slide: SlidePlan, strategy: Strategy) -> dict[SlotRole, int]:
    """Какие места нужны этому слайду. Это и есть его блочная сигнатура.

    Сигнатура строится из плана, а не из текста: во что превратится числовой
    ряд — график, таблицу или крупное число — решает вариант, и у трёх
    вариантов одного слайда сигнатуры законно разные.
    """
    profile: dict[SlotRole, int] = {SlotRole.TITLE: 1}
    for block in plan_slide.blocks:
        role = strategy.role_for(block)
        profile[role] = profile.get(role, 0) + 1
    return profile


def _title_fits(
    pattern: Pattern,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladder: list[float],
) -> bool:
    """Влезает ли заголовок слайда в заголовочную рамку этой композиции.

    Меряем до выбора, а не после. Композиция с узкой заголовочной рамкой
    посреди слайда годится для «Итоги», но не для формулировки в семь слов, и
    узнать это дешевле сейчас: потом останется только завести находку.
    """
    if metrics is None:
        return True
    slot = _title_slot(pattern)
    if slot is None:
        return False
    declared = slot.style.size_pt if slot.style is not None else (ladder[-1] if ladder else 18.0)
    start = strategy.start_size(ladder, declared)
    return fit_size(plan_slide.takeaway_title, metrics, slot.box, ladder, start).fits


# Роли, для которых «влезает» мерится площадью, а не текстом, и какую долю
# слайда рамка обязана занимать по каждой стороне. График в рамке подписи
# формально «влезает» — строка идентификатора ряда короткая, — а на слайде
# его не видно: так было на `vk_tech`, график 4.7 × 0.7 дюйма. Четверть высоты
# и треть ширины — нижняя граница, при которой у столбцов остаются оси и
# подписи.
_DATA_ROLES = frozenset({SlotRole.CHART, SlotRole.TABLE, SlotRole.IMAGE})
_DATA_MIN_HEIGHT_SHARE = 0.25
_DATA_MIN_WIDTH_SHARE = 1 / 3


def _roomy_for_data(box: Box, spec: TemplateSpec) -> bool:
    return (
        box.h >= spec.slide_height_emu * _DATA_MIN_HEIGHT_SHARE
        and box.w >= spec.slide_width_emu * _DATA_MIN_WIDTH_SHARE
    )


def _seats_all(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None = None,
    ladders: dict[SlotRole, list[float]] | None = None,
) -> bool:
    """Получит ли каждый блок свой слот — с учётом доли мест варианта.

    Слабее `_blocks_fit`: влезет ли текст целиком, не проверяет. Нужен
    запасному пути, когда не влезает нигде: блок без слота уходит в запасную
    полосу, а та на `vk_education` ложилась поверх соседнего блока. Но слот,
    куда не помещается ни одной строки, — не место: так показатель садился в
    рамку высотой меньше строки и пропадал со слайда.
    """
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    taken = _header_taken(pattern, plan_slide)
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        role = strategy.role_for(block)
        seats = _seat(
            pattern, block, role, free, taken,
            reserve=_text_after(plan_slide.blocks, block, strategy),
        )
        if seats is None:
            return False
        if _grid_seated(pattern, seats):
            role = SlotRole.BODY
        # Место под график или таблицу — не меньше четверти слайда и в
        # запасном пути: иначе график садился в иконку 0.24 дюйма
        # (`vk_workspace`, airy, слайд 8) и пропадал со слайда.
        if role in _DATA_ROLES and not _roomy_for_data(seats[0][0].box, spec):
            return False
        for slot, lines in seats:
            ladder = (ladders or {}).get(slot.role) or []
            if metrics is not None and ladder and role not in _DATA_ROLES:
                smallest = fit_paragraphs(lines, metrics, slot.box, ladder, min(ladder))
                if not smallest.capacity_lines:
                    return False
            taken.append(slot.box)
    return True


def _seatings(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> list[tuple[object, list]]:
    """Раскладка блоков слайда по местам композиции — та же, что при сборке.

    Блок без места получает пустой список мест.
    """
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    taken = _header_taken(pattern, plan_slide)
    result = []
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        seats = _seat(
            pattern, block, strategy.role_for(block), free, taken,
            reserve=_text_after(plan_slide.blocks, block, strategy),
        ) or []
        result.append((block, seats))
        taken.extend(slot.box for slot, _ in seats)
    return result


def _figure_unsplit(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> bool:
    """Показатель с подписью не получает здесь своего крупного числа.

    Не делится на число и подпись — или делится, но число садится в
    картинку-показатель донора (кольцо «10%» `vk_education`): кольцо с его
    долей тогда остаётся и изображает данные, которых у нас нет.
    """
    for block, seats in _seatings(pattern, spec, plan_slide, strategy):
        if block.kind is not BlockKind.KPI or not block.heading or not block.items:
            continue
        if len(seats) < 2 or seats[0][0].role is not SlotRole.KPI_VALUE:
            return True
        if any(
            _share(slot.box, figure) >= 0.5
            for slot, _ in seats
            for figure in pattern.figure_pictures
        ):
            return True
    return False


def _accent_for_data(pattern: Pattern, spec: TemplateSpec, has_series: bool) -> bool:
    """График или таблица на акцентном фоне шаблона.

    Фон, которым шаблон пишет меньшинство своих слайдов (тёмные обложки и
    разделы светлого `vk_tech`), — акцент: обложка, раздел, заявление.
    Данные читают на основном фоне. Мест под таблицу у `vk_tech` нет ни в
    одной композиции, подбор шёл по разнообразию, и четвёртая таблица колоды
    садилась на тёмную обложку раздела. Шаблон, где тёмных и светлых поровну
    или все одного фона, акцента не имеет.
    """
    if not has_series:
        return False
    dark = sum(1 for other in spec.patterns if other.is_dark)
    light = len(spec.patterns) - dark
    return (pattern.is_dark and dark < light) or (not pattern.is_dark and light < dark)


def _figure_small(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]] | None,
) -> bool:
    """Единственный показатель слайда пишется здесь мельче заголовка.

    Слайд, где кроме заголовка только число, держится на этом числе. В
    месте-строке под заголовком (`vk_tech`, 0.3″) «23 %» выходило кеглем 16
    при заголовке 24 — одна мелкая цифра на пустом слайде. Крупное число —
    не мельче типичного заголовка шаблона: так его набирают и сами доноры
    показателей («42 минуты» — 48 pt при заголовке 20).
    """
    if metrics is None or not ladders:
        return False
    blocks = [block for block in plan_slide.blocks if _block_lines(block)]
    if len(blocks) != 1 or blocks[0].kind is not BlockKind.KPI:
        return False
    title = role_typical(spec, SlotRole.TITLE)
    for block, seats in _seatings(pattern, spec, plan_slide, strategy):
        if not seats:
            return True
        seat, lines = seats[0]
        fits, _ = _fit_block(
            [(seat, seat.box, lines)], spec, strategy, metrics, ladders,
            strategy.role_for(block),
        )
        return fits[0].size_pt < title
    return False


def _at_declared_size(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]] | None,
) -> bool:
    """Текст каждого блока влезает кеглем своего места, не уменьшаясь.

    Место без объявленного кегля или мельче типичного тела шаблона (подпись
    7 pt `vk_tech`) этой проверки не проходит: это мерка для мест, которые
    шаблон сам набирает текстом.
    """
    if metrics is None or not ladders:
        return False
    body = role_typical(spec, SlotRole.BODY)
    for block, seats in _seatings(pattern, spec, plan_slide, strategy):
        if not seats or strategy.role_for(block) in _DATA_ROLES:
            continue
        fits, starts = _fit_block(
            [(seat, seat.box, lines) for seat, lines in seats],
            spec, strategy, metrics, ladders, strategy.role_for(block),
        )
        for (seat, _), fit, start in zip(seats, fits, starts, strict=True):
            declared = seat.style.size_pt if seat.style is not None else 0.0
            # Старт варианта — кегль места со сдвигом по шкале: плотный
            # начинает на ступень мельче объявленного.
            if declared < body * _DECLARED_BODY_SHARE or fit.size_pt < min(declared, start):
                return False
    return True


# Место, набранное мельче этой доли типичного тела, — подпись, а не текст.
_DECLARED_BODY_SHARE = 0.6


def _share(inner: Box, outer: Box) -> float:
    """Какая доля рамки лежит внутри другой."""
    hit = inner.intersection(outer)
    return hit.area / inner.area if hit is not None and inner.area else 0.0


def _lists_spread(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> bool:
    """Каждый пункт каждого списка получает своё место.

    Список из трёх пунктов в одной рамке при пустом слайде — то, что
    правило выбора запрещает, пока есть композиция, где пункты разложены
    по карточкам (`vk_tech` dense 9, `vk_workspace` dense 5). Два пункта на
    карточку — тоже не своё место: на `vk_education` четыре шага садились
    парами в две подписи под иконками мимо сетки из четырёх карточек.
    """
    return all(
        len(seats) >= len(block.items)
        for block, seats in _seatings(pattern, spec, plan_slide, strategy)
        if block.kind in _SPREAD_KINDS and len(block.items) > 1
    )


def _spare_cards(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> int:
    """Сколько карточек донора останется без пункта после раскладки списков."""
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    taken = _header_taken(pattern, plan_slide)
    spare = 0
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        seats = _seat(
            pattern, block, strategy.role_for(block), free, taken,
            reserve=_text_after(plan_slide.blocks, block, strategy),
        )
        if seats is None:
            continue
        # Повторитель, по которому разложен список, и его элементы донора.
        # И одиночное место в карточке: блок в одной карточке из четырёх —
        # узкая колонка посреди пустого слайда (`vk_tech`, показатель).
        for repeater in pattern.repeaters:
            if seats[0][0].id.startswith(f"{repeater.id}_"):
                spare += max(0, repeater.observed_count - len(seats))
        taken.extend(slot.box for slot, _ in seats)
    return spare


def _unsuitable(
    pattern: Pattern, spec: TemplateSpec, plan_slide: SlidePlan, strategy: Strategy
) -> bool:
    """Композиция, которую для этого слайда не берут, если есть другая.

    Два случая, оба — правило выбора, а не принятое ограничение:

    * элементы нарисованы в картинке layout'а (`baked_items`), и не все они
      получат содержание: пустую нарисованную карточку не убрать — «03 04»
      под таблицей `vk_tech`;
    * список садится в одно место, а рядом такие же места той же роли —
      карточки, не опознанные повтором: три пункта мелко в одной карточке
      при пустом слайде (`vk_tech`, dense 9).
    """
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    taken = _header_taken(pattern, plan_slide)
    filled: dict[str, set[str]] = {}
    cramped = []
    for block in plan_slide.blocks:
        if not _block_lines(block):
            continue
        seats = _seat(
            pattern, block, strategy.role_for(block), free, taken,
            reserve=_text_after(plan_slide.blocks, block, strategy),
        )
        if seats is None:
            continue
        for slot, _ in seats:
            for repeater in pattern.repeaters:
                prefix = f"{repeater.id}_"
                if slot.id.startswith(prefix):
                    index = slot.id[len(prefix) :].split("_", 1)[0]
                    filled.setdefault(repeater.id, set()).add(index)
        if block.kind in _SPREAD_KINDS and len(block.items) > 1 and len(seats) == 1:
            cramped.append(seats[0][0])
            # Весь список в одном элементе повторителя — карточке «01», — а
            # остальные элементы пусты: пункты могли получить каждый свою.
            # Место к этому времени выросло на всю карточку (`_absorb`) и
            # размером с соседними не совпадает — поэтому по элементу.
            member = _member_prefix(pattern, seats[0][0].id)
            if member is not None:
                repeater_head = member.rsplit("_", 2)[0] + "_"
                if any(
                    slot.id.startswith(repeater_head) and not slot.id.startswith(member)
                    for slot in free
                ):
                    return True
        if any(_on_picture(slot, pattern, len(lines)) for slot, lines in seats):
            return True
        if len(seats) > 1 and not pattern.is_dark and any(
            _on_badge(slot, lines) for slot, lines in seats
        ):
            return True
        taken.extend(slot.box for slot, _ in seats)
    # Список в одном месте, а такое же место рядом осталось пустым: пункты
    # могли получить каждый своё. Если двойники заняты другими блоками —
    # слайд заполнен, и список в одной колонке законен.
    empty = [s for s in free if not any(s.box == box for box in taken)]
    if any(_twins(slot, other) for slot in cramped for other in empty):
        return True
    if pattern.baked_items:
        return any(
            len(filled.get(repeater.id, set())) < repeater.observed_count
            for repeater in pattern.repeaters
        )
    return False


def _on_picture(slot, pattern: Pattern, lines: int) -> bool:
    """Текст на картинке длиннее, чем положил туда донор.

    Подпись поверх иллюстрации занимает её верх, а ниже — сам рисунок:
    рамка текста донора накрывает карточку целиком, но строк в ней две. Три
    пункта в ту же рамку ложатся на шар `vk_tech` (dense 9).
    """
    donor_lines = len(slot.placeholder_text.splitlines()) or 1
    return lines > donor_lines and any(
        other.role is SlotRole.IMAGE and other.box.contains(slot.box)
        for other in pattern.slots
    )


# Сколько раз композиция берётся, прежде чем уступить ещё не взятой.
_MAX_REPEATS = 2

# Фото «сбоку»: не ниже этой доли высоты слайда и не шире этой доли ширины.
_SIDE_PHOTO_HEIGHT = 0.6
_SIDE_PHOTO_WIDTH = 0.5


def _side_photos(pattern: Pattern, spec: TemplateSpec) -> list[Box]:
    return [
        photo
        for photo in pattern.photo_slots
        if photo.h >= spec.slide_height_emu * _SIDE_PHOTO_HEIGHT
        and photo.w <= spec.slide_width_emu * _SIDE_PHOTO_WIDTH
    ]


def _photo_left_empty(pattern: Pattern, spec: TemplateSpec, has_data: bool) -> bool:
    """Останется ли на месте убранного фото донора пустота.

    Фото сбоку отдаёт место тексту (`_without_photos`), фото любой формы —
    графику или таблице слайда. Иначе — баннер над карточками без данных —
    место фото остаётся пустым, и такую композицию берут последней.
    """
    if not pattern.photo_slots or has_data:
        return False
    return len(_side_photos(pattern, spec)) < len(pattern.photo_slots)


def _without_photos(pattern: Pattern, spec: TemplateSpec) -> Pattern:
    """Композиция, перестроенная без фото донора: текст занимает место фото.

    Фото сбоку («студент за компьютером» слева, текст справа) убирается
    рендером, а заголовок и текст рядом с ним растягиваются на его место в
    полях шаблона. Без этого половина слайда оставалась пустой.
    """
    side = _side_photos(pattern, spec)
    if not side:
        return pattern
    grid = spec.grid
    left = grid.margin_left_emu if grid else spec.slide_width_emu // 20
    right = spec.slide_width_emu - (grid.margin_right_emu if grid else spec.slide_width_emu // 20)
    # Рамки карточек повторителя. Карточки на место фото не растут, и
    # место, стоящее с ними в одну колонку, — тоже: широкая карточка под
    # двумя на шаблоне экзаменов растягивалась к левому полю, и её текст
    # выходил за обводку, оставшуюся на месте.
    frames = [frame for repeater in pattern.repeaters for frame in repeater.member_frames]
    column = spec.slide_width_emu // 50
    grown = []
    for slot in pattern.slots:
        box = slot.box
        in_column = any(
            abs(frame.x - box.x) <= column or abs(frame.right - box.right) <= column
            for frame in frames
        )
        if slot.role in _TEXT_GROWS_OVER_PHOTO and not in_column:
            for photo in side:
                overlap = min(box.bottom, photo.bottom) - max(box.y, photo.y)
                if overlap < box.h // 2:
                    continue
                if photo.right <= box.x:
                    new_x = max(left, photo.x)
                    box = box.model_copy(update={"x": new_x, "w": box.right - new_x})
                elif photo.x >= box.right:
                    new_right = min(right, photo.right)
                    box = box.model_copy(update={"w": new_right - box.x})
        grown.append(slot.model_copy(update={"box": box}) if box is not slot.box else slot)
    photo_ids = {
        slot.id
        for slot in pattern.slots
        if slot.role is SlotRole.IMAGE and any(slot.box == photo for photo in side)
    }
    return pattern.model_copy(
        update={"slots": [slot for slot in grown if slot.id not in photo_ids]}
    )


# Роли, чья рамка растягивается на место убранного бокового фото.
_TEXT_GROWS_OVER_PHOTO = frozenset(
    {SlotRole.TITLE, SlotRole.SUBTITLE, SlotRole.BODY, SlotRole.BULLETS}
)


def _on_badge(slot, lines: list[str]) -> bool:
    """Пункт списка в тёмной однострочной плашке на светлом слайде.

    Шапка календаря «Период 1…4» и шапка таблицы — тёмные ячейки в строку
    под короткую подпись. Три пункта плана ложились в них по ячейке мелким
    белым текстом: плашка — место подписи, а не мысли.
    """
    backdrop = slot.backdrop
    if backdrop is None or backdrop.luminance >= 0.5:
        return False
    donor = slot.placeholder_text.strip()
    return len(donor.splitlines()) <= 1 and sum(len(line) for line in lines) > 2 * max(
        1, len(donor)
    )


def _twins(slot, other) -> bool:
    """Места одной роли и одного размера, в пределах четверти."""
    return (
        other.id != slot.id
        and other.role is slot.role
        and abs(other.box.w - slot.box.w) <= 0.25 * slot.box.w
        and abs(other.box.h - slot.box.h) <= 0.25 * slot.box.h
    )


def _step_below(ladder: list[float], size: float) -> float:
    """Ступень шкалы непосредственно под кеглем `size`; сам он, если ниже нет."""
    lower = [step for step in ladder if step < size]
    return lower[-1] if lower else size


def _undeclared(spec: TemplateSpec, role: SlotRole, ladder: list[float]) -> float:
    """Кегль места, для которого шаблон его не объявил: типичный для роли.

    Карточки повторителя стиля не несут. Середина шкалы для них — 14 pt у
    `vk_tech` при типичном теле 18 pt: фиттер стартовал ниже порога
    читаемости и не мог его достичь, а подбор принимал любые карточки,
    вплоть до 4 pt. Ниже середины шкалы старт не опускается: у ролей, которые
    шаблон набирает мелко, крупное число показателя стало бы подписью.
    """
    middle = ladder[len(ladder) // 2] if ladder else 18.0
    return max(role_typical(spec, role), middle)


def _blocks_fit(
    pattern: Pattern,
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]] | None,
    typical: dict[SlotRole, float] | None = None,
) -> bool:
    """Влезает ли содержание слайда в рамки этой композиции.

    `typical` — строже: текст обязан влезть кеглем не мельче типичного для
    его роли в этом шаблоне (`role_typical`), а не только минимальным.

    Та же проверка, что `_title_fits`, но для блоков, и тем же порядком, что
    и сборка слайда: доля занимаемых мест по варианту, подмена ролей, рамки,
    уже занятые заголовком и соседями, шкала кеглей от стартового вниз.

    Без неё композиция выбиралась по сигнатуре и заголовку, а текст тела не
    мерился вовсе: на живом плане #12 повестка из пяти пунктов ложилась в
    рамку на две строки, хотя в том же шаблоне под ту же сигнатуру были
    рамки на пять. 37 переполненных слайдов из 90.
    """
    if metrics is None or not ladders:
        return True
    free = _fill_share(
        pattern, _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu), strategy
    )
    taken = _header_taken(pattern, plan_slide)
    for block in plan_slide.blocks:
        lines = _block_lines(block)
        if not lines:
            continue
        role = strategy.role_for(block)
        seats = _seat(
            pattern, block, role, free, taken,
            reserve=_text_after(plan_slide.blocks, block, strategy),
        )
        if seats is None:
            # Блок без места уходит в запасную полосу — это уже не
            # композиция шаблона, а вынужденная мера.
            return False
        slot = seats[0][0]
        if _grid_seated(pattern, seats):
            role = SlotRole.BODY
        if role in _DATA_ROLES:
            if not _roomy_for_data(slot.box, spec):
                return False
            taken.append(slot.box)
            continue
        fits, _ = _fit_block(
            [(seat, seat.box, text) for seat, text in seats], spec, strategy, metrics, ladders, role
        )
        if not all(fit.fits for fit in fits):
            return False
        # Порог — ступень шкалы под типичным кеглем роли, одна для всех
        # вариантов. Ровно типичный недостижим для плотного (старт на
        # ступень ниже), со сдвигом вверх — для воздушного. Строгая ступень
        # пустела, подбор уходил в «влезает хоть как-то», и три пункта на
        # `vk_tech` садились в карточку кеглем 8 pt, а то и в таймлайн 4 pt.
        # Порог — и роли блока, и роли места: показатель в рамке основного
        # текста читается не мельче основного текста (13 pt при 18 на
        # `vk_tech` проходили по мерке показателя). Блок, разделённый по
        # местам разных ролей (число и подпись), меряется ролью каждого места.
        split = len({seat.role for seat, _ in seats}) > 1
        if typical is not None:
            for (seat, _), fit in zip(seats, fits, strict=True):
                wanted = (
                    typical.get(seat.role, 0.0)
                    if split
                    else max(typical.get(role, 0.0), typical.get(seat.role, 0.0))
                )
                if fit.size_pt < _step_below(ladders[seat.role], wanted):
                    return False
        taken.extend(seat.box for seat, _ in seats)
    return True


def pick_pattern(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    used: set[str] | None = None,
    metrics: FontMetrics | None = None,
    title_ladder: list[float] | None = None,
    ladders: dict[SlotRole, list[float]] | None = None,
    avoid: set[str] | None = None,
) -> Pattern | None:
    """Композиция под сигнатуру слайда, или None, если такой нет.

    Среди одинаково подходящих предпочитается та, которой колода ещё не
    пользовалась. Без этого все десять слайдов ложатся в одну и ту же
    композицию: `patterns_matching` возвращает устойчивый порядок, а слайды
    с одинаковой сигнатурой берут из него первый. Колода из десяти
    одинаковых слайдов формально верна и практически бесполезна — шаблон
    даёт десятки композиций именно затем, чтобы презентация не выглядела
    одним слайдом, повторённым десять раз.
    """
    used = used or set()
    avoid = avoid or set()
    title_ladder = title_ladder or (ladders or {}).get(SlotRole.TITLE, [])
    needed = needed_profile(plan_slide, strategy)

    typical = {role: role_typical(spec, role) for role in SlotRole}

    def fitting(
        candidates: list[Pattern], tiers: tuple = (typical, None)
    ) -> list[Pattern]:
        """Те, куда влезают и заголовок, и содержание.

        Сначала — где содержание читается типичным кеглем шаблона; если таких
        нет, где оно влезает хотя бы минимальным.
        """
        titled = [
            pattern
            for pattern in candidates
            if _title_fits(pattern, plan_slide, strategy, metrics, title_ladder)
        ]
        for strict in tiers:
            found = [
                pattern
                for pattern in titled
                if _blocks_fit(pattern, spec, plan_slide, strategy, metrics, ladders, strict)
            ]
            if found:
                return found
        return []

    verdicts: dict[str, bool] = {}

    def unsuitable(pattern: Pattern) -> bool:
        if pattern.id not in verdicts:
            verdicts[pattern.id] = _unsuitable(pattern, spec, plan_slide, strategy)
        return verdicts[pattern.id]

    def suitable(candidates: list[Pattern]) -> list[Pattern]:
        return [pattern for pattern in candidates if not unsuitable(pattern)]

    small: dict[str, bool] = {}

    def figure_small(pattern: Pattern) -> bool:
        if pattern.id not in small:
            small[pattern.id] = _figure_small(
                pattern, spec, plan_slide, strategy, metrics, ladders
            )
        return small[pattern.id]

    def best_of(candidates: list[Pattern], strict_ids: set[str] | None = None) -> Pattern:
        """Порядок решений: влезает текст → ещё не было в колоде → вариант.

        «Влезает» стоит первым сознательно. Разнообразие композиций ценно,
        но слайд, где текст не помещается, модель со зрением в #12 видела
        пустым — «только заголовок и пустые маркеры». Предпочтение варианта
        сохраняется порядком кандидатов внутри каждой ступени.
        """
        roomy = fitting(candidates) or [
            pattern
            for pattern in candidates
            if _title_fits(pattern, plan_slide, strategy, metrics, title_ladder)
        ] or candidates
        order = {cls: rank for rank, cls in enumerate(strategy.pattern_preference)}

        has_data = any(
            block.kind in (BlockKind.SERIES, BlockKind.KPI, BlockKind.TABLE)
            for block in plan_slide.blocks
        )
        # График или таблица займут место убранного фото донора; показатель —
        # нет, он садится в своё место.
        has_series = any(
            block.kind in (BlockKind.SERIES, BlockKind.TABLE) for block in plan_slide.blocks
        )

        def key(pattern: Pattern) -> tuple:
            # `avoid` — композиции, которые для этого слайда уже взяли другие
            # варианты. Варианты строятся независимо, и там, где влезающих
            # композиций мало, два из них брали одну и ту же: на `vk_tech`
            # dense и balanced совпали на всех десяти слайдах (A12).
            # Лишние карточки — после разнообразия: пустую карточку рендер
            # уберёт, но не ту, что нарисована в самом layout'е, — на
            # `vk_tech` три пункта ложились в пять пронумерованных карточек.
            return (
                unsuitable(pattern),
                _accent_for_data(pattern, spec, has_series),
                # Фото донора — его тема, а не наша: студент за компьютером
                # доезжал до колоды о продажах кофеен на семь слайдов из
                # одиннадцати. Композиция без фото — раньше разнообразия; с
                # фото — только если без него никак, и тогда рендер фото
                # уберёт, а его место достанется графику или таблице.
                _photo_left_empty(pattern, spec, has_series),
                # Композиция с картинками-показателями донора (кольца «10%») —
                # макет под данные: слайду без данных картинки уйдут, и он
                # останется с тремя строками посреди пустоты.
                bool(pattern.figure_pictures) and not has_data,
                # Показатель с подписью — крупным числом и подписью отдельно;
                # это важнее разнообразия: мелкое «42 минуты» в одном абзаце
                # с подписью — не показатель.
                _figure_unsplit(pattern, spec, plan_slide, strategy),
                figure_small(pattern),
                pattern.id in avoid,
                # Сколько раз колода уже брала её — меньше лучше, а не только
                # «брала или нет»: иначе при всех взятых выигрывала первая.
                used[pattern.id] if isinstance(used, Counter) else pattern.id in used,
                _spare_cards(pattern, spec, plan_slide, strategy),
                strict_ids is not None and pattern.id not in strict_ids,
                order.get(pattern.pattern_class, len(order)),
            )

        return min(roomy, key=key)
    preferred = None
    for candidate in strategy.pattern_preference:
        matches = spec.patterns_matching(needed, preferred=candidate)
        if matches and matches[0].pattern_class is candidate:
            preferred = candidate
            break

    # Строгая сигнатура: паттерн обязан дать ровно то, что просят.
    matches = [
        pattern
        for pattern in spec.patterns_matching(needed, preferred=preferred)
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]

    # Мягкий подбор: заголовок плюс сколько-нибудь мест. Здесь порядок решает
    # не класс, а **сколько блоков паттерн реально усадит**. Иначе выбирается
    # композиция с единственным местом под картинку, два текстовых блока не
    # находят себе рамки и ложатся друг на друга в запасной области.
    wanted = [role for role, count in needed.items() if role is not SlotRole.TITLE
              for _ in range(count)]
    relaxed = [
        pattern
        for pattern in spec.patterns_matching({SlotRole.TITLE: 1}, preferred=preferred)
        if _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
    ]
    # Строгой сигнатуры нет или текст в неё не влезает. Композиция с
    # текстовыми рамками другой роли, куда он влезает, лучше: подмена роли
    # («абзац» вместо «списка») видна только в коде, переполнение — глазами.
    #
    # Сначала — только пригодные композиции (`_unsuitable`): строгая сигнатура,
    # потом мягкая. Непригодная берётся, лишь когда пригодной нет нигде.
    # И типичный кегль — в обоих пулах раньше минимального: строгая
    # сигнатура, где список влезает только кеглем 8 pt, хуже мягкой, где он
    # читается. Внутри ступени сначала композиции, где список разложен по
    # местам, — пункт на место.
    #
    # Строгая и мягкая сигнатуры внутри одной ступени — один пул: иначе
    # вариант брал лучшую из двух-трёх строгих, хотя мягких, так же
    # читаемых и не взятых соседями, было десяток, и три варианта сходились
    # на одной композиции. Строгая сигнатура остаётся предпочтением в ключе
    # `best_of` — после разнообразия.
    strict_ids = {pattern.id for pattern in matches}
    pool = matches + [pattern for pattern in relaxed if pattern.id not in strict_ids]
    repeats = used if isinstance(used, Counter) else Counter(used)
    has_series = any(
        block.kind in (BlockKind.SERIES, BlockKind.TABLE) for block in plan_slide.blocks
    )
    for tier in (typical, None):
        for spread in (True, False):
            roomy = fitting(
                [
                    pattern
                    for pattern in suitable(pool)
                    if not spread or _lists_spread(pattern, spec, plan_slide, strategy)
                ],
                (tier,),
            )
            if not roomy:
                continue
            best = best_of(roomy, strict_ids)
            if repeats[best.id] >= _MAX_REPEATS:
                # Список по карточкам — лучше списка в одной рамке, но не
                # ценой колоды из одинаковых слайдов: на шаблоне, где без фото
                # одна композиция с карточками, она брала восемь слайдов из
                # четырнадцати. Третий раз подряд уступает той, что колода
                # брала реже, если текст там читается тем же кеглем.
                rarer = [
                    pattern
                    for pattern in suitable(pool)
                    if repeats[pattern.id] < repeats[best.id]
                    and not _photo_left_empty(pattern, spec, has_series)
                    # Крупное число — не ценой разнообразия: третий слайд
                    # с одним показателем берёт ту же композицию, а не
                    # строку под заголовком.
                    and (figure_small(best) or not figure_small(pattern))
                ]
                # Тем же кеглем — лучше; нет таких — та, где текст влезает
                # хотя бы кеглем своего места. На синтетическом шаблоне с фото
                # типичный кегль тела (24) взят из layout'ов мастера, а
                # текстовые композиции набраны 16: по типичному проходили
                # одни карточки, и они брали девять слайдов из четырнадцати.
                fresh = fitting(rarer, (tier,)) or [
                    pattern
                    for pattern in fitting(rarer, (None,))
                    if _at_declared_size(pattern, spec, plan_slide, strategy, metrics, ladders)
                    # И не ценой списка в одной рамке, если у повторённой он
                    # разложен по карточкам, — до четвёртого повтора: три
                    # пункта шаблона экзаменов садились в одну рамку 11 pt
                    # ради третьего слайда с карточками.
                    and (
                        not spread
                        or repeats[best.id] > _MAX_REPEATS
                        or _lists_spread(pattern, spec, plan_slide, strategy)
                    )
                ]
                if fresh:
                    return best_of(fresh, strict_ids)
            return best
    if matches and fitting(matches):
        return best_of(matches)
    roomy = fitting(relaxed)
    if roomy:
        return best_of(roomy)
    # Не влезает нигде. Тогда хотя бы так, чтобы у каждого блока был свой
    # слот: переполнение останется находкой, но блоки не лягут друг на друга.
    seated = [
        pattern
        for pattern in (matches or []) + relaxed
        if _seats_all(pattern, spec, plan_slide, strategy, metrics, ladders)
    ]
    # И из них — те, где влезает хотя бы заголовок.
    titled = [
        pattern
        for pattern in seated
        if _title_fits(pattern, plan_slide, strategy, metrics, title_ladder)
    ]
    if titled or seated:
        return best_of(titled or seated)
    if matches:
        return best_of(matches)
    if not relaxed:
        return None

    def seats(pattern: Pattern) -> int:
        free = _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
        return sum(1 for role in wanted if _assign(free, role) is not None)

    best = max(seats(pattern) for pattern in relaxed)
    return best_of([pattern for pattern in relaxed if seats(pattern) == best])


def _on_slide(box: Box, width: int, height: int) -> bool:
    """Помещается ли рамка на слайде целиком."""
    return box.x >= 0 and box.y >= 0 and box.right <= width and box.bottom <= height


def _usable_slots(pattern: Pattern, slide_w: int, slide_h: int) -> list:
    """Слоты композиции, куда можно положить содержание, в порядке чтения.

    Повторители раскрываются в свои места: сетка из четырёх карточек даёт
    четыре комплекта слотов, и содержание раскладывается по ним.

    Раскрытие обрывается краем слайда. `max_count` — оценка того, сколько
    элементов выдержит сетка, и она бывает щедрее слайда: на `vk_education`
    пятая карточка уезжала на полтора дюйма за правый край. Место, которого
    на слайде нет, — не место.

    Слоты, выходящие за край в самом шаблоне, тоже отбрасываются: содержание
    в них гарантированно даст находку «текст за границей слайда».
    """
    slots = [
        s
        for s in pattern.slots
        if s.role in _CONTENT_ROLES and _on_slide(s.box, slide_w, slide_h)
    ]
    for repeater in pattern.repeaters:
        for index in range(repeater.max_count):
            dx, dy = repeater.offset(index)
            expanded = []
            for slot in repeater.item_slots:
                if slot.role not in _CONTENT_ROLES:
                    continue
                box = Box(x=slot.box.x + dx, y=slot.box.y + dy, w=slot.box.w, h=slot.box.h)
                if not _on_slide(box, slide_w, slide_h):
                    expanded = []
                    break
                backdrop = (
                    repeater.member_backdrops[index]
                    if index < len(repeater.member_backdrops)
                    else None
                )
                align = (
                    repeater.member_aligns[index]
                    if index < len(repeater.member_aligns)
                    and repeater.member_aligns[index] is not None
                    and slot.box.area == max(s.box.area for s in repeater.item_slots)
                    else slot.text_align
                )
                expanded.append(
                    slot.model_copy(
                        update={
                            "id": f"{repeater.id}_{index}_{slot.id}",
                            "box": box,
                            "backdrop": backdrop,
                            "text_align": align,
                        }
                    )
                )
            if not expanded:
                # Этот элемент сетки уже не на слайде — следующие тем более.
                break
            slots.extend(expanded)
    return sorted(slots, key=lambda s: (s.box.y, s.box.x))


def _fill_share(pattern: Pattern | None, slots: list, strategy: Strategy) -> list:
    """Места, которые вариант согласен занять.

    Вариант решает, какую долю мест композиции занимать: плотный забивает
    все, воздушный оставляет воздух. Доля режет одиночные места композиции,
    но не карточки повторителя: их число задаёт список, а лишние уходят в
    рендере. Резать и их значило отдавать списку две карточки из четырёх —
    подписи всех карточек стоят в порядке чтения раньше их текста.
    """
    fixed_ids = {slot.id for slot in pattern.slots} if pattern is not None else None
    # Широкая карточка под рядом карточек — продолжение ряда (`_extra_cards`),
    # и доля её не режет, как не режет карточки повторителя.
    fixed = [
        slot
        for slot in slots
        if (fixed_ids is None or slot.id in fixed_ids)
        and not (pattern is not None and _continues_row(slot, pattern))
    ]
    allowed = max(1, round(len(fixed) * strategy.slot_fill_target)) if fixed else 0
    kept = {id(slot) for slot in fixed[:allowed]}
    return [
        slot
        for slot in slots
        if id(slot) in kept
        or (fixed_ids is not None and slot.id not in fixed_ids)
        or (pattern is not None and _continues_row(slot, pattern))
    ]


def _continues_row(slot, pattern: Pattern) -> bool:
    """Текстовое место прямо под рядом карточек, в его границах и не уже карточки."""
    if slot.role not in _CARD_TEXT_ROLES:
        return False
    for repeater in pattern.repeaters:
        frames = repeater.member_frames
        if len(frames) < 2:
            continue
        tolerance = min(frame.w for frame in frames) // 10
        if (
            slot.box.x >= min(frame.x for frame in frames) - tolerance
            and slot.box.right <= max(frame.right for frame in frames) + tolerance
            and slot.box.y >= max(frame.bottom for frame in frames) - tolerance
            and slot.box.w >= min(frame.w for frame in frames)
        ):
            return True
    return False


def _title_slot(container):
    for slot in container.slots:
        if slot.role is SlotRole.TITLE:
            return slot
    return None


def _bookend(spec: TemplateSpec, plan_slide: SlidePlan) -> Pattern | None:
    """Обложка шаблона для титула, его финал — для финала.

    Их берут целиком: первый и последний слайды колоды — это титул и финал
    самого шаблона, меняется только текст. Финала в шаблоне нет — финалом
    служит обложка.
    """
    by_id = {pattern.id: pattern for pattern in spec.patterns}
    if plan_slide.intent is SlideIntent.TITLE:
        wanted = spec.cover_pattern_id
    elif plan_slide.intent is SlideIntent.CLOSING:
        wanted = spec.closing_pattern_id or spec.cover_pattern_id
    else:
        return None
    return by_id.get(wanted) if wanted else None


def pick_layout(spec: TemplateSpec, plan_slide: SlidePlan) -> LayoutSpec:
    """Запасной путь: layout, чья структура не противоречит намерению."""
    if not spec.layouts:
        raise LayoutError(f"в шаблоне {spec.source_name!r} нет ни одного layout'а")

    with_title = [layout for layout in spec.layouts if _title_slot(layout) is not None]
    candidates = with_title or spec.layouts
    if plan_slide.intent in _BARE_INTENTS:
        return candidates[0]
    with_content = [
        layout
        for layout in candidates
        if any(slot.role in _CONTENT_ROLES for slot in layout.slots)
    ]
    return (with_content or candidates)[0]


# Какую долю меньшей из двух рамок разрешено перекрыть, прежде чем считать,
# что они налезли друг на друга. Ноль здесь не годится: рамки шаблона
# соприкасаются краями на пиксель сплошь и рядом.
_OVERLAP_TOLERANCE = 0.15

# Минимальная высота полосы под блок без слота. Меньше этого рамка не
# удерживает и одной строки, и фиттер честно заведёт находку о переполнении.
_MIN_BAND_SHARE_DIVISOR = 274_320  # 0.3 дюйма


def _overlaps(a: Box, b: Box) -> bool:
    """Существенно ли перекрываются две рамки."""
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    if width <= 0 or height <= 0:
        return False
    smaller = min(a.w * a.h, b.w * b.h)
    return smaller > 0 and (width * height) / smaller > _OVERLAP_TOLERANCE


def _assign(
    slots: list, role: SlotRole, taken: list[Box] | None = None, fallback: bool = True
) -> object | None:
    """Свободный слот нужной роли, иначе — ближайший подходящий.

    Точное совпадение роли предпочтительнее, но отказываться от вёрстки из-за
    того, что в композиции список называется абзацем, не за что: и то и
    другое — рамка под текст.

    Слот, налезающий на уже занятую рамку, не берётся вовсе. Композиция
    снимается со слайда-примера, и её слоты перекрываются там, где у дизайнера
    надпись лежала поверх карточки. Положить туда два разных текста значит
    выдать кашу: на `zelenie_investicii` так получалось до шести наложений на
    колоду.
    """
    taken = taken or []
    for wanted in (role, *(_TEXT_FALLBACK.get(role, ()) if fallback else ())):
        free = [slot for slot in slots if slot.role is wanted]
        clear = [
            slot for slot in free if not any(_overlaps(slot.box, box) for box in taken)
        ]
        if clear:
            slots.remove(clear[0])
            return clear[0]
    return None


def _chunks(items: list[str], count: int) -> list[list[str]]:
    """Пункты подряд на `count` частей, различающихся не больше чем на один."""
    size, extra = divmod(len(items), count)
    parts, start = [], 0
    for index in range(count):
        end = start + size + (1 if index < extra else 0)
        parts.append(list(items[start:end]))
        start = end
    return parts


def _spread(
    pattern: Pattern | None,
    block,
    wanted: SlotRole,
    free: list,
    taken: list[Box],
    reserve: int = 0,
) -> list[tuple[object, list[str]]] | None:
    """Список, разложенный по элементам повторителя: пункт в карточку.

    Донор с четырьмя карточками — это четыре места под четыре мысли. Весь
    список в первой карточке при трёх пустых рядом — главный дефект листов
    #12: 14 слайдов из 90 на `vk_tech`. Пунктов больше, чем карточек, —
    они делятся поровну подряд; меньше — лишние карточки уходят в рендере.

    Берутся только элементы, которые есть у донора (`observed_count`): свою
    подложку и пиктограмму получают лишь они, дорисованная карточка была бы
    текстом без карточки. Все места повторителя после этого заняты — иначе
    следующий блок сел бы в четвёртую карточку при пустых второй и третьей.
    """
    if pattern is None or block.kind not in _SPREAD_KINDS or block.heading:
        return None
    if len(block.items) < 2:
        return None
    by_id = {slot.id: slot for slot in free}
    # Повторителей на слайде бывает несколько: две кнопки «готово / доработка»
    # и три колонки проверки на шаблоне-бланке. Первый попавшийся уводил три
    # пункта в две кнопки; берётся тот, где на все пункты хватает элементов,
    # а среди таких — с самым вместительным местом.
    options = []
    for repeater in pattern.repeaters:
        for item_slot in sorted(repeater.item_slots, key=lambda s: -s.box.area):
            if item_slot.role is not wanted:
                continue
            members = []
            for index in range(repeater.observed_count):
                slot = by_id.get(f"{repeater.id}_{index}_{item_slot.id}")
                if slot is None or any(_overlaps(slot.box, box) for box in taken):
                    break
                members.append(slot)
            if len(members) < 2:
                continue
            members += _extra_cards(repeater, item_slot, members, free, taken, block, reserve)
            covered = min(len(block.items), len(members))
            options.append(((covered, item_slot.box.area), repeater, members))
            break
    if not options:
        return None
    _, repeater, members = max(options, key=lambda option: option[0])
    count = min(len(block.items), len(members))
    prefix = f"{repeater.id}_"
    chosen = {slot.id for slot in members[:count]}
    free[:] = [
        slot for slot in free if not slot.id.startswith(prefix) and slot.id not in chosen
    ]
    return list(zip(members[:count], _chunks(list(block.items), count), strict=True))


def _extra_cards(
    repeater, item_slot, members: list, free: list, taken: list[Box], block, reserve: int
) -> list:
    """Места того же рода вслед за карточками, если пунктов больше карточек.

    «Две карточки и широкая под ними» шаблона экзаменов: повторитель — две
    одинаковые карточки, широкая — отдельное место той же роли. Три пункта
    делились 2 + 1 по верхним карточкам, широкая оставалась под абзацем.
    Места ниже или правее карточек, той же роли, не уже карточки и не меньше
    половины её места под текст, продолжают ряд по порядку чтения; из
    мест одной карточки берётся просторное.
    """
    prefix = f"{repeater.id}_"
    # Места, нужные текстовым блокам слайда после списка, не берутся: иначе
    # абзац под списком остаётся без места.
    singles = [
        slot
        for slot in free
        if not slot.id.startswith(prefix)
        and slot.role in _CARD_TEXT_ROLES
        and not any(_overlaps(slot.box, box) for box in taken)
    ]
    wanted = min(len(block.items) - len(members), len(singles) - reserve)
    if wanted <= 0:
        return []
    first = members[0].box
    extra = [
        slot
        for slot in free
        if not slot.id.startswith(prefix)
        and slot.role is item_slot.role
        and slot.box.w >= first.w
        and slot.box.area >= first.area // 2
        and slot.box.y >= first.y
        and not any(_overlaps(slot.box, box) for box in taken)
        and not any(_overlaps(slot.box, member.box) for member in members)
    ]
    # Из мест одной карточки — одно, просторное, под текст, а не строка
    # шапки: шапка и текст широкой карточки — одна колонка вплотную, и
    # четыре пункта садились в неё двумя.
    chosen: list = []
    for slot in sorted(extra, key=lambda slot: -slot.box.area):
        if len(chosen) == wanted:
            break
        if not any(
            _stacked(slot.box, other.box) and _gap(slot.box, other.box) <= slot.box.h
            for other in chosen
        ):
            chosen.append(slot)
    return sorted(chosen, key=lambda slot: (slot.box.y, slot.box.x))


def _text_after(blocks: list, block, strategy: Strategy) -> int:
    """Сколько текстовых блоков слайда садятся после этого."""
    index = next(i for i, other in enumerate(blocks) if other is block)
    return sum(
        1
        for other in blocks[index + 1 :]
        if _block_lines(other) and strategy.role_for(other) not in _DATA_ROLES
    )


def _seat(
    pattern: Pattern | None,
    block,
    role: SlotRole,
    free: list,
    taken: list[Box],
    reserve: int = 0,
) -> list[tuple[object, list[str]]] | None:
    """Места блока и строки для каждого; None — места не нашлось.

    Роли перебираются в порядке предпочтения, и на каждой сначала —
    повторитель, потом одиночный слот. Не наоборот: на holdout повторитель
    был только из подписей в кружках, и список, раскладываясь «по
    повторителю любой роли», уходил в кружки мимо трёх карточек под текст.

    Одна и та же раскладка нужна подбору композиции, расчёту ёмкости и
    сборке слайда: мерить одно, а собирать другое значит снова получить
    переполнение, которого подбор не предвидел.
    """
    seats = _seat_raw(pattern, block, role, free, taken, reserve)
    if seats is None or pattern is None or not pattern.decor:
        return seats
    return [
        (slot.model_copy(update={"box": _clear_of_decor(slot.box, pattern.decor)}), lines)
        for slot, lines in seats
    ]


# Оформление шапки — не дальше этого под заголовком или подзаголовком.
_HEADER_DECOR_REACH = 274_320  # 0.3″


def _below(area: Box, top: int) -> Box:
    """Область, обрезанная сверху: то, что ниже `top`."""
    if top <= area.y or top >= area.bottom:
        return area
    return area.model_copy(update={"y": top, "h": area.bottom - top})


def _header_bottom(pattern: Pattern | None, title_bottom: int) -> int:
    """Низ шапки слайда: заголовок, подзаголовок и оформление сразу под ними.

    График рос от низа заголовка и ложился на подзаголовок и прогресс-бар
    шаблона-бланка, а уборка под ним снимала сегменты прогресс-бара.
    """
    if pattern is None:
        return title_bottom
    bottom = max(
        [title_bottom]
        + [slot.box.bottom for slot in pattern.slots if slot.role is SlotRole.SUBTITLE]
    )
    for box in sorted(pattern.decor, key=lambda item: item.y):
        if bottom <= box.y <= bottom + _HEADER_DECOR_REACH:
            bottom = max(bottom, box.bottom)
    return bottom


def _grid_seats(pattern: Pattern | None, block, free: list) -> list | None:
    """Таблица плана — по ячейкам таблицы из прямоугольников.

    Шапка — названия колонок, строки — элементы повторителя, ячейка — своё
    место. Колонок больше, чем в шаблоне, или строк больше, чем у донора, —
    не сюда: дорисованная строка была бы текстом без полосы.
    """
    if pattern is None or pattern.table_grid is None:
        return None
    if block.kind is not BlockKind.TABLE or block.table is None:
        return None
    grid = pattern.table_grid
    repeater = next((r for r in pattern.repeaters if r.id == grid.repeater_id), None)
    columns, rows = block.table.columns, block.table.rows
    if repeater is None or not rows or len(columns) > len(grid.cell_slot_ids):
        return None
    if len(rows) > repeater.observed_count:
        return None
    fixed = {slot.id: slot for slot in pattern.slots}
    by_id = {slot.id: slot for slot in free}
    seats: list[tuple[object, list[str]]] = []
    number = fixed.get(grid.number_header_id or "")
    if number is not None and number.placeholder_text:
        # Подпись колонки номеров — часть таблицы, номера пишет рендер.
        seats.append((number, [number.placeholder_text]))
    for head_id, name in zip(grid.header_slot_ids, columns, strict=False):
        seats.append((fixed[head_id], [name]))
    for index, row in enumerate(rows):
        for cell_id, value in zip(grid.cell_slot_ids, row, strict=False):
            slot = by_id.get(f"{repeater.id}_{index}_{cell_id}")
            if slot is None:
                return None
            seats.append((slot, [value]))
    prefix = f"{repeater.id}_"
    free[:] = [slot for slot in free if not slot.id.startswith(prefix)]
    return seats


def _grid_seated(pattern: Pattern | None, seats) -> bool:
    """Разложен ли блок по таблице из прямоугольников: тогда это текст."""
    if pattern is None or pattern.table_grid is None or not seats:
        return False
    grid = pattern.table_grid
    ids = {seat.id for seat, _ in seats if seat is not None}
    return bool(ids & set(grid.header_slot_ids)) or any(
        seat_id.startswith(f"{grid.repeater_id}_") for seat_id in ids
    )


def _seat_raw(
    pattern: Pattern | None,
    block,
    role: SlotRole,
    free: list,
    taken: list[Box],
    reserve: int = 0,
) -> list[tuple[object, list[str]]] | None:
    grid = _grid_seats(pattern, block, free)
    if grid is not None:
        return grid
    split = _split_heading(pattern, block, free, taken)
    if split:
        return split
    roles = (role, *_TEXT_FALLBACK.get(role, ()))
    # Список раскладывается по элементам повторителя любой подходящей роли
    # раньше, чем садится целиком в одиночную рамку: на шаблоне-бланке у
    # слайда ресурсов под четырьмя колонками стоит строка «Бюджет /
    # ограничение», и три пункта уходили в неё, мимо колонок.
    if block.kind in _SPREAD_KINDS and len(block.items) > 1:
        for wanted in roles:
            pool = list(free)
            spread = _spread(pattern, block, wanted, free, taken, reserve)
            if spread:
                return [
                    (_absorb(pattern, seat, pool, free, taken, block), lines)
                    for seat, lines in spread
                ]
    for wanted in roles:
        pool = list(free)
        spread = _spread(pattern, block, wanted, free, taken, reserve)
        if spread:
            return [
                (_absorb(pattern, seat, pool, free, taken, block), lines)
                for seat, lines in spread
            ]
        slot = _assign(free, wanted, taken, fallback=False)
        if slot is not None:
            return [(_absorb(pattern, slot, pool, free, taken, block), _block_lines(block))]
    return None


# Доля рамки от края, в которой графика считается «сверху» или «снизу» текста.
_DECOR_EDGE_SHARE = 0.2
# Зазор между текстом и графикой донора: текст не касается линии. С запасом
# на строку ниже нашей модели: LibreOffice ставит строки чуть ниже.
_DECOR_GAP = 182_880  # 0.2″


def _clear_of_decor(box: Box, decor: list[Box]) -> Box:
    """Место текста без графики донора: кончается там, где она начинается.

    Линия под цифрой, иконка в карточке, картинка сбоку лежат внутри рамки
    донора, потому что его текст короче рамки и до них не доходит (или
    отступ рамки уводит его в сторону). Наш может дойти — место обрезается
    над графикой, если там хватает места (текст идёт сверху), иначе с той
    стороны, где места больше: линия внизу срезает низ, значок слева в
    невысокой карточке — левый край. Графика, накрывающая рамку
    целиком, — подложка, её не трогаем; место, от которого осталась бы
    полоска, — тоже.
    """
    left, top, right, bottom = box.x, box.y, box.right, box.bottom
    for item in decor:
        current = Box(x=left, y=top, w=right - left, h=bottom - top)
        if item.intersection(current) is None or item.contains(current):
            continue
        options = [
            (left, top, right, item.y - _DECOR_GAP),  # над графикой
            (left, item.bottom + _DECOR_GAP, right, bottom),  # под ней
            (left, top, item.x - _DECOR_GAP, bottom),  # слева
            (item.right + _DECOR_GAP, top, right, bottom),  # справа
        ]
        roomy = [
            option
            for option in options
            if option[2] - option[0] >= box.w * _DECOR_EDGE_SHARE
            and option[3] - option[1] >= box.h * _DECOR_EDGE_SHARE
        ]
        if not roomy:
            continue
        # Текст идёт сверху: если над графикой места хватает, начало текста
        # не двигается (значок в середине высокой карточки `vk_workspace`
        # не сдвигает «4 системы» вправо). Иначе — сторона с наибольшим местом.
        left, top, right, bottom = (
            options[0]
            if options[0] in roomy
            else max(
                roomy, key=lambda option: (option[2] - option[0]) * (option[3] - option[1])
            )
        )
    return Box(x=left, y=top, w=right - left, h=bottom - top)


# Место подписи меньше этой доли места числа — бирка, а не подпись.
_LABEL_MIN_SHARE = 0.15
# Рамка числа, делимая на число и подпись: доля числа и наименьшая высота
# (две строки подписи и строка числа не уже дюйма).
_FIGURE_SHARE = 0.45
_MIN_SPLIT_HEIGHT = 914_400
# Роли, которыми пишется подпись показателя, от точной к общей.
_LABEL_ROLES = (
    SlotRole.KPI_LABEL,
    SlotRole.CAPTION,
    SlotRole.BODY,
    SlotRole.BULLETS,
    SlotRole.SUBTITLE,
)
# Текстовые места карточки: их объединение — текстовая область карточки.
_CARD_TEXT_ROLES = frozenset(
    {
        SlotRole.BODY,
        SlotRole.BULLETS,
        SlotRole.CAPTION,
        SlotRole.SUBTITLE,
        SlotRole.KPI_LABEL,
        SlotRole.KPI_VALUE,
    }
)


def _member_prefix(pattern: Pattern | None, slot_id: str) -> str | None:
    """Префикс мест одного элемента повторителя (`rep_2_`), если место — из него."""
    if pattern is None:
        return None
    for repeater in pattern.repeaters:
        head = f"{repeater.id}_"
        if slot_id.startswith(head):
            return f"{head}{slot_id[len(head):].split('_', 1)[0]}_"
    return None


def _gap(a: Box, b: Box) -> int:
    """Расстояние между рамками: 0, если касаются или пересекаются."""
    dx = max(0, b.x - a.right, a.x - b.right)
    dy = max(0, b.y - a.bottom, a.y - b.bottom)
    return max(dx, dy)


def _split_heading(
    pattern: Pattern | None, block, free: list, taken: list[Box]
) -> list[tuple[object, list[str]]] | None:
    """Показатель: число — в место числа, подпись — в соседнее текстовое место.

    Шаблон кладёт их раздельно: крупное число и подпись под ним кеглем
    текста. Вместе в месте числа подпись обязана влезть кеглем числа — на
    holdout 115 pt — и не влезает; в месте текста число пишется подписью.
    Подпись берётся из того же элемента повторителя, иначе — самое
    просторное место не дальше высоты большего из двух.

    Место подписи может заходить в низ рамки числа: рамка у донора выше
    самой цифры («Описание показателя» под «91%» на `vk_tech`). Тогда рамка
    числа кончается там, где начинается подпись; раньше такое место
    отбрасывалось, и число с подписью писались вместе мелко.

    Места под подпись нет вовсе — рамка числа делится: сверху число, снизу
    подпись своим кеглем. Рамка числа сначала обрезается по графике донора
    (линия под цифрой).
    """
    if block.kind is not BlockKind.KPI or not block.heading or not block.items:
        return None

    def clear(slot) -> bool:
        return not any(_overlaps(slot.box, box) for box in taken)

    values = [slot for slot in free if slot.role is SlotRole.KPI_VALUE and clear(slot)]
    if not values:
        return None
    # Самое крупное место числа: у ряда показателей донора бывают и
    # микроподписи с ролью числа (0.18″ на `vk_tech`).
    value = max(values, key=lambda slot: slot.box.area)
    decor = pattern.decor if pattern is not None else []
    value_box = _clear_of_decor(value.box, decor)
    member = _member_prefix(pattern, value.id)

    def below(slot) -> bool:
        return slot.box.y >= value_box.y + value_box.h // 2

    labels = [
        slot
        for slot in free
        if slot is not value
        and slot.role in _LABEL_ROLES
        and clear(slot)
        and (not _overlaps(slot.box, value_box) or below(slot))
        and _gap(slot.box, value_box) <= max(slot.box.h, value_box.h)
    ]
    if member is not None:
        labels = [slot for slot in labels if slot.id.startswith(member)] or labels
    # Самое просторное из соседних: описание показателя, а не бирка
    # «Заголовок» 0.8×0.2″ рядом с числом.
    label = max(
        labels, key=lambda slot: (slot.box.area, -_gap(slot.box, value_box)), default=None
    )
    if label is not None and label.box.area >= value_box.area * _LABEL_MIN_SHARE:
        if _overlaps(label.box, value_box):
            value_box = value_box.model_copy(update={"h": label.box.y - value_box.y})
        free.remove(value)
        free.remove(label)
        return [
            (value.model_copy(update={"box": value_box}), [block.heading]),
            (label, list(block.items)),
        ]
    # Рядом только бирка или ничего: рамка числа делится по высоте.
    if value_box.h < _MIN_SPLIT_HEIGHT:
        return None
    top = int(value_box.h * _FIGURE_SHARE)
    free.remove(value)
    return [
        (value.model_copy(update={"box": value_box.model_copy(update={"h": top})}),
         [block.heading]),
        (
            value.model_copy(
                update={
                    "id": f"{value.id}_label",
                    # Подпись — текст, и пишется кеглем основного текста, как
                    # «Описание показателя» донора, а не бирок показателя.
                    "role": SlotRole.BODY,
                    "style": None,
                    "text_color": None,
                    "text_valign": VAlign.TOP,
                    "box": value_box.model_copy(
                        update={"y": value_box.y + top, "h": value_box.h - top}
                    ),
                }
            ),
            list(block.items),
        ),
    ]


def _absorb(
    pattern: Pattern | None, slot, pool: list, free: list, taken: list[Box], block=None
):
    """Место в карточке растёт на её текстовую область.

    Донор делит карточку на подзаголовок и описание. Блок без подзаголовка,
    усаженный в одно из них, либо садился ниже пустого места подзаголовка
    (пункты `vk_tech` — в серое описание), либо писался в месте подзаголовка
    (абзац `vk_workspace` balanced 4). Его место — вся текстовая область
    карточки: объединение её текстовых мест, стоящих друг над другом.
    """
    member = _member_prefix(pattern, slot.id)
    if member is None or slot.role not in _CARD_TEXT_ROLES:
        return slot
    below = _body_below_picture(pattern, slot, pool, taken, member)
    if (
        below is not None
        and block is not None
        and not block.heading
        and block.kind is not BlockKind.KPI
    ):
        # Рамка заголовка во всю карточку, в середине — иконка, под ней
        # место текста (`vk_workspace`). Абзац без заголовка начинается с
        # места текста и идёт до низа карточки, а не пишется вверху, где
        # шаблон ставит заголовок.
        free[:] = [other for other in free if other.id != below.id]
        return below.model_copy(
            update={"box": below.box.model_copy(update={"h": slot.box.bottom - below.box.y})}
        )
    siblings = [
        other
        for other in pool
        if other.id != slot.id
        and other.id.startswith(member)
        and other.role in _CARD_TEXT_ROLES
        and _stacked(other.box, slot.box)
        and not any(_overlaps(other.box, box) for box in taken)
    ]
    if not siblings:
        return slot
    left = min(box.x for box in [slot.box, *(o.box for o in siblings)])
    top = min(box.y for box in [slot.box, *(o.box for o in siblings)])
    right = max(box.right for box in [slot.box, *(o.box for o in siblings)])
    bottom = max(box.bottom for box in [slot.box, *(o.box for o in siblings)])
    ids = {other.id for other in siblings}
    free[:] = [other for other in free if other.id not in ids]
    return slot.model_copy(update={"box": Box(x=left, y=top, w=right - left, h=bottom - top)})


def _body_below_picture(pattern: Pattern, slot, pool: list, taken: list[Box], member: str):
    """Место текста карточки под картинкой внутри рамки `slot`; None — нет такого."""
    pictures = [
        other.box
        for other in pool
        if other.role in (SlotRole.IMAGE, SlotRole.ICON) and slot.box.contains(other.box)
    ]
    pictures += [
        other.box
        for other in pattern.slots
        if other.role in (SlotRole.IMAGE, SlotRole.ICON) and slot.box.contains(other.box)
    ]
    candidates = [
        other
        for other in pool
        if other.id != slot.id
        and other.id.startswith(member)
        and other.role in _CARD_TEXT_ROLES
        and slot.box.contains(other.box)
        and any(picture.bottom <= other.box.y for picture in pictures)
        and any(picture.y >= slot.box.y + slot.box.h // 10 for picture in pictures)
        and not any(_overlaps(other.box, box) for box in taken)
    ]
    return min(candidates, key=lambda other: other.box.y) if candidates else None


def _stacked(a: Box, b: Box) -> bool:
    """Рамки одной колонки: ширина в пределах четверти, левые края вровень.

    Подзаголовок и описание карточки — одна колонка. Подпись в кружке над
    карточкой (holdout, 0.8″ при 2.6″) и номер шага таймлайна над описанием
    (`vk_workspace`) — нет: слитый с ними текст ложится под кружок и на
    линию таймлайна.
    """
    wide = max(a.w, b.w)
    return min(a.w, b.w) >= 0.75 * wide and abs(a.x - b.x) <= 0.25 * wide


def _fit_block(
    seats: list[tuple[object, Box, list[str]]],
    spec: TemplateSpec,
    strategy: Strategy,
    metrics: FontMetrics,
    ladders: dict[SlotRole, list[float]],
    role: SlotRole,
) -> tuple[list[FitResult], list[float]]:
    """Кегль мест блока: один на места одной роли, у каждой роли — свой.

    Карточки одного ряда пишутся одинаково. Число показателя и его подпись —
    разные роли со своими шкалами: 115 pt числу и 20 pt подписи на holdout.
    Возвращает подбор и стартовый кегль для каждого места.
    """
    fits: list[FitResult | None] = [None] * len(seats)
    starts = [0.0] * len(seats)
    groups: dict[SlotRole, list[int]] = {}
    for number, (slot, _, _) in enumerate(seats):
        groups.setdefault(slot.role if slot is not None else role, []).append(number)
    for group_role, numbers in groups.items():
        slot = seats[numbers[0]][0]
        ladder = ladders[group_role]
        declared = (
            slot.style.size_pt
            if slot is not None and slot.style is not None
            else _undeclared(spec, group_role, ladder)
        )
        start = strategy.start_size(ladder, declared)
        found = _fit_seats(
            [(seats[n][1], seats[n][2]) for n in numbers], metrics, ladder, start
        )
        for n, fit in zip(numbers, found, strict=True):
            fits[n] = fit
            starts[n] = start
    return fits, starts


def _fit_seats(
    seats: list[tuple[Box, list[str]]],
    metrics: FontMetrics,
    ladder: list[float],
    start: float,
) -> list[FitResult]:
    """Один кегль на все места блока: карточки одного ряда пишутся одинаково."""
    first = [fit_paragraphs(lines, metrics, box, ladder, start) for box, lines in seats]
    common = min(fit.size_pt for fit in first)
    if all(fit.size_pt == common for fit in first):
        return first
    return [fit_paragraphs(lines, metrics, box, ladder, common) for box, lines in seats]


# ── Цвет и свободная область ─────────────────────────────────────────────────


# Порог контраста из Приложения 1 ТЗ. Тем же числом меряет и аудит, поэтому
# вёрстка не имеет права выдавать то, что он справедливо забракует.
MIN_CONTRAST = 4.5

# Высота строки таблицы в кеглях и сколько символов кегля нужно колонке, чтобы
# в неё влезло хоть что-то. Оценка грубая и намеренно такая: точную ширину
# колонок считает PowerPoint, а здесь решается только «влезет или переехать».
TABLE_ROW_HEIGHT = 1.8
TABLE_MIN_CHARS = 6
EMU_PER_POINT = 12_700


def _under(slot, background: Color | None) -> Color | None:
    """Фон, на котором окажется текст слота: его подложка, иначе фон слайда."""
    if slot is not None and getattr(slot, "backdrop", None) is not None:
        return slot.backdrop
    return background


def _text_color(
    container,
    role: SlotRole,
    is_dark: bool,
    background: Color | None,
    spec: TemplateSpec | None = None,
    slot=None,
    size_pt: float = 0.0,
    bold: bool = False,
) -> Color:
    """Цвет текста для роли — взятый из самого шаблона и проверенный на фоне.

    Порядок предпочтений: цвет, которым шаблон пишет в этом самом месте
    (`slot`); затем — текст этой роли в композиции; затем цвет любого её
    текстового слота; и только если шаблон не сказал ничего — выбор по
    яркости фона.

    Так правильнее, чем всегда считать по фону: шаблон уже решил, каким
    цветом здесь писать, и его решение учитывает градиенты, фоновые картинки
    и декор, о которых мы не знаем ничего.

    Но взятый цвет обязан пройти проверку контрастом — порогом для этого
    кегля: крупному тексту WCAG требует 3:1, основному 4.5:1. Композиция
    снимается со слайда-примера, а фон слайду назначает его layout — и это
    законно разные слайды: на `vk_workspace` композиция со светлого примера
    приезжала на чёрный фон, и текст получался чёрным по чёрному. Не
    влезающий в порог цвет заменяется на тот, что читается.
    """
    # Фон бывает неизвестен: композиция снята с донора, а фон слайду назначает
    # его layout. Судим тогда по яркости шаблона — ею фон и окажется.
    judged = background or (Color(rgb="000000") if is_dark else Color(rgb="FFFFFF"))
    needed = required_contrast(size_pt, bold, MIN_CONTRAST)

    def readable(color: Color | None) -> bool:
        if color is None:
            return False
        ratio = color.contrast_ratio(judged)
        # Пара, которой шаблон пишет сам, — решение бренда; ей хватает
        # порога крупного текста. Белый по фирменному синему `vk_education`
        # (4.4:1) иначе становился чёрным — «чёрный текст на синих подложках».
        return ratio >= needed or (
            spec is not None
            and spec.writes_on(color, judged)
            and ratio >= required_contrast(LARGE_TEXT_PT, False, MIN_CONTRAST)
        )

    own = (
        slot.text_color or (slot.style.color if slot.style is not None else None)
        if slot is not None
        else None
    )
    candidates: list[Color | None] = []
    # Обложка и финал — слайды шаблона целиком, с заменой только текста: их
    # место пишется своим цветом (чёрный заголовок финала `vk_education`
    # при синих заголовках содержательных слайдов).
    if spec is not None and getattr(container, "id", None) in spec.bookend_ids:
        candidates.append(own)
    if spec is not None:
        # Место на цветной подложке: чем шаблон пишет на ней самой (белым по
        # синей карточке); затем — цвет роли на фоне этой яркости.
        card = getattr(slot, "backdrop", None)
        if card is not None:
            candidates.extend(_written_on(card, spec))
        candidates.append(spec.role_color(role, judged.luminance < 0.5))
    candidates.append(own)
    candidates.extend(
        candidate.style.color
        for candidate in container.slots
        if candidate.role is role and candidate.style is not None
    )
    for candidate in candidates:
        if readable(candidate):
            return candidate

    # Донорский цвет на нашем фоне не читается (или его нет). Прежде чем
    # придумывать свой, спрашиваем палитру шаблона: колода обязана быть
    # набрана его цветами, а не нашими. `#111111` остаётся крайним случаем —
    # шаблоном, в палитре которого нет ни одного читаемого цвета текста.
    if spec is not None:
        from_palette = readable_text_color(spec.palette, background, is_dark)
        if from_palette is not None:
            return from_palette
    # Ни один цвет шаблона не читается. Из белого и почти чёрного — тот, что
    # контрастнее на этом фоне: на средне-яркой карточке признак «шаблон
    # тёмный» выбирал не тот.
    return max(
        (Color(rgb="FFFFFF"), Color(rgb="111111")),
        key=lambda color: color.contrast_ratio(judged),
    )


def _align_of(slot) -> Align:
    """Выравнивание места — то, которым пишет донор; по левому краю, если не сказано."""
    if slot is None:
        return Align.LEFT
    if slot.text_align is not None:
        return slot.text_align
    return slot.style.align if slot.style is not None else Align.LEFT


def _valign_of(slot) -> VAlign:
    """Привязка места по вертикали — донора; сверху, если не сказано."""
    if slot is None:
        return VAlign.TOP
    if slot.text_valign is not None:
        return slot.text_valign
    return slot.style.valign if slot.style is not None else VAlign.TOP


def _content_area(spec: TemplateSpec, container) -> Box:
    """Свободная область: поля шаблона минус то, что занимает заголовок."""
    grid = spec.grid
    left = grid.margin_left_emu if grid else spec.slide_width_emu // 20
    right = grid.margin_right_emu if grid else spec.slide_width_emu // 20
    top = grid.margin_top_emu if grid else spec.slide_height_emu // 12
    bottom = grid.margin_bottom_emu if grid else spec.slide_height_emu // 12

    width = spec.slide_width_emu - left - right
    if width <= 0:
        raise LayoutError(
            f"поля шаблона {spec.source_name!r} не оставляют места под содержание"
        )

    # Обычно содержание идёт под заголовком. Но заголовок композиции бывает и
    # внизу слайда — тогда места под ним нет, и упираться в это нельзя:
    # берём всю область внутри полей, а разложить по ней — забота слотов.
    title = _title_slot(container)
    under_title = top
    if title is not None:
        under_title = max(top, title.box.bottom + spec.slide_height_emu // 40)
    if spec.slide_height_emu - under_title - bottom > spec.slide_height_emu // 10:
        top = under_title

    height = spec.slide_height_emu - top - bottom
    if height <= 0:
        raise LayoutError(
            f"поля шаблона {spec.source_name!r} не оставляют места под содержание"
        )
    return Box(x=left, y=top, w=width, h=height)


def _free_band(
    spec: TemplateSpec,
    container,
    index: int,
    bands: int,
    taken: list[Box],
    data: bool = False,
) -> Box:
    """Полоса свободной области для блока, которому не досталось слота.

    Блоки без слота нельзя класть в одну и ту же рамку: на `vk_workspace`
    два таких блока легли друг на друга, и слайд читался как каша из двух
    текстов. Свободная область делится на равные полосы по числу блоков
    слайда — верхняя граница, зато без наложений между самими полосами.

    Отсчёт идёт ниже всего, что слайд уже занял. Иначе полоса ложится на
    слот, взятый предыдущим блоком: на `zelenie_investicii` она садилась
    ровно на рамку соседа, потому что свободная область считалась от
    заголовка и про занятые слоты не знала.
    """
    area = _content_area(spec, container)
    floor = max(
        (box.bottom for box in taken if box.bottom <= area.bottom), default=area.y
    )
    # И ниже шапки: подзаголовок и прогресс-бар под заголовком не заняты
    # нашим содержанием, но место — их.
    if isinstance(container, Pattern):
        floor = _header_bottom(container, floor)
    top = min(max(area.y, floor), area.bottom - _MIN_BAND_SHARE_DIVISOR)
    remaining = max(_MIN_BAND_SHARE_DIVISOR, area.bottom - top)
    share = max(1, bands - index)
    band = Box(x=area.x, y=top, w=area.w, h=max(1, remaining // share))
    if not data or _roomy_for_data(band, spec):
        return band
    side = _side_region(spec, container, area, taken)
    return side if side is not None else band


def _side_region(spec: TemplateSpec, container, area: Box, taken: list[Box]) -> Box | None:
    """Место графика сбоку от текста, если под текстом его не осталось.

    Полоса под всем занятым — на `vk_workspace` показатель сел в нижнее
    место справа, и график получил под ним полосу в 0.15″, хотя слева от
    показателя под заголовком пустовала половина слайда. Берётся область
    под шапкой слева или справа от занятого содержанием, наибольшая из
    тех, где график читается.
    """
    header = max((box.bottom for box in taken[:1]), default=area.y)
    if isinstance(container, Pattern):
        header = _header_bottom(container, header)
    top = max(area.y, header) + spec.slide_height_emu // DATA_GAP_SHARE
    content = [box for box in taken[1:] if box.bottom > top]
    if not content or top >= area.bottom:
        return None
    gap = spec.slide_height_emu // DATA_GAP_SHARE
    left = min(box.x for box in content) - gap
    right = max(box.right for box in content) + gap
    candidates = [
        Box(x=x, y=top, w=w, h=area.bottom - top)
        for x, w in ((area.x, left - area.x), (right, area.right - right))
        if w > 0
    ]
    roomy = [box for box in candidates if _roomy_for_data(box, spec)]
    return max(roomy, key=lambda box: box.area, default=None)


def _data_element(
    element_id: str,
    block,
    role: SlotRole,
    box: Box,
    style: TextStyle,
    spec: TemplateSpec,
    pack,
    background: Color | None,
    is_dark: bool,
    roomy: Box,
    plan_slide: SlidePlan | None = None,
    title: str = "",
) -> Element | None:
    """Блок плана как нативный график или таблица, если это возможно.

    Возвращает None, когда данных не хватает: ряда нет в пакете, категории
    рядов не сошлись, таблица пуста. Тогда блок верстается текстом — это хуже
    графика, но честнее пустой рамки.

    Растр здесь невозможен по построению: и `c:chart`, и `a:tbl` — нативные
    объекты, которые человек может открыть и поправить.
    """
    if role is SlotRole.CHART and block.series_ids and pack is not None:
        # В поля шаблона: место под фото бывает во весь слайд, от края до
        # края, а график у края выглядит обрезанным.
        inner = _within_margins(box, roomy)
        if inner is not None and _roomy_for_data(inner, spec):
            box = inner
        brand = _brand_colors(spec, background, is_dark)
        categories, series = series_from_pack(block.series_ids, pack, brand)
        if categories and series:
            # Кегль подписей — текстовый, а не кегль слота донора: на
            # `vk_workspace` график сел в место под «ххх%» кеглем 80, и его
            # подписи легли вертикальной кашей.
            body = role_typical(spec, SlotRole.BODY) or style.size_pt
            label = style.model_copy(
                update={"size_pt": max(CHART_MIN_PT, min(style.size_pt, body))}
            )
            single = len(series) == 1
            unit = series_unit(block.series_ids, pack)
            # Подпись значения — в одну строку над столбиком: кегль по ширине
            # столбика в пределах шкалы, а не влезло — без единицы.
            size, unit = _value_label(
                box, categories, series[0].values, unit, label.size_pt, spec
            )
            label = label.model_copy(update={"size_pt": size})
            kind = _chart_kind(block.series_ids, categories, unit, pack)
            parts = kind is ChartKind.DOUGHNUT
            return Element(
                id=element_id,
                kind=ElementKind.CHART,
                role=SlotRole.CHART,
                box=box,
                provenance=Provenance(kind=SourceKind.DERIVED, ref=block.id),
                chart=ChartContent(
                    chart_kind=kind,
                    categories=categories,
                    series=series,
                    has_legend=len(series) > 1 or parts,
                    unit=unit,
                    label_style=label,
                    highlight=(
                        _key_points(series[0].values, plan_slide, pack)
                        if single and not parts
                        else []
                    ),
                    muted_color=(
                        _muted(series[0].color, background, is_dark)
                        if single and not parts
                        else None
                    ),
                    show_values=single,
                    title=title,
                    point_colors=brand if parts else [],
                    language=pack.brief.language,
                ),
            )

    if role is SlotRole.TABLE:
        body = role_typical(spec, SlotRole.BODY) or style.size_pt
        cell = style.model_copy(update={"size_pt": max(CHART_MIN_PT, min(style.size_pt, body))})
        brand = _brand_colors(spec, background, is_dark)
        table = _table_content(block, pack, cell)
        if table is not None:
            # Кегль — наибольший из шкалы, при котором таблица помещается в
            # отданное ей место: не мелкий текст в четверти слайда.
            size = _table_size(table, box, spec, body)
            cell = cell.model_copy(
                update={
                    "size_pt": size,
                    "color": _table_text(spec, background, is_dark, size) or cell.color,
                }
            )
            table = table.model_copy(update={"cell_style": cell})
            fill = brand[0]
            header = cell.model_copy(
                update={"color": _header_text(fill, spec, cell.size_pt), "bold": True}
            )
            values = [_number(row[-1]) for row in table.rows]
            keys = (
                _key_points(values, plan_slide, pack)
                if block.series_ids and all(v is not None for v in values)
                else []
            )
            table = table.model_copy(
                update={
                    "header_style": header,
                    "header_fill": fill,
                    "accent": fill,
                    "highlight_rows": keys,
                }
            )
            return Element(
                id=element_id,
                kind=ElementKind.TABLE,
                role=SlotRole.TABLE,
                box=_table_box(table, cell, box, roomy),
                provenance=Provenance(kind=SourceKind.DERIVED, ref=block.id),
                table=table,
            )
    return None


# До скольких текстовых кеглей таблица может вырасти, если место есть, и до
# какой высоты строки в кеглях — растянуть строки по месту.
TABLE_MAX_SCALE = 2.0
TABLE_ROW_MAX = 2.6


def _table_size(table: TableContent, box: Box, spec: TemplateSpec, body: float) -> float:
    """Наибольший кегль шкалы, при котором таблица помещается в рамку.

    По высоте — строки по кеглю; по ширине — самая длинная ячейка колонки.
    Выше двух текстовых кеглей не растёт: таблица — не заголовок.
    """
    rows = len(table.rows) + 1
    # Ширина — по самой длинной ячейке каждой колонки, а не самой длинной
    # ячейке таблицы на все колонки: «Электроэнергия, МВт·ч» в первой колонке
    # сводной таблицы `zelenie_investicii` мерило и три колонки чисел, и
    # таблица выходила кеглем 12 в верхней трети слайда.
    widths = [
        max((len(row[column]) for row in [table.header, *table.rows] if column < len(row)),
            default=1)
        for column in range(max(1, len(table.header)))
    ]
    ceiling = max(CHART_MIN_PT, body * TABLE_MAX_SCALE)
    steps = sorted(
        {step for step in scale_ladder(spec) if CHART_MIN_PT <= step <= ceiling}
        | {CHART_MIN_PT},
        reverse=True,
    )
    for step in steps:
        height = rows * step * TABLE_ROW_HEIGHT * EMU_PER_POINT
        width = sum(length * CHAR_WIDTH + 2 for length in widths) * step * EMU_PER_POINT
        if height <= box.h * 0.9 and width <= box.w:
            return step
    return steps[-1]


def _header_text(fill: Color, spec: TemplateSpec | None = None, size_pt: float = 0.0) -> Color:
    """Текст шапки на заливке бренда — цветом, которым шаблон пишет на ней.

    Шаблон уже писал на подложках этого цвета: карточки, панели, плашки
    его слайдов-примеров. Самый частый их цвет текста, если читается при
    полужирной шапке, — и берётся. Только если шаблон на такой заливке не
    писал ничего, — белый на насыщенной (так пишут синие карточки всех трёх
    шаблонов VK), иначе самый контрастный.
    """
    needed = required_contrast(size_pt, True, MIN_CONTRAST)
    if spec is not None:
        for color in _written_on(fill, spec):
            if color.contrast_ratio(fill) >= needed:
                return color
    white = Color(rgb="FFFFFF")
    if _saturation(fill) >= 0.5 and white.contrast_ratio(fill) >= GRAPHIC_CONTRAST:
        return white
    return readable_text_color_on(fill)


def _table_text(
    spec: TemplateSpec, background: Color | None, is_dark: bool, size_pt: float
) -> Color | None:
    """Цвет текста ячеек — тот, которым пишет таблицы сам шаблон.

    Ячейки брали цвет места, куда села таблица: на `vk_workspace` — голубой
    подписи (70C3FF), хотя таблица шаблона пишет ячейки светло-серым
    (E4E7EA). Берётся цвет таблиц шаблона, если он читается на этом фоне;
    таблиц в шаблоне нет или цвет не читается — None, и остаётся цвет места.
    """
    judged = background or (Color(rgb="000000") if is_dark else Color(rgb="FFFFFF"))
    needed = required_contrast(size_pt, False, MIN_CONTRAST)
    for pattern in spec.patterns:
        for slot in pattern.slots:
            color = slot.style.color if slot.role is SlotRole.TABLE and slot.style else None
            if color is not None and color.contrast_ratio(judged) >= needed:
                return color
    return None


def _written_on(fill: Color, spec: TemplateSpec) -> list[Color]:
    """Цвета текста шаблона на подложке цвета `fill`, от частого к редкому."""
    return spec.colors_written_on(fill)


def _table_box(table: TableContent, style: TextStyle, slot: Box, roomy: Box) -> Box:
    """Рамка, в которую таблица действительно помещается.

    Слот композиции бывает подписью в одну строку, а таблице нужно столько
    строк, сколько в ней данных. Втиснутая в подпись таблица не ужимается —
    она растёт вниз и уезжает за слайд, а колонки сжимаются до одной буквы в
    строке. Поэтому высота считается заранее, и если слот её не держит,
    таблица переезжает в свободную область.

    Ширина смотрится так же: колонке нужно место хотя бы под пару символов
    кегля, которым её набирают.
    """
    rows = len(table.rows) + 1
    needed_h = round(rows * style.size_pt * TABLE_ROW_HEIGHT * EMU_PER_POINT)
    needed_w = round(len(table.header) * style.size_pt * TABLE_MIN_CHARS * EMU_PER_POINT)
    if slot.h >= needed_h and slot.w >= needed_w:
        # Высота — по строкам, а не во всю отданную рамку: растянутые на
        # полслайда строки с одной цифрой читаются как пустая сетка. Место
        # есть — строки просторнее, но не больше чем в 2.6 кегля.
        roomy_h = round(rows * style.size_pt * TABLE_ROW_MAX * EMU_PER_POINT)
        return slot.model_copy(update={"h": min(slot.h, max(needed_h, roomy_h))})
    return Box(
        x=roomy.x,
        y=roomy.y,
        w=max(roomy.w, needed_w) if roomy.w >= needed_w else roomy.w,
        h=min(roomy.h, needed_h),
    )


# Отступ данных от заголовка — доля высоты слайда.
DATA_GAP_SHARE = 30

# Высота строки заголовка в кеглях — та же, которой мерит фиттер.
TITLE_LINE = 1.2


# Доля ширины категории, которую занимает столбик, и средняя ширина знака
# в кеглях: оценка для подписи над столбиком, не точный замер.
BAR_SHARE = 0.55
CHAR_WIDTH = 0.6


def _value_label(
    box: Box, categories: list[str], values: list[float], unit: str, size: float,
    spec: TemplateSpec,
) -> tuple[float, str]:
    """Кегль и единица подписи значения, при которых она не переносится.

    «42 мин» над узким столбиком переносилась в «42 / мин». Кегль опускается
    по шкале шаблона до предела подписи; не хватило — подпись без единицы.
    """
    bar = box.w * 0.9 / max(1, len(categories)) * BAR_SHARE / EMU_PER_POINT
    longest = max((len(f"{value:g}") for value in values), default=1)
    ladder = sorted(
        {step for step in scale_ladder(spec) if CHART_MIN_PT <= step <= size} | {size},
        reverse=True,
    )
    for label_unit in ((unit, "") if unit else ("",)):
        chars = longest + (len(label_unit) + 1 if label_unit else 0)
        for step in ladder:
            if chars * step * CHAR_WIDTH <= bar:
                return step, label_unit
    return ladder[-1], ""


def _above_service(area: Box, pattern: Pattern | None, spec: TemplateSpec) -> Box:
    """Область, кончающаяся над номером страницы и колонтитулом композиции.

    График, выросший на свободное место, ложился на номер страницы бланка, и
    уборка под графиком номер снимала.
    """
    if pattern is None:
        return area
    tops = [
        slot.box.y
        for slot in pattern.slots
        if slot.role in (SlotRole.SLIDE_NUMBER, SlotRole.FOOTER) and slot.box.y > area.y
    ]
    if not tops:
        return area
    bottom = min(area.bottom, min(tops) - spec.slide_height_emu // DATA_GAP_SHARE)
    return area.model_copy(update={"h": max(1, bottom - area.y)})


def _free_region(
    slot: Box, area: Box, top: int, others: list[Box], spec: TemplateSpec
) -> Box:
    """Наибольшая свободная область вокруг места под данные.

    Область содержания ниже заголовка, от которой отрезано то, что занимают
    соседние блоки: слева от места — по их правый край, справа — по левый,
    сверху — по низ. Не вышло места больше исходного — остаётся исходное.
    """
    gap = spec.slide_height_emu // DATA_GAP_SHARE
    left, right = area.x, area.right
    upper, lower = max(area.y, top), area.bottom
    for other in others:
        if other.right <= slot.x:
            left = max(left, other.right + gap)
        elif other.x >= slot.right:
            right = min(right, other.x - gap)
        elif other.bottom <= slot.y:
            upper = max(upper, other.bottom + gap)
        elif other.y >= slot.bottom:
            lower = min(lower, other.y - gap)
    region = Box(x=left, y=upper, w=max(1, right - left), h=max(1, lower - upper))
    if region.area <= slot.area or not _roomy_for_data(region, spec):
        clipped_top = max(slot.y, top)
        clipped_bottom = min(slot.bottom, area.bottom)
        return slot.model_copy(
            update={"y": clipped_top, "h": max(1, clipped_bottom - clipped_top)}
        )
    return region


# Насыщенность второстепенных точек: цвет бренда угадывается, но не спорит
# с главными.
MUTED_SATURATION = 0.18

# Мельче этого подписи графика не читаются с проектора.
CHART_MIN_PT = 10.0

# Порог контраста для заливок графика: нетекстовая графика по WCAG — 3:1.
# Порог текста 4.5 отсекал фирменный синий на белом (4.2), и столбики
# `vk_education` красились чёрным — первым цветом палитры.
GRAPHIC_CONTRAST = 3.0


def _brand_colors(
    spec: TemplateSpec, background: Color | None, is_dark: bool
) -> list[Color]:
    """Цвета шаблона для заливок графика: сначала акцентные, видимые на фоне.

    Акцент — самый насыщенный из употребляемых цветов: серый и чёрный — цвета
    текста и фона, а не бренда.
    """
    judged = background or (Color(rgb="000000") if is_dark else Color(rgb="FFFFFF"))
    visible = [
        token.color
        for token in spec.palette
        if token.color.contrast_ratio(judged) >= GRAPHIC_CONTRAST
    ]
    if not visible:
        return _visible_palette(spec, background, is_dark)
    return sorted(visible, key=lambda color: -_saturation(color))


def _saturation(color: Color) -> float:
    red, green, blue = (int(color.rgb[i : i + 2], 16) / 255 for i in (0, 2, 4))
    high, low = max(red, green, blue), min(red, green, blue)
    return 0.0 if high == 0 else (high - low) / high


def _muted(color: Color, background: Color | None, is_dark: bool) -> Color:
    """Второстепенные точки: тот же цвет без насыщенности, видимый на фоне.

    Не растворение в фоне: на тёмном фоне оно темнит, и «21 мин»
    тёмно-синим на чёрном `vk_workspace` почти пропадала. Цвет теряет
    насыщенность и сдвигается по светлоте от фона, пока не наберёт 3:1 —
    как любая графика.
    """
    base = background or (Color(rgb="000000") if is_dark else Color(rgb="FFFFFF"))
    red, green, blue = (int(color.rgb[i : i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, _ = colorsys.rgb_to_hls(red, green, blue)

    def at(lightness: float) -> Color:
        channels = colorsys.hls_to_rgb(hue, min(1.0, max(0.0, lightness)), MUTED_SATURATION)
        return Color(rgb="".join(f"{round(c * 255):02X}" for c in channels))

    steps = [light + step * 0.02 for step in range(0, 50)]
    if base.luminance < 0.5:
        # Тёмный фон: светлеть, пока не станет видно.
        return next(
            (c for c in map(at, steps) if c.contrast_ratio(base) >= GRAPHIC_CONTRAST),
            at(steps[-1]),
        )
    # Светлый фон: светлеть, пока держится 3:1, — второстепенное отступает,
    # но не пропадает.
    chosen = at(light)
    for candidate in map(at, steps):
        if candidate.contrast_ratio(base) < GRAPHIC_CONTRAST + 0.1:
            break
        chosen = candidate
    return chosen


def _key_points(values: list[float], plan_slide: SlidePlan | None, pack) -> list[int]:
    """Точки ряда, которые несут вывод слайда.

    Концы ряда — «было» и «стало» — выделены всегда: один и тот же вывод
    («от 42 до 9») должен выглядеть одинаково в любой колоде, а объявленные
    числа слайда у разных планов разные. К ним добавляются точки, чьё
    значение слайд объявил в своих числах (`figures`) ссылкой на факт пакета.
    Текст заголовка не разбирается — числа слайда уже структурированы.
    """
    facts = {fact.id: fact.value for fact in getattr(pack, "facts", []) if fact.value is not None}
    declared = {
        facts[fact_id]
        for figure in (plan_slide.figures if plan_slide is not None else [])
        for fact_id in figure.fact_ids
        if fact_id in facts
    }
    ends = {0, len(values) - 1} if len(values) > 1 else {0}
    return sorted(ends | {index for index, value in enumerate(values) if value in declared})


def _within_margins(box: Box, area: Box) -> Box | None:
    """Пересечение рамки с областью содержания; None — если его нет."""
    x, y = max(box.x, area.x), max(box.y, area.y)
    right, bottom = min(box.right, area.right), min(box.bottom, area.bottom)
    if right <= x or bottom <= y:
        return None
    return Box(x=x, y=y, w=right - x, h=bottom - y)


def readable_text_color_on(fill: Color) -> Color:
    """Белый или почти чёрный — что контрастнее на заливке."""
    return max(
        (Color(rgb="FFFFFF"), Color(rgb="111111")),
        key=lambda color: color.contrast_ratio(fill),
    )


def _number(text: str) -> float | None:
    """Число в начале ячейки, которую мы сами составили из ряда: «42 мин»."""
    head = text.split(" ")[0]
    try:
        return float(head)
    except ValueError:
        return None


def _visible_palette(
    spec: TemplateSpec, background: Color | None, is_dark: bool
) -> list[Color]:
    """Цвета шаблона, которые видно на его же фоне.

    Первый цвет палитры — самый употребительный по площади, а на тёмном
    шаблоне это фон. Покрасить им столбики значит нарисовать чёрное по
    чёрному: график есть, данные верные, видно ничего. Та же ошибка, что и с
    текстом, и тот же порог из Приложения 1.
    """
    colors = [token.color for token in spec.palette]
    if background is not None:
        colors = [
            color
            for color in colors
            if color.contrast_ratio(background) >= MIN_CONTRAST
        ]
    if colors:
        return colors
    return [Color(rgb="FFFFFF") if is_dark else Color(rgb="111111")]


def _table_content(block, pack, style: TextStyle) -> TableContent | None:
    """Таблица блока: своя, если планировщик её составил, иначе из рядов."""
    language = pack.brief.language if pack is not None else ""
    if block.table is not None and block.table.columns:
        return TableContent(
            header=list(block.table.columns),
            rows=_plain_numbers([list(row) for row in block.table.rows], language),
            header_style=style,
            cell_style=style,
        )

    known = {item.id: item for item in getattr(pack, "series", [])} if pack else {}
    chosen = [known[sid] for sid in block.series_ids if sid in known]
    if not chosen:
        return None
    categories = chosen[0].categories
    chosen = [item for item in chosen if item.categories == categories]
    if not chosen:
        return None
    return TableContent(
        header=["", *[item.name for item in chosen]],
        rows=[
            [
                label,
                *[
                    f"{format_value(item.values[row], item.values, language)} {item.unit}".strip()
                    for item in chosen
                ],
            ]
            for row, label in enumerate(categories)
        ],
        header_style=style,
        cell_style=style,
    )


def _plain_numbers(rows: list[list[str]], language: str) -> list[list[str]]:
    """Ячейки-числа таблицы плана — записью колоды, как числа графиков.

    Модель пишет числа ячеек по-программистски: «412.0», «-2.0» в сводной
    таблице `zelenie_investicii`. Ячейка, целиком состоящая из числа,
    переписывается `format_value` по своей колонке: «412», «14,2»,
    «41 380». Ячейки с единицей или знаком процента не трогаются.
    """
    def plain(cell: str) -> float | None:
        try:
            return float(cell.strip())
        except ValueError:
            return None

    columns = max((len(row) for row in rows), default=0)
    for column in range(columns):
        values = [plain(row[column]) for row in rows if column < len(row)]
        numbers = [value for value in values if value is not None]
        for row in rows:
            if column < len(row) and (value := plain(row[column])) is not None:
                row[column] = format_value(value, numbers, language)
    return rows


def _block_lines(block) -> list[str]:
    """Блок плана, приведённый к строкам для текстовой рамки."""
    lines: list[str] = []
    if block.heading:
        lines.append(block.heading)
    lines.extend(block.items)
    if block.table is not None and not lines:
        lines.append(" | ".join(block.table.columns))
        lines.extend(" | ".join(row) for row in block.table.rows)
    if block.series_ids and not lines:
        lines.append(", ".join(block.series_ids))
    return lines


# ── Сборка слайда ────────────────────────────────────────────────────────────


def _overflow_issue(
    slide_index: int, element_id: str, box: Box, result: FitResult, what: str
) -> Issue:
    """Находка о тексте, не влезшем даже на минимальной ступени шкалы.

    Заводится честно и сразу: молча обрезанный текст хуже помеченного, а
    дальше уменьшать кегль нельзя — промежуточное значение поймает проверка
    «кегль не из шкалы шаблона».
    """
    return Issue(
        check_id="layout.text_overflow",
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=Severity.WARNING,
        slide_index=slide_index,
        element_ids=[element_id],
        bbox=box,
        message=(
            f"{what} не помещается в рамку на минимальном кегле шкалы "
            f"({result.size_pt:g} pt): помещается {result.capacity_lines} "
            f"строк, занято {result.lines}"
        ),
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="сократить текст или разнести блоки на два слайда",
            action="shorten_or_split",
            # Ёмкость рамки едет с находкой: тот, кто будет сокращать, должен
            # знать не «покороче», а «в две строки».
            params={
                "slide_index": slide_index,
                "element_id": element_id,
                "capacity_lines": result.capacity_lines,
                "used_lines": result.lines,
            },
        ),
    )


def _no_place_issue(slide_index: int, block_id: str, box: Box) -> Issue:
    return Issue(
        check_id="layout.text_without_place",
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=Severity.WARNING,
        slide_index=slide_index,
        bbox=box,
        message=(
            f"блок {block_id!r}: у служебного слайда шаблона нет места под этот текст, "
            "он не вёрстан"
        ),
        fix=ProposedFix(
            kind=FixKind.ASSISTED,
            description="убрать текст с титула или финала либо перенести его на соседний слайд",
            action="shorten_or_split",
            params={"slide_index": slide_index},
        ),
    )


def _speaker_elements(
    pattern: Pattern, pack, slide_index: int, font_family: str, spec: TemplateSpec
) -> list[Element]:
    """Подпись спикера — из данных о выступающем, а не из текста плана.

    Данных нет — подписи нет, и рендер уберёт спикера целиком, с кружком
    под фото. Одна подпись на слайд: второго выступающего пакет не знает.
    """
    brief = getattr(pack, "brief", None)
    names = (getattr(brief, "author", ""), getattr(brief, "author_role", ""))
    lines = [line for line in names if line]
    speaker = next((slot for slot in pattern.slots if slot.role is SlotRole.SPEAKER), None)
    if not lines or speaker is None:
        return []
    size = (
        speaker.style.size_pt
        if speaker.style is not None
        else role_typical(spec, SlotRole.BODY)
    )
    color = _text_color(
        pattern, SlotRole.SPEAKER, pattern.is_dark, _under(speaker, None), spec, slot=speaker
    )
    style = TextStyle(
        font_family=font_family, size_pt=size or 12.0, color=color, align=_align_of(speaker)
    )
    return [
        Element(
            id=f"s{slide_index}_speaker",
            kind=ElementKind.TEXT,
            role=SlotRole.SPEAKER,
            box=speaker.box,
            provenance=Provenance(kind=SourceKind.SLIDE, ref=pattern.id),
            text=TextContent(paragraphs=[Paragraph(text=line, style=style) for line in lines]),
        )
    ]


def build_slide_ir(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    font_family: str,
    used: set[str] | None = None,
    pack=None,
    avoid: set[str] | None = None,
    sections: list[str] | None = None,
) -> tuple[SlideIR, list[Issue]]:
    """Один слайд: композиция шаблона, заполненная содержанием плана.

    `sections` — разделы колоды (пункты повестки): ими подписывается маршрут
    или ряд элементов обложки, если он у неё есть.
    """
    pattern = _bookend(spec, plan_slide) or pick_pattern(
        spec, plan_slide, strategy, used, metrics, ladders[SlotRole.TITLE], ladders, avoid
    )
    layout = pick_layout(spec, plan_slide)
    container = pattern if pattern is not None else layout

    if pattern is not None:
        is_dark = pattern.is_dark
        # Собственный фон донора перекрывает фон layout'а: он приедет на
        # слайд вместе с композицией, и цвет текста выбирается под него.
        background = pattern.background or next(
            (lay.background for lay in spec.layouts if lay.id == pattern.layout_id),
            layout.background,
        )
        provenance = Provenance(kind=SourceKind.SLIDE, ref=pattern.id)
    else:
        is_dark = layout.is_dark
        background = layout.background
        provenance = Provenance(kind=SourceKind.LAYOUT, ref=layout.id)

    issues: list[Issue] = []
    elements: list[Element] = []

    # ── Заголовок ────────────────────────────────────────────────────────
    title_slot = _title_slot(container)
    title_box = title_slot.box if title_slot else _content_area(spec, container)
    title_ladder = ladders[SlotRole.TITLE]
    title_declared = (
        title_slot.style.size_pt
        if title_slot is not None and title_slot.style is not None
        else (title_ladder[-1] if title_ladder else 18.0)
    )
    title_start = strategy.start_size(title_ladder, title_declared)
    title_id = f"s{plan_slide.index}_title"

    title_overflowed = False
    title_steps = 0
    title_capacity = 0
    title_used = 0
    if metrics is not None:
        fit = fit_size(
            plan_slide.takeaway_title, metrics, title_box, title_ladder, title_start
        )
        title_size = fit.size_pt
        title_steps = fit.steps_down
        title_overflowed = not fit.fits
        title_capacity, title_used = fit.capacity_lines, fit.lines
        if title_overflowed:
            issues.append(
                _overflow_issue(plan_slide.index, title_id, title_box, fit, "заголовок")
            )
    else:
        title_size = title_start
    # Низ заголовка — по его строкам, а не по рамке: переполненный заголовок
    # выходит из рамки вниз, и график под ним касался подписи «42 мин».
    title_bottom = title_box.y + max(
        title_box.h, round(title_used * title_size * TITLE_LINE * EMU_PER_POINT)
    )

    elements.append(
        Element(
            id=title_id,
            kind=ElementKind.TEXT,
            role=SlotRole.TITLE,
            box=title_box,
            provenance=provenance,
            backdrop=_under(title_slot, None),
            text=TextContent(
                scale_steps_down=title_steps,
                truncated=title_overflowed,
                capacity_lines=title_capacity,
                used_lines=title_used,
                paragraphs=[
                    Paragraph(
                        text=plan_slide.takeaway_title,
                        style=TextStyle(
                            font_family=font_family,
                            size_pt=title_size,
                            bold=True,
                            align=_align_of(title_slot),
                            valign=_valign_of(title_slot),
                            color=_text_color(
                                container,
                                SlotRole.TITLE,
                                is_dark,
                                _under(title_slot, background),
                                spec,
                                slot=title_slot,
                                size_pt=title_size,
                                bold=True,
                            ),
                        ),
                    )
                ]
            ),
        )
    )

    # ── Подзаголовок ─────────────────────────────────────────────────────
    # Место подзаголовка композиции получает подзаголовок плана. Раньше его
    # не получал никто: рамка донора оставалась пустой на каждом слайде.
    # Нечего положить — рендер уберёт пустую фигуру.
    subtitle_slot = _subtitle_slot(container)
    # Заголовок в две строки при рамке донора на одну занимает место
    # подзаголовка: тогда подзаголовка нет, а не надпись поверх заголовка.
    if (
        subtitle_slot is not None
        and plan_slide.subtitle.strip()
        and title_bottom <= subtitle_slot.box.y + subtitle_slot.box.h // 4
    ):
        subtitle = _subtitle_element(
            plan_slide, subtitle_slot, container, spec, metrics, ladders, strategy,
            font_family, is_dark, background, provenance, issues,
        )
        if subtitle is not None:
            elements.append(subtitle)
            title_bottom = max(title_bottom, subtitle.box.bottom)

    # ── Содержание ───────────────────────────────────────────────────────
    bookend = pattern is not None and pattern.id in spec.bookend_ids
    free_slots = (
        # У обложки и финала порядок мест задал разбор: заголовок, под ним
        # подзаголовок и контакты. Порядок чтения отдал бы текст квадрату
        # под QR-код над заголовком.
        [slot for slot in pattern.slots if slot.role in _CONTENT_ROLES]
        if bookend
        else _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
        if pattern is not None
        else [
            slot
            for slot in layout.slots
            if slot.role in _CONTENT_ROLES
            and _on_slide(slot.box, spec.slide_width_emu, spec.slide_height_emu)
        ]
    )
    # На обложке и финале доля мест не режется: мест там два-три, и все они
    # — места шаблона под название, подзаголовок и контакты.
    if not bookend:
        free_slots = _fill_share(pattern, free_slots, strategy)

    # Сколько блоков уже не нашли себе слота: каждому следующему достаётся
    # своя полоса свободной области, иначе они лягут друг на друга.
    homeless = 0
    bands = max(1, len(plan_slide.blocks))
    # Рамки, которые слайд уже занял. Заголовок занимает свою первым.
    # Заголовок занимает рамку, а переполненный — и строки ниже неё: на
    # `vk_workspace` заголовок в четыре строки при рамке на две, а место
    # показателя донора заходит в низ рамки на 7 % своей площади — в
    # пределах допуска, — и «Итоговый результат пилота» ложился на заголовок.
    title_taken = title_box.model_copy(update={"h": title_bottom - title_box.y})
    taken = [title_taken] + [
        element.box for element in elements if element.role is SlotRole.SUBTITLE
    ]

    blocks = list(plan_slide.blocks)
    multiples = _small_multiples(spec, container, pattern, blocks, strategy, title_bottom, pack)
    if bookend and pattern is not None:
        members = [
            slot
            for slot in _usable_slots(pattern, spec.slide_width_emu, spec.slide_height_emu)
            if slot.id not in {fixed.id for fixed in pattern.slots}
        ]
        cover = _cover_sections(
            pattern, sections, free_slots + members, taken, metrics, ladders
        )
        if cover is not None:
            free_slots = free_slots + members
            blocks.append(cover)
    # Текст садится первым, график и таблица — последними: им нужно место
    # покрупнее, и они растут в то, что осталось. Первым график садился в
    # строку «Бюджет» донора под карточками — графиком в полдюйма высотой,
    # — а карточки занимали место, где он поместился бы.
    order = sorted(
        enumerate(blocks), key=lambda item: strategy.role_for(item[1]) in _DATA_ROLES
    )
    last = order[-1][0] if order else -1
    for position, block in order:
        lines = _block_lines(block)
        if not lines:
            continue
        role = strategy.role_for(block)
        seats = _seat(
            pattern, block, role, free_slots, taken,
            reserve=_text_after([b for _, b in order], block, strategy),
        )
        if _grid_seated(pattern, seats):
            # Таблица из прямоугольников — надписи по ячейкам, а не `a:tbl`.
            role = SlotRole.BODY
        slot = seats[0][0] if seats else None
        if slot is None and bookend:
            # У обложки или финала шаблона нет места под этот текст. Класть
            # его в свободную полосу поверх оформления нельзя, в подпись
            # спикера — тоже: это место имени, а не подзаголовка.
            issues.append(
                _no_place_issue(plan_slide.index, block.id, _content_area(spec, container))
            )
            continue
        if block.id in multiples:
            # Несколько рядов на слайде — графики в ряд, каждому своя колонка
            # под шапкой, а не место донора: в кружке схемы график в дюйм
            # шириной не читается.
            seats, slot, box = [(None, lines)], None, multiples[block.id]
        elif slot is None:
            seats = [(None, lines)]
            box = _free_band(
                spec, container, homeless, bands, taken, data=role in _DATA_ROLES
            )
            homeless += 1
        else:
            box = slot.box
        element_id = f"s{plan_slide.index}_b{position}"
        placed = [
            (seat.box if seat is not None else box, text) for seat, text in seats
        ]
        if metrics is not None:
            fits, starts = _fit_block(
                [(seat, seat_box, text) for (seat, _), (seat_box, text) in zip(
                    seats, placed, strict=True
                )],
                spec,
                strategy,
                metrics,
                ladders,
                role,
            )
        else:
            block_ladder = ladders[slot.role if slot is not None else role]
            declared = (
                slot.style.size_pt
                if slot is not None and slot.style is not None
                else _undeclared(spec, slot.role if slot is not None else role, block_ladder)
            )
            fits, starts = [], [strategy.start_size(block_ladder, declared)] * len(seats)
        start = starts[0]
        # Цвет — каждого места и по его подложке: карточки одного ряда бывают
        # разного цвета. Порог контраста — по кеглю, которым место набрано.
        colors = [
            _text_color(
                container,
                # Блок, разделённый по местам (число и подпись), пишется
                # цветом роли каждого места.
                seat.role if seat is not None and len(seats) > 1 else role,
                is_dark,
                _under(seat, background),
                spec,
                slot=seat,
                size_pt=fits[number].size_pt if number < len(fits) else 0.0,
            )
            for number, (seat, _) in enumerate(seats)
        ]
        color = colors[0]
        others = [taken_box for taken_box in taken if taken_box is not title_taken]
        taken.extend(seat_box for seat_box, _ in placed)

        # График и таблица занимают освободившееся место: место донора под
        # них часто — карточка в углу, а картинки рядом уходят в рендере. Растут
        # в свободную область ниже заголовка, в полях шаблона, не заходя на
        # соседние блоки. Последний блок — чтобы не занять место следующих.
        if role in _DATA_ROLES and slot is not None:
            top = _header_bottom(pattern, title_bottom) + spec.slide_height_emu // DATA_GAP_SHARE
            if position == last:
                box = _free_region(
                    box, _above_service(_content_area(spec, container), pattern, spec), top,
                    others, spec,
                )
            elif box.y < top:
                box = box.model_copy(update={"y": top, "h": max(1, box.bottom - top)})
            if not _roomy_for_data(box, spec):
                # Место донора под данными выше шапки (фото-баннер над
                # заголовком): обрезанное по шапке, оно схлопывалось в
                # график высотой в 1 EMU. Тогда — свободная полоса.
                band = _free_band(spec, container, 0, 1, [title_taken, *others], data=True)
                if _roomy_for_data(band, spec):
                    box = band

        # Числовой ряд и таблица становятся нативными объектами, а не
        # пересказом строками: ТЗ засчитывает только `c:chart` и `a:tbl`, и
        # человек должен мочь открыть данные и поправить их.
        native = _data_element(
            element_id,
            block,
            role,
            box,
            TextStyle(
                font_family=font_family,
                size_pt=fits[0].size_pt if fits else start,
                color=color,
            ),
            spec,
            pack,
            background,
            is_dark,
            _above_service(
                _below(
                    _content_area(spec, container),
                    _header_bottom(pattern, title_bottom)
                    + spec.slide_height_emu // DATA_GAP_SHARE,
                ),
                pattern,
                spec,
            ),
            plan_slide,
            title=_series_title(block, pack) if block.id in multiples else "",
        )
        if native is not None:
            elements.append(native)
            continue

        for number, (seat_box, text) in enumerate(placed):
            # Блок, разложенный по карточкам, даёт элемент на карточку.
            seat_id = element_id if len(placed) == 1 else f"{element_id}_{number}"
            fit = fits[number] if fits else None
            if fit is not None and not fit.fits:
                issues.append(
                    _overflow_issue(
                        plan_slide.index, seat_id, seat_box, fit, f"блок {block.id!r}"
                    )
                )
            style = TextStyle(
                font_family=font_family,
                size_pt=fit.size_pt if fit is not None else starts[number],
                color=colors[number],
                align=_align_of(seats[number][0]),
                valign=_valign_of(seats[number][0]),
            )
            # Число и его подпись в одном месте показателя: шаблон пишет
            # цветом показателя только число (синее «91%» `vk_tech`), подпись
            # — цветом подписи или основного текста композиции.
            styles = [style] * len(text)
            seat = seats[number][0]
            seat_role = seat.role if seat is not None else role
            if seat_role is SlotRole.KPI_VALUE and len(text) > 1:
                label_role = (
                    SlotRole.KPI_LABEL
                    if any(s.role is SlotRole.KPI_LABEL for s in container.slots)
                    else SlotRole.BODY
                )
                label = style.model_copy(
                    update={
                        "color": _text_color(
                            container,
                            label_role,
                            is_dark,
                            _under(seats[number][0], background),
                            spec,
                            size_pt=style.size_pt,
                        )
                    }
                )
                styles = [style] + [label] * (len(text) - 1)
            elements.append(
                Element(
                    id=seat_id,
                    kind=ElementKind.TEXT,
                    role=seat.role if seat is not None else role,
                    box=seat_box,
                    provenance=provenance,
                    backdrop=_under(seats[number][0], None),
                    text=TextContent(
                        paragraphs=[
                            Paragraph(text=line, style=line_style, bullet=len(text) > 1)
                            for line, line_style in zip(text, styles, strict=True)
                        ],
                        # Переполнение записывается в само представление, а не
                        # только в находки вёрстки: аудит читает IR и обязан
                        # видеть то же, что видел фиттер.
                        scale_steps_down=fit.steps_down if fit is not None else 0,
                        truncated=fit is not None and not fit.fits,
                        # Ёмкость рамки и занятое ею: «сократите текст» без этих
                        # чисел — совет, который живая модель уже не выполнила.
                        capacity_lines=fit.capacity_lines if fit is not None else 0,
                        used_lines=fit.lines if fit is not None else 0,
                    ),
                )
            )

    if bookend:
        elements.extend(_speaker_elements(pattern, pack, plan_slide.index, font_family, spec))

    slide = SlideIR(
        index=plan_slide.index,
        layout_id=pattern.layout_id if pattern is not None else layout.id,
        pattern_id=pattern.id if pattern is not None else None,
        donor_slide_index=pattern.donor_slide_index if pattern is not None else None,
        elements=elements,
        background=background,
        is_dark=is_dark,
        speaker_notes=plan_slide.speaker_notes,
    )
    return slide, issues


def build_deck_ir(
    spec: TemplateSpec,
    plan,
    variant,
    strategy: Strategy | None = None,
    pack=None,
    siblings: dict[str, dict[int, str]] | None = None,
) -> tuple[DeckIR, list[Issue]]:
    """Колода одного варианта и находки, которые вёрстка завела о себе сама.

    `variant` принимается и строкой, и пресетом из конфига: строка означает
    стратегию по умолчанию, и тогда вёрстка ведёт себя как «сбалансированная».

    `siblings` — реестр выбранных композиций, общий для вариантов одного
    прогона: {вариант: {номер слайда плана: композиция}}. Вариант избегает
    чужих композиций на том же слайде, если есть другая, куда текст влезает,
    и дописывает в реестр свои. Своих не избегает: пересборка в цикле
    исправления обязана выбрать то же, что и в первый раз.
    """
    if strategy is None:
        strategy = (
            Strategy.from_config(variant)
            if hasattr(variant, "strategy")
            else Strategy(
                name=str(variant),
                slot_fill_target=0.7,
                type_scale_bias="mid",
                data_viz_mode="auto",
                blocks_per_slide=2,
                pattern_preference=(),
            )
        )
    variant_name = getattr(variant, "name", str(variant))

    # Композиции с фото донора сбоку — перестроенными: фото уйдёт, текст
    # займёт его место. Так их и меряет подбор, и верстает сборка.
    if any(pattern.photo_slots for pattern in spec.patterns):
        spec = spec.model_copy(
            update={"patterns": [_without_photos(pattern, spec) for pattern in spec.patterns]}
        )
    font_family = spec.fonts[0].family if spec.fonts else "Arial"
    metrics = metrics_for_spec(spec).metrics
    # Лестница у каждой роли своя: предел «мельче нельзя» шаблон задаёт для
    # заголовка и для тела текста по-разному.
    ladders = {role: ladder_for_role(spec, role) for role in SlotRole}

    slides: list[SlideIR] = []
    issues: list[Issue] = []
    # Сколько раз колода уже взяла каждую композицию: разнообразие меряется
    # числом повторов, а не только «было или нет».
    used: Counter[str] = Counter()
    others = [
        chosen for name, chosen in (siblings or {}).items() if name != variant_name
    ]
    mine: dict[int, str] = {}
    sections = _sections(plan)
    for plan_slide in plan.slides:
        plan_slide = _mixed_units_as_table(
            _tables_as_series(_heading_as_subtitle(spec, _figure_first(plan_slide)), pack),
            pack,
        )
        avoid = {chosen[plan_slide.index] for chosen in others if plan_slide.index in chosen}
        built, found = _slides_for(
            spec, plan_slide, strategy, metrics, ladders, font_family, used, pack, avoid,
            sections,
        )
        if built and built[0].pattern_id:
            mine[plan_slide.index] = built[0].pattern_id
        slides.extend(built)
        issues.extend(found)
        used.update(s.pattern_id for s in built if s.pattern_id)

    if siblings is not None:
        siblings[variant_name] = mine

    # Индексы обязаны идти подряд: разбиение слайда сдвигает всё, что ниже.
    for position, slide in enumerate(slides, start=1):
        slide.index = position

    return (
        DeckIR(
            variant=variant_name,
            template_sha256=spec.template_sha256,
            slide_width_emu=spec.slide_width_emu,
            slide_height_emu=spec.slide_height_emu,
            slides=slides,
        ),
        issues,
    )


def _series_of_table(block, pack) -> str | None:
    """Ряд пакета, который таблица плана пересказывает, — или None.

    Таблица «Канал / Выручка» с числами ряда «Выручка по каналам» — это ряд,
    а не таблица: две колонки, подписи и числа совпадают с рядом из входа.
    Живой план сделал такими доли каналов и выручку по городам, и в колоде
    о продажах не осталось ни одной диаграммы.
    """
    table = getattr(block, "table", None)
    if block.kind is not BlockKind.TABLE or table is None or pack is None:
        return None
    if len(table.columns) != 2 or len(table.rows) < 2:
        return None
    values = [parse_number(row[1]) if len(row) > 1 else None for row in table.rows]
    if any(value is None for value in values):
        return None
    for series in getattr(pack, "series", []):
        if len(series.values) != len(values):
            continue
        if all(
            abs(ours - theirs) <= 0.005 * max(1.0, abs(theirs))
            for ours, theirs in zip(values, series.values, strict=True)
        ):
            return series.id
    return None


def _tables_as_series(plan_slide: SlidePlan, pack) -> SlidePlan:
    """Таблицы плана, пересказывающие ряд из входа, — блоками ряда."""
    blocks = []
    changed = False
    for block in plan_slide.blocks:
        series_id = _series_of_table(block, pack)
        if series_id is None:
            blocks.append(block)
            continue
        changed = True
        blocks.append(
            block.model_copy(
                update={"kind": BlockKind.SERIES, "series_ids": [series_id], "table": None}
            )
        )
    return plan_slide.model_copy(update={"blocks": blocks}) if changed else plan_slide


def _mixed_units_as_table(plan_slide: SlidePlan, pack) -> SlidePlan:
    """Блок из рядов разных единиц — таблицей «категория × ряд».

    Выручка в млн руб., число чеков и средний чек в руб. на одной оси: ось
    до 60 000, и выручки не видно вовсе (шаблон экзаменов, airy). Числа
    таблицы — из пакета, как у графика; единица — в шапке колонки.
    """
    if pack is None:
        return plan_slide
    known = {series.id: series for series in getattr(pack, "series", [])}
    blocks = []
    changed = False
    for block in plan_slide.blocks:
        chosen = [known[sid] for sid in block.series_ids if sid in known]
        if (
            block.kind is not BlockKind.SERIES
            or len(chosen) < 2
            or len({series.unit for series in chosen}) < 2
            or any(series.categories != chosen[0].categories for series in chosen)
        ):
            blocks.append(block)
            continue
        language = pack.brief.language
        columns = [""] + [
            f"{series.name}, {series.unit}" if series.unit else series.name for series in chosen
        ]
        rows = [
            [label]
            + [format_value(series.values[i], series.values, language) for series in chosen]
            for i, label in enumerate(chosen[0].categories)
        ]
        blocks.append(
            block.model_copy(
                update={
                    "kind": BlockKind.TABLE,
                    "table": TableData(columns=columns, rows=rows),
                }
            )
        )
        changed = True
    return plan_slide.model_copy(update={"blocks": blocks}) if changed else plan_slide


def _chart_kind(series, categories: list[str], unit: str, pack) -> ChartKind:
    """Вид графика по смыслу ряда: доли — кольцом, динамика и сравнение — столбцами.

    Смысл ряда (`Series.shape`) называет модель, читавшая вход: по числам доли
    от динамики не отличить — помесячная выручка в сумме тоже даёт квартальную.
    Неизвестен — доли узнаются только по процентам, дающим в сумме сто.
    """
    known = {item.id: item for item in getattr(pack, "series", [])}
    first = next((known[sid] for sid in series if sid in known), None)
    shape = getattr(first, "shape", None)
    values = first.values if first is not None else []
    parts = shape is SeriesShape.PARTS or (
        shape is None and unit.strip() == "%" and abs(sum(values) - 100) <= 2
    )
    if (
        parts
        and len(series) == 1
        and 2 <= len(values) <= _MAX_PARTS
        and all(value > 0 for value in values)
    ):
        return ChartKind.DOUGHNUT
    return ChartKind.COLUMN if len(categories) > 2 else ChartKind.BAR


# Долей больше — кольцо не читается, и ряд рисуется столбцами.
_MAX_PARTS = 6


# Зазор между графиками в ряд — доля ширины области.
_MULTIPLES_GUTTER_SHARE = 0.04


def _small_multiples(
    spec: TemplateSpec,
    container,
    pattern: Pattern | None,
    blocks: list,
    strategy: Strategy,
    title_bottom: int,
    pack,
) -> dict[str, Box]:
    """Колонки под графики, если рядов-графиков на слайде больше одного.

    Выручка, чеки и средний чек по месяцам — три ряда разных единиц: на одной
    оси их не нарисовать. Живой план положил их тремя блоками на один слайд,
    и они садились в места донора — кружки и подписи схемы, — графиками в
    дюйм. Здесь каждый ряд получает равную колонку во всю высоту под шапкой.
    """
    charts = [
        block
        for block in blocks
        if block.series_ids and strategy.role_for(block) is SlotRole.CHART
    ]
    if len(charts) < 2 or pack is None:
        return {}
    area = _above_service(
        _below(
            _content_area(spec, container),
            _header_bottom(pattern, title_bottom) + spec.slide_height_emu // DATA_GAP_SHARE,
        ),
        pattern,
        spec,
    )
    others = [block for block in blocks if block not in charts and _block_lines(block)]
    if others:
        # Текст рядом с графиками — под ними, в нижней трети.
        area = area.model_copy(update={"h": area.h * 2 // 3})
    gutter = int(area.w * _MULTIPLES_GUTTER_SHARE)
    width = (area.w - gutter * (len(charts) - 1)) // len(charts)
    return {
        block.id: Box(x=area.x + index * (width + gutter), y=area.y, w=width, h=area.h)
        for index, block in enumerate(charts)
    }


def _series_title(block, pack) -> str:
    """Подпись графика в ряду: имя ряда и его единица."""
    known = {item.id: item for item in getattr(pack, "series", [])}
    first = next((known[sid] for sid in block.series_ids if sid in known), None)
    if first is None:
        return block.heading
    return f"{first.name}, {first.unit}" if first.unit else first.name


def _cover_sections(
    pattern: Pattern | None,
    sections,
    free: list,
    taken: list[Box],
    metrics: FontMetrics | None = None,
    ladders: dict[SlotRole, list[float]] | None = None,
):
    """Разделы колоды блоком для ряда элементов обложки — или None.

    Маршрут из шести узлов на обложке «Дорожной карты» — оформление, а
    подписи узлов («Идея», «План») — текст шаблона. Подписать их разделами
    колоды — то же, что делает слайд-повестка. Разделов больше, чем узлов, —
    не подписываем: два раздела в одном узле хуже пустых узлов. Раздел не
    влезает в подпись узла кеглем шаблона — тоже: маршрут без подписей лучше
    подписей мельче шкалы.
    """
    if pattern is None or pattern.pattern_class is not PatternClass.TITLE or not sections:
        return None
    if len(sections) < 2:
        return None
    block = ContentBlock(id="sections", kind=BlockKind.STEPS, items=list(sections))
    for repeater in pattern.repeaters:
        if repeater.observed_count < len(sections):
            continue
        trial = _spread(pattern, block, SlotRole.CAPTION, list(free), taken)
        if trial is None or len(trial) != len(sections):
            continue
        if metrics is not None and ladders:
            for slot, lines in trial:
                ladder = ladders[slot.role]
                declared = (
                    slot.style.size_pt
                    if slot.style is not None
                    else (ladder[-1] if ladder else 12.0)
                )
                if not fit_size(" ".join(lines), metrics, slot.box, [declared], declared).fits:
                    return None
        return block
    return None


def _sections(plan) -> list[str]:
    """Разделы колоды — пункты первого списка на слайде-повестке."""
    for plan_slide in plan.slides:
        if plan_slide.intent is not SlideIntent.AGENDA:
            continue
        for block in plan_slide.blocks:
            if block.kind in _SPREAD_KINDS and len(block.items) > 1:
                return list(block.items)
    return []


def _subtitle_element(
    plan_slide: SlidePlan,
    slot,
    container,
    spec: TemplateSpec,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    strategy: Strategy,
    font_family: str,
    is_dark: bool,
    background: Color | None,
    provenance: Provenance,
    issues: list[Issue],
) -> Element | None:
    """Подзаголовок слайда в месте подзаголовка композиции, кеглем этого места."""
    ladder = ladders[SlotRole.SUBTITLE] or ladders[SlotRole.BODY]
    declared = (
        slot.style.size_pt
        if slot.style is not None
        else _undeclared(spec, SlotRole.SUBTITLE, ladder)
    )
    start = strategy.start_size(ladder, declared)
    element_id = f"s{plan_slide.index}_subtitle"
    size, steps, overflowed, capacity, used = start, 0, False, 0, 0
    if metrics is not None:
        fit = fit_size(plan_slide.subtitle, metrics, slot.box, ladder, start)
        size, steps, overflowed = fit.size_pt, fit.steps_down, not fit.fits
        capacity, used = fit.capacity_lines, fit.lines
        if overflowed:
            issues.append(
                _overflow_issue(plan_slide.index, element_id, slot.box, fit, "подзаголовок")
            )
    return Element(
        id=element_id,
        kind=ElementKind.TEXT,
        role=SlotRole.SUBTITLE,
        box=slot.box,
        provenance=provenance,
        backdrop=_under(slot, None),
        text=TextContent(
            scale_steps_down=steps,
            truncated=overflowed,
            capacity_lines=capacity,
            used_lines=used,
            paragraphs=[
                Paragraph(
                    text=plan_slide.subtitle.strip(),
                    style=TextStyle(
                        font_family=font_family,
                        size_pt=size,
                        align=_align_of(slot),
                        valign=_valign_of(slot),
                        color=_text_color(
                            container,
                            SlotRole.SUBTITLE,
                            is_dark,
                            _under(slot, background),
                            spec,
                            slot=slot,
                            size_pt=size,
                        ),
                    ),
                )
            ],
        ),
    )


def _header_taken(pattern: Pattern, plan_slide: SlidePlan) -> list[Box]:
    """Что шапка слайда займёт до содержания: заголовок и подзаголовок.

    Подбор мерил места содержания только за вычетом заголовка, а сборка
    сначала сажает подзаголовок плана в его место. На `vk_tech` (run 33)
    подзаголовок лежит на большой картинке донора: подбор видел её свободной
    под график, сборка — занятой, и график сел в значок 0.15″ высотой 1 EMU.
    """
    title = _title_slot(pattern)
    taken = [title.box] if title is not None else []
    subtitle = _subtitle_slot(pattern)
    if subtitle is not None and plan_slide.subtitle.strip():
        taken.append(subtitle.box)
    return taken


def _subtitle_slot(container):
    for slot in getattr(container, "slots", []):
        if slot.role is SlotRole.SUBTITLE:
            return slot
    return None


def _figure_first(plan_slide: SlidePlan) -> SlidePlan:
    """Показатель, где подпись стоит на месте числа, — число вперёд.

    Число показателя — `heading`, подпись — `items`. Модель иногда меняет
    их местами: «Итоговый результат пилота» числом и «9 минут» подписью, и
    на `vk_workspace` крупным кеглем выходила подпись. Поле без единой
    цифры числом не бывает: если в подписи цифры есть, а в числе нет, они
    меняются местами.
    """
    def digits(text: str) -> bool:
        return any(char.isdigit() for char in text)

    blocks = [
        block.model_copy(update={"heading": block.items[0], "items": [block.heading]})
        if block.kind is BlockKind.KPI
        and len(block.items) == 1
        and block.heading.strip()
        and not digits(block.heading)
        and digits(block.items[0])
        else block
        for block in plan_slide.blocks
    ]
    return plan_slide.model_copy(update={"blocks": blocks})


def _heading_as_subtitle(spec: TemplateSpec, plan_slide: SlidePlan) -> SlidePlan:
    """Заголовок списка — в подзаголовок слайда, если тот пуст.

    Список с заголовком («Ключевые метрики» и три пункта) по карточкам не
    раскладывался: заголовку не было места, и весь блок садился в одно поле
    первой карточки — «01» с заголовком и всеми пунктами в `check-title-0`
    на пяти слайдах колоды. Шаблон держит под это место подзаголовка; туда
    заголовок и уходит, а пункты — каждый в свою карточку.

    Подзаголовок уже занят — заголовок списка уходит в заметки докладчика:
    заголовок и подзаголовок слайда и так говорят, о чём список, а пункты
    получают по карточке. Потерять его молча нельзя, втиснуть — тоже.
    """
    headed = [
        block
        for block in plan_slide.blocks
        if block.kind in _SPREAD_KINDS and block.heading.strip() and len(block.items) > 1
    ]
    if len(headed) != 1 or not any(
        repeater.observed_count > 1
        for pattern in spec.patterns
        if pattern.id not in spec.bookend_ids
        for repeater in pattern.repeaters
    ):
        return plan_slide
    block = headed[0]
    heading = block.heading.strip()
    update: dict[str, object] = {
        "blocks": [
            other.model_copy(update={"heading": ""}) if other is block else other
            for other in plan_slide.blocks
        ]
    }
    has_subtitle = any(
        _subtitle_slot(pattern) is not None
        for pattern in spec.patterns
        if pattern.id not in spec.bookend_ids
    )
    if has_subtitle and not plan_slide.subtitle.strip():
        update["subtitle"] = heading
    else:
        notes = plan_slide.speaker_notes.strip()
        update["speaker_notes"] = f"{heading}. {notes}".strip() if notes else heading
    return plan_slide.model_copy(update=update)


def _slides_for(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    font_family: str,
    used: set[str],
    pack=None,
    avoid: set[str] | None = None,
    sections: list[str] | None = None,
) -> tuple[list[SlideIR], list[Issue]]:
    """Слайд, а если он переполнен и деление помогает — два.

    Деление — третий шаг закона фиттера, и применяется **после** того, как
    шкала кончилась: пока кегль ещё можно опустить на ступень, слайд делить
    не за что.

    Второе средство того же шага — сокращение текста — требует ещё одного
    обращения к модели, а сокращает текст планировщик, который отработал
    раньше вёрстки. Пока сокращателя нет, `SHORTEN` пользуется тем средством,
    которое есть. Молча не делать ничего нельзя: без деления блоки, которым
    не хватило места, ложатся друг на друга — на `theme_only` так выходило
    шесть наложений на колоду.
    """
    slide, issues = build_slide_ir(
        spec, plan_slide, strategy, metrics, ladders, font_family, used, pack, avoid, sections
    )

    # Переполнение заголовка делением не лечится: у обеих половин заголовок
    # тот же. Делить имеет смысл только из-за содержания.
    title_id = f"s{plan_slide.index}_title"
    content_overflow = [
        issue
        for issue in issues
        if issue.check_id == "layout.text_overflow" and title_id not in issue.element_ids
    ]
    # График или таблица в месте, где их не прочитать (строка «Бюджет» под
    # карточками — полдюйма высоты), — такая же причина делить, как
    # переполнение: у половины с одним графиком место найдётся.
    cramped = _cramped_data(slide, spec)
    # Обложку и финал не делят: вторая обложка — не выход.
    if (not content_overflow and not cramped) or slide.pattern_id in spec.bookend_ids:
        return [slide], issues

    parts = split_blocks(list(plan_slide.blocks))
    if len(parts) < 2:
        # Делить нечего: блок один. Тесный график — в другую композицию: на
        # `vk_education` (run 33) заголовок в четыре строки занял место фото
        # донора, и единственный график слайда вышел высотой в 1 EMU.
        if cramped:
            return _roomier(
                spec, plan_slide, strategy, metrics, ladders, font_family, used, pack,
                avoid, sections, slide, issues,
            )
        # Переполнение остаётся находкой — это четвёртый шаг закона, и он
        # честнее молчаливой обрезки.
        return [slide], issues

    halves: list[SlideIR] = []
    remaining: list[Issue] = []
    for part_no, chunk in enumerate(parts):
        piece = plan_slide.model_copy(
            update={"index": plan_slide.index + part_no, "blocks": chunk}
        )
        built, found = build_slide_ir(
            spec,
            piece,
            strategy,
            metrics,
            ladders,
            font_family,
            used + Counter([slide.pattern_id or ""]),
            pack,
            avoid,
        )
        # Идентификаторы элементов обязаны остаться уникальными в колоде.
        for element in built.all_elements():
            element.id = f"{element.id}_p{part_no}"
        for issue in found:
            issue.element_ids = [f"{i}_p{part_no}" for i in issue.element_ids]
        halves.append(built)
        remaining.extend(found)

    # Деление обязано себя окупить. Лишний слайд, который не убрал ни одного
    # переполнения, — чистый проигрыш: колода длиннее, а текст всё так же не
    # влезает. На синтетическом шаблоне безусловное деление растило колоду с
    # десяти слайдов до пятнадцати и число находок с шести до одиннадцати.
    if cramped:
        if any(_cramped_data(half, spec) for half in halves) or len(remaining) > len(issues):
            return _roomier(
                spec, plan_slide, strategy, metrics, ladders, font_family, used, pack,
                avoid, sections, slide, issues,
            )
        return halves, remaining
    if len(remaining) >= len(issues):
        return [slide], issues
    return halves, remaining


def _roomier(
    spec: TemplateSpec,
    plan_slide: SlidePlan,
    strategy: Strategy,
    metrics: FontMetrics | None,
    ladders: dict[SlotRole, list[float]],
    font_family: str,
    used,
    pack,
    avoid: set[str] | None,
    sections: list[str] | None,
    slide: SlideIR,
    issues: list[Issue],
) -> tuple[list[SlideIR], list[Issue]]:
    """Тот же слайд в другой композиции, где графику хватает места.

    Деление не помогло: у половины с графиком тот же тесный остаток. На
    шаблоне экзаменов график ложился полосой в 0.1–0.55″ под текстом. Здесь
    слайд собирается заново, избегая уже опробованных композиций, пока график
    не станет читаемым или композиции не кончатся; не вышло — остаётся первый.
    """
    tried = {slide.pattern_id or ""}
    for _ in range(len(spec.patterns)):
        other, found = build_slide_ir(
            spec, plan_slide, strategy, metrics, ladders, font_family, used, pack,
            (avoid or set()) | tried, sections,
        )
        if not _cramped_data(other, spec):
            return [other], found
        if (other.pattern_id or "") in tried:
            break
        tried.add(other.pattern_id or "")
    return [slide], issues


def _cramped_data(slide: SlideIR, spec: TemplateSpec) -> bool:
    """Есть ли на слайде график или таблица в месте, где их не прочитать."""
    for element in slide.elements:
        if element.kind not in (ElementKind.CHART, ElementKind.TABLE):
            continue
        # График в ряду («малые графики») уже трети слайда по замыслу: у него
        # меряется только высота.
        in_row = element.chart is not None and bool(element.chart.title)
        if in_row and element.box.h >= spec.slide_height_emu * _DATA_MIN_HEIGHT_SHARE:
            continue
        if not in_row and _roomy_for_data(element.box, spec):
            continue
        return True
    return False
