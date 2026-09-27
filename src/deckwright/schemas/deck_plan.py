"""`DeckPlan` — что говорим и в каком порядке. Выход слоя plan, до всякой вёрстки.

План намеренно ничего не знает про шаблон: ни координат, ни кеглей, ни имён
layout'ов. Он описывает **содержание и намерение**, а как это ляжет на слайд,
решает слой layout, у которого уже есть `TemplateSpec`. Так один и тот же план
раскладывается тремя вариантами вёрстки и на любом шаблоне.

Схема же задаёт форму ответа модели: `DeckPlan` валидируется Pydantic'ом
напрямую, без разбора свободного текста.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deckwright.schemas.content_pack import DeckPurpose


class SlideIntent(StrEnum):
    """Роль слайда в повествовании. Слой layout подбирает под неё паттерн."""

    TITLE = "title"
    AGENDA = "agenda"
    SECTION = "section"
    CONTEXT = "context"
    PROBLEM = "problem"
    SOLUTION = "solution"
    HOW_IT_WORKS = "how_it_works"
    EVIDENCE = "evidence"
    COMPARISON = "comparison"
    ROADMAP = "roadmap"
    TEAM = "team"
    RISKS = "risks"
    ASK = "ask"
    CLOSING = "closing"


class FigureKind(StrEnum):
    """Откуда на слайде взялось число."""

    # Процитировано из материалов как есть: «42 минуты».
    CITED = "cited"
    # Выведено из них: «в 4.6 раза» — это 42 / 9, и в материалах такого
    # числа нет.
    DERIVED = "derived"


class Figure(BaseModel):
    """Число на слайде вместе с его происхождением.

    Настоящая модель на живом прогоне написала «время обнаружения сократилось
    в 4.6 раза» — числа, которого в контент-пакете нет: оно выведено из 42 и 9.
    Само по себе это хорошая работа, но проверка «все цифры со слайда есть в
    исходных материалах» на таком заголовке срабатывает ложно, если ищет
    подстроку.

    С происхождением проверка становится детерминированной: процитированное
    число сверяется со значением факта, выведенное пересчитывается по формуле.
    Спорить остаётся не о чем.
    """

    model_config = ConfigDict(extra="forbid")

    # Как число написано на слайде: «4.6», «34 %», «9 минут».
    text: str = Field(min_length=1)
    # Процитированное — по умолчанию: так модель не пишет `kind` у каждого
    # числа, а ответ плана — это секунды ожидания (plan_deck.v6).
    kind: FigureKind = FigureKind.CITED
    # Идентификаторы фактов, на которых оно держится.
    fact_ids: list[str] = Field(min_length=1)
    # Для выведенного — выражение над значениями фактов: «f1 / f3».
    # Допустимы только идентификаторы, числа и четыре действия.
    formula: str = ""

    @model_validator(mode="before")
    @classmethod
    def _kind_from_formula(cls, data):
        """Без `kind` число с формулой — выведенное, без формулы — цитата.

        plan_deck.v6 разрешил не писать `kind`, и модель опускала его и у
        выведенного числа: «cited с формулой» не проходило проверку, и план
        запрашивался заново (живой прогон docx × vk_tech: 3 вызова вместо 1).
        """
        if isinstance(data, dict) and not data.get("kind"):
            data = {**data, "kind": "derived" if data.get("formula") else "cited"}
        return data

    @model_validator(mode="after")
    def _derived_shows_its_working(self) -> Figure:
        if self.kind is FigureKind.DERIVED and not self.formula:
            raise ValueError(
                f"выведенное число {self.text!r} без формулы: проверить его нечем"
            )
        if self.kind is FigureKind.CITED and self.formula:
            raise ValueError(
                f"процитированное число {self.text!r} с формулой: "
                "либо оно взято из материалов, либо выведено"
            )
        return self


class TableData(BaseModel):
    """Таблица как структура, а не как строки с разделителем.

    Модель, которой негде объявить таблицу, складывает её в список строк
    вида «Метрика | До | После». Вёрстке пришлось бы разбирать текст обратно
    в структуру — а контракт обязан нести структуру.
    """

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _rows_match_columns(self) -> TableData:
        width = len(self.columns)
        wrong = [i for i, row in enumerate(self.rows) if len(row) != width]
        if wrong:
            raise ValueError(
                f"строки {wrong} не по ширине заголовка ({width} колонок)"
            )
        return self


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    BULLETS = "bullets"
    KPI = "kpi"
    SERIES = "series"
    TABLE = "table"
    QUOTE = "quote"
    STEPS = "steps"
    IMAGE = "image"


class ContentBlock(BaseModel):
    """Смысловая единица слайда. Во что она превратится — решает вёрстка."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: BlockKind
    heading: str = ""
    items: list[str] = Field(default_factory=list)
    # Ссылки в ContentPack: series_ids на числовые ряды, fact_ids на факты.
    series_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    quote_id: str | None = None
    image_prompt: str = ""
    table: TableData | None = None

    @model_validator(mode="after")
    def _has_payload(self) -> ContentBlock:
        empty = (
            not self.items
            and not self.series_ids
            and not self.fact_ids
            and self.quote_id is None
            and not self.image_prompt
            and not self.heading
            and self.table is None
        )
        if empty:
            raise ValueError(f"блок {self.id!r} пуст: нечего верстать")
        if self.kind is BlockKind.SERIES and not self.series_ids:
            raise ValueError(f"блок {self.id!r} объявлен как SERIES, но рядов не указано")
        if self.kind is BlockKind.QUOTE and self.quote_id is None:
            raise ValueError(f"блок {self.id!r} объявлен как QUOTE, но цитаты не указано")
        if self.kind is BlockKind.TABLE and self.table is None:
            raise ValueError(
                f"блок {self.id!r} объявлен как TABLE, но таблицы не приложено: "
                "строки с разделителем таблицей не считаются"
            )
        return self


class SlidePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    intent: SlideIntent
    # Заголовок-вывод, а не название темы: «Выручка выросла на 34 %»,
    # а не «Выручка». Это отдельная проверка аудита.
    takeaway_title: str = Field(min_length=1)
    subtitle: str = ""
    blocks: list[ContentBlock] = Field(default_factory=list)
    speaker_notes: str = ""
    # Числа, вынесенные на слайд, с их происхождением.
    figures: list[Figure] = Field(default_factory=list)

    @model_validator(mode="after")
    def _body_slides_have_content(self) -> SlidePlan:
        # Титул, разделитель и финал законно состоят из одного заголовка.
        bare_ok = {SlideIntent.TITLE, SlideIntent.SECTION, SlideIntent.CLOSING}
        if self.intent not in bare_ok and not self.blocks:
            raise ValueError(
                f"слайд {self.index} ({self.intent}) без блоков: "
                "получится слайд с одним заголовком"
            )
        return self


class DeckPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    subtitle: str = ""
    purpose: DeckPurpose
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    slides: list[SlidePlan] = Field(min_length=1)

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @model_validator(mode="after")
    def _indices_are_contiguous(self) -> DeckPlan:
        expected = list(range(1, len(self.slides) + 1))
        actual = [s.index for s in self.slides]
        if actual != expected:
            raise ValueError(f"индексы слайдов должны идти 1..N подряд, получено {actual}")
        return self

    @model_validator(mode="after")
    def _block_ids_are_unique(self) -> DeckPlan:
        seen: set[str] = set()
        for slide in self.slides:
            for block in slide.blocks:
                if block.id in seen:
                    raise ValueError(f"идентификатор блока {block.id!r} повторяется")
                seen.add(block.id)
        return self
