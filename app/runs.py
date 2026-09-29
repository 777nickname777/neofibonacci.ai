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

import contextlib
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deckwright.audit import rewrite as rewrite_step
from deckwright.audit.contextual.runner import SharedAudit
from deckwright.config import Config
from deckwright.content.ingest import IngestInput, ingest
from deckwright.llm.base import StructuredClient
from deckwright.pipeline import (
    STAGE_INGEST,
    STAGE_PLAN,
    STAGE_TEMPLATE,
    LaidOut,
    PipelineResult,
    apply_selection,
    complete_variant,
    lay_out_variant,
    stage_times,
)
from deckwright.render.pdf import ConversionError, ConversionReport, pptx_to_pdf
from deckwright.render.png import pdf_to_png
from deckwright.schemas import ContentPack, ConversionAttempt, DeckPurpose, FixKind
from deckwright.workspace import RunWorkspace


@dataclass
class VariantState:
    """Что известно про один вариант вёрстки прямо сейчас.

    Состояние генерации и состояние экспорта разделены намеренно: колода
    может быть собрана, а PDF не получен, и это не повод объявлять вариант
    проваленным. `export_errors` перечисляет форматы, которых не хватает.
    """

    name: str
    stage: str = "ожидает"
    result: PipelineResult | None = None
    error: str | None = None
    # Файлы варианта, готовые до аудита. Аудит по картинкам занимает
    # 80-111 с из 170-262 с прогона, а колода к его началу уже собрана,
    # проверена на целостность и выгружена: держать её до конца проверки
    # значит отдавать то же самое, но на полторы минуты позже. Находки
    # приходят следом и дописываются во вкладку.
    files: dict[str, Path] = field(default_factory=dict)
    early_export_errors: dict[str, str] = field(default_factory=dict)
    early_slides: int = 0
    # Идёт ли прямо сейчас повторный экспорт (без перегенерации).
    exporting: bool = False
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
    # Сессия браузера, которой принадлежит прогон. Реестр общий на процесс,
    # поэтому без владельца номер прогона открывал бы чужие результаты.
    owner: str = ""
    workspace: RunWorkspace | None = None
    variants: dict[str, VariantState] = field(default_factory=dict)
    finished: bool = False
    error: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    # Вход, приведённый к контент-пакету, и что при этом отброшено.
    stage: str = "ожидает"
    pack: ContentPack | None = None
    ingest_seconds: float = 0.0
    ingest_warnings: list[str] = field(default_factory=list)
    # Манифесты разложенных вариантов: в них разбор шаблона и план — видны,
    # пока идут сборка и аудит.
    laid_manifests: list = field(default_factory=list)
    # Снимок настроек прогона. Неизменяемый: страница может переключить
    # что угодно, но идущий прогон обязан доработать на том, с чем начат, —
    # иначе исправление применяется по одним правилам, а отчёт объясняется
    # по другим.
    cfg: Config | None = None
    # Отмена. Внешний запрос к модели прервать нельзя — HTTP уже в пути, —
    # поэтому отмена значит две вещи: новых задач не ставим и поздний ответ
    # не публикуем. Обещать мгновенную остановку было бы неправдой.
    cancelled: bool = False

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 1)

    def stages(self) -> dict[str, float]:
        """Время по этапам: что уже закончилось, по часам (`pipeline.stage_times`).

        Пока варианты собираются, известны разбор входа, шаблона и план; к
        концу прогона — все пять этапов. На сайте живой прогон шёл 418.9 с при
        бюджете 300, и по одному общему числу нельзя было сказать, где.
        """
        manifests = [
            state.result.manifest for state in self.variants.values() if state.result is not None
        ]
        if manifests and len(manifests) == len(self.variants):
            return stage_times(self.ingest_seconds, manifests)
        known = {STAGE_INGEST: self.ingest_seconds} if self.pack is not None else {}
        if self.laid_manifests:
            laid = stage_times(self.ingest_seconds, self.laid_manifests)
            known = {
                name: laid[name] for name in (STAGE_TEMPLATE, STAGE_INGEST, STAGE_PLAN)
            }
        return known

    @property
    def done_count(self) -> int:
        return sum(1 for state in self.variants.values() if state.result is not None)

    def ready(self) -> list[VariantState]:
        return [state for state in self.variants.values() if state.result is not None]

    def summary(self) -> dict:
        """Паспорт прогона для диагностики: что, когда, чем кончилось.

        Секретов и содержимого пользовательских документов здесь нет — только
        номер прогона, этапы, форматы, попытки экспорта и причины отказов.
        Имя шаблона оставлено: без него в каталоге не разобраться.
        """
        variants = {}
        for name, vs in self.variants.items():
            entry: dict = {"stage": vs.stage, "error": vs.error}
            if vs.result is not None:
                entry["formats"] = sorted(vs.result.available_formats())
                entry["missing"] = vs.result.export_errors
                entry["slides"] = len(vs.result.deck.slides)
                entry["findings"] = len(vs.result.report.issues)
                entry["export_attempts"] = [
                    {
                        "target": Path(c.target).name,
                        "ok": c.ok,
                        "attempts": c.attempts,
                        "seconds": c.seconds,
                        "pages": c.pages,
                        "error_kind": c.error_kind,
                        "error": c.error,
                    }
                    for c in vs.result.manifest.conversions
                ]
                entry["artifacts"] = {
                    k: Path(v).name for k, v in vs.result.manifest.artifacts.items()
                }
            variants[name] = entry
        return {
            "run_id": self.run_id,
            "template": self.template_name,
            "started_at": self.started_at.isoformat(),
            "finished": self.finished,
            "stage": self.stage,
            "elapsed_seconds": self.elapsed,
            "ingest_seconds": self.ingest_seconds,
            "stages": self.stages(),
            "error": self.error,
            "variants": variants,
        }

    def write_summary(self) -> Path | None:
        """Кладёт паспорт прогона в его каталог диагностики."""
        if self.workspace is None:
            return None
        target = self.workspace.diag / "run.json"
        try:
            target.write_text(
                json.dumps(self.summary(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):  # диагностика не роняет прогон
            return None
        return target


_RUNS: dict[str, RunState] = {}
_LOCK = threading.Lock()


def get(run_id: str, owner: str | None = None) -> RunState | None:
    """Прогон по номеру — только своему владельцу.

    `owner` — идентификатор сессии браузера. Без совпадения прогон не
    отдаётся: раньше номер прогона был единственным ключом, и его знание
    открывало чужие результаты и чужие кнопки скачивания.

    `owner=None` оставлен для CLI и тестов, где сессии нет вовсе; из
    пользовательского пути так звать нельзя.
    """
    with _LOCK:
        state = _RUNS.get(run_id)
    if state is None:
        return None
    if owner is not None and state.owner != owner:
        return None
    return state


def latest(owner: str) -> RunState | None:
    """Последний прогон **этой** сессии.

    Раньше функция отдавала последний прогон вообще, и человек, открывший
    страницу без номера в адресе, видел чужую работу. Область поиска теперь
    обязательный аргумент — глобального «последнего» больше нет.
    """
    with _LOCK:
        mine = [s for s in _RUNS.values() if s.owner == owner]
    return max(mine, key=lambda state: state.started_at) if mine else None


def forget(run_id: str, owner: str, *, keep_outputs: bool = True) -> bool:
    """Убирает прогон этой сессии из реестра и чистит его каталог.

    Чужой прогон не трогается: без совпадения владельца возвращается `False`
    и на диске ничего не меняется. Уборка идёт только внутри каталога своего
    прогона, поэтому соседний, идущий прямо сейчас, не задет.
    """
    with _LOCK:
        state = _RUNS.get(run_id)
        if state is None or state.owner != owner:
            return False
        if not state.finished:
            return False
        del _RUNS[run_id]
    if state.workspace is not None:
        state.workspace.cleanup(keep_outputs=keep_outputs)
    return True


class _Cancelled(Exception):
    """Прогон отменён пользователем. Не ошибка: решение, а не сбой."""


def cancel(run_id: str, owner: str) -> bool:
    """Отменяет прогон своей сессии. Чужой не отменяется.

    Идущий вызов модели или конвертацию это не прерывает: HTTP-запрос уже
    отправлен, а `soffice` работает в своём процессе. Отмена значит, что
    новых задач прогон не поставит, а поздний результат не опубликует.
    """
    state = get(run_id, owner)
    if state is None or state.finished:
        return False
    state.cancelled = True
    state.stage = "отменяю"
    return True


def start(
    template_path: str | Path,
    request: IngestInput | ContentPack,
    cfg: Config,
    client: StructuredClient,
    variants: list[str],
    workspace: RunWorkspace,
    vlm_client: StructuredClient | None = None,
    fix_mode: str | None = None,
) -> RunState:
    """Запускает прогон в фоне и сразу возвращает его состояние.

    `workspace` задаёт и номер прогона, и владельца, и все каталоги: входы,
    варианты и диагностика лежат под ним и ни с кем не делятся.
    """
    run_id = workspace.run_id
    state = RunState(
        run_id=run_id,
        owner=workspace.owner,
        workspace=workspace,
        template_name=Path(template_path).name,
        started_at=datetime.now(UTC),
        variants={name: VariantState(name=name) for name in variants},
        cfg=cfg,
    )
    with _LOCK:
        _RUNS[run_id] = state

    def work() -> None:
        # План и находки текстового прохода не зависят от варианта вёрстки:
        # оба считаются один раз. Раскладка — по очереди (вариант избегает
        # композиций предыдущих), сборка и аудит — параллельно, как в CLI.
        prepared = None
        shared_audit = SharedAudit()
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
            if state.cancelled:
                raise _Cancelled
            state.stage = "собираю варианты"
            laid_out = []
            for name in variants:
                if state.cancelled:
                    raise _Cancelled
                variant_state = state.variants[name]

                def on_stage(stage: str, target: VariantState = variant_state) -> None:
                    target.stage = stage

                def on_export(
                    ready: dict, target: VariantState = variant_state
                ) -> None:
                    """Файлы готовы — отдать их, не дожидаясь аудита."""
                    target.files = {
                        fmt: path
                        for fmt in ("pptx", "pdf", "html")
                        if isinstance(path := ready.get(fmt), Path)
                        and path.is_file()
                        and path.stat().st_size > 0
                    }
                    target.early_export_errors = dict(ready.get("export_errors") or {})
                    target.early_slides = int(ready.get("slides") or 0)

                laid = lay_out_variant(
                    template_path=template_path,
                    pack=pack,
                    cfg=cfg,
                    client=client,
                    variant=name,
                    output_dir=workspace.variant(name),
                    run_id=f"{run_id}-{name}",
                    vlm_client=vlm_client,
                    fix_mode=fix_mode,
                    prepared=prepared,
                    text_findings=shared_audit,
                    on_stage=on_stage,
                    on_export=on_export,
                )
                prepared = laid.prepared
                laid_out.append((variant_state, laid))
                state.laid_manifests.append(laid.manifest)

            def complete(item: tuple[VariantState, LaidOut]) -> None:
                """Один вариант. Его падение не уносит соседние.

                Раньше исключение вылетало из `pool.map` наружу, и прогон
                помечал прервáнными все варианты разом — включая те, что уже
                собрались. Теперь отказ остаётся внутри своего варианта.
                """
                variant_state, laid = item
                if state.cancelled:
                    variant_state.stage = "отменено"
                    return
                try:
                    built = complete_variant(laid)
                    if state.cancelled:
                        # Ответ пришёл после отмены: его не публикуем —
                        # пользователь уже решил, что этот прогон ему не нужен.
                        variant_state.stage = "отменено"
                        return
                    variant_state.result = built
                    result = variant_state.result
                    variant_state.stage = (
                        "готово"
                        if result.complete
                        else "готово частично: " + ", ".join(sorted(result.export_errors))
                    )
                except Exception as failure:
                    variant_state.error = f"{type(failure).__name__}: {failure}"
                    variant_state.stage = "не собрался"
                    _write_diag(state, variant_state, failure)

            with ThreadPoolExecutor(max_workers=len(laid_out)) as pool:
                list(pool.map(complete, laid_out))
        except _Cancelled:
            for variant_state in state.variants.values():
                if variant_state.result is None:
                    variant_state.stage = "отменено"
        except Exception:  # поток не должен умирать молча: иначе страница ждёт вечно
            state.error = traceback.format_exc(limit=4)
            for variant_state in state.variants.values():
                if variant_state.result is None:
                    variant_state.stage = "прервано"
        finally:
            state.finished = True
            if state.cancelled:
                state.stage = "отменено"
            elif state.error is not None:
                state.stage = "прервано"
            elif any(v.result is None for v in state.variants.values()):
                state.stage = "готово не всё"
            elif any(v.result.export_errors for v in state.variants.values()):
                state.stage = "готово частично"
            else:
                state.stage = "готово"
            state.write_summary()

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


def _write_diag(state: RunState, variant_state: VariantState, failure: BaseException) -> None:
    """Складывает подробности отказа в каталог диагностики прогона.

    Человеку на странице нужна одна понятная строка; полный traceback нужен
    тому, кто будет чинить. Секретов и содержимого пользовательских
    документов здесь нет — только номер прогона, вариант, этап и стек.
    """
    if state.workspace is None:
        return
    try:
        target = state.workspace.diag / f"{variant_state.name}.error.txt"
        target.write_text(
            f"run_id: {state.run_id}\n"
            f"вариант: {variant_state.name}\n"
            f"этап: {variant_state.stage}\n"
            f"шаблон: {state.template_name}\n"
            f"время: {datetime.now(UTC).isoformat()}\n\n"
            + "".join(traceback.format_exception(failure)),
            encoding="utf-8",
        )
    except OSError:  # диагностика не должна ронять прогон вторично
        pass


def retry_export(state: RunState, variant: str, cfg: Config) -> None:
    """Повторяет ТОЛЬКО экспорт в PDF — в фоне, без перегенерации.

    Колода уже собрана и лежит на диске, план посчитан, модель отвечала.
    Повторять всё это ради одного внешнего процесса незачем и дорого: берём
    готовый `.pptx` и конвертируем его заново. Если получилось — вариант
    доукомплектовывается PDF и картинками, а отчёт аудита остаётся прежним:
    содержание колоды не менялось.
    """
    variant_state = state.variants.get(variant)
    if variant_state is None or variant_state.result is None:
        return
    if variant_state.exporting or "pdf" not in variant_state.result.export_errors:
        return
    variant_state.exporting = True
    variant_state.stage = "повторяю экспорт PDF"

    def work() -> None:
        result = variant_state.result
        report = ConversionReport(source=str(result.pptx))
        try:
            pdf = pptx_to_pdf(
                result.pptx,
                Path(result.pptx).parent,
                soffice_binary=cfg.render.soffice_binary,
                timeout_seconds=cfg.render.soffice_timeout_seconds,
                expected_pages=len(result.deck.slides),
                max_attempts=cfg.render.pdf_max_attempts,
                report=report,
            )
            result.pdf = pdf
            result.export_errors.pop("pdf", None)
            try:
                result.pages = pdf_to_png(
                    pdf, Path(result.pptx).parent / "png", dpi=cfg.render.png_dpi
                )
                result.export_errors.pop("png", None)
            except Exception as failure:  # картинки — не повод терять PDF
                result.export_errors["png"] = str(failure)
            variant_state.stage = "готово"
        except ConversionError as failure:
            result.export_errors["pdf"] = str(failure)
            variant_state.stage = "PDF снова не получился"
        finally:
            result.manifest.conversions.append(
                ConversionAttempt(
                    source=report.source, target=report.target, ok=report.ok,
                    attempts=report.attempts, seconds=report.seconds,
                    pages=report.pages, error_kind=report.error_kind,
                    error=report.error, notes=report.notes,
                )
            )
            result.manifest.export_errors = dict(result.export_errors)
            # Артефакты и паспорт прогона на диске обязаны догнать состояние:
            # после удачного повтора в манифесте должен появиться путь к PDF,
            # после неудачного — причина и число попыток.
            if result.pdf is not None:
                result.manifest.artifacts["pdf"] = str(result.pdf)
            if result.pages:
                result.manifest.artifacts["png_dir"] = str(Path(result.pptx).parent / "png")
            deck = Path(result.pptx)
            with contextlib.suppress(OSError):  # диагностика не роняет повтор
                deck.with_name(f"{deck.stem}.manifest.json").write_text(
                    result.manifest.model_dump_json(indent=2), encoding="utf-8"
                )
            variant_state.exporting = False
            state.write_summary()

    threading.Thread(
        target=work, name=f"deckwright-export-{state.run_id}-{variant}", daemon=True
    ).start()


def applicable(issue) -> bool:
    """Умеет ли «Применить отмеченное» что-то сделать с этой находкой.

    Автоматическая правка — да; переписывание текста моделью — да. Находка
    «к сведению» (плотность, повтор, вопрос к человеку) исправления не имеет:
    отмеченная, она не попадала в применяемое, и кнопка показывала «(0)» при
    отмеченных пунктах.
    """
    return issue.fix.kind is FixKind.AUTOMATIC or bool(rewrite_step.rewritable([issue]))
