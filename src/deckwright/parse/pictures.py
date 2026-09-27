"""Фото шаблона — место под картинку, а не содержание.

Шаблон «План подготовки к экзаменам» несёт на слайдах-примерах фотографии:
студент за компьютером, часы. Клонированием композиции они доезжали до колоды
о продажах кофеен — на семь слайдов из одиннадцати. Фото донора иллюстрирует
его собственную тему; для нашей колоды это пустое место под картинку, которое
пока нечем заполнить (генерации картинок нет).

Логотипы, иконки и декор шаблона — другое дело: это оформление, и они
остаются. Отличаются они от фото не именем фигуры, а самим изображением:

* фото крупное — не меньше `MIN_PHOTO_SHARE` площади слайда;
* фото непрозрачное — вырезанный по контуру декор («M» на обложке) и
  логотипы лежат на прозрачном фоне;
* фото многоцветное — после уменьшения до 64×64 в нём тысячи цветов, у
  плоской графики со сглаживанием краёв — сотни;
* у фото нет одного цвета на полкадра: фирменный 3D-шар `vk_tech` и
  абстрактные линии `vk_workspace` многоцветны, но лежат на белом и чёрном
  поле (0.49–0.94 кадра), у фото примера главный цвет занимает 0.11–0.35.

Все признаки вместе: крупная непрозрачная картинка из пяти цветов —
плашка, а мелкая многоцветная — значок.
"""

from __future__ import annotations

import io
from collections import Counter

from lxml import etree

from deckwright.parse.geometry import iter_shapes
from deckwright.schemas import Box

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Доля площади слайда, с которой картинка может быть фото-иллюстрацией.
# Логотип и значок мельче; самое мелкое фото примера — 0.23.
MIN_PHOTO_SHARE = 0.05
# Сколько разных цветов в уменьшенной до 64×64 картинке делает её фото.
# Фото примера — 1600–3300, плоский декор со сглаживанием — 300–800.
MIN_PHOTO_COLORS = 1000
# Доля прозрачных точек, при которой картинка — вырезанный декор.
MAX_TRANSPARENT_SHARE = 0.02
# Доля кадра под самым частым цветом (огрублённым до 8 уровней на канал),
# выше которой картинка — графика на поле, а не фото.
MAX_DOMINANT_SHARE = 0.4


def is_photo(blob: bytes, box: Box, slide_w: int, slide_h: int) -> bool:
    """Фото-иллюстрация шаблона (место под картинку), а не декор и не логотип."""
    if box.w * box.h < MIN_PHOTO_SHARE * slide_w * slide_h:
        return False
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(blob))
        image.draft("RGB", (256, 256))
        small = image.convert("RGBA").resize((64, 64))
    except Exception:
        # Векторная картинка (EMF, SVG) или битая — не фото.
        return False
    raw = small.tobytes()
    pixels = [raw[i : i + 4] for i in range(0, len(raw), 4)]
    transparent = sum(1 for pixel in pixels if pixel[3] < 250)
    if transparent > MAX_TRANSPARENT_SHARE * len(pixels):
        return False
    if len({pixel[:3] for pixel in pixels}) < MIN_PHOTO_COLORS:
        return False
    coarse = Counter((pixel[0] // 32, pixel[1] // 32, pixel[2] // 32) for pixel in pixels)
    return coarse.most_common(1)[0][1] <= MAX_DOMINANT_SHARE * len(pixels)


def photo_boxes(tree: etree._Element, part, slide_w: int, slide_h: int) -> list[Box]:
    """Рамки фото на слайде: картинки и фигуры с заливкой-картинкой."""
    found: list[Box] = []
    for element, box, _ in iter_shapes(tree):
        if box is None:
            continue
        blips = element.findall(f".//{{{A_NS}}}blip")
        # Группа отдаёт свои картинки детям: у неё своя рамка шире фото.
        if etree.QName(element).localname == "grpSp" or len(blips) != 1:
            continue
        rid = blips[0].get(f"{{{R_NS}}}embed")
        if not rid:
            continue
        try:
            blob = part.related_part(rid).blob
        except (KeyError, AttributeError):
            continue
        if is_photo(blob, box, slide_w, slide_h):
            found.append(box)
    return found
