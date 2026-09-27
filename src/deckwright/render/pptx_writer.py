"""Сборка `.pptx` из `SlideIR` на шаблоне-доноре.

Колода строится не с нуля, а поверх самого шаблона: открываем его, снимаем
слайды-примеры, добавляем новые на его же layout'ах. Всё оформление — мастер,
тема, декор layout'ов, встроенные шрифты, колонтитулы — остаётся на месте,
потому что это те же самые части пакета.

**Композиция не рисуется заново, а клонируется.** Если вёрстка выбрала
паттерн, его донорский слайд копируется целиком — карточки, коннекторы,
картинки, декор, — и текст подставляется **в те самые фигуры**, где он стоял у
дизайнера. Нарисованное заново было бы «похоже на шаблон», а это не то же
самое: у карточки есть заливка, скругление, тень и отступы, которых в `SlideIR`
нет и быть не должно.

Отсюда же берётся и положение текста. Скелетная версия клала заголовок в
плейсхолдер layout'а, и он садился не туда, где стоит в `SlideIR`: у паттерна
своя заголовочная рамка, у layout'а своя. Теперь целевая фигура ищется по
рамке — она у слота и у фигуры донора одна и та же, потому что слот с неё и
снят.

Все объекты нативные. Растеризация слайда запрещена ТЗ, и её здесь нет.
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

from deckwright.parse.geometry import iter_shapes
from deckwright.parse.patterns import is_figure_text
from deckwright.parse.tokens import own_background
from deckwright.render.clone import clone_background, clone_shape, purge_slides
from deckwright.schemas import (
    Align,
    Box,
    Color,
    DeckIR,
    Element,
    ElementKind,
    SlotRole,
    TemplateSpec,
    TextStyle,
    VAlign,
)
from deckwright.visuals.charts import add_chart
from deckwright.visuals.tables import add_table

_ANCHOR = {
    VAlign.TOP: MSO_ANCHOR.TOP,
    VAlign.MIDDLE: MSO_ANCHOR.MIDDLE,
    VAlign.BOTTOM: MSO_ANCHOR.BOTTOM,
}

_ALIGN = {
    Align.LEFT: PP_ALIGN.LEFT,
    Align.CENTER: PP_ALIGN.CENTER,
    Align.RIGHT: PP_ALIGN.RIGHT,
    Align.JUSTIFY: PP_ALIGN.JUSTIFY,
}


class RenderError(RuntimeError):
    """Колоду нельзя собрать по этому `SlideIR`."""


def _layout_index(spec: TemplateSpec, layout_id: str | None) -> tuple[int, int]:
    """(индекс мастера, индекс layout'а) по идентификатору из `TemplateSpec`.

    Идентификатор собран парсером как `masterN/layoutM`, поэтому разбор здесь —
    обратная операция к тому, что делает `parse.opener`, а не догадка.
    """
    if not layout_id:
        return 0, 0
    try:
        master_part, layout_part = layout_id.split("/", 1)
        return int(master_part.removeprefix("master")) - 1, int(
            layout_part.removeprefix("layout")
        ) - 1
    except (ValueError, IndexError) as exc:
        raise RenderError(f"не разобрать идентификатор layout'а {layout_id!r}") from exc


def _apply_style(run, style: TextStyle) -> None:
    run.font.name = style.font_family
    run.font.size = Pt(style.size_pt)
    run.font.bold = style.bold
    run.font.italic = style.italic
    run.font.color.rgb = RGBColor.from_string(style.color.rgb)


def _fill_text_frame(text_frame, element: Element, *, styled: bool) -> None:
    """Заполняет текстовый фрейм абзацами элемента.

    `styled=False` для плейсхолдеров: там кегль, гарнитура и цвет приходят с
    layout'а, и перебивать их своими значениями означало бы выкинуть ровно ту
    типографику шаблона, ради которой шаблон и взяли.
    """
    if element.text is None:
        raise RenderError(f"элемент {element.id!r} без текста попал в текстовый фрейм")

    text_frame.clear()
    for index, paragraph in enumerate(element.text.paragraphs):
        target = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        target.alignment = _ALIGN.get(paragraph.style.align)
        target.level = paragraph.level
        run = target.add_run()
        run.text = paragraph.text
        if styled:
            _apply_style(run, paragraph.style)
            # Интервал — одинарный, явно. Иначе абзац наследует интервал
            # донора, подобранный под его текст: на `vk_tech` это 16 % под
            # цифру кеглем 166 pt, и наш текст кеглем 32 pt ложился строка на
            # строку. Одинарный интервал — это 1.2 кегля, ровно то, чем
            # фиттер мерит «влезает»: картинка обязана совпадать с моделью.
            target.line_spacing = 1.0


def _placeholder_at(slide, box: Box):
    """Плейсхолдер слайда, стоящий ровно в этой рамке — своей или унаследованной.

    Рамку плейсхолдер часто не держит сам, а наследует с layout'а: так
    устроены обложки и финалы шаблонов, и по списку фигур с рамками их не
    найти.
    """
    for shape in slide.placeholders:
        ph = shape._element.xpath(".//*[local-name()='ph']")
        if ph and ph[0].get("type", "body") in _TEMPLATE_PLACEHOLDERS:
            continue
        if not shape.has_text_frame:
            continue
        own = _shape_box_of(shape)
        if _same_box(own, box):
            return shape
        # Рамка, выросшая вниз под наш текст (подзаголовок обложки): тот же
        # плейсхолдер, и рамка ему задаётся явно.
        if _same_box(own, box.model_copy(update={"h": own.h})) and box.h > own.h:
            shape.left, shape.top, shape.width, shape.height = box.x, box.y, box.w, box.h
            return shape
    return None


def _keep_fitted_size(text_frame, element: Element) -> None:
    """Кегль плейсхолдера — шаблонный, если фиттеру не пришлось его уменьшать.

    Уменьшил — значит, шаблонным кеглем текст не влезает, и кегль фиттера
    записывается явно; всё остальное оформление остаётся шаблонным.
    """
    if element.text is None or not element.text.scale_steps_down:
        return
    size = Pt(element.text.paragraphs[0].style.size_pt)
    for paragraph in text_frame.paragraphs:
        # Интервал — тот, которым мерил фиттер, как и у прочего нашего текста.
        paragraph.line_spacing = 1.0
        for run in paragraph.runs:
            run.font.size = size


# Насколько рамка клона может разойтись с рамкой слота, чтобы всё ещё считаться
# той же самой. Ноль не годится: обе стороны считаются в EMU через дробное
# масштабирование групп, и округление расходится на единицы.
_BOX_TOLERANCE_EMU = 9_144  # 0.01 дюйма

# Запас при отнесении фигуры к полосе повторителя: шаг умножается на номер
# элемента, и округление копится от строки к строке.
_BAND_TOLERANCE_EMU = 45_720  # 0.05 дюйма


def _same_box(a: Box, b: Box) -> bool:
    return (
        abs(a.x - b.x) <= _BOX_TOLERANCE_EMU
        and abs(a.y - b.y) <= _BOX_TOLERANCE_EMU
        and abs(a.w - b.w) <= _BOX_TOLERANCE_EMU
        and abs(a.h - b.h) <= _BOX_TOLERANCE_EMU
    )


def _all_shapes(shapes):
    """Фигуры с раскрытыми группами, на любую глубину."""
    for shape in shapes:
        yield shape
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            yield from _all_shapes(shape.shapes)


def _text_shapes(slide) -> list[tuple[Box, object]]:
    """Текстовые фигуры слайда с их абсолютными рамками.

    Абсолютными — потому что фигура внутри группы держит координаты в системе
    группы, а слот вёрстки несёт рамку, посчитанную по слайду. Сравнивать
    можно только приведённые к одной системе.
    """
    found: list[tuple[Box, object]] = []
    tree = slide.shapes._spTree
    # Фигуры внутри групп — тоже. Раньше брался только верхний уровень, и
    # текст в группе не заполнялся и не очищался: подсказка дизайнера
    # («Опишите преимущества…» на `vk_education`) доезжала до колоды.
    by_element = {shape._element: shape for shape in _all_shapes(slide.shapes)}
    for element, box, _ in iter_shapes(tree):
        if box is None:
            continue
        if not element.xpath(".//*[local-name()='txBody']"):
            continue
        shape = by_element.get(element)
        if shape is not None and shape.has_text_frame:
            found.append((box, shape))
    return found


def _take_matching_shape(candidates: list[tuple[Box, object]], box: Box):
    """Фигура клона, стоящая ровно там, где слот вёрстки.

    Слот снят с этой самой фигуры, поэтому совпадение точное, а не на глаз.
    Найденная фигура вынимается из списка: две разных строки в одну рамку —
    это потерянный текст.
    """
    for index, (shape_box_, shape) in enumerate(candidates):
        if _same_box(shape_box_, box):
            candidates.pop(index)
            return shape
    # Рамка, выросшая вниз под наш текст (обложка, финал): та же фигура, выше.
    for index, (shape_box_, shape) in enumerate(candidates):
        taller = box.h > shape_box_.h
        if taller and _same_box(shape_box_, box.model_copy(update={"h": shape_box_.h})):
            candidates.pop(index)
            shape.height = shape.height + (box.h - shape_box_.h)
            return shape
    return None


def _render_element(
    slide,
    element: Element,
    candidates: list[tuple[Box, object]],
    accent: Color | None = None,
    slide_ir: object | None = None,
) -> Box | None:
    """Кладёт элемент на слайд. Возвращает рамку, которую занял."""
    if element.kind is ElementKind.CHART and element.chart is not None:
        # Место донора под график занимала его собственная фигура — она
        # уступает настоящему графику, а не просвечивает из-под него.
        _take_matching_shape(candidates, element.box)
        _remove_shape_at(slide, element.box)
        _clear_under_data(slide, element.box, candidates)
        add_chart(slide, element.box, element.chart)
        return element.box

    if element.kind is ElementKind.TABLE and element.table is not None:
        _take_matching_shape(candidates, element.box)
        _remove_shape_at(slide, element.box)
        _clear_under_data(slide, element.box, candidates)
        add_table(
            slide,
            element.box,
            element.table,
            accent,
            getattr(slide_ir, "background", None),
        )
        return element.box

    if element.kind is not ElementKind.TEXT:
        # Картинки приезжают клонированием вместе с композицией; своей
        # отрисовки они пока не получают.
        return None

    # Фигура донора на своём месте — лучшая цель: у неё уже есть заливка,
    # отступы, выравнивание и всё прочее оформление карточки.
    target = _take_matching_shape(candidates, element.box)
    if target is not None:
        _fill_text_frame(target.text_frame, element, styled=True)
        return element.box

    placeholder = _placeholder_at(slide, element.box)
    if placeholder is not None:
        _fill_text_frame(placeholder.text_frame, element, styled=False)
        _keep_fitted_size(placeholder.text_frame, element)
        return element.box

    box = element.box
    textbox = slide.shapes.add_textbox(Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h))
    textbox.text_frame.word_wrap = True
    # Привязка по вертикали — из IR: место, обрезанное по графике донора,
    # уже не совпадает с его рамкой и пишется новой надписью, а число у
    # донора прижато к низу рамки, над линией.
    if element.text is not None and element.text.paragraphs:
        textbox.text_frame.vertical_anchor = _ANCHOR[element.text.paragraphs[0].style.valign]
    _fill_text_frame(textbox.text_frame, element, styled=True)
    return box


def _shape_box_of(shape) -> Box:
    return Box(
        x=shape.left or 0, y=shape.top or 0, w=shape.width or 1, h=shape.height or 1
    )


# Плейсхолдеры, которые шаблон держит для себя: колонтитул, номер слайда,
# дата. Их содержимое — оформление шаблона, а не наше содержание, и убирать
# их нельзя: дизайнер поставил их намеренно.
_TEMPLATE_PLACEHOLDERS = frozenset({"ftr", "sldNum", "dt"})


def _drop_unfilled_placeholders(slide, filled: list[Box]) -> None:
    """Убирает плейсхолдеры, в которые ничего не положили.

    Не только пустые. Плейсхолдер, созданный вместе со слайдом, приносит с
    собой текст-подсказку своего layout'а — «Вставьте заголовок», «Образец
    текста». На чужом шаблоне такой заголовок доезжал до готовой колоды и
    садился над настоящим: слайд с двумя заголовками, один из которых
    просьба его заполнить.

    Содержание слайда задаёт `SlideIR`. Плейсхолдер, которого в нём нет, — не
    содержание, чем бы он ни был заполнен. Колонтитул, номер слайда и дата не
    трогаются: это оформление шаблона.
    """
    for shape in list(slide.placeholders):
        ph = shape._element.xpath(".//*[local-name()='ph']")
        if ph and ph[0].get("type", "body") in _TEMPLATE_PLACEHOLDERS:
            continue
        # Пустой плейсхолдер уходит, даже если стоит на занятом месте: это
        # дубль, пришедший с layout'а рядом с клоном донора, — в редакторе он
        # просит «вставить заголовок» поверх нашего.
        empty = not (shape.has_text_frame and shape.text_frame.text.strip())
        if not empty and any(_same_box(_shape_box_of(shape), box) for box in filled):
            continue
        shape._element.getparent().remove(shape._element)


def render_deck(
    deck: DeckIR,
    spec: TemplateSpec,
    template_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Собирает `.pptx` поверх шаблона и сохраняет его."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation(str(template_path))
    if prs.slide_width != deck.slide_width_emu or prs.slide_height != deck.slide_height_emu:
        raise RenderError(
            "размер слайда в IR не совпадает с шаблоном: "
            f"{deck.slide_width_emu}×{deck.slide_height_emu} против "
            f"{prs.slide_width}×{prs.slide_height}"
        )

    # Доноров надо забрать **до** снятия слайдов: после `purge_slides` их в
    # презентации уже нет, а части с картинками живы, пока на них кто-то
    # ссылается. Порядок здесь не вкусовой.
    donors = _collect_donors(prs, deck)
    patterns = {pattern.id: pattern for pattern in spec.patterns}
    # Заливка шапки таблицы — акцент шаблона, а не умолчательный синий Office.
    accent = spec.palette[0].color if spec.palette else None

    purge_slides(prs)

    for slide_ir in deck.slides:
        master_index, layout_index = _layout_index(spec, slide_ir.layout_id)
        try:
            layout = prs.slide_masters[master_index].slide_layouts[layout_index]
        except IndexError as exc:
            raise RenderError(
                f"слайд {slide_ir.index}: в шаблоне нет layout'а {slide_ir.layout_id!r}"
            ) from exc

        slide = prs.slides.add_slide(layout)
        donor = donors.get(slide_ir.donor_slide_index)
        if donor is not None:
            _clone_composition(
                slide, donor, deck.slide_width_emu, deck.slide_height_emu
            )

        # Фото донора — место под картинку, а не содержание: в колоду оно не
        # переносится (генерации картинок пока нет). Логотипы, значки и декор
        # в `photo_slots` не входят и остаются.
        _drop_donor_photos(slide, patterns.get(slide_ir.pattern_id))

        # Какие фигуры соединяет каждая линия донора — до всякой уборки.
        anchors = _line_anchors(slide)
        candidates = _text_shapes(slide)
        # Где у донора был текст — до того, как его заменят или сотрут.
        donor_text = [box for box, shape in candidates if shape.text_frame.text.strip()]
        # Где у донора стояли его числа: «10%», «ххх%», «7».
        donor_numbers = [
            (box, shape) for box, shape in candidates if is_figure_text(shape.text_frame.text)
        ]
        filled: list[Box] = []
        for element in slide_ir.elements:
            used = _render_element(slide, element, candidates, accent, slide_ir)
            if used is not None:
                filled.append(used)
        # Номер страницы и номера элементов — служебный текст: в «занятое
        # нашим содержанием» они не входят. Иначе номер страницы в углу
        # делал занятым четвёртый столбец, и пустой узел «04» оставался.
        pattern = patterns.get(slide_ir.pattern_id)
        bookend = slide_ir.pattern_id in spec.bookend_ids
        # Обложка и финал без подписей в ряду элементов: ряд остаётся целиком.
        whole = bookend and not _used_member_bands(pattern, filled, deck)
        _number_page(candidates, pattern, slide_ir.index)
        _number_members(candidates, pattern, filled, deck, keep_all=whole)

        # Рыбный текст донора, в который ничего не положили, обязан уйти:
        # иначе в колоде останется «Lorem ipsum» дизайнера, и аудит
        # справедливо найдёт текст-заглушку.
        for _, leftover in candidates:
            leftover.text_frame.clear()
        _drop_unfilled_subtitle(slide, patterns.get(slide_ir.pattern_id), candidates)

        # Обложка и финал — лицо шаблона: маршрут, рамка «Название проекта»,
        # карточки итога — оформление, и оно остаётся, даже когда подписать
        # его нечем. Маршрут, подписанный разделами колоды, укорачивается до
        # их числа, как на рабочем слайде; не подписанный ничем — остаётся
        # целиком, без подписей.
        if not whole:
            _drop_unused_repeater_items(slide, pattern, filled, deck, connectors=anchors)
        _drop_unfilled_data_frames(slide, filled)
        _drop_donor_figures(slide, donor_numbers, filled)
        _drop_sibling_figures(slide, pattern, slide_ir)
        # Панели обложки и финала вне ряда элементов — рамка «Название
        # проекта», плашка контактов — оформление: остаются и пустыми.
        # Пустые карточки ряда уходят, как на рабочем слайде.
        _drop_emptied_panels(
            slide,
            donor_text,
            filled,
            deck,
            kept=_used_member_bands(pattern, filled, deck),
            only_within=_member_frames(pattern) if bookend else None,
        )
        if not bookend:
            _drop_orphan_decor(slide, pattern, donor_text, filled)

        _drop_unfilled_placeholders(slide, filled)
        _drop_writing_lines(slide, patterns.get(slide_ir.pattern_id))
        _drop_dangling_lines(slide, anchors)
        _drop_orphan_links(slide, patterns.get(slide_ir.pattern_id), filled, deck, anchors)
        if slide_ir.speaker_notes:
            slide.notes_slide.notes_text_frame.text = slide_ir.speaker_notes

    prs.save(str(output_path))
    return output_path


# Линия для записи: тонкая горизонталь без текста («____» бланка).
_WRITING_LINE_EMU = 27_432  # 0.03″
# Насколько конец линии может не доходить до фигуры, чтобы считаться на ней.
_ANCHOR_TOLERANCE_EMU = 91_440  # 0.1″


def _is_line(element) -> bool:
    if etree.QName(element).localname == "cxnSp":
        return True
    preset = element.xpath("./*[local-name()='spPr']/*[local-name()='prstGeom']/@prst")
    return bool(preset) and ("line" in preset[0].lower() or "connector" in preset[0].lower())


def _line_ends(element, box: Box) -> tuple[tuple[int, int], tuple[int, int]]:
    """Концы линии с учётом отражения: габарит — это ещё не направление."""
    xfrm = element.xpath("./*[local-name()='spPr']/*[local-name()='xfrm']")
    flip_h = bool(xfrm) and xfrm[0].get("flipH") == "1"
    flip_v = bool(xfrm) and xfrm[0].get("flipV") == "1"
    x0, x1 = (box.right, box.x) if flip_h else (box.x, box.right)
    y0, y1 = (box.bottom, box.y) if flip_v else (box.y, box.bottom)
    return (x0, y0), (x1, y1)


def _on_shape(point: tuple[int, int], box: Box) -> bool:
    x, y = point
    return (
        box.x - _ANCHOR_TOLERANCE_EMU <= x <= box.right + _ANCHOR_TOLERANCE_EMU
        and box.y - _ANCHOR_TOLERANCE_EMU <= y <= box.bottom + _ANCHOR_TOLERANCE_EMU
    )


def _line_anchors(slide) -> dict[object, tuple[object, object]]:
    """Для каждой линии донора — фигуры, на которых стоят оба её конца.

    Линия, у которой оба конца на фигурах, — связь: маршрут между узлами,
    луч от центра схемы к узлу. Линия без такой пары — оформление, и её
    правило о повисших связях не касается.
    """
    shapes = [
        (element, box)
        for element, box, _ in iter_shapes(slide.shapes._spTree)
        if box is not None and not _is_line(element)
    ]
    found: dict[object, tuple[object, object]] = {}
    for element, box, _ in iter_shapes(slide.shapes._spTree):
        if box is None or not _is_line(element):
            continue
        ends = _line_ends(element, box)
        held = [
            next((shape for shape, frame in shapes if _on_shape(end, frame)), None)
            for end in ends
        ]
        if all(anchor is not None for anchor in held):
            found[element] = (held[0], held[1])
    return found


def _drop_dangling_lines(slide, anchors: dict[object, tuple[object, object]]) -> int:
    """Убирает связи, чей узел ушёл вместе с незаполненным элементом.

    Маршрут из шести этапов при четырёх пунктах терял два узла, а отрезки к
    ним оставались висеть; от центра схемы к пустому углу шёл луч в никуда.
    """
    removed = 0
    for line, (first, second) in anchors.items():
        if line.getparent() is None:
            continue
        if first.getparent() is None or second.getparent() is None:
            line.getparent().remove(line)
            removed += 1
    return removed


def _drop_orphan_links(slide, pattern, filled: list[Box], deck: DeckIR, anchors) -> int:
    """Убирает отрезки маршрута между элементами, один из которых ушёл.

    Не все отрезки стоят концами на узлах: на бланке «Проверка» линия между
    колонками висит в зазоре под кружками и ни одного из них не касается.
    Правило повисших связей её не видит, и при одной колонке из трёх два
    отрезка вели в пустоту. Связью здесь считается линия вдоль оси
    повторителя, которая лежит в полосе элементов поперёк оси и своей
    серединой приходится между центрами двух соседних элементов.
    """
    if pattern is None:
        return 0
    removed = 0
    for repeater in pattern.repeaters:
        if repeater.axis not in ("horizontal", "vertical") or repeater.max_count < 2:
            continue
        across = repeater.axis == "vertical"
        frames = [_member_frame(repeater, index) for index in range(repeater.max_count)]
        size = (deck.slide_width_emu, deck.slide_height_emu)
        used = [
            any(_inside(box, _item_band(repeater, index, *size)) for box in filled)
            for index in range(repeater.max_count)
        ]
        if all(used):
            continue
        low = min(frame.x if across else frame.y for frame in frames)
        high = max(frame.right if across else frame.bottom for frame in frames)
        centers = [
            (frame.y + frame.h / 2) if across else (frame.x + frame.w / 2) for frame in frames
        ]
        for element, box, _ in list(iter_shapes(slide.shapes._spTree)):
            if box is None or element in anchors or not _is_line(element):
                continue
            along, thick = (box.h, box.w) if across else (box.w, box.h)
            if thick > _WRITING_LINE_EMU or along == 0:
                continue
            start, end = (box.x, box.right) if across else (box.y, box.bottom)
            if start < low or end > high:
                continue
            # Линия внутри одного элемента — его часть, а не связь.
            if any(_inside(box, frame) for frame in frames):
                continue
            middle = (box.y + box.h / 2) if across else (box.x + box.w / 2)
            for index in range(len(centers) - 1):
                if not centers[index] < middle < centers[index + 1]:
                    continue
                if not (used[index] and used[index + 1]) and element.getparent() is not None:
                    element.getparent().remove(element)
                    removed += 1
                break
    return removed


def _drop_writing_lines(slide, pattern) -> int:
    """Убирает линии для записи из мест под текст.

    Шаблон-бланк чертит под подписью линии, на которых пишут от руки. Наш
    текст ложится на их место (рамка при разборе выросла на них), а под
    незаполненным местом пустые линии бланка — та же заглушка шаблона.
    """
    if pattern is None:
        return 0
    frames = [slot.box for slot in pattern.slots] + [
        slot.box.model_copy(update={"x": slot.box.x + dx, "y": slot.box.y + dy})
        for repeater in pattern.repeaters
        for dx, dy in (repeater.member_offsets or [(0, 0)])
        for slot in repeater.item_slots
    ]
    removed = 0
    for element, box, _ in list(iter_shapes(slide.shapes._spTree)):
        if box is None or box.h > _WRITING_LINE_EMU or box.w <= 10 * box.h:
            continue
        if not _is_line(element) or element.getparent() is None:
            continue
        # Линия из декора — оформление (линия под цифрой `vk_tech`), её текст
        # обходит. Линии для записи разбор из декора убрал сам.
        if any(_same_box(box, item) for item in pattern.decor):
            continue
        if any(_share_inside(box, frame) >= _PANEL_USE_SHARE for frame in frames):
            element.getparent().remove(element)
            removed += 1
    return removed


def _write_number(shape, number: int, width: int) -> None:
    runs = [run for paragraph in shape.text_frame.paragraphs for run in paragraph.runs]
    if not runs:
        return
    runs[0].text = str(number).zfill(width)
    for run in runs[1:]:
        run.text = ""


def _number_members(
    candidates: list[tuple[Box, object]],
    pattern,
    filled: list[Box],
    deck: DeckIR,
    keep_all: bool = False,
) -> list[Box]:
    """Номера элементов повторителя — по порядку занятых элементов.

    «01» в узле маршрута — не текст-заглушка, а часть элемента: стирать его
    значило оставить пустые кружки. Номер пишется в записи донора.

    `keep_all` — маршрут обложки без подписей: он остаётся целиком, и
    нумеруется каждый его узел.
    """
    if pattern is None:
        return []
    placed: list[Box] = []
    for repeater in pattern.repeaters:
        ordinals = [slot for slot in repeater.item_slots if slot.role is SlotRole.ORDINAL]
        if not ordinals:
            continue
        number = 0
        for index in range(repeater.observed_count if keep_all else repeater.max_count):
            band = _item_band(repeater, index, deck.slide_width_emu, deck.slide_height_emu)
            if not keep_all and not any(_inside(box, band) for box in filled):
                continue
            number += 1
            dx, dy = repeater.offset(index)
            for slot in ordinals:
                box = slot.box.model_copy(update={"x": slot.box.x + dx, "y": slot.box.y + dy})
                shape = _take_matching_shape(candidates, box)
                if shape is None:
                    continue
                _write_number(shape, number, len((slot.placeholder_text or "").strip()))
                placed.append(box)
    return placed


def _number_page(candidates: list[tuple[Box, object]], pattern, index: int) -> list[Box]:
    """Номер страницы донора — номером этого слайда, в той же записи.

    Номер, набранный надписью, а не полем, сам не обновляется: без этого он
    стирался вместе с текстом-заглушкой донора, и номера в колоде не было.
    «02» остаётся двузначным: запись — часть оформления шаблона.
    """
    if pattern is None:
        return []
    placed: list[Box] = []
    for slot in pattern.slots:
        if slot.role is not SlotRole.SLIDE_NUMBER:
            continue
        shape = _take_matching_shape(candidates, slot.box)
        if shape is None:
            continue
        _write_number(shape, index, len((slot.placeholder_text or "").strip()))
        placed.append(slot.box)
    return placed


def _inside(inner: Box, outer: Box) -> bool:
    """Лежит ли рамка внутри другой с запасом на округление."""
    return (
        inner.x >= outer.x - _BAND_TOLERANCE_EMU
        and inner.y >= outer.y - _BAND_TOLERANCE_EMU
        and inner.right <= outer.right + _BAND_TOLERANCE_EMU
        and inner.bottom <= outer.bottom + _BAND_TOLERANCE_EMU
    )


def _remove_shape_at(slide, box: Box) -> bool:
    """Убирает фигуру донора, стоящую ровно в этой рамке.

    Нужно графику и таблице: у донора на их месте стоит его собственный
    `graphicFrame` с рыбными данными («Category 1», «Series 1»). Текстовые
    рамки очищаются, а этот — нет, и без удаления настоящий график ложится
    поверх чужого, просвечивающего снизу.
    """
    for element, shape_box, _ in list(iter_shapes(slide.shapes._spTree)):
        if shape_box is None or not _same_box(shape_box, box):
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
            return True
    return False


# Какая доля площади фигуры донора должна лежать внутри рамки графика или
# таблицы, чтобы фигура считалась «под данными» и убиралась. Больше половины:
# подложка-карточка, в которую график вписан, крупнее его и остаётся.
_UNDER_DATA_SHARE = 0.6


def _clear_under_data(slide, box: Box, candidates: list[tuple[Box, object]]) -> int:
    """Убирает фигуры донора, которые окажутся под графиком или таблицей.

    У графика прозрачный фон, и всё, что под ним, просвечивает. На `vk_tech`
    так выглядела «чёрная заливка» из прогона #12: график лёг поверх тёмной
    панели донора, подписи осей и заголовок — чёрные на чёрном. И так же —
    кольцо логотипов и надпись «Группа VK» поверх столбцов. Цвет здесь не
    поможет: под графиком половина светлая, половина тёмная, и читаемого на
    обеих цвета нет. Убирается то, что под данными, а не перекрашиваются
    данные.
    """
    removed = 0
    # Текстовая фигура, которой уже нет среди кандидатов, занята нашим
    # содержанием — например, заголовком слайда. Её не трогаем.
    free_text = {shape._element for _, shape in candidates}
    for element, shape_box, _ in list(iter_shapes(slide.shapes._spTree)):
        if shape_box is None or shape_box.w <= 0 or shape_box.h <= 0:
            continue
        if (
            element not in free_text
            and element.xpath(".//*[local-name()='txBody']")
            and element.xpath(".//*[local-name()='t']/text()")
        ):
            continue
        inside_w = min(shape_box.right, box.right) - max(shape_box.x, box.x)
        inside_h = min(shape_box.bottom, box.bottom) - max(shape_box.y, box.y)
        if inside_w <= 0 or inside_h <= 0:
            continue
        if inside_w * inside_h < _UNDER_DATA_SHARE * shape_box.w * shape_box.h:
            continue
        parent = element.getparent()
        if parent is None:
            continue
        parent.remove(element)
        removed += 1
        # Убранная фигура не должна достаться следующему элементу слайда.
        candidates[:] = [
            (cbox, shape) for cbox, shape in candidates if shape._element is not element
        ]
    return removed


def _drop_unfilled_data_frames(slide, filled: list[Box]) -> int:
    """Убирает графики и таблицы донора, которым не нашлось содержания.

    Рыбные данные дизайнера в готовой колоде — это не оформление, а мусор:
    «Category 1» и «Series 2» на слайде о наблюдаемости читаются как ошибка,
    и проверка «остался текст-заглушка» сработает справедливо.
    """
    removed = 0
    for element, box, _ in list(iter_shapes(slide.shapes._spTree)):
        if etree.QName(element).localname != "graphicFrame":
            continue
        if box is not None and any(_same_box(box, taken) for taken in filled):
            continue
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
            removed += 1
    return removed


def _item_band(repeater, index: int, slide_w: int, slide_h: int) -> Box:
    """Полоса слайда, занятая одним элементом повторителя.

    Не рамка текста: у элемента есть ещё подложка, плашка и пиктограмма —
    отдельные фигуры рядом. На `vk_workspace` подпись стоит в 7.47 дюйма,
    кнопка со стрелкой в 6.47, и удаление «по рамке» оставляло три пустые
    кнопки при одной подписи. Вдоль оси повторения полоса берётся по рамке
    всего элемента (`member_frames`): подложка карточки на `vk_tech` шире текста,
    и по рамке текста она оставалась на слайде пустой.

    Поперёк оси полоса берёт весь слайд: у вертикального ряда это
    горизонтальная лента, у горизонтального — вертикальная. Так в неё попадают
    все фигуры строки, где бы они по другой оси ни стояли. У сетки в
    несколько рядов такой ленты нет — там полоса равна клетке элемента.
    """
    if index < len(repeater.member_frames):
        frame = repeater.member_frames[index]
    else:
        dx, dy = repeater.offset(index)
        item = repeater.item_box
        frame = Box(x=item.x + dx, y=item.y + dy, w=item.w, h=item.h)
    if repeater.axis == "grid":
        return frame
    if repeater.axis != "vertical":
        return Box(x=frame.x, y=0, w=frame.w, h=slide_h)
    return Box(x=0, y=frame.y, w=slide_w, h=frame.h)


def _drop_unused_repeater_items(
    slide, pattern, filled: list[Box], deck: DeckIR, connectors=()
) -> int:
    """Убирает элементы повторителя, которым не досталось содержания.

    Донор показывает четыре карточки, потому что дизайнеру было что сказать
    четырежды. Если у нас один пункт, правильная вёрстка — одна карточка, а не
    одна карточка и три пустые кнопки со стрелкой. Пустой элемент выглядит не
    лаконично, а недоделанно.

    Удаляется весь элемент: подпись, плашка и пиктограмма — одна смысловая
    единица. Фигура, которая лишь пересекает полосу, но не помещается в неё
    целиком, не трогается: сквозная картинка во весь слайд пересекает все
    полосы сразу и к элементам не относится.
    """
    if pattern is None:
        return 0

    # Шапка слайда — заголовок, подзаголовок и то, что под ними стоит на
    # каждом слайде (прогресс-бар шаблона-бланка). Полоса горизонтального
    # повторителя идёт на всю высоту слайда и снимала сегменты прогресс-бара
    # над незаполненными элементами.
    heads = [
        slot.box.bottom
        for slot in pattern.slots
        if slot.role in (SlotRole.TITLE, SlotRole.SUBTITLE)
    ]
    header_bottom = max(heads) + _HEADER_ZONE_EMU if heads else 0
    # Полоса элемента идёт через весь слайд и задевает чужое: кнопка «готово»
    # под тремя колонками проверки уносила узлы «02» и «03» соседних колонок,
    # полоса крайней колонки — номер страницы. Занятые элементы других
    # повторителей и номер страницы уборке не подлежат.
    protected = [
        slot.box for slot in pattern.slots if slot.role is SlotRole.SLIDE_NUMBER
    ] + _used_member_frames(pattern, filled, deck)

    removed = 0
    for repeater in pattern.repeaters:
        for index in range(repeater.max_count):
            band = _item_band(
                repeater, index, deck.slide_width_emu, deck.slide_height_emu
            )
            if any(_inside(box, band) for box in filled):
                continue
            for element, box, _ in list(iter_shapes(slide.shapes._spTree)):
                if box is None or not _inside(box, band):
                    continue
                if box.bottom <= header_bottom:
                    continue
                if any(_inside(box, frame) for frame in protected):
                    continue
                # Связь между узлами решает правило повисших связей, а линия
                # вне рамки самого элемента — не его часть: отрезок маршрута
                # между двумя занятыми узлами лежал в полосе кнопки под ними.
                if element in connectors:
                    continue
                if _is_line(element) and not _inside(box, _member_frame(repeater, index)):
                    continue
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
                    removed += 1
    return removed


# Шапка слайда тянется на столько ниже заголовка или подзаголовка.
_HEADER_ZONE_EMU = 228_600  # 0.25″

# Панель — залитая фигура заметного размера, но не фон всего слайда.
_PANEL_MIN_SHARE = 0.01
_PANEL_MAX_SHARE = 0.6
# Какая доля нашего элемента должна лежать на панели, чтобы она считалась
# занятой им.
_PANEL_USE_SHARE = 0.5
_FILLS = ("solidFill", "gradFill", "pattFill", "blipFill")


def _drop_donor_photos(slide, pattern) -> int:
    """Убирает фото донора: картинки и фигуры с заливкой-картинкой на месте фото."""
    if pattern is None or not pattern.photo_slots:
        return 0
    removed = 0
    for element, box, _ in list(iter_shapes(slide.shapes._spTree)):
        if box is None or not element.xpath(".//*[local-name()='blip']"):
            continue
        if etree.QName(element).localname == "grpSp":
            continue
        if any(_same_box(box, photo) for photo in pattern.photo_slots):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                removed += 1
    return removed


def _drop_unfilled_subtitle(slide, pattern, leftovers: list[tuple[Box, object]]) -> int:
    """Рамка подзаголовка донора, которой нечего получить, уходит со слайда.

    Пустая надпись на картинке не видна, но в редакторе это фигура
    «slide-subtitle» без текста на каждом слайде: её либо заполняют, либо
    убирают. Своя подложка у неё — оформление, такую фигуру не трогаем.
    """
    if pattern is None:
        return 0
    boxes = [slot.box for slot in pattern.slots if slot.role is SlotRole.SUBTITLE]
    removed = 0
    for box, shape in leftovers:
        if shape.text_frame.text.strip() or _has_fill(shape._element):
            continue
        if any(_same_box(box, subtitle) for subtitle in boxes):
            shape._element.getparent().remove(shape._element)
            removed += 1
    return removed


def _has_fill(element) -> bool:
    return any(
        etree.QName(child).localname in _FILLS
        for sppr in element.xpath("./*[local-name()='spPr']")
        for child in sppr
    )


def _member_frame(repeater, index: int) -> Box:
    if index < len(repeater.member_frames):
        return repeater.member_frames[index]
    dx, dy = repeater.offset(index)
    item = repeater.item_box
    return Box(x=item.x + dx, y=item.y + dy, w=item.w, h=item.h)


def _used_member_frames(pattern, filled: list[Box], deck: DeckIR) -> list[Box]:
    """Рамки самих занятых элементов — не полосы через весь слайд."""
    frames = []
    for repeater in pattern.repeaters:
        for index in range(repeater.max_count):
            band = _item_band(repeater, index, deck.slide_width_emu, deck.slide_height_emu)
            if any(_inside(box, band) for box in filled):
                frames.append(_member_frame(repeater, index))
    return frames


def _used_member_bands(pattern, filled: list[Box], deck: DeckIR) -> list[Box]:
    """Полосы элементов повторителя, которым досталось наше содержание."""
    if pattern is None:
        return []
    bands = []
    for repeater in pattern.repeaters:
        for index in range(repeater.max_count):
            band = _item_band(repeater, index, deck.slide_width_emu, deck.slide_height_emu)
            if any(_inside(box, band) for box in filled):
                bands.append(band)
    return bands


def _member_frames(pattern) -> list[Box]:
    """Рамки всех элементов всех повторителей композиции."""
    if pattern is None:
        return []
    return [
        _member_frame(repeater, index)
        for repeater in pattern.repeaters
        for index in range(repeater.observed_count)
    ]


def _drop_emptied_panels(
    slide,
    donor_text: list[Box],
    filled: list[Box],
    deck: DeckIR,
    kept: list[Box] = (),
    only_within: list[Box] | None = None,
) -> int:
    """Убирает панели донора, у которых не осталось содержания.

    Карточка, плашка, кнопка, серая панель под таблицей — у донора в них
    был текст. Если нашему содержанию место в них не досталось, текст
    стёрт, а подложка осталась: пустая карточка, пустая кнопка, пустая
    таблица-заглушка из линий. На листах #12 это самый частый дефект — 41
    слайд из 90. Панель уходит вместе со всем, что на ней лежит.

    Панель, на которой донор текста не держал, — декор, и её не трогаем.
    """
    tree = slide.shapes._spTree
    slide_area = deck.slide_width_emu * deck.slide_height_emu
    removed = 0
    for element, box, _ in list(iter_shapes(tree)):
        if box is None or etree.QName(element).localname not in ("sp", "pic"):
            continue
        if tree not in element.iterancestors():
            continue
        if not _PANEL_MIN_SHARE * slide_area <= box.area <= _PANEL_MAX_SHARE * slide_area:
            continue
        # Картинка — тоже подложка: на `vk_tech` серая панель под рыбной
        # таблицей нарисована картинкой.
        if etree.QName(element).localname == "sp" and not _has_fill(element):
            continue
        if not any(_inside(text, box) for text in donor_text):
            continue
        if any(_share_inside(taken, box) >= _PANEL_USE_SHARE for taken in filled):
            continue
        # Узел использованного элемента — часть элемента, а не подложка
        # чужого текста: подпись в кружке схемы пуста, а текст элемента лёг
        # под кружок. Без узла пропадала и связь к нему.
        if any(_inside(box, band) for band in kept):
            continue
        if only_within is not None and not any(
            _share_inside(box, frame) >= _PANEL_USE_SHARE for frame in only_within
        ):
            continue
        for other, other_box, _ in list(iter_shapes(tree)):
            if other is element or other_box is None or not _inside(other_box, box):
                continue
            if tree in other.iterancestors():
                other.getparent().remove(other)
        element.getparent().remove(element)
        removed += 1
    return removed


def _drop_orphan_decor(slide, pattern, donor_text: list[Box], filled: list[Box]) -> int:
    """Убирает графику донора, оставшуюся от незаполненного места.

    Линия под цифрой «7» на `vk_tech` лежит внутри рамки этой цифры. Место
    не досталось нашему содержанию — рамка очищена, а линия висела под
    пустотой. Графика уходит, если лежит в рамке донорского текста, на
    которую не пришлось ничего нашего.
    """
    if pattern is None or not pattern.decor:
        return 0
    empty = [
        frame
        for frame in donor_text
        if not any(_share_inside(taken, frame) >= _PANEL_USE_SHARE for taken in filled)
    ]
    removed = 0
    for item in pattern.decor:
        # Большая часть графики — в пустой рамке: линия донора выступает за
        # рамку цифры на 0.12″, и «целиком внутри» её не находило.
        if any(_share_inside(item, frame) >= _PANEL_USE_SHARE for frame in empty) and (
            _remove_shape_at(slide, item)
        ):
            removed += 1
    return removed


def _share_inside(inner: Box, outer: Box) -> float:
    """Какая доля рамки лежит внутри другой."""
    width = min(inner.right, outer.right) - max(inner.x, outer.x)
    height = min(inner.bottom, outer.bottom) - max(inner.y, outer.y)
    if width <= 0 or height <= 0 or inner.area <= 0:
        return 0.0
    return width * height / inner.area


def _drop_donor_figures(
    slide, numbers: list[tuple[Box, object]], filled: list[Box]
) -> int:
    """Убирает картинки донора, на которых стоит его число, а наших данных нет.

    Кольцо с подписью «10%» на `vk_tech` — это визуализация данных, которых
    в контент-пакете нет: число мы стираем, а картинка с чужой долей
    оставалась и выдавала себя за наши данные. Такая фигура либо заполняется
    нашими данными, либо уходит — вместе с числом.
    """
    tree = slide.shapes._spTree
    removed = 0
    for box, shape in numbers:
        if any(_same_box(box, taken) for taken in filled):
            continue
        under = 0
        pictures = [
            (element, pic_box)
            for element, pic_box, _ in iter_shapes(tree)
            if pic_box is not None and etree.QName(element).localname == "pic"
        ]
        figures = [pic_box for _, pic_box in pictures if _inside(box, pic_box)]
        for element, pic_box in pictures:
            # Кольцо бывает собрано из нескольких картинок: дуга лежит
            # внутри подложки кольца, и уходит вместе с ней.
            if not any(_inside(pic_box, figure) for figure in figures):
                continue
            if tree not in element.iterancestors():
                continue
            element.getparent().remove(element)
            under += 1
        if under and shape._element.getparent() is not None:
            shape._element.getparent().remove(shape._element)
        removed += under
    return removed


def _drop_sibling_figures(slide, pattern, slide_ir) -> int:
    """Убирает картинки донора того же размера, что и место под наш график.

    Рыбный график дизайнера бывает картинкой: на `vk_education` два графика
    «Ряд 1/2/3» стоят рядом, наш встаёт на место одного, второй оставался.
    Картинка того же размера, что и место графика, — такой же пример данных,
    а не декор.
    """
    if pattern is None:
        return 0
    charts = [
        element.box
        for element in slide_ir.elements
        if element.kind in (ElementKind.CHART, ElementKind.TABLE)
    ]
    if not charts:
        return 0
    places = [
        slot.box
        for slot in pattern.slots
        if slot.role in (SlotRole.IMAGE, SlotRole.CHART, SlotRole.TABLE)
    ]
    taken = [place for place in places if any(_same_box(place, box) for box in charts)]
    tree = slide.shapes._spTree
    removed = 0
    for element, box, _ in list(iter_shapes(tree)):
        if box is None or etree.QName(element).localname != "pic":
            continue
        if tree not in element.iterancestors():
            continue
        if any(_similar_size(box, place) for place in taken):
            element.getparent().remove(element)
            removed += 1
    return removed


def _similar_size(a: Box, b: Box) -> bool:
    """Размеры рамок совпадают с точностью до 15 %."""
    return abs(a.w - b.w) <= 0.15 * b.w and abs(a.h - b.h) <= 0.15 * b.h


def _collect_donors(prs, deck: DeckIR) -> dict[int, tuple[object, list, object]]:
    """Донорские слайды, нужные этой колоде: часть пакета и список фигур.

    Забираются до снятия слайдов. Хранится именно часть (`slide.part`), а не
    сам слайд: `clone_shape` перепривязывает связи от неё, и её картинки
    остаются достижимы, пока на них ссылается новый слайд.
    """
    wanted = {
        slide.donor_slide_index
        for slide in deck.slides
        if slide.donor_slide_index is not None
    }
    if not wanted:
        return {}

    donors: dict[int, tuple[object, list, object]] = {}
    for index, slide in enumerate(prs.slides, start=1):
        if index in wanted:
            donors[index] = (
                slide.part,
                list(slide.shapes._spTree),
                own_background(slide._element),
            )
    return donors


A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _off_canvas(element: etree._Element, width: int, height: int) -> bool:
    """Лежит ли фигура целиком за пределами холста.

    У `vk_education` таких десять: экспорт из Google Slides оставляет
    служебные надписи справа от слайда, и клонирование композиции тащило их с
    собой — шесть доезжали до готовой колоды. На картинке их не видно, но
    PowerPoint показывает их человеку, а наша же проверка «элемент за
    границами слайда» права насчёт них.

    Частично вылезшую фигуру не трогаем: это может быть задуманный дизайнером
    вынос за край, и решать за него — не наше дело.
    """
    # Геометрия лежит под `p:spPr` (у группы — под `p:grpSpPr`), а у картинки
    # ещё глубже. Берём первый `a:xfrm` в порядке документа: он принадлежит
    # самой фигуре, вложенные идут после него.
    xfrm = element.find(f".//{{{A_NS}}}xfrm")
    offset = xfrm.find(f"{{{A_NS}}}off") if xfrm is not None else None
    extent = xfrm.find(f"{{{A_NS}}}ext") if xfrm is not None else None
    if offset is None or extent is None:
        # Фигура без собственной геометрии наследует её от плейсхолдера;
        # судить о ней по отсутствующим числам нельзя.
        return False
    try:
        x, y = int(offset.get("x", 0)), int(offset.get("y", 0))
        cx, cy = int(extent.get("cx", 0)), int(extent.get("cy", 0))
    except (TypeError, ValueError):
        return False
    return x >= width or y >= height or x + cx <= 0 or y + cy <= 0


def _clone_composition(
    slide, donor: tuple[object, list, object], width: int, height: int
) -> None:
    """Переносит фигуры донора на новый слайд вместе со связями.

    Клонируется всё, кроме служебных узлов дерева фигур и того, что лежит за
    холстом: карточки, картинки, коннекторы, декор. Это и есть та композиция,
    которую выбрала вёрстка, — и её оформление приезжает целиком, а не
    пересказывается.
    """
    donor_part, donor_shapes, donor_bg = donor
    if donor_bg is not None:
        clone_background(donor_bg, donor_part, slide)
    # `add_slide` уже создал пустые плейсхолдеры макета. Если донор несёт
    # свой плейсхолдер того же типа и номера, пустой — дубль в той же рамке:
    # текст ложился в него шрифтом макета, а клон донора оставался с
    # подсказкой «Вставьте заголовок» (`zelenie_investicii`, каждый слайд).
    blank = {_ph_key(shape._element): shape._element for shape in slide.placeholders}
    for element in donor_shapes:
        tag = etree.QName(element).localname
        if tag in ("nvGrpSpPr", "grpSpPr"):
            continue
        if _off_canvas(element, width, height):
            continue
        duplicate = blank.pop(_ph_key(element), None)
        if duplicate is not None:
            duplicate.getparent().remove(duplicate)
        clone_shape(element, donor_part, slide)


def _ph_key(element) -> tuple[str, str] | None:
    """Тип и номер плейсхолдера фигуры; `None`, если это не плейсхолдер."""
    ph = element.xpath("./*/*[local-name()='nvPr']/*[local-name()='ph']")
    if not ph:
        return None
    return ph[0].get("type", "body"), ph[0].get("idx", "0")


def count_native_shapes(path: str | Path) -> list[int]:
    """Сколько нативных фигур на каждом слайде готового файла.

    Нужно проверке «слайд не является одной картинкой»: слайд, состоящий из
    единственного изображения, ТЗ не засчитывает.
    """
    prs = Presentation(str(path))
    return [len(slide.shapes) for slide in prs.slides]


def slide_is_single_image(path: str | Path) -> list[int]:
    """Номера слайдов, которые состоят из одной картинки и ничего больше."""
    prs = Presentation(str(path))
    offenders: list[int] = []
    for index, slide in enumerate(prs.slides, start=1):
        shapes = list(slide.shapes)
        if len(shapes) == 1 and shapes[0].shape_type == 13:
            offenders.append(index)
    return offenders
