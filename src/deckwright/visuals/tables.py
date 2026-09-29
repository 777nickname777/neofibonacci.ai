"""Нативные таблицы в токенах шаблона.

Таблица обязана остаться `a:tbl`: картинку таблицы нельзя ни отредактировать,
ни прочитать программе чтения с экрана, и ТЗ её не засчитывает.

Оформление берётся из шаблона — заливка шапки из палитры, гарнитура и кегль из
стиля слота. Умолчательный синий стиль Office в чужой колоде виден сразу.
"""

from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR
from pptx.util import Emu, Pt

from deckwright.schemas import Box, Color, TableContent, TextStyle


def add_table(
    slide,
    box: Box,
    content: TableContent,
    accent: Color | None = None,
    background: Color | None = None,
):
    """Кладёт нативную таблицу в рамку и оформляет её по шаблону."""
    rows = len(content.rows) + 1
    columns = len(content.header)
    frame = slide.shapes.add_table(
        rows, columns, Emu(box.x), Emu(box.y), Emu(box.w), Emu(box.h)
    )
    table = frame.table
    _use_template_styling(table)
    # Высота строки — по кеглю, а не доля растянутой рамки; текст — по
    # центру ячейки по вертикали.
    row_height = Emu(round(box.h / rows))
    for row in table.rows:
        row.height = row_height
        for cell in row.cells:
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE

    accent = content.header_fill or accent
    for index, title in enumerate(content.header):
        _write(table.cell(0, index), title, content.header_style, bold=True)
        if accent is not None:
            cell = table.cell(0, index)
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor.from_string(accent.rgb)

    for row_index, row in enumerate(content.rows, start=1):
        for column_index, value in enumerate(row):
            cell = table.cell(row_index, column_index)
            # Строка с выводом выделяется начертанием, а не цветом: весь
            # текст слайда кроме заголовка пишется одним цветом, и таблица
            # не исключение. Цветом брать было заманчиво — но тогда на
            # слайде оказывалось два цвета текста, и правило нарушала сама
            # подсветка.
            key = row_index - 1 in content.highlight_rows
            _write(cell, value, content.cell_style, bold=key)
            if background is not None:
                # Тело таблицы живёт на фоне слайда, а не на белой подложке:
                # иначе на тёмном шаблоне посреди колоды появляется белый
                # прямоугольник.
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor.from_string(background.rgb)

    return frame


def _use_template_styling(table) -> None:
    """Снимает умолчательное оформление Office.

    Свежая таблица приходит в сине-полосатом стиле «Medium Style 2 Accent 1».
    В чужой колоде он виден сразу: всё вокруг в цветах шаблона, а таблица нет.
    Полосы и выделение первого столбца тоже снимаются — заливку кладём сами,
    из палитры шаблона.
    """
    table.first_row = True
    table.horz_banding = False
    table.first_col = False
    table.last_row = False
    table.last_col = False
    # Стиль задаётся идентификатором в XML; «никакого стиля» выражается его
    # отсутствием, а не особым значением.
    properties = table._tbl.find(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}tblPr"
    )
    if properties is not None:
        for style_id in properties.findall(
            "{http://schemas.openxmlformats.org/drawingml/2006/main}tableStyleId"
        ):
            properties.remove(style_id)


def _write(cell, text: str, style: TextStyle | None, *, bold: bool = False) -> None:
    cell.text_frame.clear()
    paragraph = cell.text_frame.paragraphs[0]
    run = paragraph.add_run()
    run.text = text
    if style is None:
        run.font.bold = bold
        return
    run.font.name = style.font_family
    run.font.size = Pt(style.size_pt)
    run.font.bold = bold or style.bold
    run.font.color.rgb = RGBColor.from_string(style.color.rgb)
