"""Добыча композиционных паттернов из слайдов-примеров.

Центральный модуль парсера. Причина, по которой он существует, — устройство
реальных шаблонов: у большинства layout'ов датасета нет ни одного контентного
плейсхолдера. У всех пятнадцати layout'ов `vk_workspace` ровно один
плейсхолдер заголовка и три-девять декоративных фигур; в `vk_tech` тридцать
шесть слайдов из пятидесяти четырёх стоят на layout'е «Свободный дизайн» с
одним заголовком. Опереться на плейсхолдеры при вёрстке попросту не на что.

Зато сами колоды — каталоги готовых композиций с рыбным текстом. Слайд-пример
несёт и геометрию, и стили, и подсказку о роли каждого места («Заголовок»,
«Текст», «XXX%», «Вставить фото»). Отсюда и берутся паттерны.

Отдельная ценность — повторяющиеся компоненты. На двадцать первом слайде
`vk_tech` четыре одинаковые по структуре группы стоят с шагом 2.35 дюйма при
ширине 1.77 — это карточная сетка, у которой число карточек можно менять под
содержание. Такие группы опознаются по совпадению структурной подписи и
равномерности шага, а не по именам: колоды экспортированы через Google Slides,
и все фигуры там называются `Google Shape;NNN`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import pairwise

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.schemas import (
    Align,
    Box,
    Color,
    Pattern,
    PatternClass,
    Provenance,
    Repeater,
    Slot,
    SlotRole,
    SourceKind,
    TextStyle,
    VAlign,
)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Шаги повторителя, отличающиеся меньше чем на эту долю, считаются равными.
PITCH_TOLERANCE = 0.08

# Меньше этого числа одинаковых элементов — совпадение, а не сетка.
MIN_REPEAT = 2

# Насколько можно раздвинуть наблюдённое число элементов. Шаблон показал сетку
# из четырёх карточек; три и пять в ту же полосу укладываются, десять — нет.
COUNT_SLACK = 2

# Тип содержимого `graphicFrame` объявлен в `graphicData/@uri`. В датасете
# нативных графиков не было вовсе, и рамка считалась таблицей всегда; на чужом
# шаблоне с четырьмя графиками это подменяло график таблицей.
GRAPHIC_ROLES = {
    "chart": SlotRole.CHART,
    "table": SlotRole.TABLE,
}

# Фигура мельче этой доли слайда по обеим сторонам — маркер или точка, а не
# содержательное место.
MIN_SLOT_AREA_SHARE = 0.0004

# Заголовок слайда занимает заметную долю его ширины. Крупный короткий текст
# в узкой колонке — заголовок карточки, а не слайда, и путать их нельзя:
# по заголовочным рамкам считается бюджет длины, и рамка шириной в карточку
# уводит его в двадцать символов.
TITLE_MIN_WIDTH_SHARE = 0.4

# Шаг меньше половины размера элемента означает, что элементы лежат друг на
# друге, а не стоят в ряд. Два блока, смещённые на волосок (тень, подложка),
# сеткой не являются, но формально дают равномерный шаг.
MIN_PITCH_SHARE = 0.5


@dataclass(frozen=True)
class _Shape:
    element: etree._Element
    box: Box
    tag: str
    text: str
    size_pt: float


# Чем разрешить цвет текста фигуры в RGB: тема и карта цветов мастера есть
# только у разбора шаблона. Задаётся на время разбора одного слайда
# (`mine_slide`), чтобы не тянуть тему через весь поиск повторителей.
_COLOR_OF: ContextVar[Callable[[etree._Element], Color | None] | None] = ContextVar(
    "color_of", default=None
)


def _color_of(element: etree._Element) -> Color | None:
    """Цвет, которым донор пишет текст этой фигуры; None — не объявлен."""
    resolve = _COLOR_OF.get()
    return resolve(element) if resolve is not None else None


_ALGN = {"l": Align.LEFT, "ctr": Align.CENTER, "r": Align.RIGHT, "just": Align.JUSTIFY,
         "dist": Align.JUSTIFY}


def text_align(element: etree._Element) -> Align | None:
    """Выравнивание, которым донор пишет текст фигуры; None — не объявлено.

    Выравнивание большинства абзацев с текстом; абзац без своего `algn`
    берёт его из списка стилей фигуры (`lstStyle`). Не объявлено нигде —
    наследуется, и решает звено выше: плейсхолдер макета, мастер.
    """
    shared_node = element.find(f".//{{{A_NS}}}lstStyle/{{{A_NS}}}lvl1pPr")
    shared = _ALGN.get(shared_node.get("algn", "")) if shared_node is not None else None
    counts: dict[Align, int] = {}
    for paragraph in element.iter(f"{{{A_NS}}}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{A_NS}}}t")).strip()
        if not text:
            continue
        props = paragraph.find(f"{{{A_NS}}}pPr")
        align = (_ALGN.get(props.get("algn", "")) if props is not None else None) or shared
        if align is not None:
            counts[align] = counts.get(align, 0) + len(text)
    if counts:
        return max(counts, key=counts.get)
    return shared


_ANCHOR = {"t": VAlign.TOP, "ctr": VAlign.MIDDLE, "b": VAlign.BOTTOM}


def text_valign(element: etree._Element) -> VAlign | None:
    """Привязка текста фигуры по вертикали; None — не объявлена."""
    body = element.find(f".//{{{A_NS}}}bodyPr")
    return _ANCHOR.get(body.get("anchor", "")) if body is not None else None


def _text_of(element: etree._Element) -> str:
    return " ".join(
        (node.text or "") for node in element.findall(f".//{{{A_NS}}}t")
    ).strip()


def _max_size(element: etree._Element) -> float:
    sizes = [
        int(node.get("sz")) / 100
        for node in element.iter()
        if etree.QName(node).localname in ("rPr", "defRPr") and node.get("sz")
    ]
    return max(sizes) if sizes else 0.0


_SIZE_QUANTUM = 45_720  # 1/20 дюйма

# Роли основного текста элемента: их у карточки одна.
_BODY_ROLES = frozenset({SlotRole.BODY, SlotRole.BULLETS})


def _signature(element: etree._Element, box: Box) -> tuple:
    """Структурная подпись фигуры — по чему опознаётся повтор.

    В подпись входят состав дочерних тегов и габариты, огрублённые до
    двадцатой дюйма. Ни имена, ни координаты: имена в этих колодах
    бессмысленны, а координаты у элементов сетки как раз и различаются.
    """
    tags = defaultdict(int)
    for node in element.iter():
        name = etree.QName(node).localname
        if name in ("sp", "pic", "graphicFrame", "cxnSp", "grpSp"):
            tags[name] += 1
    # Есть ли в фигуре текст — да, а сколько в нём фрагментов — нет: у
    # трёх одинаковых карточек holdout текст-заглушка разной длины, и по
    # числу фрагментов они не опознавались повтором.
    # Габариты — с точностью до двадцатой дюйма: копии карточки у дизайнера
    # расходятся на сотую (1.71 и 1.72 на `vk_tech`), и ряд из пяти распадался
    # на четыре и одну. Случайно похожие фигуры отсекает проверка равного шага.
    return (
        tuple(sorted(tags.items())),
        round(box.w / _SIZE_QUANTUM),
        round(box.h / _SIZE_QUANTUM),
        element.find(f".//{{{A_NS}}}t") is not None,
    )


# Показатель шаблона: число с процентом или множителем, либо заглушка из
# «x»/«х». Голая цифра — не показатель: «1», «2», «3» в кружках holdout —
# номера шагов. Разбирается текст шаблона, а не ответ модели.
_FIGURE = re.compile(
    r"^[~≈<>+\-−]?\s*(\d[\d\s.,]*\s*[%×x]|[xхXХ]{2,}\s*[%×x]?)$"
)


def is_figure_text(text: str) -> bool:
    """Похож ли текст шаблона на показатель: «10%», «ххх%», «×4»."""
    return bool(_FIGURE.match(text.strip())) if text.strip() else False


def _role_from_geometry(
    shape: _Shape,
    shapes: list[_Shape],
    slide_h: int,
    slide_w: int = 0,
    inside_repeater: bool = False,
) -> SlotRole:
    """Роль слота по структуре, а не по тексту-заглушке.

    Главный признак — ранг кегля среди текстовых фигур слайда: самый крупный
    текст в верхней части и есть заголовок. Текст-заглушка («Заголовок»,
    «Текст») — только подсказка, и подсказка ненадёжная: шаблоном может
    оказаться старая презентация с настоящим содержанием, где никаких рыбных
    слов нет.
    """
    if shape.tag == "pic":
        return SlotRole.IMAGE
    if shape.tag == "graphicFrame":
        data = shape.element.find(f".//{{{A_NS}}}graphicData")
        uri = (data.get("uri", "") if data is not None else "").rsplit("/", 1)[-1]
        # Неизвестный вид рамки (SmartArt, встроенный объект) честнее пометить
        # неопознанным, чем назвать таблицей и потом строить не то.
        return GRAPHIC_ROLES.get(uri, SlotRole.UNKNOWN)
    if not shape.text:
        return SlotRole.DECOR
    # Число донора — место показателя, где бы оно ни стояло: «10%» в центре
    # кольца на `vk_tech` внутри карточки становилось подписью, туда садился
    # пункт списка, и кольцо с чужой долей оставалось на слайде.
    if is_figure_text(shape.text):
        return SlotRole.KPI_VALUE

    sized = sorted({s.size_pt for s in shapes if s.size_pt > 0}, reverse=True)
    rank = sized.index(shape.size_pt) if shape.size_pt in sized else len(sized)

    in_upper_half = shape.box.y < slide_h * 0.45
    # Внутри повторяющегося элемента заголовка слайда быть не может: это
    # подпись карточки, сколько бы крупно она ни была набрана.
    wide_enough = not slide_w or shape.box.w >= slide_w * TITLE_MIN_WIDTH_SHARE
    if not inside_repeater and wide_enough:
        if rank == 0 and in_upper_half:
            return SlotRole.TITLE
        if rank == 1 and in_upper_half:
            return SlotRole.SUBTITLE
    if inside_repeater and rank <= 1 and len(shape.text) <= 60:
        return SlotRole.CAPTION

    # Очень крупный и очень короткий текст — числовой показатель, а не абзац:
    # «91 %», «XXX%», «42». Опознаётся по форме, а не по содержанию.
    compact = len(shape.text) <= 12
    if compact and rank <= 1:
        return SlotRole.KPI_VALUE
    if compact and shape.size_pt and shape.size_pt <= (sized[-1] if sized else 0) * 1.2:
        return SlotRole.KPI_LABEL
    return SlotRole.BODY


# Роли, которые автор шаблона объявил типом плейсхолдера. Объявление сильнее
# любой догадки по геометрии: на трёх шаблонах датасета заголовок, набранный
# кеглем макета (без `sz` в самом тексте), проигрывал ранг крупной цифре или
# подписи и становился подзаголовком или телом — у 97 композиций из 101, где
# заголовка «не было», на слайде стоял плейсхолдер `title` с рамкой.
#
# Колонтитул, дата и номер слайда — туда же: иначе текст колонтитула
# («Шаблоны презентаций с сайта…» на holdout) разбирался как тело и
# становился местом под содержание.
_DECLARED_ROLE = {
    "title": SlotRole.TITLE,
    "ctrTitle": SlotRole.TITLE,
    "subTitle": SlotRole.SUBTITLE,
    "ftr": SlotRole.FOOTER,
    "dt": SlotRole.FOOTER,
    "sldNum": SlotRole.SLIDE_NUMBER,
}


def _declared_role(element: etree._Element) -> SlotRole | None:
    """Роль из типа плейсхолдера фигуры, если тип однозначный."""
    for node in element.iter():
        if etree.QName(node).localname == "ph":
            return _DECLARED_ROLE.get(node.get("type", ""))
    return None


def _inherited_titles(
    container: etree._Element, inherited: dict[SlotRole, Slot], collected: list[_Shape]
) -> list[_Shape]:
    """Заголовочные плейсхолдеры без своей рамки — с рамкой из макета."""
    have = {id(shape.element) for shape in collected}
    found: list[_Shape] = []
    for element, box, _ in iter_shapes(container):
        if box is not None or id(element) in have:
            continue
        role = _declared_role(element)
        slot = inherited.get(role) if role is not None else None
        text = _text_of(element)
        if slot is None or not text:
            continue
        found.append(
            _Shape(
                element=element,
                box=slot.box,
                tag=etree.QName(element).localname,
                text=text,
                size_pt=_max_size(element),
            )
        )
    return found


# Поля текстовой рамки по умолчанию в OOXML (`a:bodyPr`): 0.1″ слева и
# справа, 0.05″ сверху и снизу.
_DEFAULT_INSETS = {"lIns": 91_440, "rIns": 91_440, "tIns": 45_720, "bIns": 45_720}


def _with_insets(element: etree._Element, box: Box) -> Box:
    """Рамка вместе с полями, объявленными её `a:bodyPr`.

    Без них фиттер считал поля умолчательными: шаблон, набравший заголовок
    36 pt в рамку 0.6″ с нулевыми полями, выглядел для вёрстки рамкой, где не
    помещается ни строки, и все его композиции отбрасывались.
    """
    body = element.xpath("./*[local-name()='txBody']/*[local-name()='bodyPr']")
    if not body or not any(body[0].get(name) is not None for name in _DEFAULT_INSETS):
        return box
    value = {
        name: int(body[0].get(name, default)) for name, default in _DEFAULT_INSETS.items()
    }
    return box.model_copy(
        update={
            "inset_x": value["lIns"] + value["rIns"],
            "inset_y": value["tIns"] + value["bIns"],
        }
    )


def _collect(container: etree._Element, slide_w: int, slide_h: int) -> list[_Shape]:
    shapes: list[_Shape] = []
    min_area = slide_w * slide_h * MIN_SLOT_AREA_SHARE
    for element, box, _ in iter_shapes(container):
        if box is None or box.area < min_area:
            continue
        shapes.append(
            _Shape(
                element=element,
                box=_with_insets(element, box),
                tag=etree.QName(element).localname,
                text=_text_of(element),
                size_pt=_max_size(element),
            )
        )
    return shapes


def _cluster(values: list[int], tolerance: int) -> list[list[int]]:
    """Близкие значения в одну группу. Нужно поиску сеток по двум осям."""
    if not values:
        return []
    ordered = sorted(values)
    clusters: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return clusters


def _grid_pitch(shapes: list[_Shape], slide_w: int) -> int | None:
    """Шаг сетки по двум осям: элементы стоят рядами и колонками.

    Карточки часто разложены в две строки по три, а не в одну полосу. Такая
    раскладка не проходит ни проверку на горизонтальный шаг, ни на
    вертикальный — по каждой оси позиции повторяются.
    """
    tolerance = max(1, slide_w // 100)
    columns = _cluster([s.box.x for s in shapes], tolerance)
    rows = _cluster([s.box.y for s in shapes], tolerance)
    if len(columns) < MIN_REPEAT or len(rows) < 2:
        return None
    if len(columns) * len(rows) != len(shapes):
        return None
    centers = [round(sum(c) / len(c)) for c in columns]
    return _even_pitch(centers)


def _even_pitch(positions: list[int]) -> int | None:
    """Шаг, если элементы расставлены равномерно. Иначе None.

    Равномерность — признак сетки. Четыре случайно похожих блока в разных
    местах слайда сеткой не являются, и раздвигать их числом элементов нельзя.
    """
    if len(positions) < MIN_REPEAT:
        return None
    steps = [b - a for a, b in pairwise(positions)]
    if not steps or min(steps) <= 0:
        return None
    average = sum(steps) / len(steps)
    if any(abs(step - average) > average * PITCH_TOLERANCE for step in steps):
        return None
    return round(average)


def _without_stray(members: list[_Shape]) -> list[_Shape]:
    """Ряд без одной случайно похожей фигуры.

    Одинаковый стиль — ещё не один ряд: подпись к слайду набрана тем же
    кеглем и цветом, что описания шагов под ней, и пять фигур уже не стояли
    равномерно — ряд из четырёх описаний не опознавался. Если без одной фигуры
    остальные (не меньше трёх) встают с равным шагом, лишняя — не член ряда.
    """
    def regular(group: list[_Shape]) -> bool:
        return (
            _even_pitch(sorted(s.box.x for s in group)) is not None
            or _even_pitch(sorted(s.box.y for s in group)) is not None
        )

    if len(members) < 4 or regular(members):
        return members
    for stray in members:
        rest = [shape for shape in members if shape is not stray]
        if regular(rest):
            return rest
    return members


def _find_repeaters(
    shapes: list[_Shape], slide_index: int, slide_w: int, slide_h: int
) -> tuple[list[Repeater], set[int]]:
    """Группы одинаковой структуры, расставленные равномерно.

    Возвращает повторители и идентификаторы использованных фигур, чтобы те не
    попали ещё и в обычные слоты.
    """
    by_signature: dict[tuple, list[_Shape]] = defaultdict(list)
    for shape in shapes:
        if shape.tag in ("grpSp", "sp") and shape.box.area > 0:
            by_signature[_signature(shape.element, shape.box)].append(shape)

    repeaters: list[Repeater] = []
    consumed: set[int] = set()
    # Повторы без текста — подложки карточек. Места под содержание в них нет,
    # но они — часть элемента, и убирать незаполненный элемент надо с ними.
    frames: list[tuple[str, int, list[_Shape]]] = []

    # Сначала самые крупные композиции: внешняя группа должна забрать свои
    # фигуры раньше, чем её же дети образуют собственный «повтор».
    candidates = sorted(
        by_signature.values(), key=lambda members: -members[0].box.area
    )
    for index, members in enumerate(candidates):
        if len(members) < MIN_REPEAT:
            continue
        if any(id(shape.element) in consumed for shape in members):
            continue
        members = _without_stray(members)
        horizontal = sorted(members, key=lambda s: s.box.x)
        vertical = sorted(members, key=lambda s: s.box.y)

        pitch = _even_pitch([s.box.x for s in horizontal])
        axis, ordered = "horizontal", horizontal
        if pitch is None:
            pitch = _even_pitch([s.box.y for s in vertical])
            axis, ordered = "vertical", vertical
        if pitch is None:
            pitch = _grid_pitch(members, slide_w)
            if pitch is None:
                continue
            # Порядок чтения: ряд за рядом, в ряду слева направо. Первым
            # должен идти левый верхний элемент — от него считаются сдвиги.
            tolerance = max(1, slide_w // 100)
            axis = "grid"
            ordered = sorted(members, key=lambda s: (round(s.box.y / tolerance), s.box.x))

        first = ordered[0]
        item_slots = _slots_of(first, shapes, slide_h, prefix=f"r{index}", slide_w=slide_w)
        held: list[list[_Shape]] = []
        if not item_slots:
            # Подложка без текста — но, может быть, текст лежит на ней
            # отдельной фигурой. На holdout три одинаковые карточки несут по
            # тексту разной высоты, и повтором опознавались только подложки.
            held = [_texts_on(member, shapes, consumed) for member in ordered]
            if all(held) and len({len(texts) for texts in held}) == 1:
                item_slots = _slots_on(held[0], slide_h, slide_w, prefix=f"r{index}")
                # Два основных текста на одной подложке — это ряд карточек,
                # а не карточка: сетка 2×2 на `vk_tech` иначе читалась двумя
                # строками, и три пункта ложились по два в карточку.
                bodies = [slot for slot in item_slots if slot.role in _BODY_ROLES]
                if len(bodies) > 1:
                    item_slots, held = [], []
        if not item_slots:
            frames.append((axis, pitch, ordered))
            continue

        span = slide_h if axis == "vertical" else slide_w
        item_size = first.box.h if axis == "vertical" else first.box.w
        if pitch < item_size * MIN_PITCH_SHARE:
            continue
        gutter = max(0, pitch - item_size)
        # Сколько элементов физически помещается в полосу с тем же шагом.
        # Сетку в несколько рядов раздвигать некуда: у неё нет одной полосы.
        fits = max(len(ordered), int(span // pitch)) if pitch else len(ordered)
        if axis == "grid":
            fits = len(ordered)

        repeaters.append(
            Repeater(
                id=f"s{slide_index}_rep{index}",
                item_slots=item_slots,
                item_box=first.box,
                axis=axis,
                observed_count=len(ordered),
                min_count=max(1, len(ordered) - COUNT_SLACK),
                max_count=min(fits, len(ordered) + COUNT_SLACK),
                pitch_emu=pitch,
                gutter_emu=gutter,
                member_offsets=[
                    (shape.box.x - first.box.x, shape.box.y - first.box.y)
                    for shape in ordered
                ],
                provenance=Provenance(
                    kind=SourceKind.SLIDE,
                    ref=f"slide{slide_index}",
                    note=f"{len(ordered)} одинаковых элементов с равным шагом",
                ),
            )
        )
        for shape in members:
            consumed.add(id(shape.element))
            consumed.update(
                id(element) for element, _, _ in iter_shapes(shape.element)
            )
        consumed.update(id(text.element) for texts in held for text in texts)

    return _with_frames(_merge_repeaters(repeaters), frames), consumed


def _texts_on(frame: _Shape, shapes: list[_Shape], consumed: set[int]) -> list[_Shape]:
    """Текстовые фигуры, лежащие на подложке целиком и ещё ничьи."""
    return [
        shape
        for shape in shapes
        if shape is not frame
        and shape.text
        and id(shape.element) not in consumed
        and _within(shape.box, frame.box)
    ]


def _within(inner: Box, outer: Box) -> bool:
    """Лежит ли рамка на другой: девять десятых её площади внутри."""
    width = min(inner.right, outer.right) - max(inner.x, outer.x)
    height = min(inner.bottom, outer.bottom) - max(inner.y, outer.y)
    return width > 0 and height > 0 and width * height >= 0.9 * inner.area


def _slots_on(texts: list[_Shape], slide_h: int, slide_w: int, prefix: str) -> list[Slot]:
    """Слоты элемента из текстов, лежащих на его подложке."""
    slots = []
    for position, shape in enumerate(texts):
        role = _role_from_geometry(shape, texts, slide_h, slide_w, inside_repeater=True)
        if role is SlotRole.DECOR:
            continue
        slots.append(
            Slot(
                id=f"{prefix}_t{position}",
                role=role,
                box=shape.box,
                placeholder_text=shape.text[:80],
                text_color=_color_of(shape.element),
                text_align=text_align(shape.element),
                text_valign=text_valign(shape.element),
                provenance=Provenance(kind=SourceKind.SLIDE, ref="элемент повторителя"),
            )
        )
    return slots


def _with_frames(
    repeaters: list[Repeater], frames: list[tuple[str, int, list[_Shape]]]
) -> list[Repeater]:
    """Каждому элементу повторителя — рамка всего элемента, с подложкой.

    Фигура — часть элемента, если она повторяется с тем же шагом и тем же
    числом, а её первый экземпляр стоит на одной полосе с текстом первого
    элемента: перекрывает его проекцию на ось повторения, а у сетки — на обе
    оси. Так в элемент попадают подложка карточки, плашка над текстом и
    рамка пиктограммы сбоку. Экземпляры сопоставляются по порядку.
    """
    result = []
    for repeater in repeaters:
        text = repeater.item_box
        for slot in repeater.item_slots:
            text = _union(text, slot.box)
        members = [
            Box(x=text.x + dx, y=text.y + dy, w=text.w, h=text.h)
            for dx, dy in repeater.member_offsets
        ]
        for axis, pitch, ordered in frames:
            if (
                axis == repeater.axis
                and len(ordered) == len(members)
                and abs(pitch - repeater.pitch_emu) <= repeater.pitch_emu * PITCH_TOLERANCE
                and _same_lane(ordered[0].box, text, axis, repeater.pitch_emu)
            ):
                members = [
                    _union(member, shape.box)
                    for member, shape in zip(members, ordered, strict=True)
                ]
        result.append(repeater.model_copy(update={"member_frames": members}))
    return result


def _same_lane(frame: Box, text: Box, axis: str, pitch: int) -> bool:
    """Принадлежит ли фигура тому же элементу, что и текст.

    Да, если она стоит на одной полосе с текстом вдоль оси повторения: та
    же колонка у горизонтального ряда, та же строка у вертикального, та же
    клетка у сетки. Или — рядом с текстом поперёк неё: кружок под фото
    спикера слева от подписи на финале `vk_workspace` проекцию подписи не
    перекрывает, но стоит в том же ряду в промежутке между элементами и к
    своей подписи ближе, чем к соседней.
    """
    across_x = min(frame.right, text.right) > max(frame.x, text.x)
    across_y = min(frame.bottom, text.bottom) > max(frame.y, text.y)
    if axis == "grid":
        return across_x and across_y
    along, beside = (across_y, across_x) if axis == "vertical" else (across_x, across_y)
    if along:
        return True
    if not beside:
        return False
    # Зазор вдоль оси не больше промежутка между элементами — и до своего
    # текста ближе, чем до соседнего.
    start, end, size = (
        (frame.y, frame.bottom, text.h) if axis == "vertical" else (frame.x, frame.right, text.w)
    )
    first = text.y if axis == "vertical" else text.x

    def gap(offset: int) -> int:
        return max(0, first + offset - end, start - (first + offset + size))

    # Сосед у первого элемента — только следующий: предыдущего нет.
    return gap(0) <= max(0, pitch - size) and gap(0) < gap(pitch)


def _union(a: Box, b: Box) -> Box:
    x, y = min(a.x, b.x), min(a.y, b.y)
    return Box(x=x, y=y, w=max(a.right, b.right) - x, h=max(a.bottom, b.bottom) - y)


def _merge_repeaters(repeaters: list[Repeater]) -> list[Repeater]:
    """Сливает повторители, описывающие один и тот же компонент.

    Карточка состоит из нескольких фигур — подложки, заголовка, подписи, — и
    каждая из них образует собственный ряд с тем же шагом. Это одна сетка,
    а не три: слоты у них складываются, а геометрия общая.
    """
    merged: list[Repeater] = []
    for repeater in sorted(repeaters, key=lambda r: -r.item_box.area):
        twin = next(
            (
                existing
                for existing in merged
                if existing.axis == repeater.axis
                and existing.observed_count == repeater.observed_count
                and abs(existing.pitch_emu - repeater.pitch_emu)
                <= existing.pitch_emu * PITCH_TOLERANCE
            ),
            None,
        )
        if twin is None:
            merged.append(repeater)
            continue
        slots = list(twin.item_slots)
        known = {slot.box for slot in slots}
        slots.extend(slot for slot in repeater.item_slots if slot.box not in known)
        merged[merged.index(twin)] = twin.model_copy(update={"item_slots": slots})
    return merged


def _slots_of(
    parent: _Shape, all_shapes: list[_Shape], slide_h: int, prefix: str, slide_w: int = 0
) -> list[Slot]:
    """Слоты внутри одного элемента повторителя."""
    inner = [
        _Shape(
            element=element,
            box=box,
            tag=etree.QName(element).localname,
            text=_text_of(element),
            size_pt=_max_size(element),
        )
        for element, box, _ in iter_shapes(parent.element)
        if box is not None
    ]
    # Повторяется не только группа. Ряд одинаковых текстовых блоков — та же
    # сетка, просто её элемент неделим: тогда слотом становится он сам.
    if not inner:
        inner = [parent]
    slots: list[Slot] = []
    for position, shape in enumerate(inner):
        role = _role_from_geometry(
            shape, inner or all_shapes, slide_h, slide_w, inside_repeater=True
        )
        if role is SlotRole.DECOR:
            continue
        slots.append(
            Slot(
                id=f"{prefix}_{position}",
                role=role,
                box=shape.box,
                placeholder_text=shape.text[:80],
                text_color=_color_of(shape.element),
                text_align=text_align(shape.element),
                text_valign=text_valign(shape.element),
                provenance=Provenance(kind=SourceKind.SLIDE, ref="элемент повторителя"),
            )
        )
    return slots


def mine_slide(
    container: etree._Element,
    slide_index: int,
    slide_w: int,
    slide_h: int,
    layout_id: str | None,
    default_style: TextStyle,
    is_dark: bool = False,
    inherited_slots: dict[SlotRole, Slot] | None = None,
    color_of: Callable[[etree._Element], Color | None] | None = None,
) -> Pattern | None:
    """Разбирает слайд-пример в композиционный паттерн.

    `inherited_slots` — слоты макета, на котором стоит слайд. Плейсхолдер
    слайда наследует из макета то, чего не задал сам: кегль, а часто и
    рамку. Заголовок без своей рамки иначе не попадал в разбор вовсе, и
    заголовком композиции становилась крупная цифра рядом («987 654 321» на
    holdout-шаблоне).
    """
    token = _COLOR_OF.set(color_of)
    try:
        return _mine_slide(
            container, slide_index, slide_w, slide_h, layout_id, default_style, is_dark,
            inherited_slots or {},
        )
    finally:
        _COLOR_OF.reset(token)


# Нижняя доля слайда, где стоит номер страницы.
_PAGE_NUMBER_ZONE = 0.85
_PAGE_DIGITS = re.compile(r"^\d{1,3}$")


def _is_page_number(shape: _Shape, slide_index: int, slide_h: int) -> bool:
    """Номер страницы, набранный текстом, а не полем: «02» внизу второго слайда.

    Шаблон без плейсхолдеров пишет номер обычной надписью. Место под
    содержание из него получалось («показатель», и туда садился пункт), а
    номера в колоде не оставалось. Опознаётся по двум признакам сразу: число
    равно номеру слайда-донора и стоит у нижнего края.
    """
    text = shape.text.strip()
    return (
        bool(_PAGE_DIGITS.match(text))
        and int(text) == slide_index
        and shape.box.y >= slide_h * _PAGE_NUMBER_ZONE
    )


# Насколько ниже заголовка может начинаться подзаголовок — в долях высоты
# заголовка, и насколько его левый край может разойтись с заголовочным.
_SUBTITLE_GAP_SHARE = 0.6
_SUBTITLE_ALIGN_EMU = 137_160  # 0.15″
_SUBTITLE_MIN_WIDTH_SHARE = 0.5


def _subtitle_under_title(slots: list[Slot]) -> list[Slot]:
    """Строка сразу под заголовком, по его левому краю и мельче — подзаголовок.

    Ранг кегля этого не видит: на слайде со схемой второй по величине текст —
    подписи узлов, и подзаголовок становился телом. Весь список пунктов
    садился в эту строку первым, в порядке чтения.
    """
    title = next((slot for slot in slots if slot.role is SlotRole.TITLE), None)
    if title is None or any(slot.role is SlotRole.SUBTITLE for slot in slots):
        return slots
    title_size = title.style.size_pt if title.style is not None else 0.0

    def below(slot: Slot) -> bool:
        size = slot.style.size_pt if slot.style is not None else 0.0
        gap = slot.box.y - title.box.bottom
        return (
            slot.role in (SlotRole.BODY, SlotRole.KPI_LABEL, SlotRole.CAPTION)
            and -_SUBTITLE_ALIGN_EMU <= gap <= title.box.h * _SUBTITLE_GAP_SHARE
            and abs(slot.box.x - title.box.x) <= _SUBTITLE_ALIGN_EMU
            and slot.box.w >= title.box.w * _SUBTITLE_MIN_WIDTH_SHARE
            and (not title_size or size < title_size)
        )

    found = next((slot for slot in sorted(slots, key=lambda s: s.box.y) if below(slot)), None)
    if found is None:
        return slots
    return [
        slot.model_copy(update={"role": SlotRole.SUBTITLE}) if slot is found else slot
        for slot in slots
    ]


def _inherited_color(role: SlotRole, inherited_slots: dict[SlotRole, Slot]) -> Color | None:
    """Цвет, который место этой роли наследует от макета."""
    for wanted in (role, SlotRole.BODY):
        slot = inherited_slots.get(wanted)
        if slot is not None and slot.style is not None:
            return slot.style.color
    return None


def _mine_slide(
    container: etree._Element,
    slide_index: int,
    slide_w: int,
    slide_h: int,
    layout_id: str | None,
    default_style: TextStyle,
    is_dark: bool,
    inherited_slots: dict[SlotRole, Slot],
) -> Pattern | None:
    shapes = _collect(container, slide_w, slide_h)
    shapes.extend(_inherited_titles(container, inherited_slots, shapes))
    if not shapes:
        return None

    repeaters, consumed = _find_repeaters(shapes, slide_index, slide_w, slide_h)
    # Место повторителя без своего цвета пишется унаследованным: у
    # плейсхолдера макета той же роли, у основного текста, у мастера. Иначе
    # вёрстка брала цвет первого слота композиции со стилем — синего
    # заголовка `vk_education` — и красила им текст карточек.
    repeaters = [
        repeater.model_copy(
            update={
                "item_slots": [
                    slot
                    if slot.text_color is not None
                    else slot.model_copy(
                        update={"text_color": _inherited_color(slot.role, inherited_slots)
                                or default_style.color}
                    )
                    for slot in repeater.item_slots
                ]
            }
        )
        for repeater in repeaters
    ]

    own = [
        shape
        for shape in shapes
        if id(shape.element) not in consumed and shape.tag != "grpSp"
    ]
    declared = {id(shape.element): _declared_role(shape.element) for shape in own}
    has_declared_title = SlotRole.TITLE in declared.values()

    slots: list[Slot] = []
    for position, shape in enumerate(shapes):
        if id(shape.element) in consumed or shape.tag == "grpSp":
            continue
        role = declared.get(id(shape.element))
        if role is not None and not shape.text:
            role = None  # пустой плейсхолдер — не место для содержания
        if role is None and _is_page_number(shape, slide_index, slide_h):
            role = SlotRole.SLIDE_NUMBER
        if role is None:
            role = _role_from_geometry(shape, shapes, slide_h, slide_w)
            # Заголовок на слайде один. Если автор объявил его сам, крупный
            # текст, «выигравший» ранг кегля, — это не второй заголовок.
            # Короткий крупный текст — показатель («987 654 321»), как и в
            # правиле для чисел внутри `_role_from_geometry`.
            if role is SlotRole.TITLE and has_declared_title:
                role = SlotRole.KPI_VALUE if len(shape.text) <= 12 else SlotRole.SUBTITLE
        if role is SlotRole.DECOR:
            continue
        inherited = inherited_slots.get(role)
        if shape.size_pt > 0:
            style = default_style.model_copy(update={"size_pt": shape.size_pt})
        elif inherited is not None and inherited.style is not None:
            style = inherited.style
        else:
            style = default_style
        # Цвет — тот, которым донор пишет эту фигуру; не объявлен — у
        # плейсхолдера макета; и только потом цвет текста мастера. Раньше
        # цвет не читался вовсе, и всему шёл самый частый цвет палитры —
        # серый `C4C4C4` у `vk_tech`, чёрный на тёмных слайдах `vk_workspace`.
        color = _color_of(shape.element) or (
            inherited.style.color
            if inherited is not None and inherited.style is not None
            else None
        )
        if color is not None:
            style = style.model_copy(update={"color": color})
        align = text_align(shape.element) or (
            inherited.style.align
            if inherited is not None and inherited.style is not None
            else None
        )
        if align is not None:
            style = style.model_copy(update={"align": align})
        slots.append(
            Slot(
                id=f"s{slide_index}_{position}",
                role=role,
                box=shape.box,
                style=style,
                placeholder_text=shape.text[:80],
                text_align=align,
                text_valign=text_valign(shape.element)
                or (
                    inherited.style.valign
                    if inherited is not None and inherited.style is not None
                    else None
                ),
                provenance=Provenance(kind=SourceKind.SLIDE, ref=f"slide{slide_index}"),
            )
        )

    if not slots and not repeaters:
        return None
    slots = _subtitle_under_title(slots)

    boxes = [slot.box for slot in slots] + [r.item_box for r in repeaters]
    # Область обрезается по слайду: дизайнеры выпускают фигуры за обрез, и
    # объемлющий прямоугольник тогда выходит за холст. Класть туда содержание
    # нельзя — часть его окажется за краем, а аудит справедливо это найдёт.
    left = max(0, min(b.x for b in boxes))
    top = max(0, min(b.y for b in boxes))
    right = min(slide_w, max(b.right for b in boxes))
    bottom = min(slide_h, max(b.bottom for b in boxes))
    content_area = Box(x=left, y=top, w=max(1, right - left), h=max(1, bottom - top))

    return Pattern(
        id=f"pattern{slide_index}",
        pattern_class=PatternClass.FREEFORM,
        layout_id=layout_id,
        donor_slide_index=slide_index,
        slots=slots,
        repeaters=repeaters,
        content_area=content_area,
        is_dark=is_dark,
        provenance=Provenance(
            kind=SourceKind.SLIDE,
            ref=f"slide{slide_index}",
            note="композиция снята со слайда-примера",
        ),
    )
