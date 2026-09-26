"""Сводный лист: все страницы колоды на одной картинке, с номерами слайдов.

Листы до/после — обязательная часть каждого PR с изменением вёрстки. Этот
скрипт собирает один лист из PNG-страниц варианта; несколько листов
склеиваются по вертикали флагом `--stack`.

Запуск:
    python scripts/contact_sheet.py КАТАЛОГ_PNG ЛИСТ.png "заголовок" [колонок=5] [ширина=360]
    python scripts/contact_sheet.py --stack ИТОГ.png ЛИСТ1.png ЛИСТ2.png …
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def sheet(source: Path, out: Path, title: str, cols: int = 5, width: int = 360) -> None:
    files = sorted(source.glob("*.png"))
    if not files:
        raise SystemExit(f"в {source} нет PNG")
    height = round(width * 9 / 16)
    font = ImageFont.truetype(FONT, 14)
    big = ImageFont.truetype(FONT_BOLD, 18)
    rows = (len(files) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (width + 8) + 8, 34 + rows * (height + 24)), "#dddddd")
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 6), title, fill="black", font=big)
    for index, path in enumerate(files):
        image = Image.open(path).convert("RGB")
        image = image.resize((width, round(width * image.height / image.width)))
        x = 8 + (index % cols) * (width + 8)
        y = 34 + (index // cols) * (height + 24)
        canvas.paste(image, (x, y + 20))
        draw.text((x, y + 2), f"{index + 1}", fill="#c00000", font=font)
    canvas.save(out)


def stack(out: Path, parts: list[Path]) -> None:
    images = [Image.open(path).convert("RGB") for path in parts]
    size = (max(i.width for i in images), sum(i.height for i in images))
    canvas = Image.new("RGB", size, "white")
    y = 0
    for image in images:
        canvas.paste(image, (0, y))
        y += image.height
    canvas.save(out)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--stack"] and len(args) >= 3:
        stack(Path(args[1]), [Path(p) for p in args[2:]])
    elif len(args) >= 3:
        sheet(
            Path(args[0]),
            Path(args[1]),
            args[2],
            int(args[3]) if len(args) > 3 else 5,
            int(args[4]) if len(args) > 4 else 360,
        )
    else:
        sys.exit(__doc__)
