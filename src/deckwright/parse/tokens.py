"""Токены дизайн-системы, добытые статистикой употребления.

Тема шаблона не обязана описывать его дизайн-систему. У `vk_workspace` в
`clrScheme` лежит стоковая офисная палитра (4472C4, ED7D31, A5A5A5), не имеющая
отношения к оформлению колоды: реальный брендовый цвет `0077FF` встречается на
слайдах 153 раза и в теме не упомянут вовсе. У всех трёх шаблонов датасета
`fontScheme` объявляет `Arial`, при том что фактически везде набрано шрифтом
`Play`.

Поэтому палитра и гарнитуры считаются по фактическому употреблению, а тема
служит двум другим целям: словарём для резолва `schemeClr` в RGB и слабым
приором, когда статистики не хватает.

Вес цвета — не число упоминаний, а закрашенная площадь: заливка подложки во
весь слайд важнее для впечатления, чем десять мелких обводок. Цвет текста
считается отдельно: его площадь мала, а значение велико.
"""

from __future__ import annotations

import colorsys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.schemas import (
    Box,
    Color,
    ColorToken,
    FontToken,
    Grid,
    Provenance,
    SourceKind,
    hardest_ground,
)

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"

# Роль, в которой встретился цвет. Разделение нужно вёрстке: цвет заливки и
# цвет текста не взаимозаменяемы, даже если совпали по значению.
ROLE_FILL = "fill"
ROLE_TEXT = "text"
ROLE_LINE = "line"

# Кегли ближе этого числа пунктов считаются одной ступенью шкалы: редакторы
# дают дробные значения после масштабирования (8.12, 14.06), и каждое такое
# значение отдельной ступенью превращает шкалу в мусор.
SCALE_TOLERANCE_PT = 0.75

# Ступень, встретившаяся реже этой доли, в шкалу не попадает: случайно
# изменённый кегль одного блока не является правилом шаблона.
SCALE_MIN_SHARE = 0.01

# Рёбра ближе этого расстояния считаются одной направляющей (0.02 дюйма).
EDGE_TOLERANCE_EMU = 18_288

# Направляющая, у которой меньше стольких фигур, не является сеткой.
MIN_EDGE_SUPPORT = 3

# Фигура, вплотную прижатая к краю слайда, — декор, а не контент: полосы,
# подложки, фоновые паттерны. Их рёбра к полям отношения не имеют.
EDGE_HUG_EMU = 9_144


@dataclass
class Usage:
    """Накопленная статистика по шаблону."""

    colors: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    color_counts: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    # Гарнитуры со слайдов и гарнитуры, объявленные в мастерах и layout'ах,
    # считаются раздельно. Мастер объявляет умолчание, которым может не быть
    # набрано ни строчки: у `vk_tech` `Calibri` объявлен щедро, а видно на
    # слайдах `Play`. Смешивать их — значит выбрать гарнитурой шаблона ту,
    # которой в нём не написано ничего.
    fonts: Counter[str] = field(default_factory=Counter)
    fonts_declared: Counter[str] = field(default_factory=Counter)
    sizes: Counter[float] = field(default_factory=Counter)
    left_edges: list[int] = field(default_factory=list)
    right_edges: list[int] = field(default_factory=list)
    top_edges: list[int] = field(default_factory=list)
    bottom_edges: list[int] = field(default_factory=list)


def theme_colors(theme: etree._Element | None) -> dict[str, str]:
    """{слот темы: RRGGBB}. Словарь для резолва `schemeClr`."""
    if theme is None:
        return {}
    scheme = theme.find(f".//{{{A_NS}}}clrScheme")
    if scheme is None:
        return {}
    resolved: dict[str, str] = {}
    for entry in scheme:
        srgb = entry.find(f"{{{A_NS}}}srgbClr")
        sys_clr = entry.find(f"{{{A_NS}}}sysClr")
        value = (
            srgb.get("val")
            if srgb is not None
            else (sys_clr.get("lastClr") if sys_clr is not None else None)
        )
        if value:
            resolved[etree.QName(entry).localname] = value.upper()
    return resolved


