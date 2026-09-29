"""Геометрические проверки: где элементы стоят и не мешают ли они друг другу.

Всё считается по `SlideIR` — тому же представлению, по которому колоду верстали.
Проверять по собранному `.pptx` было бы вернее к результату, но `SlideIR`
несёт роли и провенанс, без которых находка не объяснима человеку: «элемент
s3_b1 за правым краем» полезнее, чем «фигура 7».
"""

from __future__ import annotations

from deckwright.audit.registry import check
from deckwright.schemas import (
    EMU_PER_POINT,
    Box,
    DeckIR,
    FixKind,
    Issue,
    ProposedFix,
    SlideIR,
    SlotRole,
    TemplateSpec,
    VAlign,
)

# Какую долю меньшего элемента разрешено перекрыть. Рамки шаблона
# соприкасаются краями постоянно, и ноль здесь дал бы находку на каждом слайде.
OVERLAP_TOLERANCE = 0.15

# Допуск выравнивания по направляющей сетки: четверть миллиметра. Глазом такое
# не видно, а находка о нём только засоряет отчёт.
GRID_TOLERANCE_EMU = 9_144

# Насколько пропорции картинки могут разойтись с исходными, прежде чем это
# станет заметным растяжением.
ASPECT_TOLERANCE = 0.05

# Роли, которые живут в полях намеренно: логотип и колонтитул для того там и
# стоят. Требовать от них соблюдения полей значит находить то, что задумано.
_MARGIN_EXEMPT = frozenset({SlotRole.LOGO, SlotRole.FOOTER, SlotRole.SLIDE_NUMBER, SlotRole.DECOR})


def _issue(check_id: str, slide_index: int, message: str, **kwargs) -> Issue:
    """Находка с паспортом из реестра: серьёзность и категория не выдумываются."""
    spec = check(check_id)
    fix = kwargs.pop("fix", None)
    return Issue(
        check_id=check_id,
        kind=spec.kind,
        category=spec.category,
        severity=spec.severity,
        slide_index=slide_index,
        message=message,
        fix=fix or ProposedFix(kind=FixKind.NONE),
        **kwargs,
    )


def _overlap_area(a: Box, b: Box) -> int:
    width = min(a.right, b.right) - max(a.x, b.x)
    height = min(a.bottom, b.bottom) - max(a.y, b.y)
    return max(0, width) * max(0, height)


def out_of_bounds(slide: SlideIR, deck: DeckIR) -> list[Issue]:
    """Элемент, уехавший за край, в PDF просто обрезается — молча."""
    found: list[Issue] = []
    for element in slide.all_elements():
        box = element.box
        over = max(
            -box.x, -box.y, box.right - deck.slide_width_emu, box.bottom - deck.slide_height_emu
        )
        if over <= 0:
            continue
        found.append(
            _issue(
                "layout.out_of_bounds",
                slide.index,
                f"{element.id}: выходит за слайд на {over / 914400:.2f} дюйма",
                element_ids=[element.id],
                bbox=box,
                fix=ProposedFix(
                    kind=FixKind.AUTOMATIC,
                    description="сдвинуть элемент внутрь слайда",
                    action="move_inside_slide",
                    params={"element_id": element.id},
                ),
            )
        )
    return found


