"""Ограничитель частоты и расхода токенов.

Провайдер ограничивает две величины: запросы в минуту (RPM) и токены в минуту
(TPM). Узкое место — второе. На тарифе L0 SiliconFlow это 1000 RPM против
40 000 TPM: тысячи запросов мы не сделаем и близко, а сорок тысяч токенов
полный прогон выбирает за пару минут.

Счёт по числу одновременных вызовов от TPM не спасает: восемь параллельных
запросов с картинкой слайда — это около двадцати тысяч токенов разом, то есть
половина минутного лимита одним всплеском. Поэтому здесь скользящее окно в
минуту по обеим величинам, и вызов ждёт, пока в окне освободится место.

Оценка расхода даётся до запроса (картинка считается по своим размерам, текст
приблизительно), а после ответа заменяется фактическим значением из `usage` —
так окно не расходится с действительностью на длинных прогонах.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 60.0

# Токенов на пиксель у моделей семейства Qwen-VL: картинка режется на патчи
# 28x28, каждый даёт один токен. Для других семейств значение другое, поэтому
# оно вынесено сюда, а не зашито в расчёт.
PIXELS_PER_IMAGE_TOKEN = 28 * 28

# Грубый перевод символов в токены для кириллицы. Оценка нужна только до
# запроса; после ответа окно поправляется фактом из `usage`.
CHARS_PER_TOKEN = 2.5


def estimate_image_tokens(width: int, height: int) -> int:
    """Сколько токенов займёт картинка такого размера."""
    return max(1, round(width * height / PIXELS_PER_IMAGE_TOKEN))


def estimate_text_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


@dataclass
class _Entry:
    at: float
    tokens: int


class RateLimiter:
    """Скользящее окно по запросам и токенам за минуту.

    Потокобезопасен: контекстный аудит идёт параллельно, и окно у всех потоков
    общее — иначе каждый считал бы свой лимит и вместе они выбрали бы кратно
    больше.

    Нулевой лимит означает «не ограничивать»: у своего инференса лимитов может
    не быть вовсе, и заставлять указывать число там бессмысленно.
    """

    def __init__(
        self,
        tokens_per_minute: int = 0,
        requests_per_minute: int = 0,
        *,
        time_source=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self._tpm = max(0, tokens_per_minute)
        self._rpm = max(0, requests_per_minute)
        self._now = time_source
        self._sleep = sleep
        self._entries: deque[_Entry] = deque()
        self._lock = threading.Lock()
        # Ожидающий просыпается не только когда старая запись уходит из окна,
        # но и когда ответ поправил оценку фактом. Оценка берёт `max_tokens`
        # целиком (1500 у аудита при фактических ~420): без пробуждения по
        # `settle` вызов спал до минуты рядом с уже свободным местом. Живой
        # прогон docx × zelenie: аудит 121 с при 54 тыс. токенов — лимиту
        # хватило бы 81 с.
        self._freed = threading.Condition(self._lock)
        self.waited_seconds = 0.0
        self.waits = 0

    @property
    def enabled(self) -> bool:
        return bool(self._tpm or self._rpm)

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        while self._entries and self._entries[0].at <= cutoff:
            self._entries.popleft()

    def _used(self) -> tuple[int, int]:
        return len(self._entries), sum(entry.tokens for entry in self._entries)

    def _delay_needed(self, now: float, tokens: int) -> float:
        """Сколько ждать, чтобы запрос на `tokens` поместился в окно."""
        self._prune(now)
        requests, used = self._used()

        over_tokens = self._tpm and used + tokens > self._tpm
        over_requests = self._rpm and requests + 1 > self._rpm
        if not over_tokens and not over_requests:
            return 0.0
        if not self._entries:
            # Один запрос крупнее всего лимита: ждать бессмысленно, пропускаем
            # и полагаемся на повтор по ответу 429.
            return 0.0
        return max(0.0, self._entries[0].at + WINDOW_SECONDS - now)

    def acquire(self, estimated_tokens: int) -> _Entry:
        """Занимает место в окне, дождавшись, если его нет.

        Возвращает запись, которую потом уточняет `settle` фактическим числом
        токенов из ответа.
        """
        estimated_tokens = max(1, estimated_tokens)
        waited = False
        while True:
            with self._lock:
                now = self._now()
                delay = self._delay_needed(now, estimated_tokens)
                if delay <= 0:
                    entry = _Entry(at=now, tokens=estimated_tokens)
                    self._entries.append(entry)
                    return entry
                if not waited:
                    self.waits += 1
                    waited = True
                if self._sleep is time.sleep:
                    self._freed.wait(delay)
                    self.waited_seconds += self._now() - now
                    continue
                self.waited_seconds += delay
            self._sleep(delay)

    def settle(self, entry: _Entry, actual_tokens: int) -> None:
        """Заменяет оценку фактом из ответа модели."""
        with self._lock:
            entry.tokens = max(1, actual_tokens)
            self._freed.notify_all()

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            self._prune(self._now())
            requests, tokens = self._used()
            return {
                "requests_in_window": requests,
                "tokens_in_window": tokens,
                "tokens_per_minute": self._tpm,
                "requests_per_minute": self._rpm,
                "waits": self.waits,
                "waited_seconds": round(self.waited_seconds, 3),
            }