def color_map(master_element: etree._Element) -> dict[str, str]:
    """`p:clrMap` мастера: `bg1`/`tx1` → слот темы.

    Фигуры ссылаются на цвета через `bg1`, `tx1`, `bg2`, `tx2`, а те через
    карту мастера указывают на `lt1`, `dk1`. Без этого шага `schemeClr
    val="bg1"` не резолвится ни во что.
    """
    clr_map = master_element.find(f"{{{P_NS}}}clrMap")
    return dict(clr_map.attrib) if clr_map is not None else {}


def resolve_color(
    node: etree._Element, theme: dict[str, str], clr_map: dict[str, str]
) -> Color | None:
    """Первый цвет внутри узла, приведённый к RGB.

    С модификаторами яркости: `accent4` с `lumMod 50%` — это тёмно-зелёная
    карточка holdout, а без модификатора выходил бирюзовый, по которому
    чёрный текст проходил порог контраста и садился на тёмное.
    """
    srgb = node.find(f".//{{{A_NS}}}srgbClr")
    if srgb is not None and srgb.get("val"):
        return _modified(srgb.get("val"), srgb, None)
    scheme = node.find(f".//{{{A_NS}}}schemeClr")
    if scheme is not None and scheme.get("val"):
        name = scheme.get("val")
        slot = clr_map.get(name, name)
        value = theme.get(slot)
        if value:
            return _modified(value, scheme, slot)
    return None


R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def own_background(slide_element: etree._Element) -> etree._Element | None:
    """Собственный фон слайда (`p:cSld/p:bg`) или `None`, если он наследуется.

    Бланк задаёт фон на каждом слайде, а у мастера оставляет белый: такой
    фон живёт только на слайде и при сборке по донору сам не приезжает.
    """
    csld = slide_element.find(f"{{{P_NS}}}cSld")
    return csld.find(f"{{{P_NS}}}bg") if csld is not None else None


def background_signature(bg: etree._Element | None, part) -> str | None:
    """Отпечаток фона: разметка заливки, где ссылка на картинку заменена её
    содержимым. У донора и у собранного слайда `rId` разные, а картинка одна.
    """
    if bg is None:
        return None
    import hashlib

    node = etree.fromstring(etree.tostring(bg))
    for child in node.iter():
        for attr in ("embed", "link", "id"):
            rid = child.get(f"{{{R_NS}}}{attr}")
            if not rid:
                continue
            try:
                blob = part.related_part(rid).blob
            except (KeyError, AttributeError):
                blob = rid.encode()
            child.set(f"{{{R_NS}}}{attr}", hashlib.sha1(blob).hexdigest()[:12])
    # Исключающая канонизация: объявления пространств имён, которые разметка
    # не использует (`p14`, `mc` у слайда шаблона), в отпечаток не входят —
    # иначе перенесённая один в один жёлтая заливка экзаменов считалась другой.
    canonical = etree.tostring(node, method="c14n", exclusive=True)
    return hashlib.sha1(canonical).hexdigest()[:16]


_LUMINANCE_MODS = ("lumMod", "lumOff", "shade", "tint")


