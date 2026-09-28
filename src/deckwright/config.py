"""Загрузка конфигурации прогона.

Единственная точка, где `configs/config.yaml` превращается в типизированный
объект. Секретов в YAML нет — там ссылки вида ``${LLM_API_KEY}``, которые
подставляются из окружения здесь.

Отсутствующая переменная окружения не является ошибкой загрузки: разбор
шаблона работает без доступа к модели, а проверка «ключ есть» делается тем
слоем, которому ключ реально нужен.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class StepParams(BaseModel):

    model_config = ConfigDict(extra="forbid")
    """Параметры одного шага пайплайна при обращении к модели.

    ``enable_thinking`` и ``reasoning_effort`` поддерживаются не всеми
    endpoint'ами. Незаданное поле в запрос не уходит вовсе, а заданное, но
    отвергнутое endpoint'ом, отбрасывается клиентом и прогон не роняет.
    """

    temperature: float = 0.0
    max_tokens: int = Field(default=2000, gt=0)
    enable_thinking: bool = False
    reasoning_effort: str | None = None
    # Через сколько секунд без ответа отправить второй такой же запрос и
    # взять первый пришедший ответ. Не задано — дубля нет. Нужен шагу, чей
    # разброс времени ломает бюджет: планировщик на шести живых вызовах —
    # от 47.5 до 194.9 с при бюджете 300 на всю генерацию.
    hedge_after_seconds: float | None = Field(default=None, gt=0)
    # Модель шага и его пределы — из агента (`agents/*.vN.yaml`). Не заданы —
    # берутся у endpoint'а: `model`, `timeout_seconds`, `max_retries`.
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)


class AgentLimits(BaseModel):
    """Пределы одного обращения агента к модели."""

    model_config = ConfigDict(extra="forbid")
    timeout_seconds: int | None = Field(default=None, gt=0)
    # Повторы после ответа, не прошедшего Pydantic-схему шага.
    max_retries: int | None = Field(default=None, ge=0)
    hedge_after_seconds: float | None = Field(default=None, gt=0)


class Agent(BaseModel):
    """Шаг пайплайна, обращающийся к модели, описанный одним файлом.

    `agents/<имя>.vN.yaml`: модель, версия промпта, параметры запроса и
    пределы. Версия агента и хэш его файла пишутся в манифест прогона —
    так же, как версии промптов (A19).
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    step: str
    description: str = ""
    endpoint: str = Field(pattern=r"^(llm|vlm)$")
    model: str = ""
    prompt: str
    params: dict[str, Any] = Field(default_factory=dict)
    limits: AgentLimits = Field(default_factory=AgentLimits)
    sha256: str = ""

    def step_params(self) -> StepParams:
        return StepParams(
            **self.params,
            model=self.model or None,
            timeout_seconds=self.limits.timeout_seconds,
            max_retries=self.limits.max_retries,
            hedge_after_seconds=self.limits.hedge_after_seconds,
        )


class ModelConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: int = Field(default=120, gt=0)
    max_retries: int = Field(default=3, ge=0)
    # Цены провайдера за миллион токенов. Нужны, чтобы прогон сам считал
    # стоимость, а не оставлял это умножению в уме.
    price_per_1m_input: float = Field(default=0.0, ge=0)
    price_per_1m_output: float = Field(default=0.0, ge=0)
    # Сколько запросов к этой модели идёт одновременно. Контекстный аудит
    # делает по вызову на слайд, и последовательно это не влезает в бюджет.
    # Значение консервативное: провайдеры ограничивают частоту запросов, и
    # упереться в 429 дороже, чем идти на восьми потоках.
    max_concurrent_calls: int = Field(default=8, gt=0)
    # Лимиты провайдера за минуту. Ноль = не ограничивать: у собственного
    # инференса лимитов может не быть, и требовать число там незачем.
    # Узкое место — токены, а не запросы: на тарифе L0 SiliconFlow это
    # 1000 запросов против 40 000 токенов в минуту.
    tokens_per_minute: int = Field(default=0, ge=0)
    requests_per_minute: int = Field(default=0, ge=0)
    steps: dict[str, StepParams] = Field(default_factory=dict)

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Стоимость по ценам из конфига. Без цен — ноль, а не выдумка."""
        return (
            prompt_tokens * self.price_per_1m_input
            + completion_tokens * self.price_per_1m_output
        ) / 1_000_000

    @property
    def has_prices(self) -> bool:
        return self.price_per_1m_input > 0 or self.price_per_1m_output > 0

    @property
    def configured(self) -> bool:
        """Есть ли всё, чтобы вообще пойти в модель."""
        return bool(self.base_url and self.api_key and self.model)

    def step(self, name: str) -> StepParams:
        """Параметры шага; для неописанного шага — значения по умолчанию."""
        return self.steps.get(name, StepParams())

    @model_validator(mode="after")
    def _steps_are_called_by_someone(self) -> ModelConfig:
        """Параметры под именем шага, которого нет, не применяются никогда.

        Выглядит это как настроенная температура, а работает как умолчание.
        Поэтому опечатка в имени шага — ошибка загрузки конфига, а не тихий
        откат к значениям по умолчанию.
        """
        from deckwright.llm.base import STEPS

        unknown = sorted(set(self.steps) - STEPS)
        if unknown:
            raise ValueError(
                f"параметры заданы для шагов, которых в коде нет: {', '.join(unknown)}; "
                f"известны: {', '.join(sorted(STEPS))}"
            )
        return self


class ImageProviderConfig(ModelConfig):
    enabled: bool = False


class RunConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    seed: int = 0
    output_dir: Path = Path("outputs")
    # Вход прогона. Заданные здесь, они делают запуск воспроизводимым одной
    # командой `deckwright run --config configs/config.yaml` (A22): что
    # собиралось, видно из файла конфига, а не из истории команд. Аргументы
    # командной строки их перекрывают.
    template: Path | None = None
    content: Path | None = None
    time_budget_seconds: int = Field(default=300, gt=0)
    max_fix_iterations: int = Field(default=1, ge=0)
    # Что прогон делает с находками аудита сам.
    #
    #   review — остановиться с отчётом: выбирает пользователь (умолчание ТЗ);
    #   auto   — применить находки с исправлением типа AUTOMATIC и пересобрать;
    #   off    — не трогать колоду вовсе.
    #
    # ASSISTED и контекстные находки не применяются ни в одном из режимов:
    # они идут только через явный выбор (`pipeline.apply_selection`).
    fix_mode: str = Field(default="review", pattern=r"^(off|review|auto)$")
    # Переписывать ли выбранные текстовые ASSISTED-находки моделью. Без флага
    # такая находка остаётся помеченной «требует редактирования»: это не
    # вторая ветка поведения, а отсутствие шага — в тестах и e2e модель не
    # дёргается.
    rewrite_assisted: bool = False


class TemplateConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    cache_dir: Path = Path(".cache/templates")


class DeckConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    slide_count: int | None = None
    min_slides: int = Field(default=10, gt=0)
    max_slides: int = Field(default=15, gt=0)
    language: str = Field(default="ru", pattern=r"^[a-z]{2}$")
    purpose: str = "product"

    @model_validator(mode="after")
    def _check_bounds(self) -> DeckConfig:
        if self.min_slides > self.max_slides:
            raise ValueError("min_slides не может быть больше max_slides")
        if self.slide_count is not None and not (
            self.min_slides <= self.slide_count <= self.max_slides
        ):
            raise ValueError(
                f"slide_count={self.slide_count} вне границ "
                f"{self.min_slides}..{self.max_slides}"
            )
        return self


class FontsConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    extract_dir: Path = Path(".cache/fonts")
    # Во сколько раз ужимать бюджет длины, когда текст меряли шрифтом с
    # другими ширинами. Метрически совместимый клон запаса не требует: у
    # Carlito те же ширины, что у Calibri, и строки переносятся там же.
    substitution_slack: float = Field(default=0.8, gt=0, le=1)


class RenderConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    soffice_binary: str = "soffice"
    soffice_timeout_seconds: int = Field(default=180, gt=0)
    png_dpi: int = Field(default=96, gt=0)


class AuditConfig(BaseModel):

    model_config = ConfigDict(extra="forbid")
    contrast_min_ratio: float = Field(default=4.5, gt=0)
    max_bullets_per_slide: int = Field(default=6, gt=0)
    max_words_per_bullet: int = Field(default=15, gt=0)
    min_fill_ratio: float = Field(default=0.25, ge=0, le=1)
    max_fill_ratio: float = Field(default=0.75, ge=0, le=1)
    contextual_enabled: bool = True
    contextual_dpi: int = Field(default=96, gt=0)
    skip_unchanged_slides: bool = True
    text_checks_once_per_deck: bool = True
    # {идентификатор проверки: "image" | "text"}. Пустой словарь означает,
    # что все контекстные проверки идут по картинке — более дорогой, но
    # заведомо корректный вариант.
    contextual_checks: dict[str, str] = Field(default_factory=dict)

    def checks_by_mode(self, mode: str) -> list[str]:
        """Идентификаторы проверок, идущих указанным способом."""
        return sorted(check for check, how in self.contextual_checks.items() if how == mode)

    @model_validator(mode="after")
    def _check_fill(self) -> AuditConfig:
        if self.min_fill_ratio >= self.max_fill_ratio:
            raise ValueError("min_fill_ratio должен быть меньше max_fill_ratio")
        return self

    @model_validator(mode="after")
    def _modes_are_known(self) -> AuditConfig:
        unknown = {
            check: how for check, how in self.contextual_checks.items()
            if how not in ("image", "text")
        }
        if unknown:
            raise ValueError(
                f"способ проверки бывает только image или text, получено: {unknown}"
            )
        return self

    @model_validator(mode="after")
    def _image_pass_is_not_empty(self) -> AuditConfig:
        """Хотя бы один вопрос обязан идти по картинке.

        ТЗ задаёт картинку слайда входом для валидации контента. Колода,
        проверенная только по тексту, не проверена: текст показывает
        намерение, а не то, что получилось на слайде.
        """
        configured = self.contextual_enabled and self.contextual_checks
        if configured and not any(how == "image" for how in self.contextual_checks.values()):
            raise ValueError(
                "все контекстные проверки переведены на текст: "
                "картиночный проход обязателен"
            )
        return self


class LayoutStrategy(BaseModel):

    model_config = ConfigDict(extra="forbid")
    """Ось различий между тремя вариантами вёрстки.

    Не три ветки кода, а один параметр: пайплайн один, пресеты разные
    (``configs/variants/*.yaml``).
    """

    slot_fill_target: float = Field(default=0.7, gt=0, le=1)
    type_scale_bias: str = Field(default="mid", pattern=r"^(smaller|mid|larger)$")
    data_viz_mode: str = Field(default="auto", pattern=r"^(table|chart|factoid|auto)$")
    blocks_per_slide: int = Field(default=2, gt=0)
    pattern_preference: list[str] = Field(default_factory=list)


class Variant(BaseModel):

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    strategy: LayoutStrategy = Field(default_factory=LayoutStrategy)


class Config(BaseModel):

    model_config = ConfigDict(extra="forbid")
    run: RunConfig = Field(default_factory=RunConfig)
    template: TemplateConfig = Field(default_factory=TemplateConfig)
    deck: DeckConfig = Field(default_factory=DeckConfig)
    variants: list[Variant] = Field(default_factory=list)
    llm: ModelConfig = Field(default_factory=ModelConfig)
    vlm: ModelConfig = Field(default_factory=ModelConfig)
    image_provider: ImageProviderConfig = Field(default_factory=ImageProviderConfig)
    fonts: FontsConfig = Field(default_factory=FontsConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    agents: list[Agent] = Field(default_factory=list)

    def agent(self, step: str) -> Agent:
        for agent in self.agents:
            if agent.step == step:
                return agent
        raise KeyError(f"агент шага {step!r} не задан")

    def variant(self, name: str) -> Variant:
        for v in self.variants:
            if v.name == name:
                return v
        known = ", ".join(v.name for v in self.variants) or "—"
        raise KeyError(f"вариант {name!r} не найден; известны: {known}")


def _expand_env(value: Any) -> Any:
    """Подставляет ``${VAR}`` из окружения. Незаданная переменная → пустая строка."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: ожидался словарь на верхнем уровне, получено {type(data).__name__}"
        )
    return data


def load_config(path: str | Path) -> Config:
    """Читает config.yaml, подставляет окружение, подтягивает пресеты вариантов.

    ``variants`` в YAML — список путей к файлам пресетов; пути разрешаются
    относительно каталога самого config.yaml, чтобы конфиг не зависел от того,
    из какого каталога запущен процесс.
    """
    path = Path(path)
    raw = _expand_env(_read_yaml(path))

    variant_refs = raw.pop("variants", []) or []
    variants: list[dict[str, Any]] = []
    for ref in variant_refs:
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            # Сначала рядом с config.yaml, затем от текущего каталога.
            candidate = path.parent / ref_path.name
            ref_path = candidate if candidate.exists() else ref_path
        variants.append(_expand_env(_read_yaml(ref_path)))
    raw["variants"] = variants

    refs = raw.pop("agents", None)
    agents = (
        [_load_agent(_resolve(path, Path(ref))) for ref in refs]
        if refs is not None
        else _latest_agents()
    )
    steps = [agent.step for agent in agents]
    if len(steps) != len(set(steps)):
        raise ValueError(f"у шага несколько агентов: {sorted(steps)}")
    for agent in agents:
        endpoint = raw.setdefault(agent.endpoint, {}) or {}
        raw[agent.endpoint] = endpoint
        declared = endpoint.setdefault("steps", {}) or {}
        endpoint["steps"] = declared
        if agent.step in declared:
            # Два источника параметров одного шага — и неясно, какой
            # применился. Параметры шага живут только в агенте.
            raise ValueError(
                f"параметры шага {agent.step} заданы и в агенте {agent.name}.v{agent.version}, "
                f"и в {path.name}: оставьте их только в агенте"
            )
        declared[agent.step] = agent.step_params().model_dump(exclude_none=True)
    raw["agents"] = [agent.model_dump() for agent in agents]
    config = Config.model_validate(raw)
    _ACTIVE.clear()
    _ACTIVE.update({agent.step: agent for agent in config.agents})
    return config


# Каталог агентов и промптов — рядом с пакетом, в корне репозитория.
AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Агенты последнего загруженного конфига: по ним слои выбирают версию
# промпта (`agent_prompt`), не получая конфиг аргументом.
_ACTIVE: dict[str, Agent] = {}


def _resolve(config_path: Path, ref: Path) -> Path:
    if ref.is_absolute():
        return ref
    for candidate in (config_path.parent / ref, config_path.parent.parent / ref, ref):
        if candidate.exists():
            return candidate
    return ref


def _load_agent(path: Path) -> Agent:
    raw_bytes = path.read_bytes()
    data = _expand_env(yaml.safe_load(raw_bytes.decode("utf-8")))
    agent = Agent.model_validate({**data, "sha256": hashlib.sha256(raw_bytes).hexdigest()})
    prompt = PROMPTS_DIR / f"{agent.prompt}.yaml"
    if not prompt.exists():
        raise ValueError(f"{path.name}: промпта {agent.prompt} нет в {PROMPTS_DIR}")
    prompt_step = (yaml.safe_load(prompt.read_text("utf-8")) or {}).get("step")
    if prompt_step != agent.step:
        raise ValueError(
            f"{path.name}: промпт {agent.prompt} написан для шага {prompt_step}, "
            f"а агент — шаг {agent.step}"
        )
    return agent


def _latest_agents() -> list[Agent]:
    """Последняя версия каждого агента из `agents/` — если конфиг их не назвал."""
    latest: dict[str, tuple[int, Path]] = {}
    for path in AGENTS_DIR.glob("*.v*.yaml"):
        name, _, version = path.stem.rpartition(".v")
        if version.isdigit() and int(version) > latest.get(name, (0, path))[0]:
            latest[name] = (int(version), path)
    return [_load_agent(path) for _, path in sorted(latest.values())]


def agent_prompt(step: str) -> str:
    """Имя промпта шага («plan_deck.v6») — из агента этого шага."""
    if not _ACTIVE:
        _ACTIVE.update({agent.step: agent for agent in _latest_agents()})
    if step not in _ACTIVE:
        raise KeyError(f"агент шага {step!r} не задан в agents/")
    return _ACTIVE[step].prompt
