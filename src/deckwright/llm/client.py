"""Живой клиент к OpenAI-совместимому endpoint'у.

Модель задаётся конфигом: на разработке `Qwen/Qwen3.6-27B` через SiliconFlow,
в финале `Qwen/Qwen3.8-27B` на инференсе VK. Переход — смена `base_url` и
`model`, кода это не касается.

Две особенности, ради которых клиент вообще написан своими руками.

**Ответ по схеме.** В запрос уходит JSON-схема Pydantic-модели, ответ ею же и
валидируется. Невалидный ответ даёт ограниченное число повторов с текстом
ошибки валидации в подсказке — и ни одной регулярки по свободному тексту.

**Параметры, которых endpoint не знает.** `enable_thinking` и
`reasoning_effort` поддерживаются не везде. Провайдер, который их не знает,
отвечает 400. Ронять из-за этого прогон нельзя, поэтому клиент отбрасывает
непонятый параметр, повторяет запрос один раз и запоминает отказ для этого
endpoint'а — дальше не посылает. Отброшенное уходит в манифест прогона, чтобы
расхождение между провайдерами было видно, а не пряталось.
"""

from __future__ import annotations

import base64
import json
import queue
import re
import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from openai import (
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

from deckwright.config import ModelConfig
from deckwright.llm.base import StructuredError
from deckwright.llm.ratelimit import (
    RateLimiter,
    estimate_image_tokens,
    estimate_text_tokens,
)
from deckwright.llm.response import has_reasoning, strip_wrapping

T = TypeVar("T", bound=BaseModel)

# Из текста ошибки 400 достаём имя параметра, который провайдер не принял.
_UNKNOWN_PARAM = re.compile(
    r"(?:unknown|unsupported|unrecognized|invalid)[^\"']*[\"']?(\w+)[\"']?", re.IGNORECASE
)

@dataclass
class StepUsage:
    """Расход одного шага: сколько раз звали, сколько он ответил и как долго."""

    calls: int = 0
    completion_tokens: int = 0
    answer_chars: int = 0
    slowest_seconds: float = 0.0
    # Последняя ошибка проверки ответа: повтор стоит целого вызова, и без
    # причины его не устранить.
    last_error: str = ""


_SCHEMA_INSTRUCTION = (
    "Ответь строго одним объектом JSON по схеме ниже. "
    "Без пояснений, без markdown-ограждения.\n\nСхема:\n{schema}"
)


def probe_endpoint(cfg: ModelConfig) -> tuple[bool, str, list[str]]:
    """Проверяет, принимает ли endpoint ключ, и что у него есть из моделей.

    Отдельный дешёвый запрос перед работой. Без него неверный ключ или
    опечатка в имени модели вскрываются как провал посреди генерации, а
    сообщение провайдера («Token is invalid») не подсказывает, что искать.

    Возвращает (принял ли ключ, что показать пользователю, список моделей).
    """
    if not cfg.configured:
        return False, "не настроен: нет base_url, api_key или model", []

    # Ключ уходит в заголовок HTTP, а туда можно класть только ASCII. Символ,
    # случайно попавший при копировании, иначе валит диагностику трейсбеком —
    # ровно тогда, когда от неё нужно внятное объяснение.
    try:
        cfg.api_key.encode("ascii")
    except UnicodeEncodeError:
        return False, "содержит не-ASCII символы: похоже, скопирован с лишним знаком", []
    if cfg.api_key != cfg.api_key.strip():
        return False, "содержит пробелы или перенос строки по краям", []

    client = OpenAI(
        base_url=cfg.base_url, api_key=cfg.api_key, timeout=cfg.timeout_seconds, max_retries=0
    )
    try:
        models = [model.id for model in client.models.list().data]
    except AuthenticationError as exc:
        return False, f"отклонён endpoint'ом ({_short(exc)})", []
    except OpenAIError as exc:
        # Список моделей отдают не все провайдеры; это не повод считать ключ
        # плохим — просто проверить его заранее не вышло.
        return True, f"проверить не удалось ({_short(exc)}), пробуем запрос", []
    except Exception as exc:  # диагностика обязана объяснить, а не упасть
        return True, f"проверить не удалось ({type(exc).__name__}: {_short(exc)})", []
    return True, "принят", sorted(models)


def _png_size(data: bytes) -> tuple[int, int]:
    """Размер PNG из заголовка. Неразборчивый файл — консервативная оценка."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    return 1920, 1080


def _short(exc: Exception) -> str:
    text = str(exc).strip().replace("\n", " ")
    return text[:160]


class LiveClient:
    """Обращение к модели с валидацией ответа по Pydantic-схеме."""

    mocked = False

    def __init__(self, cfg: ModelConfig) -> None:
        if not cfg.configured:
            raise StructuredError(
                "модель не настроена: нужны base_url, api_key и model. "
                "Заполните .env по образцу .env.example."
            )
        self._cfg = cfg
        self._client = OpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout_seconds,
            max_retries=cfg.max_retries,
        )
        self.dropped_params: set[str] = set()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.retries = 0
        self.thinking_blocks = 0
        # Самый долгий вызов, включая повторы, которые SDK делает сам при
        # таймауте и сбое соединения: они нигде больше не видны. Вызов дольше
        # `timeout_seconds` — это и есть такой невидимый повтор.
        self.slowest_call_seconds = 0.0
        # Сколько раз ушёл дубль запроса и сколько раз он ответил первым.
        self.hedges = 0
        self.hedge_wins = 0
        # Ответы 429. SDK сам повторяет их с выдержкой; счётчик нужен, чтобы
        # было видно, упёрлись ли мы в лимит провайдера, а не гадать по времени.
        self.rate_limit_hits = 0
        # Расход по шагам: суммарные счётчики склеивают разбор входа и план, а
        # решать, что сокращать, можно только по ответу каждого шага отдельно.
        self.by_step: dict[str, StepUsage] = {}
        self._step_lock = threading.Lock()
        self.limiter = RateLimiter(
            tokens_per_minute=cfg.tokens_per_minute,
            requests_per_minute=cfg.requests_per_minute,
        )
        # Последний сырой ответ: нужен диагностике `deckwright probe`, чтобы
        # показать, что именно вернул endpoint, а не пересказ.
        self.last_raw = ""

    def _attempts(self, step: str) -> int:
        """Попыток на шаг: предел агента, иначе endpoint'а."""
        retries = self._cfg.step(step).max_retries
        return (self._cfg.max_retries if retries is None else retries) + 1

    def _extra_body(self, step: str) -> dict[str, object]:
        params = self._cfg.step(step)
        extra: dict[str, object] = {"enable_thinking": params.enable_thinking}
        # reasoning_effort уходит только если задан в конфиге явно.
        if params.reasoning_effort is not None:
            extra["reasoning_effort"] = params.reasoning_effort
        return {k: v for k, v in extra.items() if k not in self.dropped_params}

    def _message(self, prompt: str, schema: type[T], images: list[bytes] | None) -> list[dict]:
        text = prompt + "\n\n" + _SCHEMA_INSTRUCTION.format(
            schema=json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        if not images:
            return [{"role": "user", "content": text}]
        content: list[dict] = [{"type": "text", "text": text}]
        for image in images:
            encoded = base64.b64encode(image).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
        return [{"role": "user", "content": content}]

    def _estimate(self, step: str, messages: list[dict], images: list[bytes] | None) -> int:
        """Сколько токенов запрос займёт — до того, как он ушёл.

        Нужно ограничителю: место в минутном окне занимается заранее, иначе
        восемь параллельных вызовов уйдут одновременно и пробьют лимит. После
        ответа оценка заменяется фактом из `usage`.
        """
        text = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for message in messages
            for part in (
                message["content"]
                if isinstance(message["content"], list)
                else [{"text": message["content"]}]
            )
        )
        tokens = estimate_text_tokens(text) + self._cfg.step(step).max_tokens
        for image in images or ():
            width, height = _png_size(image)
            tokens += estimate_image_tokens(width, height)
        return tokens

    def _ask(self, step: str, messages: list[dict], estimated: int = 0) -> str:
        params = self._cfg.step(step)
        entry = self.limiter.acquire(estimated) if self.limiter.enabled else None
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=params.model or self._cfg.model,
                messages=messages,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                response_format={"type": "json_object"},
                extra_body=self._extra_body(step),
                timeout=params.timeout_seconds or self._cfg.timeout_seconds,
            )
        except RateLimitError:
            # SDK уже исчерпал свои повторы; отмечаем и передаём выше.
            self.rate_limit_hits += 1
            raise
        except BadRequestError as exc:
            rejected = self._rejected_param(str(exc))
            if rejected is None:
                raise
            # Провайдер не знает параметр: забываем его и пробуем ещё раз.
            self.dropped_params.add(rejected)
            response = self._client.chat.completions.create(
                model=params.model or self._cfg.model,
                messages=messages,
                temperature=params.temperature,
                max_tokens=params.max_tokens,
                response_format={"type": "json_object"},
                extra_body=self._extra_body(step),
                timeout=params.timeout_seconds or self._cfg.timeout_seconds,
            )
        self.calls += 1
        self.slowest_call_seconds = max(
            self.slowest_call_seconds, round(time.monotonic() - started, 3)
        )
        usage = getattr(response, "usage", None)
        with self._step_lock:
            record = self.by_step.setdefault(step, StepUsage())
            record.calls += 1
            record.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            record.answer_chars += len(response.choices[0].message.content or "")
            record.slowest_seconds = max(
                record.slowest_seconds, round(time.monotonic() - started, 3)
            )
        actual = 0
        if usage is not None:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            actual = getattr(usage, "total_tokens", 0) or 0
        if entry is not None and actual:
            self.limiter.settle(entry, actual)

        content = response.choices[0].message.content or ""
        # Некоторые провайдеры кладут рассуждения в отдельное поле, некоторые
        # оставляют тегом внутри ответа. Считаем оба случая.
        reasoning = getattr(response.choices[0].message, "reasoning_content", None)
        if reasoning or has_reasoning(content):
            self.thinking_blocks += 1
        self.last_raw = content
        return content

    def _ask_hedged(self, step: str, messages: list[dict], estimated: int) -> str:
        """Запрос с дублем: если за `hedge_after_seconds` ответа нет — второй такой же.

        Берётся первый пришедший ответ. Медленный ответ провайдера — не сбой:
        таймаут с повтором его обрывает и ждёт заново, а дубль даёт ему
        дойти и лишь страхует. На замеренных вызовах: 89.8 с остаётся 89.8,
        194.9 с становятся ≈111 (60 с ожидания + обычный вызов).

        Каждая копия занимает своё место в окне ограничителя, так что лимит
        токенов в минуту учитывает обе. Потоки фоновые: проигравший запрос
        доходит сам и не держит процесс при выходе.
        """
        delay = self._cfg.step(step).hedge_after_seconds
        if not delay:
            return self._ask(step, messages, estimated)

        results: queue.Queue = queue.Queue()

        def attempt(copy: int) -> None:
            try:
                results.put((copy, self._ask(step, messages, estimated), None))
            except Exception as failure:  # отказ копии не роняет вторую
                results.put((copy, None, failure))

        threading.Thread(target=attempt, args=(0,), daemon=True).start()
        try:
            copy, raw, failure = results.get(timeout=delay)
            launched = 1
        except queue.Empty:
            self.hedges += 1
            threading.Thread(target=attempt, args=(1,), daemon=True).start()
            copy, raw, failure = results.get()
            launched = 2
        if failure is not None and launched == 2:
            # Первая пришедшая копия упала — ждём вторую.
            copy, raw, failure = results.get()
        if failure is not None:
            raise failure
        if copy == 1:
            self.hedge_wins += 1
        return raw

    def _rejected_param(self, message: str) -> str | None:
        match = _UNKNOWN_PARAM.search(message)
        if match is None:
            return None
        name = match.group(1)
        known = {"enable_thinking", "reasoning_effort"}
        return name if name in known and name not in self.dropped_params else None

    def complete(
        self,
        step: str,
        prompt: str,
        schema: type[T],
        images: list[bytes] | None = None,
    ) -> T:
        messages = self._message(prompt, schema, images)
        estimated = self._estimate(step, messages, images)
        last_error = ""
        attempts = self._attempts(step)
        for _ in range(attempts):
            raw = self._ask_hedged(step, messages, estimated)
            try:
                return schema.model_validate_json(strip_wrapping(raw))
            except ValidationError as exc:
                last_error = str(exc)
                self.retries += 1
                with self._step_lock:
                    record = self.by_step.setdefault(step, StepUsage())
                    record.last_error = " ".join(last_error.split())[:300]
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Ответ не прошёл валидацию. Исправь и пришли только JSON.\n"
                            f"Ошибки:\n{last_error}"
                        ),
                    },
                ]
        raise StructuredError(
            f"шаг {step}: модель не отдала валидный {schema.__name__} "
            f"за {attempts} попыток. Последние ошибки:\n{last_error}"
        )