def _modified(rgb: str, node: etree._Element, scheme: str | None) -> Color:
    """Цвет с модификаторами OOXML: яркость (`lumMod`, `lumOff`, `shade`,
    `tint`) и прозрачность (`alpha`).

    Изменённый цвет теряет ссылку на тему: иначе рендер записал бы цвет
    темы без модификатора.
    """
    alpha = next(
        (
            int(child.get("val", "100000")) / 100_000
            for child in node
            if etree.QName(child).localname == "alpha"
        ),
        1.0,
    )
    mods = {
        etree.QName(child).localname: int(child.get("val", "100000")) / 100_000
        for child in node
        if etree.QName(child).localname in _LUMINANCE_MODS
    }
    if not mods:
        return Color(rgb=rgb, scheme=scheme, alpha=alpha)
    red, green, blue = (int(rgb[i : i + 2], 16) / 255 for i in (0, 2, 4))
    if "shade" in mods:
        red, green, blue = (c * mods["shade"] for c in (red, green, blue))
    if "tint" in mods:
        red, green, blue = (1 - (1 - c) * mods["tint"] for c in (red, green, blue))
    if "lumMod" in mods or "lumOff" in mods:
        hue, light, sat = colorsys.rgb_to_hls(red, green, blue)
        light = light * mods.get("lumMod", 1.0) + mods.get("lumOff", 0.0)
        red, green, blue = colorsys.hls_to_rgb(hue, min(1.0, max(0.0, light)), sat)
    value = "".join(f"{round(min(1.0, max(0.0, c)) * 255):02X}" for c in (red, green, blue))
    return Color(rgb=value, alpha=alpha)


def backdrop_color(
    container: etree._Element,
    slide_w: int,
    slide_h: int,
    theme: dict[str, str],
    clr_map: dict[str, str],
) -> Color | None:
    """Цвет фоновой подложки — фигуры, закрывающей почти весь слайд.

    Фон объявляют двумя способами: элементом `p:bg` или просто прямоугольником
    во весь слайд. Второй способ распространён не меньше первого, а в `p:bg`
    при этом не попадает ничего.

    Разница не косметическая. От того, тёмный фон или светлый, зависит цвет
    текста; шаблон, у которого фон нарисован фигурой, при проверке только
    `p:bg` считается светлым, и по чёрному фону пишется чёрным. Ровно это
    вскрыл синтетический тёмный holdout: `vk_workspace` из датасета объявляет
    фон через `p:bg`, и дыра не была видна.
    """
    slide_area = slide_w * slide_h
    best: tuple[int, Color] | None = None
    for element, box, _ in iter_shapes(container):
        if box is None:
            continue
        covers = box.w >= slide_w * 0.9 and box.h >= slide_h * 0.9
        if not covers:
            continue
        fill = element.find(f".//{{{A_NS}}}solidFill")
        if fill is None:
            continue
        color = resolve_color(fill, theme, clr_map)
        if color is None:
            continue
        # Из нескольких подложек берём самую крупную: она внизу стопки и
        # задаёт общий тон.
        if best is None or box.area > best[0]:
            best = (min(box.area, slide_area), color)
    return best[1] if best else None


# Порог контраста Приложения 1 — тот же, что у вёрстки и аудита. Здесь он
# нужен, чтобы ответить на один вопрос: спорят ли две заливки под одной
# рамкой, то есть есть ли цвет, читаемый на обеих.
# Доля рамки, начиная с которой заливка считается фоном её текста, а не
# декором, задевшим рамку углом.
GROUND_SHARE = 0.12
def fill_colors(
    element: etree._Element, theme: dict[str, str], clr_map: dict[str, str]
) -> list[Color]:
    """Цвета заливки фигуры: у сплошной один, у градиента — все опорные.

    Градиент читается не для красоты. Карточки `vk_workspace` залиты
    `00AEE8`→`0077FF`, и пока здесь искался только `a:solidFill`, подложка
    выходила `None`: цвет текста выбирался по чёрному фону слайда, и по
    яркому голубому писалось белым — 2.8:1 в отрисованном PDF. По любому
    из опорных цветов этого градиента читается чёрный (5.1:1 и 8.1:1).

    Опорные цвета — не весь градиент: между ними лежат промежуточные. Но
    у линейного перехода яркость монотонна между соседними опорами, так
    что крайние случаи — именно опоры, и решение по ним верно для всей
    заливки.
    """
    spr = element.find(f"{{{P_NS}}}spPr")
    if spr is None:
        return []
    solid = spr.find(f"{{{A_NS}}}solidFill")
    if solid is not None:
        color = resolve_color(solid, theme, clr_map)
        return [color] if color is not None else []
    gradient = spr.find(f"{{{A_NS}}}gradFill")
    if gradient is None:
        return []
    stops = []
    for stop in gradient.findall(f"{{{A_NS}}}gsLst/{{{A_NS}}}gs"):
        color = resolve_color(stop, theme, clr_map)
        if color is not None:
            stops.append(color)
    return stops


