"""`ContentPack` — нормализованные исходные материалы. Выход слоя content.

Смысл этой схемы один: **каждый факт и каждое число знают, откуда они**.
Без этого не работает проверка аудита «все цифры и факты со слайда есть в
исходных материалах» — сверять было бы не с чем.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceDoc(BaseModel):
    """Документ контент-пакета, на который ссылаются факты."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: str = Field(pattern=r"^(pdf|docx|pptx|txt|md|json|inline)$")
    char_count: int = Field(default=0, ge=0)


class Fact(BaseModel):
    """Утверждение из материалов, пригодное к переносу на слайд.

    Числовое значение хранится отдельно от текста, когда оно есть. Без этого
    проверка «цифры со слайда есть в исходных материалах» умеет только искать
    подстроку, а выведенное число («сократилось в 4.6 раза» из 42 и 9) в
    материалах не встречается ни разу и помечается выдумкой.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    source_doc_id: str
    # Где именно в документе: страница, слайд, абзац. Пусто — если источник
    # не даёт позиционирования (например, вставленный текст).
    locator: str = ""
    value: float | None = None
    unit: str = ""


class SeriesShape(StrEnum):
    """Что ряд показывает — от этого зависит вид графика.

    Помесячная выручка в сумме тоже даёт квартальную, но это динамика, а не
    доли: отличить их по числам нельзя, а по смыслу — может модель, которая
    читала вход. Пусто — неизвестно, вёрстка решает по числам.
    """

    TIME = "time"  # динамика: месяцы, кварталы, годы, этапы по порядку
    PARTS = "parts"  # доли целого: каналы, сегменты, структура
    COMPARE = "compare"  # сравнение независимых категорий: города, продукты


class NumericPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: float


class Series(BaseModel):
    """Числовой ряд для нативного графика или таблицы."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    unit: str = ""
    points: list[NumericPoint] = Field(min_length=1)
    source_doc_id: str
    locator: str = ""
    shape: SeriesShape | None = None

    @property
    def categories(self) -> list[str]:
        return [p.label for p in self.points]

    @property
    def values(self) -> list[float]:
        return [p.value for p in self.points]


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str = Field(min_length=1)
    author: str = ""
    role: str = ""
    source_doc_id: str


class DeckPurpose(StrEnum):
    """Назначение колоды из ТЗ — влияет на нарратив плана."""

    FEATURE = "feature"
    PRODUCT = "product"
    PROJECT = "project"
    INITIATIVE = "initiative"


class Brief(BaseModel):
    """Краткое задание от пользователя."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    purpose: DeckPurpose
    audience: str = ""
    goal: str = ""
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    extra_instructions: str = ""
    # Запрос пользователя его словами — вставленный бриф. Планировщику он
    # нужен целиком: из двух предложений брифа факты извлекаются скупо, а
    # смысл и акценты — в самой формулировке.
    request: str = ""
    # Кто выступает: подпись спикера на обложке и финале шаблона. Пусто —
    # блок спикера убирается целиком, с кружком под фото: подпись спикера —
    # не место для урезанного подзаголовка.
    author: str = ""
    author_role: str = ""


class ContentPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief: Brief
    documents: list[SourceDoc] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list)
    quotes: list[Quote] = Field(default_factory=list)

    def document(self, doc_id: str) -> SourceDoc:
        for doc in self.documents:
            if doc.id == doc_id:
                return doc
        raise KeyError(f"документ {doc_id!r} не найден в контент-пакете")

    @model_validator(mode="after")
    def _every_reference_resolves(self) -> ContentPack:
        known = {doc.id for doc in self.documents}
        dangling = [
            f"{kind} {item.id!r} → {item.source_doc_id!r}"
            for kind, items in (
                ("факт", self.facts),
                ("ряд", self.series),
                ("цитата", self.quotes),
            )
            for item in items
            if item.source_doc_id not in known
        ]
        if dangling:
            raise ValueError(
                "ссылки на несуществующие документы: " + "; ".join(dangling)
            )
        return self


# ── Ответ модели при приведении произвольного входа к контент-пакету ────────
#
# Модель не пишет идентификаторы фактов и рядов: их проставляет код, по
# порядку. Идентификатор документа она берёт из списка, который видит в
# запросе. Каждое число ответа потом сверяется с текстом входа (`content.
# grounding`): число, которого во входе нет, отбрасывается вместе с фактом.


class IngestFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    doc_id: str
    locator: str = ""
    value: float | None = None
    unit: str = ""


class IngestSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    unit: str = ""
    doc_id: str
    locator: str = ""
    points: list[NumericPoint] = Field(min_length=2)
    shape: SeriesShape | None = None


class IngestQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    author: str = ""
    role: str = ""
    doc_id: str


class IngestAnswer(BaseModel):
    """Что модель извлекла из входа: тема, назначение, факты, ряды, цитаты."""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    purpose: DeckPurpose | None = None
    audience: str = ""
    goal: str = ""
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    facts: list[IngestFact] = Field(default_factory=list)
    series: list[IngestSeries] = Field(default_factory=list)
    quotes: list[IngestQuote] = Field(default_factory=list)