def overlaps(slide: SlideIR) -> list[Issue]:
    """Текст поверх текста или поверх данных читается как каша.

    График и таблица раньше в сравнение не входили — проверка смотрела
    только текст на текст. На `vk_tech` подпись слайда лежала целиком
    внутри рамки графика, и аудит сообщал о нуле наложений: читаемость
    держалась на том, что столбик в этом месте оказался невысоким.

    Текст внутри рамки данных — почти всегда авария, но не всегда: подпись
    оси и значение живут внутри графика по замыслу. Различаем по тому, кто
    рисует текст: собственные подписи графика в `SlideIR` отдельными
    элементами не лежат, поэтому любой текстовый элемент поверх данных —
    наш, и он там лишний.
    """
    found: list[Issue] = []
    elements = [
        e
        for e in slide.all_elements()
        if e.text is not None or e.chart is not None or e.table is not None
    ]
    for first in range(len(elements)):
        for second in range(first + 1, len(elements)):
            a, b = elements[first], elements[second]
            if a.text is None and b.text is None:
                # График рядом с таблицей — вопрос вёрстки, а не читаемости
                # текста: их наложение ловит проверка полей и границ.
                continue
            area = _overlap_area(a.box, b.box)
            smaller = min(a.box.w * a.box.h, b.box.w * b.box.h)
            if smaller <= 0 or area / smaller <= OVERLAP_TOLERANCE:
                continue
            found.append(
                _issue(
                    "layout.overlap",
                    slide.index,
                    f"{a.id} и {b.id} перекрываются на {100 * area / smaller:.0f}%",
                    element_ids=[a.id, b.id],
                    bbox=Box(
                        x=max(a.box.x, b.box.x),
                        y=max(a.box.y, b.box.y),
                        w=max(1, min(a.box.right, b.box.right) - max(a.box.x, b.box.x)),
                        h=max(1, min(a.box.bottom, b.box.bottom) - max(a.box.y, b.box.y)),
                    ),
                )
            )
    return found


# Поле текстовой рамки по умолчанию (OOXML): 0.05″ сверху и снизу.
_INSET = 45_720
# Пересечение уже этого — касание краем, а не наложение.
# Какую часть меньшего из двух — текста или графики — заход обязан
# накрыть, чтобы считаться наложением, а не касанием угла.
_COVERED_SHARE = 0.25