def ground_regions(
    container: etree._Element,
    box: Box,
    theme: dict[str, str],
    clr_map: dict[str, str],
    base: Color | None = None,
    min_share: float = GROUND_SHARE,
) -> list[tuple[Box, list[Color], int]]:
    """Заливки, видимые под рамкой, вместе с их областями.

    Вместе с порядком отрисовки: что нарисовано позже, то и видно. В
    отличие от `local_backdrop` сюда попадают и заливки, накрывающие рамку
    лишь частью: шапка карточки, полоса под заголовком, плашка в углу.
    Именно они делают фон неоднородным, и по ним решается, не лежит ли
    рамка сразу на двух фонах, требующих разного цвета текста.
    """
    found: list[tuple[int, Box, list[Color], int]] = []
    for order, (element, shape_box, _) in enumerate(iter_shapes(container)):
        if shape_box is None or shape_box.area <= 0:
            continue
        inside = shape_box.intersection(box)
        if inside is None or inside.area < min_share * box.area:
            continue
        colors = fill_colors(element, theme, clr_map)
        if not colors:
            continue
        if base is not None:
            colors = [
                color if color.alpha >= 1.0 else _over(color, base)
                for color in colors
            ]
        elif any(color.alpha < 1.0 for color in colors):
            continue
        found.append((shape_box.area, shape_box, colors, order))
    # От крупной к тесной: ближе к тексту лежит самая тесная фигура.
    found.sort(key=lambda item: -item[0])
    return [(shape_box, colors, order) for _, shape_box, colors, order in found]


def local_backdrop(
    container: etree._Element,
    box: Box,
    theme: dict[str, str],
    clr_map: dict[str, str],
    base: Color | None = None,
) -> Color | None:
    """Цвет, который виден под рамкой: залитые фигуры под ней поверх фона.

    Берётся заливка фигуры (`p:spPr/a:solidFill`), а не цвет текста в ней.
    Ближе всего к тексту лежит самая тесная фигура — карточка, а не подложка
    слайда под ней; непрозрачная, она и есть ответ. Полупрозрачная — нет:
    карточка `vk_workspace` — это `#0077FF` с прозрачностью 70% на чёрном,
    то есть тёмно-синяя, и по ярко-синему под неё выбирался тёмный текст.
    Такие заливки накладываются по порядку, от самой крупной фигуры к самой
    тесной, поверх `base` — фона слайда. Фон неизвестен — неизвестен и цвет.

    Градиент читается по опорным цветам (`fill_colors`) и сводится к тому
    из них, на котором текст читается хуже всего. Картинку так не
    распознать: это задача растра.
    """
    layers: list[tuple[int, list[Color]]] = []
    for element, shape_box, _ in iter_shapes(container):
        if shape_box is None or shape_box.area <= 0:
            continue
        inside_w = min(shape_box.right, box.right) - max(shape_box.x, box.x)
        inside_h = min(shape_box.bottom, box.bottom) - max(shape_box.y, box.y)
        if inside_w <= 0 or inside_h <= 0 or inside_w * inside_h < 0.9 * box.area:
            continue
        colors = fill_colors(element, theme, clr_map)
        if colors:
            layers.append((shape_box.area, colors))
    if not layers:
        return None
    seen: list[Color] = [base] if base is not None else []
    for _, colors in sorted(layers, key=lambda layer: -layer[0]):
        if all(color.alpha >= 1.0 for color in colors):
            seen = colors
        elif seen:
            seen = [
                color if color.alpha >= 1.0 else _over(color, under)
                for color in colors
                for under in seen
            ]
    # Из нескольких цветов одной заливки — тот, на котором тексту тяжелее:
    # порог обязан держаться на всём градиенте, а не в его середине.
    return hardest_ground(seen)


