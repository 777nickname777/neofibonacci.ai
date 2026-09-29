"""`RunManifest` — паспорт прогона. То, что делает результат воспроизводимым.

Критерий A19: в манифест пишутся версии промптов, модель, параметры, хэш
шаблона и seed. Сюда же идут замеры времени по этапам (A18) и всё, о чём
пайплайн вынужден был умолчать: подстановки шрифтов, пропущенные проверки,
отброшенные параметры запроса.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PromptVersion(BaseModel):
    """Версия одного промпта или конфига агента.

    Промпты лежат отдельными файлами в `prompts/` и `agents/` (требование ТЗ),
    а их хэш здесь — гарантия, что прогон воспроизводится именно тем текстом.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentVersion(BaseModel):
    """Версия агента (`agents/<имя>.vN.yaml`), с которой шёл прогон.

    Хэш — файла агента: правка параметров без смены версии видна по нему.
    Модель — та, в которую ушли запросы: имя из окружения или «recorded».
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    step: str
    endpoint: str = Field(pattern=r"^(llm|vlm)$")
    model: str
    prompt: str


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^(llm|vlm|t2i)$")
    model: str
    base_url: str = ""
    steps: dict[str, dict[str, object]] = Field(default_factory=dict)
    # Параметры, которые endpoint не принял и клиент отбросил. Не ошибка, но
    # факт, влияющий на воспроизводимость на другом провайдере.
    dropped_params: list[str] = Field(default_factory=list)
    calls: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    # Повторы из-за ответа, не прошедшего валидацию по схеме. Каждый повтор —
    # это лишние секунды в бюджете и лишние токены в счёте, поэтому цифра
    # попадает в манифест, а не остаётся в логе.
    retries: int = Field(default=0, ge=0)
    rate_limit_hits: int = Field(default=0, ge=0)
    # Самый долгий вызов, с повторами SDK внутри. Дольше таймаута — значит,
    # был повтор, которого счётчики выше не видят.
    slowest_call_seconds: float = Field(default=0.0, ge=0)
    # Дубли запроса (`hedge_after_seconds`): сколько ушло и сколько ответило
    # первым. Каждый дубль — лишние токены, и это должно быть видно.
    hedges: int = Field(default=0, ge=0)
    hedge_wins: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    # Ответы, в которых пришёл блок рассуждений. Модели семейства Qwen3 умеют
    # его выдавать, и тогда ответ перестаёт быть чистым JSON.
    thinking_blocks: int = Field(default=0, ge=0)
    # Записанные ответы вместо живой модели: e2e-прогон в CI идёт так.
    mocked: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ConversionAttempt(BaseModel):
    """Экспорт одного файла во внешнем процессе: сколько попыток и чем кончилось.

    Нужен диагностике: «PDF не получен» без числа попыток и причины не
    отличает нехватку LibreOffice от сорванного таймаута, а это разные
    действия для пользователя.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str = ""
    ok: bool = False
    attempts: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0)
    pages: int | None = None
    error_kind: str = ""
    error: str = ""
    notes: list[str] = Field(default_factory=list)


class StageTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    seconds: float = Field(ge=0)


class FixIteration(BaseModel):
    """Один оборот цикла «аудит → исправление → пересборка → аудит».

    Цикл имеет право менять колоду, а значит обязан отчитываться: что было
    применено, что переписано моделью и сколько находок осталось. Без этого
    «стало лучше» проверяется только на глаз.
    """

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    # Ключи применённых находок (`slide:check_id:element`).
    applied: list[str] = Field(default_factory=list)
    # Что применить не удалось и почему: {ключ находки: причина}.
    skipped: dict[str, str] = Field(default_factory=dict)
    # Слайды, текст которых переписан моделью по явному выбору.
    rewritten_slides: list[int] = Field(default_factory=list)
    # Слайды, пересобранные и потому переспрошенные заново.
    rechecked_slides: list[int] = Field(default_factory=list)
    issues_before: int = Field(default=0, ge=0)
    issues_after: int = Field(default=0, ge=0)
    seconds: float = Field(default=0.0, ge=0)


