"""Фоновые прогоны и их состояние.

Прогон колоды занимает минуты, а Streamlit перезапускает скрипт на каждое
нажатие. Значит держать прогон в обработчике кнопки нельзя: страница будет
заблокирована, а обновление её потеряет.

Поэтому прогон живёт в отдельном потоке, а его состояние — в реестре по
`run_id`. `run_id` уходит в параметры адреса, и после обновления страницы
интерфейс находит тот же прогон, а не начинает новый.

Реестр модульного уровня, а не `st.session_state`: состояние переживает и
перезапуск скрипта, и обновление вкладки, пока жив процесс сервера.
"""

from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deckwright.audit.contextual.runner import SharedTextPass
from deckwright.config import Config
from deckwright.content.ingest import IngestInput, ingest
from deckwright.llm.base import StructuredClient
from deckwright.pipeline import (
    LaidOut,
    PipelineResult,
    apply_selection,
    complete_variant,
    lay_out_variant,
)
from deckwright.schemas import ContentPack, DeckPurpose


@dataclass
class VariantState:
    """Что известно про один вариант вёрстки прямо сейчас."""

    name: str
    stage: str = "ожидает"
    result: PipelineResult | None = None
    error: str | None = None
    # Ключи находок, отмеченных человеком. Живут здесь, а не в форме: форма
    # пересоздаётся на каждый перезапуск скрипта.
    selected: set[str] = field(default_factory=set)
    applying: bool = False


@dataclass
class RunState:
    """Прогон целиком: три варианта, общий план, общее время."""

    run_id: str
    template_name: str
    started_at: datetime
    variants: dict[str, VariantState] = field(default_factory=dict)
    finished: bool = False
    error: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    # Вход, приведённый к контент-пакету, и что при этом отброшено.
    stage: str = "ожидает"
    pack: ContentPack | None = None
    ingest_seconds: float = 0.0
    ingest_warnings: list[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    @property
    def done_count(self) -> int:
        return sum(1 for state in self.variants.values() if state.result is not None)

    def ready(self) -> list[VariantState]:
        return [state for state in self.variants.values() if state.result is not None]


_RUNS: dict[str, RunState] = {}
_LOCK = threading.Lock()


def get(run_id: str) -> RunState | None:
    with _LOCK:
        return _RUNS.get(run_id)


def latest() -> RunState | None:
    with _LOCK:
        if not _RUNS:
            return None
        return max(_RUNS.values(), key=lambda state: state.started_at)


def start(
    template_path: str | Path,
    request: IngestInput | ContentPack,
    cfg: Config,
    client: StructuredClient,
    variants: list[str],
    output_root: str | Path,
    vlm_client: StructuredClient | None = None,
    fix_mode: str | None = None,
) -> RunState:
    """Запускает прогон в фоне и сразу возвращает его состояние."""
    run_id = datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S-%f")[:-3]
    state = RunState(
        run_id=run_id,
        template_name=Path(template_path).name,
        started_at=datetime.now(UTC),
        variants={name: VariantState(name=name) for name in variants},
    )
    with _LOCK:
        _RUNS[run_id] = state

    def work() -> None:
        # План и находки текстового прохода не зависят от варианта вёрстки:
        # оба считаются один раз. Раскладка — по очереди (вариант избегает
        # композиций предыдущих), сборка и аудит — параллельно, как в CLI.
        prepared = None
        shared_text = SharedTextPass()
        try:
            # Разбор входа — внутри прогона и внутри его секундомера: бюджет
            # в пять минут считается на всю колоду, вместе с ним.
            if isinstance(request, ContentPack):
                pack = request
            else:
                state.stage = "разбираю вход"
                ingested = ingest(request, client, default_purpose=DeckPurpose(cfg.deck.purpose))
                pack = ingested.pack
                state.ingest_warnings = ingested.warnings
            state.pack = pack
            state.ingest_seconds = state.elapsed
            state.stage = "собираю варианты"
            laid_out = []
            for name in variants:
                variant_state = state.variants[name]

                def on_stage(stage: str, target: VariantState = variant_state) -> None:
                    target.stage = stage

                laid = lay_out_variant(
                    template_path=template_path,
                    pack=pack,
                    cfg=cfg,
                    client=client,
                    variant=name,
                    output_dir=Path(output_root) / run_id / name,
                    run_id=f"{run_id}-{name}",
                    vlm_client=vlm_client,
                    fix_mode=fix_mode,
                    prepared=prepared,
                    text_findings=shared_text,
                    on_stage=on_stage,
                )
                prepared = laid.prepared
                laid_out.append((variant_state, laid))

            def complete(item: tuple[VariantState, LaidOut]) -> None:
                variant_state, laid = item
                variant_state.result = complete_variant(laid)
                variant_state.stage = "готово"

            with ThreadPoolExecutor(max_workers=len(laid_out)) as pool:
                # `list` — чтобы исключение варианта дошло сюда, а не пропало.
                list(pool.map(complete, laid_out))
        except Exception:  # поток не должен умирать молча: иначе страница ждёт вечно
            state.error = traceback.format_exc(limit=4)
            for variant_state in state.variants.values():
                if variant_state.result is None:
                    variant_state.stage = "прервано"
        finally:
            state.finished = True
            state.stage = "готово" if state.error is None else "прервано"

    threading.Thread(target=work, name=f"deckwright-{run_id}", daemon=True).start()
    return state


def apply(state: RunState, variant: str, client: StructuredClient | None = None) -> None:
    """Применяет отмеченные находки и пересобирает вариант — тоже в фоне.

    Пересборка занимает секунды, а не минуты, но блокировать ими страницу
    всё равно нельзя: Streamlit на это время перестанет отвечать.
    """
    variant_state = state.variants[variant]
    if variant_state.result is None or variant_state.applying:
        return
    variant_state.applying = True
    variant_state.stage = "применяю исправления"
    chosen = set(variant_state.selected)

    def work() -> None:
        try:
            variant_state.result = apply_selection(variant_state.result, chosen, client)
            # Выбор не очищается: его держат галочки интерфейса. Исправленная
            # находка исчезает из отчёта вместе со своей галочкой, а
            # оставшаяся отмеченной честно означает, что она осталась.
            variant_state.stage = "готово"
        except Exception:
            variant_state.error = traceback.format_exc(limit=4)
            variant_state.stage = "ошибка при исправлении"
        finally:
            variant_state.applying = False

    threading.Thread(target=work, name=f"deckwright-fix-{variant}", daemon=True).start()