def _over(top: Color, bottom: Color) -> Color:
    """Полупрозрачный цвет поверх непрозрачного."""
    channels = []
    for i in (0, 2, 4):
        upper, lower = int(top.rgb[i : i + 2], 16), int(bottom.rgb[i : i + 2], 16)
        channels.append(f"{round(top.alpha * upper + (1 - top.alpha) * lower):02X}")
    return Color(rgb="".join(channels))


def _collect_colors(
    element: etree._Element,
    box: Box | None,
    usage: Usage,
    theme: dict[str, str],
    clr_map: dict[str, str],
) -> None:
    area = box.area if box is not None else 0

    for fill in element.findall(f".//{{{A_NS}}}solidFill"):
        parent = fill.getparent()
        parent_tag = etree.QName(parent).localname if parent is not None else ""
        if parent_tag in ("rPr", "defRPr", "endParaRPr"):
            role = ROLE_TEXT
        elif parent_tag == "ln":
            role = ROLE_LINE
        else:
            role = ROLE_FILL
        color = resolve_color(fill, theme, clr_map)
        if color is None:
            continue
        key = (color.rgb, role)
        usage.color_counts[key] += 1
        # Цвет текста взвешивается упоминаниями, а не площадью: площадь букв
        # мала, а значение цвета для облика шаблона велико.
        usage.colors[key] += area if role == ROLE_FILL else 0


# Ссылки на шрифты темы. Текст, набранный ими, не называет гарнитуру прямо.
THEME_FONT_REFS = {"+mj-lt": "major", "+mn-lt": "minor", "+mj-ea": "major", "+mn-ea": "minor"}


def resolve_typeface(face: str | None, theme_fonts: dict[str, str]) -> str | None:
    """Гарнитура с разрешённой ссылкой на тему. None — нечего разрешать.

    `+mj-lt` и `+mn-lt` — не имена шрифтов, а ссылки на заголовочную и
    основную гарнитуру темы. Оставить их как есть значит записать в файл
    несуществующее семейство и мерить текст неизвестно чем.
    """
    if not face:
        return None
    if face.startswith("+"):
        return theme_fonts.get(THEME_FONT_REFS.get(face, "")) or None
    return face


def _collect_typography(
    element: etree._Element,
    usage: Usage,
    on_slide: bool,
    theme_fonts: dict[str, str] | None = None,
) -> None:
    """Считает гарнитуры, разрешая ссылки на шрифты темы.

    Шаблон может не называть гарнитуру ни разу: весь текст ссылается на тему
    через `+mn-lt`, и тогда тема — единственный источник правды. Это зеркало
    случая с датасетом, где тема, наоборот, лгала. Без разрешения ссылок у
    такого шаблона гарнитуры оказываются с нулевым употреблением, и выбрать
    между ними не по чему.
    """
    counter = usage.fonts if on_slide else usage.fonts_declared
    theme_fonts = theme_fonts or {}
    for latin in element.findall(f".//{{{A_NS}}}latin"):
        family = latin.get("typeface")
        if not family:
            continue
        if family.startswith("+"):
            resolved = theme_fonts.get(THEME_FONT_REFS.get(family, ""))
            if resolved:
                counter[resolved] += 1
            continue
        counter[family] += 1
    for node in element.iter():
        tag = etree.QName(node).localname
        if tag in ("rPr", "defRPr") and node.get("sz"):
            try:
                usage.sizes[round(int(node.get("sz")) / 100, 2)] += 1
            except ValueError:
                continue


