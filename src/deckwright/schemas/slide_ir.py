"""`SlideIR` — свёрстанный слайд. Выход слоя layout.

Единый источник истины: из него собираются и `.pptx`, и `.html`, по нему же
работают детерминированные проверки аудита. Поэтому здесь абсолютная геометрия
в EMU и явные стили — ничего, что пришлось бы доопределять при экспорте.

Каждый элемент несёт `provenance`: откуда взята геометрия и стиль. Это то, что
позволяет рендереру клонировать донорскую фигуру шаблона вместо рисования
заново, а аудиту — объяснять находку, а не просто её называть.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deckwright.schemas.common import Box, Color, Provenance, TextStyle
from deckwright.schemas.template_spec import SlotRole


class ElementKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    CHART = "chart"
    TABLE = "table"
    SHAPE = "shape"
    GROUP = "group"


class ChartKind(StrEnum):
    BAR = "bar"
    COLUMN = "column"
    LINE = "line"
    PIE = "pie"
    DOUGHNUT = "doughnut"
    SCATTER = "scatter"
    AREA = "area"


class Paragraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    style: TextStyle
    bullet: bool = False
    level: int = Field(default=0, ge=0, le=4)


class TextContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraphs: list[Paragraph] = Field(min_length=1)
    # Во что уложился текст по метрикам шрифта. Считается детерминированно,
    # на autofit PowerPoint не полагаемся — это прямое требование постановки.
    measured_height_emu: int | None = None
    # Во сколько ступеней вниз по шкале шаблона пришлось спуститься.
    scale_steps_down: int = Field(default=0, ge=0)
    truncated: bool = False
    # Сколько строк помещается в рамку на выбранном кегле, и сколько заняло.
    # Без этого «сократите текст» — совет без числа: живая модель сократила
    # каждую строку втрое, а переполнение осталось, потому что дело было в
    # числе абзацев, а не в их длине.
    capacity_lines: int = Field(default=0, ge=0)
    used_lines: int = Field(default=0, ge=0)

    @property
    def plain_text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


class ChartSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    values: list[float] = Field(min_length=1)
    color: Color


class ChartContent(BaseModel):
    """Нативный график. Растр здесь невозможен по построению."""

    model_config = ConfigDict(extra="forbid")

    chart_kind: ChartKind
    categories: list[str] = Field(min_length=1)
    series: list[ChartSeries] = Field(min_length=1)
    has_legend: bool = True
    axis_title_x: str = ""
    axis_title_y: str = ""
    unit: str = ""
    label_style: TextStyle | None = None
    # Точки, которые несут вывод слайда: они — цветом бренда, остальные —
    # приглушённым. Пусто — все точки одним цветом.
    highlight: list[int] = Field(default_factory=list)
    muted_color: Color | None = None
    # Подписи значений на точках: с ними ось значений и сетка не нужны.
    show_values: bool = False
    # Подпись самого графика: у нескольких графиков в ряд («малые графики»)
    # вывод слайда один, а ряды разные — каждому нужна своя подпись.
    title: str = ""
    # Цвета секторов кольца или круга: у него ряд один, а цвет нужен каждой
    # доле. Пусто — цвет ряда.
    point_colors: list[Color] = Field(default_factory=list)
    # Язык колоды: по нему числа графика пишутся «41 380» и «14,2», а не
    # «41,380» и «14.2» — формат числа несёт локаль, а не берёт её из системы.
    language: str = ""

    @model_validator(mode="after")
    def _series_match_categories(self) -> ChartContent:
        n = len(self.categories)
        bad = [s.name for s in self.series if len(s.values) != n]
        if bad:
            raise ValueError(
                f"рядов с числом значений ≠ числу категорий ({n}): {', '.join(bad)}"
            )
        return self


class TableContent(BaseModel):
    """Нативная таблица: строки ячеек, первая — заголовки."""

    model_config = ConfigDict(extra="forbid")

    header: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)
    header_style: TextStyle | None = None
    cell_style: TextStyle | None = None
    # Заливка шапки — акцент бренда, а не первый цвет палитры (на
    # `vk_education` это чёрный: пустая чёрная шапка).
    header_fill: Color | None = None
    # Строки, которые несут вывод слайда: набираются цветом бренда.
    highlight_rows: list[int] = Field(default_factory=list)
    accent: Color | None = None

    @model_validator(mode="after")
    def _rows_are_rectangular(self) -> TableContent:
        width = len(self.header)
        bad = [i for i, row in enumerate(self.rows) if len(row) != width]
        if bad:
            raise ValueError(f"строки {bad} не по ширине заголовка ({width} колонок)")
        return self


class ImageContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Путь к файлу либо ссылка на part шаблона (иконка из библиотеки шаблона).
    path: str | None = None
    template_part: str | None = None
    alt: str = ""
    # Исходные пропорции — по ним проверяется «картинка растянута».
    native_w: int | None = Field(default=None, gt=0)
    native_h: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _has_a_source(self) -> ImageContent:
        if not self.path and not self.template_part:
            raise ValueError("у изображения нет ни файла, ни part'а шаблона")
        return self


class ShapeContent(BaseModel):
    """Фигура: подложка карточки, коннектор схемы, маркер."""

    model_config = ConfigDict(extra="forbid")

    preset: str = "rect"
    fill: Color | None = None
    line: Color | None = None
    line_width_emu: int = Field(default=0, ge=0)
    corner_radius_emu: int = Field(default=0, ge=0)


class Plate(BaseModel):
    """Залитая подложка донора, которую нужно растянуть под наш текст.

    Шаблон ставит под началом заголовка акцентную плашку под своё короткое
    название; наш заголовок длиннее и наполовину съезжает с неё на фон. Ни
    один цвет тогда не читается на обоих фонах, а переносить заголовок
    некуда — и плашка растёт под текст. Это оформление шаблона, его
    собственная фигура и его цвет: ничего нового не рисуется.
    """

    model_config = ConfigDict(extra="forbid")

    # Где плашка стоит у донора — по ней рендер её и находит.
    donor: Box
    # Какой она должна стать.
    box: Box
    # Её цвет: по нему выбран цвет текста, и по нему же аудит меряет контраст.
    color: Color


class Element(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: ElementKind
    role: SlotRole
    box: Box
    z: int = 0
    rotation: float = 0.0
    provenance: Provenance

    text: TextContent | None = None
    chart: ChartContent | None = None
    table: TableContent | None = None
    image: ImageContent | None = None
    shape: ShapeContent | None = None
    children: list[Element] = Field(default_factory=list)
    # Фон, на котором лежит элемент, если он не фон слайда: карточка или
    # панель донора. По нему и только по нему мерится контраст текста —
    # светлый текст на тёмной панели светлого слайда законен.
    backdrop: Color | None = None
    # Подложка донора, которую рендер растянет под этот текст.
    plate: Plate | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> Element:
        required = {
            ElementKind.TEXT: self.text,
            ElementKind.CHART: self.chart,
            ElementKind.TABLE: self.table,
            ElementKind.IMAGE: self.image,
            ElementKind.SHAPE: self.shape,
        }
        if self.kind is ElementKind.GROUP:
            if not self.children:
                raise ValueError(f"группа {self.id!r} без детей")
            return self
        if required[self.kind] is None:
            raise ValueError(f"элемент {self.id!r} объявлен как {self.kind}, но содержимого нет")
        return self

    def walk(self) -> list[Element]:
        """Этот элемент и всё вложенное, плоским списком. Нужен аудиту."""
        out = [self]
        for child in self.children:
            out.extend(child.walk())
        return out


class SlideIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    # Слайд плана, из которого этот вырос. Не то же самое, что `index`:
    # переполненный слайд делится надвое, а не поместившийся текст обложки
    # уезжает на свой слайд, и после перенумерации позиция в колоде с
    # номером в плане больше не совпадает. Потребителю, которому нужен
    # замысел (заголовок, намерение), нужен именно этот номер.
    plan_index: int | None = None
    layout_id: str | None = None
    pattern_id: str | None = None
    # Донорский слайд шаблона, чьё поддерево фигур клонирует рендерер.
    donor_slide_index: int | None = None
    elements: list[Element] = Field(default_factory=list)
    background: Color | None = None
    is_dark: bool = False
    speaker_notes: str = ""

    def all_elements(self) -> list[Element]:
        return [e for root in self.elements for e in root.walk()]

    @model_validator(mode="after")
    def _element_ids_are_unique(self) -> SlideIR:
        ids = [e.id for e in self.all_elements()]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"на слайде {self.index} повторяются id элементов: {dupes}")
        return self


class DeckIR(BaseModel):
    """Свёрстанная колода одного варианта."""

    model_config = ConfigDict(extra="forbid")

    variant: str
    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    slide_width_emu: int = Field(gt=0)
    slide_height_emu: int = Field(gt=0)
    slides: list[SlideIR] = Field(min_length=1)

    @model_validator(mode="after")
    def _indices_are_contiguous(self) -> DeckIR:
        expected = list(range(1, len(self.slides) + 1))
        actual = [s.index for s in self.slides]
        if actual != expected:
            raise ValueError(f"индексы слайдов должны идти 1..N подряд, получено {actual}")
        return self

    @property
    def slide_box(self) -> Box:
        return Box(x=0, y=0, w=self.slide_width_emu, h=self.slide_height_emu)
