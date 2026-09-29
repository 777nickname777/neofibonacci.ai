"""Шаблон, у которого оформление живёт в макете, а не в слайдах.

Нужен, чтобы проверять учёт декора **не на шаблонах датасета**: их имена в
коде запрещены, а совпадение с ними ещё не доказывает, что решение общее.
Композиция здесь намеренно другая, чем у датасета.

Что внутри:

* макет с фотографией во весь слайд — по ней текст читаться не будет;
* тёмная панель поверх фотографии — на ней шаблон и пишет, туда текст можно;
* группа из двух значков в углу макета — логотип, его двигать нельзя;
* второй макет: картинка справа, текст слева — две свободные области;
* повёрнутый декоративный штрих.

`python-pptx` не умеет добавлять фигуры в макеты, поэтому они собираются на
временном слайде и переносятся в макет вместе со своими связями.

Запуск: python tests/fixtures/make_decor_template.py путь/шаблон.pptx
"""

from __future__ import annotations

import copy
import io
import random
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Pt

EMU_IN = 914400
SLIDE_W = int(13.333 * EMU_IN)
SLIDE_H = int(7.5 * EMU_IN)
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_ATTRS = tuple(f"{{{R_NS}}}{name}" for name in ("id", "embed", "link"))


def _photo(width: int, height: int, seed: int) -> io.BytesIO:
    """Шумная картинка: много цветов, без прозрачности — признаки фотографии."""
    from PIL import Image

    rng = random.Random(seed)
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(width):
        for y in range(height):
            pixels[x, y] = (
                (x * 7 + rng.randrange(60)) % 256,
                (y * 11 + rng.randrange(60)) % 256,
                (x * y // 3 + rng.randrange(60)) % 256,
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _move(shape, target_part) -> None:
    """Переносит фигуру со слайда в дерево фигур макета вместе со связями."""
    element = copy.deepcopy(shape._element)
    source_part = shape.part
    for node in element.iter():
        for attr in REL_ATTRS:
            rid = node.get(attr)
            if not rid:
                continue
            rel = source_part.rels[rid]
            node.set(
                attr,
                target_part.relate_to(
                    rel.target_ref if rel.is_external else rel.target_part,
                    rel.reltype,
                    is_external=rel.is_external,
                ),
            )
    target_part._element.find(
        ".//{http://schemas.openxmlformats.org/presentationml/2006/main}spTree"
    ).append(element)
    shape._element.getparent().remove(shape._element)


def _drop_slide(prs, slide) -> None:
    """Убирает временный слайд-мастерскую вместе с его частью."""
    for sld_id in list(prs.slides._sldIdLst):
        if prs.part.rels[sld_id.rId].target_part is slide.part:
            prs.part.drop_rel(sld_id.rId)
            prs.slides._sldIdLst.remove(sld_id)
            return


def build_decor_template(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation()
    prs.slide_width = Emu(SLIDE_W)
    prs.slide_height = Emu(SLIDE_H)
    master = prs.slide_masters[0]
    blank = master.slide_layouts[6]
    layout_photo = master.slide_layouts[5]
    layout_split = master.slide_layouts[4]

    # ── Макет 1: фото во весь слайд, панель под текст, значки-логотип ────
    workshop = prs.slides.add_slide(blank)
    workshop.shapes.add_picture(
        _photo(320, 180, seed=1), Emu(0), Emu(0), Emu(SLIDE_W), Emu(SLIDE_H)
    )
    panel = workshop.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(int(0.8 * EMU_IN)), Emu(int(2.2 * EMU_IN)),
        Emu(int(6.0 * EMU_IN)), Emu(int(2.6 * EMU_IN)),
    )
    panel.fill.solid()
    panel.fill.fore_color.rgb = RGBColor(0x10, 0x20, 0x30)
    panel.line.fill.background()
    for offset in (0, int(0.45 * EMU_IN)):
        mark = workshop.shapes.add_shape(
            MSO_SHAPE.OVAL,
            Emu(int(11.9 * EMU_IN) + offset), Emu(int(0.4 * EMU_IN)),
            Emu(int(0.35 * EMU_IN)), Emu(int(0.35 * EMU_IN)),
        )
        mark.fill.solid()
        mark.fill.fore_color.rgb = RGBColor(0xFF, 0xB0, 0x20)
        mark.line.fill.background()
    for shape in list(workshop.shapes):
        _move(shape, layout_photo.part)
    _drop_slide(prs, workshop)

    # ── Макет 2: картинка справа, повёрнутый штрих слева внизу ───────────
    workshop = prs.slides.add_slide(blank)
    workshop.shapes.add_picture(
        _photo(200, 300, seed=2),
        Emu(int(7.2 * EMU_IN)), Emu(int(1.1 * EMU_IN)),
        Emu(int(5.6 * EMU_IN)), Emu(int(5.2 * EMU_IN)),
    )
    stroke = workshop.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(int(0.6 * EMU_IN)), Emu(int(6.6 * EMU_IN)),
        Emu(int(3.0 * EMU_IN)), Emu(int(0.06 * EMU_IN)),
    )
    stroke.rotation = 8.0
    stroke.fill.solid()
    stroke.fill.fore_color.rgb = RGBColor(0x30, 0x90, 0xFF)
    stroke.line.fill.background()
    for shape in list(workshop.shapes):
        _move(shape, layout_split.part)
    _drop_slide(prs, workshop)

    # ── Слайды-примеры: только текст, оформление наследуется от макетов ──
    cover = prs.slides.add_slide(layout_photo)
    title = cover.shapes.add_textbox(
        Emu(int(1.0 * EMU_IN)), Emu(int(2.5 * EMU_IN)),
        Emu(int(5.6 * EMU_IN)), Emu(int(0.9 * EMU_IN)),
    )
    title.text_frame.text = "Название материала"
    title.text_frame.paragraphs[0].runs[0].font.size = Pt(36)
    subtitle = cover.shapes.add_textbox(
        Emu(int(1.0 * EMU_IN)), Emu(int(3.6 * EMU_IN)),
        Emu(int(5.6 * EMU_IN)), Emu(int(0.35 * EMU_IN)),
    )
    subtitle.text_frame.text = "Короткая подпись"
    subtitle.text_frame.paragraphs[0].runs[0].font.size = Pt(16)

    body = prs.slides.add_slide(layout_split)
    heading = body.shapes.add_textbox(
        Emu(int(0.6 * EMU_IN)), Emu(int(0.7 * EMU_IN)),
        Emu(int(6.2 * EMU_IN)), Emu(int(0.8 * EMU_IN)),
    )
    heading.text_frame.text = "Заголовок раздела"
    heading.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
    text = body.shapes.add_textbox(
        Emu(int(0.6 * EMU_IN)), Emu(int(1.8 * EMU_IN)),
        Emu(int(6.2 * EMU_IN)), Emu(int(3.4 * EMU_IN)),
    )
    frame = text.text_frame
    frame.text = "Первый пункт раздела"
    for line in ("Второй пункт раздела", "Третий пункт раздела"):
        frame.add_paragraph().text = line
    for paragraph in frame.paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(18)

    prs.save(str(path))
    return path


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else "decor-template.pptx")
    print(build_decor_template(target))