def _has_text(element: etree._Element) -> bool:
    return any(
        (node.text or "").strip()
        for node in element.findall(f".//{{{A_NS}}}t")
    )


def _collect_edges(
    element: etree._Element, box: Box | None, slide_w: int, slide_h: int, usage: Usage
) -> None:
    """Рёбра контентных фигур — исходные данные для полей и сетки.

    Учитываются только фигуры с текстом. Декор полям не свидетель: полосы,
    подложки и фоновые паттерны прижаты к краям слайда, и их рёбра тянут
    поля к нулю. На `vk_workspace` это давало верхнее поле 0.00 дюйма и
    нижнее 7.51 при высоте слайда 7.5.

    Фигура, прижатая к краю, пропускается и по каждой стороне отдельно:
    текстовый блок может упираться в правый край, оставаясь содержательным
    слева.
    """
    if box is None or not _has_text(element):
        return
    if box.x > EDGE_HUG_EMU:
        usage.left_edges.append(box.x)
    if slide_w - box.right > EDGE_HUG_EMU:
        usage.right_edges.append(slide_w - box.right)
    if box.y > EDGE_HUG_EMU:
        usage.top_edges.append(box.y)
    if slide_h - box.bottom > EDGE_HUG_EMU:
        usage.bottom_edges.append(slide_h - box.bottom)


def collect(
    containers: list[etree._Element],
    theme: dict[str, str],
    clr_map: dict[str, str],
    slide_w: int,
    slide_h: int,
    usage: Usage | None = None,
    on_slide: bool = True,
    theme_fonts: dict[str, str] | None = None,
) -> Usage:
    """Обходит деревья фигур и накапливает статистику.

    `on_slide` различает источник: слайд показывает, чем набрано на самом
    деле, мастер и layout — чем объявлено набирать.
    """
    usage = usage or Usage()
    for container in containers:
        for element, box, _ in iter_shapes(container):
            _collect_colors(element, box, usage, theme, clr_map)
            _collect_typography(element, usage, on_slide, theme_fonts)
            _collect_edges(element, box, slide_w, slide_h, usage)
    return usage


def build_palette(usage: Usage, theme: dict[str, str]) -> list[ColorToken]:
    """Палитра, упорядоченная по значимости цвета в шаблоне.

    Значимость — закрашенная площадь для заливок и число употреблений для
    текста и линий. Цвета, объявленные темой, помечаются её слотом: рендереру
    правильнее записать такой цвет обратно как `schemeClr`, чтобы он продолжил
    жить по правилам шаблона, а не застыл константой.
    """
    by_rgb: dict[str, dict[str, int]] = defaultdict(lambda: {"area": 0, "count": 0})
    roles: dict[str, set[str]] = defaultdict(set)
    for (rgb, role), area in usage.colors.items():
        by_rgb[rgb]["area"] += area
        by_rgb[rgb]["count"] += usage.color_counts[(rgb, role)]
        roles[rgb].add(role)

    theme_slots = {value: slot for slot, value in theme.items()}

    tokens: list[ColorToken] = []
    for rgb, stats in by_rgb.items():
        slot = theme_slots.get(rgb)
        tokens.append(
            ColorToken(
                color=Color(rgb=rgb, scheme=slot),
                usage_count=stats["count"],
                painted_area_emu=stats["area"],
                roles=sorted(roles[rgb]),
                provenance=Provenance(
                    kind=SourceKind.SLIDE if slot is None else SourceKind.THEME,
                    ref="статистика употребления",
                    note=(
                        "цвет объявлен темой и подтверждён употреблением"
                        if slot
                        else "цвет употребляется, но в теме не объявлен"
                    ),
                ),
            )
        )
    # Сначала то, что красит больше площади; при равной площади — что чаще.
    tokens.sort(key=lambda t: (t.painted_area_emu, t.usage_count), reverse=True)
    return tokens


