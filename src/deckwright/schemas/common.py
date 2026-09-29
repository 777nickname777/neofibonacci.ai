"""Общие примитивы контрактов: геометрия, цвет, типографика, происхождение.

Единица длины во всех схемах — **EMU** (English Metric Unit, 914400 на дюйм).
Так OOXML хранит координаты, и так их отдаёт python-pptx. Пересчёт в пункты или
пиксели делается только на границе — при рендере в HTML и при измерении текста,
— чтобы округление не накапливалось внутри пайплайна.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EMU_PER_INCH = 914_400
EMU_PER_POINT = 12_700

_HEX_COLOR = re.compile(r"^[0-9A-F]{6}$")


class Frozen(BaseModel):
    """База для значений, которые после разбора шаблона не меняются."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# Внутренние поля текстовой рамки, которые фиттер допускает, когда рамка их
# не объявила: 0.1″ по горизонтали и 0.05″ по вертикали на обе стороны.
FRAME_INSET_X_EMU = 91_440
FRAME_INSET_Y_EMU = 45_720


class Box(Frozen):
    """Прямоугольник в EMU от левого верхнего угла слайда.

    Все координаты абсолютные. Для фигур внутри группы это означает, что
    трансформация группы (``a:xfrm`` с ``chOff``/``chExt``) уже применена —
    иначе проверки «выход за границы» и «наложение блоков» считают не то.
    """

    x: int
    y: int
    w: int = Field(gt=0)
    h: int = Field(gt=0)
    # Внутренние поля текстовой рамки, сумма по оси (слева + справа, сверху +
    # снизу), если рамка их объявила. `None` — умолчания PowerPoint. Шаблон без
    # плейсхолдеров набирает заголовок в рамку ровно в строку с нулевыми
    # полями: с умолчаниями фиттер считал, что туда не влезает ни строки.
    inset_x: int | None = Field(default=None, ge=0)
    inset_y: int | None = Field(default=None, ge=0)

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return self.w * self.h

    def intersection(self, other: Box) -> Box | None:
        x = max(self.x, other.x)
        y = max(self.y, other.y)
        r = min(self.right, other.right)
        b = min(self.bottom, other.bottom)
        if r <= x or b <= y:
            return None
        return Box(x=x, y=y, w=r - x, h=b - y)

    def contains(self, other: Box) -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