class FontSubstitution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested: str
    used: str
    reason: str


# Этапы разбора входящих данных. В бюджет времени они не входят (уточнение
# организаторов): ограничение — на генерацию, а не на чтение шаблона.
INPUT_STAGES = frozenset({"parse"})


class RunSummary(BaseModel):
    """Прогон целиком: разбор входа, генерация трёх вариантов и их сумма.

    Бюджет — 300 с на всю колоду, **включая разбор входа** (решение заказчика
    после перехода на произвольный вход: приведение входа к контент-пакету
    теперь вызов модели, и вычитать его из бюджета значило бы прятать самую
    непредсказуемую часть прогона). С бюджетом сверяется `total_seconds`;
    разбор и генерация пишутся рядом, чтобы было видно, что сколько стоит.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    template_name: str
    started_at: datetime
    finished_at: datetime
    # {вариант: секунды генерации по его собственному манифесту, без разбора}
    variant_seconds: dict[str, float] = Field(default_factory=dict)
    # Разбор входа: приведение входа к контент-пакету и разбор шаблона.
    parse_seconds: float = Field(ge=0)
    # Из них — приведение входа к контент-пакету (чтение файлов и модель).
    ingest_seconds: float = Field(default=0.0, ge=0)
    # Генерация всех вариантов по часам, от конца разбора до последнего
    # артефакта: план, вёрстка, сборка, аудит, цикл исправления.
    generation_seconds: float = Field(ge=0)
    total_seconds: float = Field(ge=0)
    # Бюджет на всю колоду: разбор входа и генерация всех вариантов.
    budget_seconds: int = Field(gt=0)
    # Время по этапам, по часам: разбор шаблона, разбор входа, план,
    # генерация вариантов, аудит (`pipeline.stage_times`). Варианты
    # собираются и проверяются параллельно — у этих этапов берётся самый
    # долгий вариант, а не сумма.
    stages: dict[str, float] = Field(default_factory=dict)

    @property
    def within_budget(self) -> bool:
        """Вся колода, от входа до последнего артефакта, в бюджете."""
        return self.total_seconds <= self.budget_seconds


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_name: str
    seed: int = 0
    variants: list[str] = Field(default_factory=list)

    prompts: list[PromptVersion] = Field(default_factory=list)
    agents: list[AgentVersion] = Field(default_factory=list)
    models: list[ModelUsage] = Field(default_factory=list)

    timings: list[StageTiming] = Field(default_factory=list)
    # Как прогон распорядился находками аудита. Пустой список означает, что
    # цикл исправления не запускался: режим `review` или `off`.
    fix_mode: str = Field(default="off", pattern=r"^(off|review|auto|selected)$")
    fix_iterations: list[FixIteration] = Field(default_factory=list)
    # Находки, оставшиеся после предела итераций. Оставить и назвать честнее,
    # чем чинить дальше вслепую.
    unresolved: list[str] = Field(default_factory=list)
    font_substitutions: list[FontSubstitution] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    artifacts: dict[str, str] = Field(default_factory=dict)
    # Попытки экспорта во внешних процессах (сейчас — только .pptx → .pdf).
    conversions: list[ConversionAttempt] = Field(default_factory=list)
    # {формат: почему не получился}. Пусто — получены все форматы.
    export_errors: dict[str, str] = Field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return round(sum(t.seconds for t in self.timings), 3)

    @property
    def parse_seconds(self) -> float:
        return round(sum(t.seconds for t in self.timings if t.stage in INPUT_STAGES), 3)

    @property
    def generation_seconds(self) -> float:
        """Время без разбора входа — то, что сверяется с бюджетом (A18)."""
        return round(self.total_seconds - self.parse_seconds, 3)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(m.cost_usd for m in self.models), 6)

    def within_budget(self, budget_seconds: int) -> bool:
        """Уложилась ли генерация этого варианта в бюджет (A18).

        Бюджет рассчитан на три варианта вместе, поэтому здесь это
        необходимое условие, а не достаточное: итог — `RunSummary`.
        """
        return self.generation_seconds <= budget_seconds