def build_fonts(usage: Usage, theme_families: list[str]) -> list[FontToken]:
    """Гарнитуры шаблона в порядке значимости.

    Первыми идут те, которыми набрано на слайдах: их видит читатель. Дальше
    объявленные в мастерах и layout'ах, но не употреблённые, и в самом конце
    объявленные темой. Без такого порядка гарнитурой шаблона становится
    умолчание мастера — у всех трёх шаблонов датасета это `Arial` или
    `Calibri`, при том что набрано везде шрифтом `Play`.
    """
    tokens = [
        FontToken(family=family, usage_count=count)
        for family, count in usage.fonts.most_common()
    ]
    known = {token.family for token in tokens}
    for family, count in usage.fonts_declared.most_common():
        if family not in known:
            tokens.append(FontToken(family=family, usage_count=count))
            known.add(family)
    tokens.extend(
        FontToken(family=family, usage_count=0)
        for family in theme_families
        if family not in known
    )
    return tokens


def build_type_scale(usage: Usage) -> list[float]:
    """Типографическая шкала кластеризацией фактических кеглей.

    Редакторы оставляют дробные значения после масштабирования (8.12, 14.06),
    и без слияния близких ступеней шкала превращается в перечень всех
    встреченных чисел. А уменьшать кегль при переполнении можно только по
    шкале шаблона — значит шкала обязана быть короткой и осмысленной.
    """
    if not usage.sizes:
        return []
    total = sum(usage.sizes.values())
    ordered = sorted(usage.sizes.items())

    clusters: list[list[tuple[float, int]]] = []
    for size, count in ordered:
        if clusters and size - clusters[-1][-1][0] <= SCALE_TOLERANCE_PT:
            clusters[-1].append((size, count))
        else:
            clusters.append([(size, count)])

    scale: list[float] = []
    for cluster in clusters:
        weight = sum(count for _, count in cluster)
        if weight / total < SCALE_MIN_SHARE:
            continue
        # Представитель ступени — самый употребительный кегль в ней.
        scale.append(max(cluster, key=lambda item: item[1])[0])
    return sorted(set(scale))


def _dominant_edge(edges: list[int]) -> int:
    """Первая направляющая, на которой стоит достаточно фигур.

    Поле шаблона — **самая близкая к краю** направляющая, которую держит
    заметное число блоков, а не самая населённая вообще. Внутри колоды почти
    всегда есть вертикаль, у которой блоков больше, чем у внешнего поля
    (вторая колонка, отступ списка), и выбор по населённости уводит поле
    вглубь слайда: на `vk_tech` это давало левое поле 4.69 дюйма.
    """
    if not edges:
        return 0
    ordered = sorted(edges)
    clusters: list[list[int]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - clusters[-1][-1] <= EDGE_TOLERANCE_EMU:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    for cluster in clusters:
        if len(cluster) >= MIN_EDGE_SUPPORT:
            return round(sum(cluster) / len(cluster))
    return 0


def build_grid(usage: Usage) -> Grid | None:
    """Поля, выведенные кластеризацией рёбер контентных фигур.

    Поле — не самое левое ребро, а то, на котором фигур больше всего:
    единственный блок, выехавший к краю, полем шаблона не является.
    """
    left = _dominant_edge(usage.left_edges)
    right = _dominant_edge(usage.right_edges)
    top = _dominant_edge(usage.top_edges)
    bottom = _dominant_edge(usage.bottom_edges)
    if not any((left, right, top, bottom)):
        return None
    return Grid(
        margin_left_emu=max(0, left),
        margin_right_emu=max(0, right),
        margin_top_emu=max(0, top),
        margin_bottom_emu=max(0, bottom),
        provenance=Provenance(
            kind=SourceKind.DERIVED,
            ref="кластеризация рёбер контентных фигур",
            confidence=0.8,
            note="поле определено по наиболее поддержанной направляющей",
        ),
    )