class Color(Frozen):
    """Цвет, приведённый к RGB, с памятью о том, как он был записан в шаблоне.

    ``scheme`` хранит имя слота темы (``accent1``, ``dk1``…), если цвет пришёл
    оттуда. Это нужно рендереру: цвет из темы правильнее записать обратно как
    ``schemeClr``, чтобы он продолжил жить по правилам шаблона, а не застыл
    отдельной константой.
    """

    rgb: str
    scheme: str | None = None
    alpha: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("rgb", mode="before")
    @classmethod
    def _normalize(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().lstrip("#").upper()
        return v

    @field_validator("rgb")
    @classmethod
    def _check(cls, v: str) -> str:
        if not _HEX_COLOR.match(v):
            raise ValueError(f"ожидался RRGGBB, получено {v!r}")
        return v

    @property
    def luminance(self) -> float:
        """Относительная яркость по WCAG 2.1 — основа проверки контраста."""

        def channel(value: int) -> float:
            c = value / 255
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (int(self.rgb[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    def contrast_ratio(self, other: Color) -> float:
        a, b = sorted((self.luminance, other.luminance), reverse=True)
        return (a + 0.05) / (b + 0.05)


# Крупный текст по WCAG 2.x: от 18 pt, или от 14 pt полужирным. Ему хватает
# 3:1 там, где основному нужно 4.5:1 (критерий 1.4.3). Без этого синий
# заголовок `vk_education` (0077FF на белом, 4.4:1) перекрашивался в чёрный,
# хотя шаблон пишет им заголовки на каждом слайде.
# Порог контраста Приложения 1. Один на все слои: разбор спрашивает, спорят
# ли две заливки под одной рамкой, вёрстка — каким цветом писать, аудит —
# прошло ли. Три ответа на один вопрос разошлись бы.
READABLE_RATIO = 4.5

INK_BLACK = Color(rgb="000000")
INK_WHITE = Color(rgb="FFFFFF")


def ink_for(ground: Color) -> Color:
    """Чёрный или белый — тот, у кого контраст с этим фоном больше.

    Ни «тёмный ли цвет по названию», ни среднее RGB, ни произвольный порог:
    считается относительная яркость sRGB с линеаризацией каналов, и из двух
    кандидатов берётся тот, что даёт больший контраст.

    Один из двух всегда даёт не меньше 4.58:1: равенство контрастов
    достигается при яркости фона 0.179, и там оба дают 4.58. Значит для
    любой сплошной заливки требование 4.5:1 выполнимо.
    """
    return (
        INK_BLACK
        if INK_BLACK.contrast_ratio(ground) >= INK_WHITE.contrast_ratio(ground)
        else INK_WHITE
    )


def best_ink(grounds: Sequence[Color]) -> Color | None:
    """Чёрный или белый — тот, у кого худший из этих фонов лучше.

    Именно худший, а не средний и не самый тяжёлый: текст читают на всей
    рамке. Фон ЛЦТ2026 — градиент от почти чёрного фиолетового до светлой
    лаванды; выбор по самому тяжёлому (середине) давал чёрный, и на тёмном
    конце выходило 2.3:1, тогда как белый держит 2.4:1 на светлом и 9:1 на
    тёмном. Ни один из двух не спасает такую рамку целиком — это решает
    геометрия, — но выбирать надо лучший из двух.
    """
    if not grounds:
        return None
    return max(
        (INK_BLACK, INK_WHITE),
        key=lambda ink: min(ink.contrast_ratio(ground) for ground in grounds),
    )


def readable_ink(grounds: Sequence[Color]) -> Color | None:
    """Чёрный или белый, читаемый на **всех** этих фонах; иначе `None`.

    `None` — это не «не нашли», а «такого цвета нет»: рамка лежит сразу на
    светлом и на тёмном, и любой текст в ней половину пути будет нечитаем.
    Решается это не цветом, а местом — рамку двигают.
    """
    best = best_ink(grounds)
    if best is None:
        return None
    worst = min(best.contrast_ratio(ground) for ground in grounds)
    return best if worst >= READABLE_RATIO else None


def hardest_ground(grounds: Sequence[Color]) -> Color | None:
    """Фон, на котором тексту тяжелее всего: по нему и выбирается цвет.

    Из опорных цветов градиента берётся тот, где лучший из чёрного и белого
    даёт меньший контраст. Выбор цвета по нему держит порог на всей заливке,
    а не в её середине.
    """
    if not grounds:
        return None
    return min(
        grounds,
        key=lambda ground: max(
            INK_BLACK.contrast_ratio(ground), INK_WHITE.contrast_ratio(ground)
        ),
    )


LARGE_TEXT_PT = 18.0
LARGE_BOLD_TEXT_PT = 14.0
LARGE_TEXT_SHARE = 3.0 / 4.5


def is_large_text(size_pt: float, bold: bool = False) -> bool:
    return size_pt >= LARGE_TEXT_PT or (bold and size_pt >= LARGE_BOLD_TEXT_PT)


def required_contrast(size_pt: float, bold: bool = False, normal: float = 4.5) -> float:
    """Порог контраста для текста этого кегля: `normal` или его 2/3 для крупного."""
    return normal * LARGE_TEXT_SHARE if is_large_text(size_pt, bold) else normal


class Align(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


class VAlign(StrEnum):
    TOP = "top"
    MIDDLE = "middle"
    BOTTOM = "bottom"


class TextStyle(Frozen):
    """Начертание текста: итог разрешения, а не пожелание.

    Порядок приоритетов — от слабого к сильному, и он один на все поля:

    1. тема шаблона (`majorFont`/`minorFont`, `p:txStyles` мастера);
    2. плейсхолдер мастера того же типа;
    3. плейсхолдер макета;
    4. фигура слайда-примера, с которой снят слот;
    5. решение вёрстки там, где оно за ней: кегль подбирает фиттер по шкале
       шаблона со сдвигом варианта (`type_scale_bias`, ровно один раз),
       цвет сводится к одному на слайд (`matcher._one_ink`), контраст
       поднимает цвет до читаемого.

    Чего здесь нет — того шаблон не сказал и никто не выдумал: пустая
    гарнитура или нулевой кегль в этот контракт не попадают вовсе.

    Рендереры значения не выбирают: `.pptx`, `.html` и подписи графиков
    пишут ровно то, что здесь записано.
    """

    font_family: str
    size_pt: float = Field(gt=0)
    bold: bool = False
    italic: bool = False
    color: Color
    align: Align = Align.LEFT
    valign: VAlign = VAlign.TOP
    line_spacing: float = Field(default=1.0, gt=0)
    space_before_pt: float = Field(default=0.0, ge=0)
    space_after_pt: float = Field(default=0.0, ge=0)


class SourceKind(StrEnum):
    """Откуда в шаблоне взялось знание. Нужно, чтобы аудит мог объяснить находку."""

    THEME = "theme"
    MASTER = "master"
    LAYOUT = "layout"
    SLIDE = "slide"
    DERIVED = "derived"
    FALLBACK = "fallback"


class Provenance(Frozen):
    """Ссылка на место в шаблоне, откуда пришли геометрия или стиль.

    Это то, что позволяет рендереру клонировать донорскую фигуру, а аудиту —
    сказать «кегль 11 pt не из шкалы шаблона», а не просто «странный кегль».
    """

    kind: SourceKind
    ref: str = ""
    shape_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str = ""

    @model_validator(mode="after")
    def _fallback_is_never_certain(self) -> Provenance:
        if self.kind is SourceKind.FALLBACK and self.confidence == 1.0:
            raise ValueError(
                "FALLBACK означает, что знания из шаблона не было: "
                "confidence должен быть ниже 1.0"
            )
        return self
