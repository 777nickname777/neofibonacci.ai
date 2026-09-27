"""Ограничитель по токенам в минуту.

Узкое место провайдера — TPM, а не число одновременных вызовов: восемь
параллельных запросов с картинкой слайда выбирают половину минутного лимита
одним всплеском. Здесь проверяется, что окно это удерживает.

Время и сон подменяются, поэтому тесты не ждут по-настоящему.
"""

from __future__ import annotations

from deckwright.llm.ratelimit import (
    RateLimiter,
    estimate_image_tokens,
    estimate_text_tokens,
)


class FakeClock:
    """Управляемое время: сон двигает часы, а не ждёт."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(clock: FakeClock, **kwargs) -> RateLimiter:
    return RateLimiter(time_source=clock.time, sleep=clock.sleep, **kwargs)


def test_no_limits_never_waits():
    clock = FakeClock()
    limiter = _limiter(clock)
    assert limiter.enabled is False
    for _ in range(100):
        limiter.acquire(10_000)
    assert clock.slept == []


def test_token_budget_holds_a_burst():
    """Всплеск параллельных вызовов не должен пробивать минутный лимит."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=40_000)

    for _ in range(16):  # 16 x 2500 = 40 000, ровно лимит
        limiter.acquire(2_500)
    assert clock.slept == [], "в лимит укладываемся, ждать незачем"

    limiter.acquire(2_500)  # семнадцатый не влезает
    assert limiter.waits == 1
    assert clock.slept == [60.0], "ждём, пока освободится самая старая запись"


def test_window_slides_and_frees_room():
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=10_000)
    limiter.acquire(10_000)

    clock.now += 61  # минута прошла, окно опустело
    limiter.acquire(10_000)
    assert clock.slept == []


def test_request_limit_is_enforced_too():
    clock = FakeClock()
    limiter = _limiter(clock, requests_per_minute=3)
    for _ in range(3):
        limiter.acquire(1)
    limiter.acquire(1)
    assert limiter.waits == 1


def test_single_call_larger_than_the_whole_limit_is_not_deadlocked():
    """Запрос крупнее лимита ждать бессмысленно: ждём отказа 429 и повтора,
    а не вечной блокировки."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=1_000)
    limiter.acquire(50_000)
    assert clock.slept == []


def test_actual_usage_replaces_the_estimate():
    """Оценка до запроса заменяется фактом, иначе окно разойдётся с реальностью."""
    clock = FakeClock()
    limiter = _limiter(clock, tokens_per_minute=10_000)
    entry = limiter.acquire(5_000)
    limiter.settle(entry, 1_000)
    assert limiter.snapshot()["tokens_in_window"] == 1_000


def test_image_estimate_scales_with_area():
    """Вчетверо больше пикселей — вчетверо больше токенов (с точностью до
    округления). Отсюда и берётся экономия на разрешении картинок аудита."""
    small = estimate_image_tokens(960, 540)
    large = estimate_image_tokens(1920, 1080)
    assert abs(large - 4 * small) <= 1
    assert estimate_text_tokens("") >= 1


# ── Дубль запроса (hedge_after_seconds) ──────────────────────────────────────


def _hedged_client(delays: list[float], hedge_after: float = 0.2):
    """Клиент, у которого провайдер отвечает с заданными задержками по очереди."""
    import threading
    import time as _time
    from types import SimpleNamespace

    from pydantic import BaseModel

    from deckwright.config import ModelConfig, StepParams
    from deckwright.llm.client import LiveClient

    class Answer(BaseModel):
        copy_no: int

    cfg = ModelConfig(
        base_url="http://provider.invalid/v1",
        api_key="test",
        model="test-model",
        tokens_per_minute=40_000,
        steps={"plan_deck": StepParams(max_tokens=6000, hedge_after_seconds=hedge_after)},
    )
    client = LiveClient(cfg)
    lock = threading.Lock()
    order = iter(range(len(delays)))

    def create(**_):
        with lock:
            copy = next(order)
        _time.sleep(delays[copy])
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=f'{{"copy_no": {copy}}}', reasoning_content=None
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=2700, completion_tokens=2000, total_tokens=4700),
        )

    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client, Answer


def test_slow_answer_is_overtaken_by_the_hedge():
    """Первый запрос завис — через `hedge_after_seconds` уходит второй и отвечает."""
    import time as _time

    client, Answer = _hedged_client([2.0, 0.1])
    started = _time.monotonic()
    answer = client.complete("plan_deck", "план", Answer)
    elapsed = _time.monotonic() - started

    assert answer.copy_no == 1
    assert elapsed < 1.0, f"ждали медленную копию: {elapsed:.2f} с"
    assert client.hedges == 1 and client.hedge_wins == 1


def test_fast_answer_sends_no_hedge():
    """Уложился до порога — второй запрос не уходит и токены не тратятся."""
    client, Answer = _hedged_client([0.05, 0.05])
    answer = client.complete("plan_deck", "план", Answer)
    assert answer.copy_no == 0
    assert client.hedges == 0 and client.calls == 1


def test_both_copies_are_counted_by_the_token_limiter():
    """Дубль — это второй запрос в окне TPM, а не бесплатная страховка.

    Замер для планировщика: одна копия резервирует ≈8.7 тыс. токенов (вход
    ≈2.7 тыс. плюс потолок ответа 6000), две — 17.4 тыс. из 40 тыс.; после
    ответа резерв заменяется фактом.
    """
    import time as _time

    client, Answer = _hedged_client([0.6, 0.1])
    client.complete("plan_deck", "план", Answer)
    _time.sleep(0.7)  # проигравшая копия доходит сама
    window = client.limiter.snapshot()
    assert window["requests_in_window"] == 2
    assert window["tokens_in_window"] == 2 * 4700
    assert window["waits"] == 0, "ограничитель заставил дубль ждать"


def test_waiter_wakes_when_an_answer_frees_room():
    """Ответ заменил оценку фактом — ждущий вызов идёт сразу, а не через минуту.

    Оценка берёт `max_tokens` целиком, факт втрое меньше: без пробуждения по
    `settle` аудит трёх вариантов спал рядом со свободным местом.
    """
    import threading
    import time

    limiter = RateLimiter(tokens_per_minute=10_000)
    first = limiter.acquire(8_000)
    passed = threading.Event()

    def second() -> None:
        limiter.acquire(5_000)
        passed.set()

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert not passed.is_set(), "в окне нет места: второй вызов обязан ждать"

    limiter.settle(first, 1_000)
    assert passed.wait(2.0), "место освободилось, а вызов всё ещё ждёт"
    assert limiter.waits == 1