def text_over_decor(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Текст, лёгший на графику шаблона: линию, значок.

    `overlaps` сравнивает только наши элементы между собой, а графика донора
    в IR не попадает — она приезжает клонированием, и линия под цифрой на
    `vk_tech` оставалась незамеченной под нашим текстом. Графика берётся из
    разбора шаблона (`Pattern.decor`), текст — полосой, которую он реально
    занимает: строки × кегль с учётом привязки рамки по вертикали.

    Оформление живёт не только на слайде-доноре: фотография, логотип и
    декоративная графика чаще лежат в макете и образце. Они приходят
    отдельным списком (`Pattern.layout_obstacles`) — вёрстка их обходит, и
    аудит обязан о них знать, иначе находка теряется ровно там, где её никто
    не увидит. Подложки макета (`Pattern.layout_backdrops`) — наоборот,
    предусмотренное дизайнером место под текст: тёмная панель поверх фото
    затем и нарисована, чтобы на ней писали. Текст внутри подложки находкой
    не считается.
    """
    pattern = next((p for p in spec.patterns if p.id == slide.pattern_id), None)
    if pattern is None:
        return []
    artwork = list(pattern.decor) + list(pattern.layout_obstacles)
    if not artwork:
        return []
    panels = list(pattern.layout_backdrops)
    found: list[Issue] = []
    for element in slide.all_elements():
        if element.text is None or not element.text.paragraphs:
            continue
        band = text_band(element)
        if any(panel.contains(band) for panel in panels):
            continue
        for item in artwork:
            hit = item.intersection(band)
            if hit is None or item.contains(band):
                continue
            # Край в край — касание. Прежняя мерка прощала узкую по ширине
            # полоску, и та же полоска по высоте считалась наложением: на
            # `vk_workspace` заголовок задевал значок нижней кромкой на
            # 0,06″ и получал находку, хотя на листе стоит выше него.
            # Мерка по доле симметрична и не зависит от того, какой стороной
            # рамки задета графика: прощается заход, не накрывающий заметной
            # части ни текста, ни самой графики. Угол сетки точек, срезанный
            # краем рамки, — касание; линия под строкой лежит в полосе
            # целиком и остаётся наложением, хотя она тоньше допуска.
            smaller = min(band.w * band.h, item.w * item.h)
            if smaller > 0 and (hit.w * hit.h) / smaller < _COVERED_SHARE:
                continue
            found.append(
                _issue(
                    "layout.text_over_decor",
                    slide.index,
                    f"{element.id}: текст ложится на графику шаблона "
                    f"({item.w / 914400:.2f}×{item.h / 914400:.2f}″)",
                    element_ids=[element.id],
                    bbox=hit,
                )
            )
            break
    return found


def text_band(element) -> Box:
    """Полоса рамки, которую текст элемента реально занимает."""
    box = element.box
    text = element.text
    style = text.paragraphs[0].style
    lines = text.used_lines or len(text.paragraphs)
    size = max(paragraph.style.size_pt for paragraph in text.paragraphs)
    height = min(box.h, int(lines * size * 1.2 * EMU_PER_POINT) + 2 * _INSET)
    if style.valign is VAlign.BOTTOM:
        top = box.bottom - height
    elif style.valign is VAlign.MIDDLE:
        top = box.y + (box.h - height) // 2
    else:
        top = box.y
    return Box(x=box.x, y=top, w=box.w, h=height)


def _template_boxes(pattern) -> set[tuple[int, int, int, int]]:
    """Рамки мест композиции, включая места каждого элемента повторителя.

    Подпись первого узла маршрута на обложке «Дорожной карты» стоит левее
    выведенного поля — там, где её поставил шаблон. Без мест повторителя
    автоправка «вернуть в поля» сдвигала её, рендер не находил рамку донора,
    и узел вместе с номером уходил как незаполненный.
    """
    boxes = {(slot.box.x, slot.box.y, slot.box.w, slot.box.h) for slot in pattern.slots}
    for repeater in pattern.repeaters:
        for dx, dy in repeater.member_offsets:
            boxes.update(
                (slot.box.x + dx, slot.box.y + dy, slot.box.w, slot.box.h)
                for slot in repeater.item_slots
            )
    return boxes


def margins(slide: SlideIR, deck: DeckIR, spec: TemplateSpec) -> list[Issue]:
    """Поля шаблона — не рекомендация: по ним колода читается как одна вещь."""
    grid = spec.grid
    if grid is None:
        return []
    left, top = grid.margin_left_emu, grid.margin_top_emu
    right = deck.slide_width_emu - grid.margin_right_emu
    bottom = deck.slide_height_emu - grid.margin_bottom_emu

    # Поля выведены эвристикой по рёбрам фигур, а рамка слота — сам шаблон.
    # Элемент, стоящий ровно в рамке слота своего паттерна, стоит там, где
    # его поставил автор шаблона: на `zelenie_investicii` заголовок шаблона
    # выше выведенного поля на 0.12″, и «правка» каждого заголовка стоила
    # полной пересборки и повторного аудита моделью всех слайдов.
    slot_boxes = {pattern.id: _template_boxes(pattern) for pattern in spec.patterns}
    member_frames = {
        pattern.id: [frame for r in pattern.repeaters for frame in r.member_frames]
        for pattern in spec.patterns
    }

    found: list[Issue] = []
    for element in slide.all_elements():
        if element.role in _MARGIN_EXEMPT:
            continue
        box = element.box
        if (box.x, box.y, box.w, box.h) in slot_boxes.get(element.provenance.ref, ()):
            continue
        # Место элемента повторителя, выросшее на всю его текстовую область:
        # внутри рамки элемента донора — там, где его поставил шаблон.
        if any(
            frame.x - GRID_TOLERANCE_EMU <= box.x
            and box.right <= frame.right + GRID_TOLERANCE_EMU
            for frame in member_frames.get(element.provenance.ref, ())
            if frame.y - GRID_TOLERANCE_EMU <= box.y <= frame.bottom
        ):
            continue
        over = max(left - box.x, top - box.y, box.right - right, box.bottom - bottom)
        if over <= GRID_TOLERANCE_EMU:
            continue
        found.append(
            _issue(
                "layout.margin_violation",
                slide.index,
                f"{element.id}: заходит в поле шаблона на {over / 914400:.2f} дюйма",
                element_ids=[element.id],
                bbox=box,
                fix=ProposedFix(
                    kind=FixKind.AUTOMATIC,
                    description="вернуть элемент в поля",
                    action="move_inside_margins",
                    params={"element_id": element.id},
                ),
            )
        )
    return found


def off_grid(slide: SlideIR, spec: TemplateSpec) -> list[Issue]:
    """Левый край элемента обязан совпадать с направляющей шаблона.

    Проверка информационная: шаблон, у которого направляющих нет вовсе,
    молчит, а не сыплет находками.
    """
    grid = spec.grid
    if grid is None or not grid.columns:
        return []
    guides = sorted(set(grid.columns) | {grid.margin_left_emu})

    found: list[Issue] = []
    for element in slide.all_elements():
        if element.role in _MARGIN_EXEMPT:
            continue
        nearest = min(guides, key=lambda guide: abs(guide - element.box.x))
        drift = abs(nearest - element.box.x)
        if drift <= GRID_TOLERANCE_EMU:
            continue
        found.append(
            _issue(
                "layout.off_grid",
                slide.index,
                f"{element.id}: левый край в {drift / 914400:.2f} дюйма "
                "от ближайшей направляющей",
                element_ids=[element.id],
                bbox=element.box,
                fix=ProposedFix(
                    kind=FixKind.AUTOMATIC,
                    description="притянуть элемент к направляющей",
                    action="snap_to_grid",
                    params={"element_id": element.id, "x": nearest},
                ),
            )
        )
    return found


def stretched_images(slide: SlideIR) -> list[Issue]:
    """Растянутая картинка — единственная ошибка вёрстки, заметная всем сразу."""
    found: list[Issue] = []
    for element in slide.all_elements():
        image = element.image
        if image is None or not image.native_w or not image.native_h:
            continue
        native = image.native_w / image.native_h
        placed = element.box.w / element.box.h if element.box.h else native
        if native <= 0 or abs(placed - native) / native <= ASPECT_TOLERANCE:
            continue
        found.append(
            _issue(
                "layout.image_stretched",
                slide.index,
                f"{element.id}: пропорции {placed:.2f} против исходных {native:.2f}",
                element_ids=[element.id],
                bbox=element.box,
                fix=ProposedFix(
                    kind=FixKind.AUTOMATIC,
                    description="вернуть картинке исходные пропорции",
                    action="restore_aspect",
                    params={"element_id": element.id},
                ),
            )
        )
    return found


def text_overflow(slide: SlideIR) -> list[Issue]:
    """Переполнение, которое вёрстка уже посчитала и записала в IR.

    Пересчитывать его здесь нечем и незачем: фиттер мерил по метрикам шрифта
    и знает, на сколько не хватило. Аудит только переводит это в находку.
    """
    found: list[Issue] = []
    for element in slide.all_elements():
        if element.text is None or not element.text.truncated:
            continue
        # Ёмкость рамки едет и в текст находки, и в параметры исправления.
        # Без неё «сократите текст» — совет без числа: на живом прогоне модель
        # сократила каждую строку втрое, а переполнение осталось, потому что
        # дело было в числе абзацев, а не в их длине.
        capacity = element.text.capacity_lines
        used = element.text.used_lines
        detail = (
            f": помещается {capacity} строк, занято {used}"
            if capacity or used
            else ""
        )
        found.append(
            _issue(
                "layout.text_overflow",
                slide.index,
                f"{element.id}: текст не помещается в рамку{detail}",
                element_ids=[element.id],
                bbox=element.box,
                fix=ProposedFix(
                    kind=FixKind.ASSISTED,
                    description="сократить текст или разнести блоки на два слайда",
                    action="shorten_or_split",
                    params={
                        "element_id": element.id,
                        "capacity_lines": capacity,
                        "used_lines": used,
                    },
                ),
            )
        )
    return found


def run(deck: DeckIR, spec: TemplateSpec) -> list[Issue]:
    """Все геометрические проверки по всей колоде."""
    found: list[Issue] = []
    for slide in deck.slides:
        found.extend(out_of_bounds(slide, deck))
        found.extend(overlaps(slide))
        found.extend(text_over_decor(slide, spec))
        found.extend(margins(slide, deck, spec))
        found.extend(off_grid(slide, spec))
        found.extend(stretched_images(slide))
        found.extend(text_overflow(slide))
    return found


__all__ = [
    "margins",
    "off_grid",
    "out_of_bounds",
    "overlaps",
    "run",
    "stretched_images",
    "text_band",
    "text_over_decor",
    "text_overflow",
]
