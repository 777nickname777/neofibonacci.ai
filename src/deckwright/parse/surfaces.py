"""Что на слайде реально видно: единое представление слайда, макета и образца.

Зачем отдельный слой. Композиция снимается со слайда-примера, и до этого
модуля ограничения вёрстки тоже брались только со слайда. Но оформление
шаблона обычно живёт **не в слайде**: у `vk_tech` в макетах и образце 160
картинок, 44 фигуры и 52 группы, и вёрстка не знала о них ничего. Слайд
объявлялся пустым холстом, рамка текста росла по «свободному» месту и
ложилась поверх картинки макета.

Здесь собирается то, что человек видит на собранном слайде:

* элементы самого слайда;
* элементы макета;
* элементы образца — только если макет их показывает (`showMasterSp`).

Унаследованное не удваивается: фигура образца, повторённая в макете на том же
месте, учитывается один раз.

## Классификация

Роль определяется несколькими признаками, а не одной площадью: большая
картинка бывает и фоном, на котором шаблон сам пишет, и содержательной
иллюстрацией, поверх которой текст читаться не будет.

| вид | что это | текст поверх |
|---|---|---|
| `background` | заливка или картинка во весь слайд в самом низу стопки | допустим |
| `panel` | подложка под текст: залитая фигура, на которой шаблон сам пишет | допустим |
| `brand` | логотип, колонтитул, номер страницы | запрещён |
| `decor` | линии, значки, орнамент | запрещён |
| `imagery` | содержательная картинка: фото или иллюстрация | запрещён |
| `placeholder` | заполнитель макета — это слот, а не препятствие | это слот |

`text_safe` отвечает ровно на вопрос вёрстки «можно ли сюда писать». Именно
он, а не площадь, отделяет фон от фотографии.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.parse.pictures import is_illustration, is_photo
from deckwright.schemas import Box, SourceKind

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Картинка или фигура, закрывающая почти весь слайд и лежащая в самом низу
# стопки, — фон. Порог высокий: «почти весь слайд» это не «половина».
BACKGROUND_SHARE = 0.92
# Доля слайда, ниже которой элемент считается значком, а не поверхностью.
ICON_SHARE = 0.004
# Толщина, ниже которой фигура — линия.
THIN_EMU = 91_440  # 0.1 дюйма
# Значок: обе стороны меньше этого.
ICON_EMU = 548_640  # 0.6 дюйма


class SurfaceKind(StrEnum):
    BACKGROUND = "background"
    PANEL = "panel"
    BRAND = "brand"
    DECOR = "decor"
    IMAGERY = "imagery"
    PLACEHOLDER = "placeholder"


#: Виды, поверх которых писать текст запрещено.
BLOCKING = frozenset({SurfaceKind.BRAND, SurfaceKind.DECOR, SurfaceKind.IMAGERY})
#: Виды, поверх которых текст предусмотрен дизайном.
TEXT_SAFE = frozenset({SurfaceKind.BACKGROUND, SurfaceKind.PANEL})


@dataclass(frozen=True)
class Surface:
    """Видимый элемент собранного слайда.

    `z` — порядок наложения в том виде, в каком его рисует PowerPoint: образец
    ниже макета, макет ниже слайда, внутри каждого — порядок дерева фигур.
    """

    id: str
    kind: SurfaceKind
    box: Box
    z: int
    source: SourceKind
    #: закрывает ли собой то, что лежит ниже
    opaque: bool = True
    #: можно ли по дизайну писать текст поверх
    text_safe: bool = False
    #: чем классифицировано — для диагностики и аудита
    note: str = ""

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING


def _has_text(element: etree._Element) -> bool:
    body = element.find(f"{{{P_NS}}}txBody")
    if body is None:
        return False
    return bool("".join(n.text or "" for n in body.iter(f"{{{A_NS}}}t")).strip())


def _is_placeholder(element: etree._Element) -> bool:
    return element.find(f".//{{{P_NS}}}ph") is not None


def _has_fill(element: etree._Element) -> bool:
    """Есть ли у фигуры собственная заливка (а не «без заливки»)."""
    props = element.find(f"{{{P_NS}}}spPr")
    if props is None:
        return False
    if props.find(f"{{{A_NS}}}noFill") is not None:
        return False
    return any(
        props.find(f"{{{A_NS}}}{tag}") is not None
        for tag in ("solidFill", "gradFill", "blipFill", "pattFill")
    )


def _has_outline(element: etree._Element) -> bool:
    """Есть ли у фигуры видимый контур."""
    props = element.find(f"{{{P_NS}}}spPr")
    if props is None:
        return False
    line = props.find(f"{{{A_NS}}}ln")
    if line is None:
        return False
    if line.find(f"{{{A_NS}}}noFill") is not None:
        return False
    return any(
        line.find(f"{{{A_NS}}}{tag}") is not None
        for tag in ("solidFill", "gradFill", "pattFill")
    )


def _invisible(element: etree._Element) -> bool:
    """Фигура без заливки, контура и текста: на листе её не видно.

    Такие рамки в шаблонах не редкость — экспорт из Google Slides оставляет
    габаритные прямоугольники вокруг рядов карточек. На `vk_tech` один такой
    занимал три четверти слайда, и по габаритам вёрстка объявляла запретной
    половину листа, а аудит заводил находку на каждый заголовок — при том,
    что на листе там пусто.
    """
    return not (_has_fill(element) or _has_outline(element) or _has_text(element))


def _image_blob(element: etree._Element, part) -> bytes | None:
    """Байты картинки фигуры `pic`, если до них можно дотянуться."""
    blip = element.find(f".//{{{A_NS}}}blip")
    if blip is None or part is None:
        return None
    rid = blip.get(f"{{{R_NS}}}embed")
    if not rid:
        return None
    try:
        return part.rels[rid].target_part.blob
    except Exception:  # часть могла не переехать — это не повод падать
        return None


#: Служебные места шаблона: их содержимое — оформление, а не наше содержание.
SERVICE_PLACEHOLDERS = frozenset({"ftr", "sldNum", "dt"})


def _placeholder_kind(element: etree._Element) -> str:
    node = element.find(f".//{{{P_NS}}}ph")
    return node.get("type", "body") if node is not None else "body"


def _classify(
    element: etree._Element,
    box: Box,
    z: int,
    slide_w: int,
    slide_h: int,
    part,
    lowest: bool,
    on_slide: bool = False,
) -> tuple[SurfaceKind, str]:
    """Вид поверхности по нескольким признакам сразу."""
    tag = etree.QName(element).localname
    area = box.w * box.h
    share = area / (slide_w * slide_h) if slide_w and slide_h else 0.0

    if _is_placeholder(element):
        # Колонтитул, номер слайда и дата — служебные места шаблона, а не
        # места под наше содержание: писать поверх них нельзя, убирать их
        # тоже (дизайнер поставил их намеренно).
        kind = _placeholder_kind(element)
        if kind in SERVICE_PLACEHOLDERS:
            return SurfaceKind.BRAND, f"служебное место шаблона ({kind})"
        return SurfaceKind.PLACEHOLDER, "заполнитель макета"

    if tag == "pic":
        # Содержание картинки решает раньше, чем её размер и место в стопке.
        # Фотография во весь слайд остаётся фотографией: текст по ней не
        # читается, сколько бы площади она ни занимала. Безопасным фоном
        # бывает ровно обратное — ровная заливка или текстура, в которой
        # нечего разглядывать.
        blob = _image_blob(element, part)
        if blob is not None:
            if is_photo(blob, box, slide_w, slide_h):
                return SurfaceKind.IMAGERY, "фотография"
            if is_illustration(blob, box, slide_w, slide_h):
                return SurfaceKind.IMAGERY, "иллюстрация"
        if max(box.w, box.h) <= ICON_EMU or share <= ICON_SHARE:
            return SurfaceKind.BRAND, "мелкая картинка: знак или логотип"
        if share >= BACKGROUND_SHARE and lowest:
            return SurfaceKind.BACKGROUND, f"ровный фон во весь слайд ({share:.0%})"
        return SurfaceKind.IMAGERY, "картинка без распознанного содержимого"

    if tag in ("sp", "cxnSp", "grpSp"):
        if _invisible(element):
            return SurfaceKind.PLACEHOLDER, "невидимая рамка"
        if min(box.w, box.h) <= THIN_EMU:
            return SurfaceKind.DECOR, "линия"
        if max(box.w, box.h) <= ICON_EMU:
            return SurfaceKind.DECOR, "значок"
        if share >= BACKGROUND_SHARE and lowest and _has_fill(element):
            return SurfaceKind.BACKGROUND, "заливка во весь слайд"
        if _has_text(element):
            if on_slide:
                # На слайде-образце фигура с собственным текстом — место
                # композиции, а не украшение: шаблон-бланк набран обычными
                # фигурами, и роли им даёт геометрия. Объявив их декором, мы
                # запретили бы вёрстке ровно те места, ради которых слайд и
                # разбирается.
                return SurfaceKind.PLACEHOLDER, "текстовое место слайда"
            # Фигура с собственным текстом — колонтитул или подпись шаблона.
            return SurfaceKind.BRAND, "фигура шаблона с текстом"
        if _has_fill(element):
            # Крупная залитая фигура без текста — подложка: шаблон кладёт на
            # неё свои заголовки. Запрещать её значит запрещать замысел.
            return SurfaceKind.PANEL, "залитая подложка"
        return SurfaceKind.DECOR, "фигура оформления"

    return SurfaceKind.DECOR, f"элемент {tag}"


def _same_place(a: Box, b: Box, tolerance: int = 9_144) -> bool:
    """Одно ли это место (с точностью до 0.01 дюйма)."""
    return (
        abs(a.x - b.x) <= tolerance
        and abs(a.y - b.y) <= tolerance
        and abs(a.w - b.w) <= tolerance
        and abs(a.h - b.h) <= tolerance
    )


def _shows_master(layout_root: etree._Element) -> bool:
    """Показывает ли макет фигуры образца. Умолчание OOXML — показывает."""
    return (layout_root.get("showMasterSp") or "1") not in ("0", "false")


def _shape_tree(root: etree._Element | None) -> etree._Element | None:
    """Дерево фигур внутри `p:cSld`. На вход можно давать корень части."""
    if root is None:
        return None
    if etree.QName(root).localname == "spTree":
        return root
    return root.find(f".//{{{P_NS}}}spTree")


def collect(
    layout_root: etree._Element,
    layout_part,
    master_root: etree._Element | None,
    master_part,
    slide_w: int,
    slide_h: int,
) -> list[Surface]:
    """Видимые элементы макета и образца в системе координат слайда.

    Образец идёт первым: он ниже по стопке. Фигура образца, повторённая
    макетом на том же месте, учитывается один раз — иначе унаследованное
    оформление удваивалось бы и съедало место дважды.
    """
    candidates: list[Surface] = []
    z = 0

    layout_tree = _shape_tree(layout_root)
    master_tree = _shape_tree(master_root)
    if layout_tree is None:
        return []

    sources: list[tuple[etree._Element, object, SourceKind]] = []
    if master_tree is not None and _shows_master(layout_root):
        sources.append((master_tree, master_part, SourceKind.MASTER))
    sources.append((layout_tree, layout_part, SourceKind.LAYOUT))

    for root, part, origin in sources:
        # Учитываются части, а не сама группа: иначе одно и то же место
        # считалось бы дважды — за родителя и за каждого ребёнка, — а габарит
        # группы вдобавок врал бы. Дизайнер группирует и композицию (пять
        # подписей `vk_education` одной группой 12×3,6″), и разрозненные
        # украшения по всему слайду (`vk_tech`, группа в три четверти листа):
        # в обоих случаях габарит объявлял бы запретной половину слайда,
        # хотя между частями пусто.
        shapes: list[tuple[etree._Element, Box]] = []
        for element, box, _ in iter_shapes(root):
            if box is None or etree.QName(element).localname == "grpSp":
                continue
            shapes.append((element, box))
        # «Нижний слой» считается внутри своего источника: первая фигура
        # образца лежит под всем, первая фигура макета — под остальным макетом.
        for index, (element, box) in enumerate(shapes):
            kind, note = _classify(
                element, box, z, slide_w, slide_h, part, lowest=(index == 0)
            )
            if kind is SurfaceKind.PLACEHOLDER:
                z += 1
                continue
            candidates.append(
                Surface(
                    id=f"{origin.value}-{index}",
                    kind=kind,
                    box=box,
                    z=z,
                    source=origin,
                    text_safe=kind in TEXT_SAFE,
                    note=note,
                )
            )
            z += 1

    # Одно место — один элемент, и это ВЕРХНИЙ: его и видно. Так решаются
    # сразу два случая. Первый — наследование: фигура образца, повторённая
    # макетом, не удваивается. Второй — перекрытие: на обложке `vk_tech` две
    # картинки во весь слайд, и нижняя фон, а верхняя кристалл. Оставив
    # нижнюю, мы объявили бы слайд свободным для текста, хотя поверх фона
    # лежит иллюстрация.
    found: list[Surface] = []
    for surface in sorted(candidates, key=lambda s: -s.z):
        if any(_same_place(surface.box, kept.box) for kept in found):
            continue
        found.append(surface)
    return sorted(found, key=lambda s: s.z)



def collect_tree(
    tree: etree._Element | None,
    part,
    slide_w: int,
    slide_h: int,
) -> list[Surface]:
    """Видимые элементы одного дерева фигур — слайда-примера.

    Тот же разбор, что для макета, но без наследования: у слайда его нет.
    Нужен потому, что оформление живёт не только в макетах. Есть шаблоны,
    где фотография во весь слайд лежит прямо на слайде-примере, и прежний
    отбор по площади (`bookends._decor_boxes`, `opener._decor`) отбрасывал её
    как «фон»: текст уходил с тёмной панели на светлое фото.
    """
    shapes = _shape_tree(tree)
    if shapes is None:
        return []
    found: list[Surface] = []
    candidates: list[Surface] = []
    items: list[tuple[etree._Element, Box]] = []
    for element, box, _ in iter_shapes(shapes):
        if box is None or etree.QName(element).localname == "grpSp":
            continue
        items.append((element, box))
    for index, (element, box) in enumerate(items):
        kind, note = _classify(
            element, box, index, slide_w, slide_h, part,
            lowest=(index == 0), on_slide=True,
        )
        if kind is SurfaceKind.PLACEHOLDER:
            continue
        candidates.append(
            Surface(
                id=f"slide-{index}",
                kind=kind,
                box=box,
                z=index,
                source=SourceKind.SLIDE,
                text_safe=kind in TEXT_SAFE,
                note=note,
            )
        )
    for surface in sorted(candidates, key=lambda item: -item.z):
        if any(_same_place(surface.box, kept.box) for kept in found):
            continue
        found.append(surface)
    return sorted(found, key=lambda item: item.z)


def obstacles(surfaces: list[Surface]) -> list[Box]:
    """Рамки, поверх которых текст класть нельзя."""
    return [s.box for s in surfaces if s.blocking]


def backdrops(surfaces: list[Surface]) -> list[Box]:
    """Рамки, поверх которых текст предусмотрен дизайном."""
    return [s.box for s in surfaces if s.text_safe]


def imagery(surfaces: list[Surface]) -> list[Box]:
    """Содержательные картинки: фотографии и иллюстрации.

    Отдельно от прочих препятствий, потому что к ним особое правило вёрстки:
    рамка, стоящая на картинке, не растёт. Донор держал её в одну строку не
    от скупости — на фотографии читается только короткая надпись.
    """
    return [s.box for s in surfaces if s.kind is SurfaceKind.IMAGERY]


def free_regions(area: Box, blocked: list[Box], minimum: int = 457_200) -> list[Box]:
    """Свободное место как набор областей, а не один прямоугольник.

    Сложный макет не сводится к одной рамке: картинка справа оставляет
    колонку слева, полоса сверху — область под ней. Возвращаются
    прямоугольники, полученные разрезами по краям препятствий; слишком узкие
    (уже `minimum`, по умолчанию полдюйма) отбрасываются — в них всё равно
    ничего не поставить.

    Алгоритм намеренно простой и предсказуемый: кандидаты строятся по сетке
    координат препятствий, каждый проверяется на пересечение. Максимального
    разбиения он не ищет, но даёт корректные непересекающиеся с декором
    области, а их выбор остаётся за вёрсткой.
    """
    inside = [b for b in blocked if b.intersection(area) is not None]
    if not inside:
        return [area]

    xs = sorted({area.x, area.right} | {
        v for b in inside for v in (max(b.x, area.x), min(b.right, area.right))
    })
    ys = sorted({area.y, area.bottom} | {
        v for b in inside for v in (max(b.y, area.y), min(b.bottom, area.bottom))
    })

    cells: list[Box] = []
    for left, right in pairwise(xs):
        for top, bottom in pairwise(ys):
            if right - left < minimum or bottom - top < minimum:
                continue
            cell = Box(x=left, y=top, w=right - left, h=bottom - top)
            if any(cell.intersection(b) is not None for b in inside):
                continue
            cells.append(cell)

    return _merge(cells)


def _merge(cells: list[Box]) -> list[Box]:
    """Склеивает соседние ячейки в вертикальные полосы, потом в строки."""
    if not cells:
        return []
    merged: list[Box] = []
    for cell in sorted(cells, key=lambda b: (b.x, b.y)):
        for i, kept in enumerate(merged):
            if kept.x == cell.x and kept.w == cell.w and kept.bottom == cell.y:
                merged[i] = Box(x=kept.x, y=kept.y, w=kept.w, h=kept.h + cell.h)
                break
        else:
            merged.append(cell)
    rows: list[Box] = []
    for cell in sorted(merged, key=lambda b: (b.y, b.x)):
        for i, kept in enumerate(rows):
            if kept.y == cell.y and kept.h == cell.h and kept.right == cell.x:
                rows[i] = Box(x=kept.x, y=kept.y, w=kept.w + cell.w, h=kept.h)
                break
        else:
            rows.append(cell)
    return sorted(rows, key=lambda b: (-b.w * b.h, b.y, b.x))


# Доля высоты слайда, которую считаем верхней и нижней полосой оформления.
# Логотипы, колонтитулы и декоративные линии живут именно там.
EDGE_BAND_SHARE = 0.18

# Доля площади, начиная с которой фигура считается рамкой во весь слайд, а не
# препятствием: у неё запрещаются только видимые полосы по краям, а не вся
# внутренняя область.
FRAME_SHARE = 0.6

# Наибольшая сторона фигуры, которую ещё можно считать частью логотипа,
# долей ширины слайда. Крупнее — это полоса или картинка, а не знак, и
# склеивать её с соседями нельзя.
LOGO_MAX_SIDE_SHARE = 0.25


def _grow(box: Box, pad: int, slide_w: int, slide_h: int) -> Box:
    """Та же рамка с отступом, обрезанная по слайду."""
    x = max(0, box.x - pad)
    y = max(0, box.y - pad)
    right = min(slide_w, box.right + pad)
    bottom = min(slide_h, box.bottom + pad)
    return Box(x=x, y=y, w=max(1, right - x), h=max(1, bottom - y))


def _edge_strips(box: Box, slide_w: int, slide_h: int) -> list[Box]:
    """Видимые полосы рамки во весь слайд, а не вся её внутренняя область.

    Декоративная рамка приходит одной фигурой с габаритами во весь лист.
    Запретить её целиком — значит запретить слайд. Запрещаются четыре
    полосы по краям: их толщина — та же, что у самой рамки, если она
    тонкая, иначе доля стороны.
    """
    thickness = max(THIN_EMU, min(box.w, box.h) // 20)
    return [
        Box(x=box.x, y=box.y, w=max(1, box.w), h=thickness),
        Box(x=box.x, y=max(0, box.bottom - thickness), w=max(1, box.w), h=thickness),
        Box(x=box.x, y=box.y, w=thickness, h=max(1, box.h)),
        Box(x=max(0, box.right - thickness), y=box.y, w=thickness, h=max(1, box.h)),
    ]


def _clustered(boxes: list[Box], gap: int) -> list[Box]:
    """Склеивает рядом стоящие рамки в одну: логотип бывает из нескольких фигур.

    Знак и слово рядом с ним — один логотип, и защищать их надо вместе,
    иначе текст сядет ровно в просвет между ними.
    """
    left = list(boxes)
    merged: list[Box] = []
    while left:
        current = left.pop()
        changed = True
        while changed:
            changed = False
            for other in list(left):
                near = _grow(current, gap, 10**9, 10**9)
                if near.intersection(other) is not None:
                    x = min(current.x, other.x)
                    y = min(current.y, other.y)
                    current = Box(
                        x=x,
                        y=y,
                        w=max(1, max(current.right, other.right) - x),
                        h=max(1, max(current.bottom, other.bottom) - y),
                    )
                    left.remove(other)
                    changed = True
        merged.append(current)
    return merged


def protected(
    surfaces: list[Surface], slide_w: int, slide_h: int, padding: int = 0
) -> list[Box]:
    """Зоны, которые текст не имеет права пересекать.

    Сюда входят логотипы и брендинг, декоративные полосы и линии, служебные
    места шаблона. Рамка во весь слайд разбирается на видимые полосы по
    краям: её внутренняя область остаётся свободной. Соседние фигуры в
    верхней и нижней полосе слайда склеиваются: логотип бывает собран из
    знака и слова, и защищать их надо как одно целое.

    `padding` — дополнительный отступ; задаётся конфигурацией и считается от
    размеров слайда, поэтому здесь приходит уже числом.
    """
    area = slide_w * slide_h
    top_band = slide_h * EDGE_BAND_SHARE
    bottom_band = slide_h * (1 - EDGE_BAND_SHARE)
    # Пустой список — пустой результат: `_clustered` не любит пустоты.

    plain: list[Box] = []
    top_edge: list[Box] = []
    bottom_edge: list[Box] = []
    for surface in surfaces:
        if not surface.blocking:
            continue
        box = surface.box
        # Рамкой считается только оформление: линии, контуры, полосы. Фото
        # и иллюстрация во весь слайд рамкой не становятся — по ним текст
        # не читается нигде, а не только по краям.
        if (
            surface.kind is not SurfaceKind.IMAGERY
            and area
            and (box.w * box.h) / area >= FRAME_SHARE
        ):
            plain.extend(_edge_strips(box, slide_w, slide_h))
            continue
        # Склеиваются только компактные фигуры у краёв: логотип — это знак
        # и слово рядом, оба небольшие. Крупная полоса оформления соседу не
        # родня, и склеивать её значит запирать пол-слайда.
        compact = max(box.w, box.h) <= slide_w * LOGO_MAX_SIDE_SHARE
        if compact and box.bottom <= top_band:
            top_edge.append(box)
        elif compact and box.y >= bottom_band:
            bottom_edge.append(box)
        else:
            plain.append(box)

    gap = max(padding, slide_h // 100)
    joined = _clustered(top_edge, gap) + _clustered(bottom_edge, gap)
    return [_grow(box, padding, slide_w, slide_h) for box in plain + joined]
