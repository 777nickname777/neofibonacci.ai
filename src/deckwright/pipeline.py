"""Сквозной прогон: шаблон и контент → `.pptx`, `.pdf`, PNG, отчёт аудита.

Здесь слои соединяются и больше ничего не делается. Логики вёрстки, разбора и
экспорта в этом модуле нет — только порядок вызовов, цикл исправления, замер
времени по этапам и сборка манифеста.

Цикл «аудит → правка → пересборка → аудит» устроен так, чтобы решение
оставалось за человеком. Сам прогон трогает только находки с исправлением
типа `AUTOMATIC`: они предсказуемы и обратимы. Всё остальное — `ASSISTED` и
контекстные — применяется исключительно через `apply_selection`, то есть по
явному выбору, и ровно те находки, которые выбраны.

Режим задаётся конфигом (`run.fix_mode`):

* `review` — остановиться с отчётом, чтобы выбирал пользователь (умолчание ТЗ);
* `auto`   — применить автоматические находки самому и пересобрать колоду;
* `off`    — не трогать колоду вовсе.

Повторный аудит спрашивает модель только про изменённые слайды: вторая
итерация трогает два-три слайда из тридцати, а полный проход стоит как первый.
Контекстные находки по неизменённым слайдам переносятся из прошлого отчёта —
иначе пропуск выглядел бы как «проблема исчезла».
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deckwright.audit import rewrite as rewrite_step
from deckwright.audit.fixers import apply as apply_fixes
from deckwright.audit.report import audit_deck
from deckwright.config import Config
from deckwright.layout.capacity import achievable, bookend_limits
from deckwright.layout.matcher import build_deck_ir
from deckwright.layout.strategy import Strategy
from deckwright.layout.text_metrics import metrics_for_spec
from deckwright.llm.base import StructuredClient
from deckwright.parse.opener import parse_template
from deckwright.plan.budget import LengthBudget, compute_budget
from deckwright.plan.planner import Prompt, build_plan, slide_count_text
from deckwright.render.html import export_html
from deckwright.render.package_check import check_package
from deckwright.render.pdf import pptx_to_pdf
from deckwright.render.png import pdf_to_png
from deckwright.render.pptx_writer import render_deck, slide_is_single_image
from deckwright.schemas import (
    AuditReport,
    CheckKind,
    ContentPack,
    DeckIR,
    DeckPlan,
    FixIteration,
    FixKind,
    FontSubstitution,
    Issue,
    ModelUsage,
    RunManifest,
    Severity,
    StageTiming,
    TemplateSpec,
)


class PipelineResult:
    """Что получилось за один прогон одного варианта."""

    def __init__(
        self,
        spec: TemplateSpec,
        plan: DeckPlan,
        deck: DeckIR,
        layout_issues: list,
        audit: AuditReport,
        pptx: Path,
        pdf: Path,
        html: Path,
        pages: list[Path],
        manifest: RunManifest,
        context: _RunContext | None = None,
        prepared: PreparedPlan | None = None,
    ) -> None:
        self.spec = spec
        self.plan = plan
        self.deck = deck
        self.layout_issues = layout_issues
        self.audit = audit
        self.pptx = pptx
        self.pdf = pdf
        self.html = html
        self.pages = pages
        self.manifest = manifest
        # Всё, что нужно, чтобы пересобрать колоду по выбору пользователя, не
        # разбирая шаблон и не планируя заново. UI фазы 11 держит результат
        # между запросами и передаёт его обратно в `apply_selection`.
        self.context = context
        # План для следующего варианта: он от варианта не зависит.
        self.prepared = prepared

    @property
    def text_findings(self) -> list[Issue] | None:
        """Находки текстового прохода — следующему варианту, а не заново."""
        return self.context.text_findings if self.context is not None else None

    @property
    def report(self) -> AuditReport:
        """Синоним `audit`: отчёт называется отчётом в UI и в CLI."""
        return self.audit


@dataclass
class PreparedPlan:
    """План и всё, чем он обоснован, — чтобы не планировать его трижды.

    План не зависит от варианта вёрстки: варианты раскладывают одно и то же
    содержание по-разному. Три вызова планировщика на три варианта — это три
    одинаковых ответа за тройную цену: по замеру 34 с и $0.0054 каждый.
    """

    plan: DeckPlan
    prompt: Prompt
    budget: LengthBudget | None
    # Какие композиции варианты уже взяли: {вариант: {слайд плана: композиция}}.
    # По нему следующий вариант избегает чужих композиций (A12).
    layouts: dict[str, dict[int, str]] = field(default_factory=dict)


@dataclass
class _RunContext:
    """Состояние прогона, нужное для пересборки после выбора исправлений."""

    cfg: Config
    pack: ContentPack
    template_path: Path
    output_dir: Path
    stem: str
    variant: str
    preset: object
    budget: LengthBudget | None
    vlm_client: StructuredClient | None
    # Находки текстового прохода: он идёт по плану, а план у вариантов один.
    # Заполняется после первого аудита и переезжает в следующий вариант.
    text_findings: list[Issue] | None = None
    # Куда сообщать о начале этапа. Нужно интерфейсу: прогон идёт минуты.
    on_stage: Callable[[str], None] | None = None
    # Реестр композиций вариантов из `PreparedPlan`: пересборка после правки
    # обязана избегать того же, что и первая сборка.
    layouts: dict[str, dict[int, str]] | None = None


def plan_limits(spec: TemplateSpec, cfg: Config) -> tuple[tuple[str, int, int], ...]:
    """Ёмкость макетов шаблона для бюджета планировщика: {вид блока: пунктов, символов}.

    Считается слоем вёрстки тем же предсказателем, по которому она потом
    выбирает макет, и для всех вариантов сразу: план один на три варианта.
    Планировщику уходят только числа — про макеты он не знает.
    """
    hint = compute_budget(
        spec,
        cfg.audit.max_bullets_per_slide,
        cfg.audit.max_words_per_bullet,
        substitution_slack=cfg.fonts.substitution_slack,
    )
    strategies = [Strategy.from_config(variant) for variant in cfg.variants]
    capacity = achievable(
        spec,
        strategies,
        item_chars=hint.bullet_chars,
        title_chars=hint.title_chars,
        max_items=cfg.audit.max_bullets_per_slide,
    )
    return tuple(
        (kind.value, point.items, point.chars)
        for kind, curve in capacity.items()
        for point in curve
    ) + tuple(bookend_limits(spec))


def _step_params(model_cfg, steps: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Параметры шагов в том виде, в каком они ушли в запрос."""
    return {name: model_cfg.step(name).model_dump() for name in steps}


@contextmanager
def _timed(manifest: RunManifest, stage: str, on_stage: Callable[[str], None] | None = None):
    """Замер этапа. `on_stage` зовётся в начале — интерфейсу нужно «идёт», а
    не «закончилось»: прогон занимает минуты, и молчащая страница выглядит
    зависшей."""
    if on_stage is not None:
        on_stage(stage)
    started = time.monotonic()
    try:
        yield
    finally:
        manifest.timings.append(
            StageTiming(stage=stage, seconds=round(time.monotonic() - started, 3))
        )


@dataclass
class _Built:
    """Артефакты одной сборки колоды и её отчёт аудита."""

    pptx: Path
    pdf: Path
    html: Path
    pages: list[Path]
    report: AuditReport


def _merge_contextual(
    previous: AuditReport | None, fresh: AuditReport, rechecked: set[int] | None
) -> AuditReport:
    """Свежий отчёт плюс контекстные находки по непереспрошенным слайдам.

    Детерминированные проверки идут по всей колоде всегда — они дёшевы.
    Контекстные задавались только про изменённые слайды, и молча потерять
    ответы про остальные значит соврать, что проблем там нет.
    """
    if previous is None or rechecked is None:
        return fresh
    # Находки текстового прохода относятся к колоде, а числятся за слайдом 1
    # или 2, и в свежий отчёт они уже вошли переиспользованием. Без сверки по
    # ключу они переносились второй раз: в прогоне #12 `title_is_takeaway` и
    # `one_sentence_summary` стояли в отчёте каждого варианта дважды.
    present = {issue.key for issue in fresh.issues}
    carried = [
        issue
        for issue in previous.issues
        if issue.kind is CheckKind.CONTEXTUAL
        and issue.slide_index not in rechecked
        and issue.key not in present
    ]
    if not carried:
        return fresh
    merged = fresh.model_copy(deep=True)
    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    merged.issues = sorted(
        merged.issues + carried,
        key=lambda issue: (order[issue.severity], issue.slide_index, issue.check_id),
    )
    return merged


def _build(
    deck: DeckIR,
    plan: DeckPlan,
    spec: TemplateSpec,
    pack: ContentPack,
    ctx: _RunContext,
    manifest: RunManifest,
    template_path: Path,
    only_slides: set[int] | None = None,
    previous: AuditReport | None = None,
) -> _Built:
    """Одна сборка: `.pptx` → проверка пакета → `.pdf` → PNG → HTML → аудит.

    Вызывается и на первой сборке, и на каждой пересборке после исправления:
    иначе форматы разъехались бы с представлением, а отчёт — с колодой.
    """
    cfg = ctx.cfg
    output_dir = ctx.output_dir
    stem = ctx.stem

    with _timed(manifest, "render_pptx", ctx.on_stage):
        pptx_path = render_deck(deck, spec, template_path, output_dir / f"{stem}.pptx")

    # Целостность пакета проверяется здесь, а не в тестах: LibreOffice о битых
    # ссылках молчит, и без этой проверки поломка доедет до PowerPoint.
    with _timed(manifest, "verify_package", ctx.on_stage):
        check_package(pptx_path).raise_if_broken(pptx_path)
        single_image = slide_is_single_image(pptx_path)
        if single_image:
            manifest.warnings.append(
                f"слайды {single_image} состоят из одной картинки — ТЗ такое не засчитывает"
            )

    with _timed(manifest, "render_pdf", ctx.on_stage):
        pdf_path = pptx_to_pdf(
            pptx_path,
            output_dir,
            soffice_binary=cfg.render.soffice_binary,
            timeout_seconds=cfg.render.soffice_timeout_seconds,
            # Шрифты, извлечённые из шаблона: иначе картинка рисуется не тем
            # шрифтом, которым фиттер мерил текст.
            font_dirs={
                Path(token.file_path).parent
                for token in spec.fonts
                if token.embedded and token.file_path
            },
        )

    with _timed(manifest, "render_png", ctx.on_stage):
        # Растеризация — самая дорогая часть пересборки (18 с из 20 на колоде
        # holdout), а итерация цикла трогает два-три слайда. Перерисовываются
        # только их страницы: остальные страницы нового `.pdf` побайтово те
        # же, потому что вёрстка каждого слайда не зависит от соседей.
        # Если число страниц изменилось, нумерация поехала — `pdf_to_png`
        # сам возвращается к полной растеризации.
        pages = pdf_to_png(
            pdf_path,
            output_dir / "png",
            dpi=cfg.render.png_dpi,
            only_pages=only_slides,
        )

    with _timed(manifest, "render_html", ctx.on_stage):
        html_path = export_html(
            deck,
            output_dir / f"{stem}.html",
            spec=spec,
            pptx_path=pptx_path,
            title=plan.title,
        )

    # Число страниц PDF обязано совпадать с числом слайдов: расхождение значит,
    # что конвертация потеряла или удвоила слайд, и заметить это можно только
    # сравнением.
    if len(pages) != len(deck.slides):
        manifest.warnings.append(
            f"страниц в PDF {len(pages)}, а слайдов в колоде {len(deck.slides)}"
        )

    with _timed(manifest, "audit", ctx.on_stage):
        report = audit_deck(
            deck,
            spec,
            plan,
            pack,
            cfg,
            pptx_path=pptx_path,
            pages=pages,
            client=ctx.vlm_client,
            only_slides=only_slides,
            text_findings=ctx.text_findings,
        )
        report = _merge_contextual(previous, report, only_slides)
        if ctx.text_findings is None:
            text_ids = set(cfg.audit.checks_by_mode("text"))
            ctx.text_findings = [
                issue for issue in report.issues if issue.check_id in text_ids
            ]
        (output_dir / f"{stem}.audit.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )

    return _Built(pptx_path, pdf_path, html_path, pages, report)


def _fix_iteration(
    chosen: list[Issue],
    deck: DeckIR,
    plan: DeckPlan,
    spec: TemplateSpec,
    ctx: _RunContext,
    client: StructuredClient | None,
    record: FixIteration,
    manifest: RunManifest,
) -> tuple[DeckIR, DeckPlan, set[int], list]:
    """Применяет выбранные находки. Возвращает колоду, план и что изменилось.

    Порядок неслучаен. Переписывание текста меняет **план**, и колода после
    него верстается заново — значит идентификаторы элементов, на которые
    ссылались геометрические находки, относятся к прошлой вёрстке. Поэтому в
    итерации с переписыванием механические правки не применяются: они будут
    пересчитаны следующим аудитом по свежему представлению. Применять правку
    по устаревшей ссылке значит двигать не тот элемент.
    """
    layout_issues: list = []
    rewrite_targets = rewrite_step.rewritable(chosen)

    if rewrite_targets and ctx.cfg.run.rewrite_assisted and client is not None:
        outcome = rewrite_step.rewrite(
            plan, rewrite_targets, client, budget=ctx.budget
        )
        record.rewritten_slides = list(outcome.rewritten)
        # Версия промпта переписывания идёт в паспорт прогона на тех же
        # правах, что и версия промпта планировщика: текст колоды теперь
        # зависит и от неё.
        if outcome.prompt is not None:
            entry = outcome.prompt.as_manifest_entry()
            if entry not in manifest.prompts:
                manifest.prompts.append(entry)
        for index, reason in outcome.rejected.items():
            record.skipped[f"слайд {index}"] = reason
        if outcome.rewritten:
            record.applied.extend(
                issue.key
                for issue in rewrite_targets
                if issue.slide_index in set(outcome.rewritten)
            )
            deck, layout_issues = build_deck_ir(
                spec, outcome.plan, ctx.preset, pack=ctx.pack, siblings=ctx.layouts
            )
            return deck, outcome.plan, set(outcome.rewritten), layout_issues
    elif rewrite_targets:
        reason = (
            "переписывание моделью выключено (run.rewrite_assisted): "
            "находка требует редактирования текста"
            if client is not None
            else "модель недоступна: находка требует редактирования текста"
        )
        for issue in rewrite_targets:
            record.skipped[issue.key] = reason

    # Сравнение по тождеству, а не по значению: две находки одной проверки на
    # одном элементе равны как объекты, и отбор по значению выкинул бы обе.
    rewrite_ids = {id(issue) for issue in rewrite_targets}
    mechanical = [issue for issue in chosen if id(issue) not in rewrite_ids]
    outcome = apply_fixes(deck, mechanical, spec)
    record.applied.extend(outcome.applied)
    for check_id, reason in outcome.skipped.items():
        record.skipped.setdefault(check_id, reason)
    return deck, plan, outcome.changed_slides, layout_issues


def _fix_loop(
    deck: DeckIR,
    plan: DeckPlan,
    spec: TemplateSpec,
    built: _Built,
    ctx: _RunContext,
    manifest: RunManifest,
    template_path: Path,
    select,
    client: StructuredClient | None,
    layout_issues: list,
) -> tuple[DeckIR, DeckPlan, _Built, list]:
    """Крутит «правка → пересборка → повторный аудит» до предела из конфига.

    `select` решает, что брать в работу на каждой итерации: автоматический
    режим берёт находки с исправлением `AUTOMATIC`, выбор пользователя — ровно
    отмеченные им. Цикл останавливается, когда применять нечего или когда
    исчерпан `run.max_fix_iterations`; всё, что осталось, попадает в
    `manifest.unresolved` под своим ключом, а не исчезает.
    """
    limit = ctx.cfg.run.max_fix_iterations
    # Детерминированная правка, после которой находка осталась, второй раз
    # не применяется: тот же вход даст тот же выход, а итерация стоит полной
    # пересборки изменённых слайдов. Переписывание моделью сюда не входит —
    # его повтор может дать другой текст.
    exhausted: set[str] = set()
    for number in range(1, limit + 1):
        chosen = [issue for issue in select(built.report) if issue.key not in exhausted]
        if not chosen:
            break

        started = time.monotonic()
        record = FixIteration(number=number, issues_before=len(built.report.issues))
        deck, plan, changed, fresh_layout_issues = _fix_iteration(
            chosen, deck, plan, spec, ctx, client, record, manifest
        )
        if fresh_layout_issues:
            layout_issues = fresh_layout_issues
        exhausted |= {
            issue.key
            for issue in chosen
            if issue.fix.kind is FixKind.AUTOMATIC and issue.key in record.applied
        }
        if not record.applied:
            # Ничего не применилось — пересобирать нечего, и следующая
            # итерация повторила бы тот же отказ.
            record.issues_after = record.issues_before
            record.seconds = round(time.monotonic() - started, 3)
            manifest.fix_iterations.append(record)
            break

        # Переспрашиваем модель только про изменённые слайды, если это
        # разрешено конфигом. Выключенный пропуск — это честный полный проход
        # ценой ещё одного вызова на слайд.
        recheck = changed if ctx.cfg.audit.skip_unchanged_slides else None
        record.rechecked_slides = sorted(changed)
        built = _build(
            deck,
            plan,
            spec,
            ctx.pack,
            ctx,
            manifest,
            template_path,
            only_slides=recheck,
            previous=built.report,
        )
        record.issues_after = len(built.report.issues)
        record.seconds = round(time.monotonic() - started, 3)
        manifest.fix_iterations.append(record)

    remaining = select(built.report)
    manifest.unresolved = [issue.key for issue in remaining]
    if remaining and manifest.fix_iterations:
        manifest.warnings.append(
            f"после {len(manifest.fix_iterations)} итераций осталось "
            f"{len(remaining)} находок: предел итераций исчерпан"
        )
    return deck, plan, built, layout_issues


def _record_vlm(manifest: RunManifest, cfg: Config, client: StructuredClient | None) -> None:
    """Модель со зрением в паспорт прогона: контекстный проход — это вызовы.

    Без этой записи манифест показывает только планирование, а половина
    обращений к моделям остаётся невидимой.
    """
    if client is None or any(usage.role == "vlm" for usage in manifest.models):
        return
    manifest.models.append(
        ModelUsage(
            role="vlm",
            model="recorded" if client.mocked else cfg.vlm.model,
            base_url="" if client.mocked else cfg.vlm.base_url,
            steps=_step_params(cfg.vlm, ("audit_slide", "audit_deck")),
            calls=getattr(client, "calls", 0),
            prompt_tokens=getattr(client, "prompt_tokens", 0),
            completion_tokens=getattr(client, "completion_tokens", 0),
            retries=getattr(client, "retries", 0),
            thinking_blocks=getattr(client, "thinking_blocks", 0),
            rate_limit_hits=getattr(client, "rate_limit_hits", 0),
            slowest_call_seconds=getattr(client, "slowest_call_seconds", 0.0),
            hedges=getattr(client, "hedges", 0),
            hedge_wins=getattr(client, "hedge_wins", 0),
            cost_usd=cfg.vlm.cost_usd(
                getattr(client, "prompt_tokens", 0), getattr(client, "completion_tokens", 0)
            ),
            dropped_params=sorted(getattr(client, "dropped_params", ())),
            mocked=client.mocked,
        )
    )


def _finish(
    result_manifest: RunManifest, cfg: Config, built: _Built, ctx: _RunContext
) -> None:
    """Дописывает манифест: артефакты, бюджет времени, файл на диске."""
    _record_vlm(result_manifest, cfg, ctx.vlm_client)
    result_manifest.finished_at = datetime.now(UTC)
    result_manifest.artifacts = {
        "pptx": str(built.pptx),
        "pdf": str(built.pdf),
        "html": str(built.html),
        "audit": str(ctx.output_dir / f"{ctx.stem}.audit.json"),
        "png_dir": str(ctx.output_dir / "png"),
    }
    if not result_manifest.within_budget(cfg.run.time_budget_seconds):
        # Бюджет — на генерацию трёх вариантов вместе; один вариант, сам по
        # себе вышедший за него, ломает бюджет наверняка.
        result_manifest.warnings.append(
            f"генерация варианта заняла {result_manifest.generation_seconds} с "
            f"при бюджете {cfg.run.time_budget_seconds} с на три варианта"
        )
    (ctx.output_dir / f"{ctx.stem}.manifest.json").write_text(
        result_manifest.model_dump_json(indent=2), encoding="utf-8"
    )


def run_variant(
    template_path: str | Path,
    pack: ContentPack,
    cfg: Config,
    client: StructuredClient,
    variant: str,
    output_dir: str | Path,
    run_id: str | None = None,
    vlm_client: StructuredClient | None = None,
    fix_mode: str | None = None,
    prepared: PreparedPlan | None = None,
    text_findings: list[Issue] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> PipelineResult:
    """Прогоняет один вариант вёрстки от шаблона до аудита.

    `vlm_client` отдельный от `client`: контекстные проверки идут в модель со
    зрением, а планирование — в текстовую. Без него выполняются только
    детерминированные проверки, и невыполненные честно перечисляются в
    `skipped_checks` отчёта.

    `fix_mode` перекрывает режим из конфига — это нужно командной строке и
    интерфейсу, где режим выбирается на запуск, а не на установку.

    `prepared` — готовый план с прошлого варианта. План от варианта не
    зависит, и планировать его заново для каждого из трёх значит трижды
    заплатить за один и тот же ответ. Ровно так же переезжает
    `text_findings`: вопросы текстового прохода аудита задаются по плану.
    """
    template_path = Path(template_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = fix_mode or cfg.run.fix_mode

    run_id = run_id or datetime.now(UTC).strftime("run-%Y%m%d-%H%M%S")
    manifest = RunManifest(
        run_id=run_id,
        started_at=datetime.now(UTC),
        template_sha256="0" * 64,
        template_name=template_path.name,
        seed=cfg.run.seed,
        variants=[variant],
        fix_mode=mode,
    )

    with _timed(manifest, "parse", on_stage):
        # Кэш по хэшу файла: три варианта вёрстки разбирают один и тот же
        # шаблон, а разбор колоды на полсотни слайдов занимает секунды.
        spec = parse_template(
            template_path,
            cache_dir=cfg.template.cache_dir,
            font_dir=cfg.fonts.extract_dir,
        )
    manifest.template_sha256 = spec.template_sha256
    manifest.warnings.extend(spec.warnings)
    # Подстановка шрифта расходится с задумкой дизайнера и ломает измерение
    # текста, поэтому она обязана быть видна в паспорте прогона.
    # Чем мерили текст и можно ли этому верить — в паспорт прогона.
    source = metrics_for_spec(spec)
    if source.substituted:
        manifest.font_substitutions.append(
            FontSubstitution(
                requested=source.requested,
                used=source.used,
                reason=(
                    "метрически совместимый клон: ширины совпадают, вёрстка не сдвигается"
                    if source.metric_compatible
                    else "ширины не совпадают с оригиналом, бюджет длины ужат"
                ),
            )
        )
    manifest.font_substitutions.extend(
        FontSubstitution(
            requested=token.family,
            used="системный подбор",
            reason="шрифт не встроен в шаблон",
        )
        for token in spec.fonts
        if token.usage_count > 0 and not token.embedded and token.family != source.requested
    )

    slide_count = slide_count_text(
        cfg.deck.slide_count, cfg.deck.min_slides, cfg.deck.max_slides
    )
    with _timed(manifest, "plan", on_stage):
        if prepared is not None:
            plan, prompt, budget = prepared.plan, prepared.prompt, prepared.budget
        else:
            plan, prompt, budget = build_plan(
                pack,
                client,
                slide_count,
                spec=spec,
                max_bullets=cfg.audit.max_bullets_per_slide,
                max_words_per_bullet=cfg.audit.max_words_per_bullet,
                substitution_slack=cfg.fonts.substitution_slack,
                block_limits=plan_limits(spec, cfg),
            )
            prepared = PreparedPlan(plan=plan, prompt=prompt, budget=budget)
    if budget is not None:
        manifest.warnings.append(
            f"бюджет длины ({budget.measured_with}): заголовок {budget.title_chars} симв, "
            f"пункт {budget.bullet_chars} симв, до {budget.max_bullets} пунктов"
        )
    manifest.prompts.append(prompt.as_manifest_entry())
    manifest.models.append(
        ModelUsage(
            role="llm",
            # A19 требует записывать параметры, а не только имя модели: та же
            # модель при другой температуре — другой прогон.
            steps=_step_params(cfg.llm, ("plan_deck", "shorten_text")),
            model="recorded" if client.mocked else cfg.llm.model,
            base_url="" if client.mocked else cfg.llm.base_url,
            calls=getattr(client, "calls", 0),
            prompt_tokens=getattr(client, "prompt_tokens", 0),
            completion_tokens=getattr(client, "completion_tokens", 0),
            retries=getattr(client, "retries", 0),
            thinking_blocks=getattr(client, "thinking_blocks", 0),
            rate_limit_hits=getattr(client, "rate_limit_hits", 0),
            slowest_call_seconds=getattr(client, "slowest_call_seconds", 0.0),
            hedges=getattr(client, "hedges", 0),
            hedge_wins=getattr(client, "hedge_wins", 0),
            cost_usd=cfg.llm.cost_usd(
                getattr(client, "prompt_tokens", 0), getattr(client, "completion_tokens", 0)
            ),
            dropped_params=sorted(getattr(client, "dropped_params", ())),
            mocked=client.mocked,
        )
    )

    with _timed(manifest, "layout", on_stage):
        # Вариант передаётся пресетом, а не именем: плотность, предпочтение
        # композиций и поведение при переполнении — это он и есть.
        try:
            preset = cfg.variant(variant)
        except KeyError:
            preset = variant
        deck, layout_issues = build_deck_ir(
            spec, plan, preset, pack=pack, siblings=prepared.layouts
        )
    # Находки вёрстки о самой себе едут дальше вместе с колодой: текст, не
    # влезший на минимальной ступени шкалы, обязан быть виден, а не обрезан
    # молча.
    manifest.warnings.extend(issue.message for issue in layout_issues)

    ctx = _RunContext(
        cfg=cfg,
        pack=pack,
        template_path=template_path,
        output_dir=output_dir,
        stem=f"{template_path.stem}_{variant}",
        variant=variant,
        preset=preset,
        budget=budget,
        vlm_client=vlm_client,
        text_findings=text_findings,
        on_stage=on_stage,
        layouts=prepared.layouts,
    )
    built = _build(deck, plan, spec, pack, ctx, manifest, template_path)

    if mode == "auto":
        deck, plan, built, layout_issues = _fix_loop(
            deck,
            plan,
            spec,
            built,
            ctx,
            manifest,
            template_path,
            select=lambda report: report.auto_fixable,
            client=client,
            layout_issues=layout_issues,
        )

    _finish(manifest, cfg, built, ctx)
    return PipelineResult(
        spec,
        plan,
        deck,
        layout_issues,
        built.report,
        built.pptx,
        built.pdf,
        built.html,
        built.pages,
        manifest,
        context=ctx,
        prepared=prepared,
    )


def apply_selection(
    result: PipelineResult,
    keys: set[str] | list[str],
    client: StructuredClient | None = None,
) -> PipelineResult:
    """Применяет выбранные пользователем находки и пересобирает колоду (A16).

    Выбор — это ключи находок (`Issue.key`) из отчёта, который пользователь
    видел. Применяется ровно выбранное: находка, которую не отметили, остаётся
    нетронутой, даже если чинится автоматически.

    `ASSISTED` с текстовым исправлением уходит в переписывание моделью — но
    только при `run.rewrite_assisted` и только с переданным клиентом. Без
    этого находка остаётся в отчёте с причиной «требует редактирования»:
    выдумывать за автора, что сократить, прогон не будет. Контекстные находки
    не применяются никогда — у них нет операции, только показ.
    """
    ctx = result.context
    if ctx is None:
        raise ValueError(
            "результат собран без контекста прогона: пересобирать нечем. "
            "Передайте результат из run_variant."
        )

    selected = set(keys)
    # Манифест копируется: прошлый прогон остаётся тем, чем был. Пересборка по
    # выбору — это новое состояние колоды, а не переписывание её истории.
    manifest = result.manifest.model_copy(deep=True)
    manifest.fix_mode = "selected"
    manifest.fix_iterations = []
    manifest.unresolved = []
    built = _Built(result.pptx, result.pdf, result.html, result.pages, result.audit)

    deck, plan, built, layout_issues = _fix_loop(
        result.deck,
        result.plan,
        result.spec,
        built,
        ctx,
        manifest,
        ctx.template_path,
        select=lambda report: report.select(selected),
        client=client,
        layout_issues=result.layout_issues,
    )

    _finish(manifest, ctx.cfg, built, ctx)
    return PipelineResult(
        result.spec,
        plan,
        deck,
        layout_issues,
        built.report,
        built.pptx,
        built.pdf,
        built.html,
        built.pages,
        manifest,
        context=ctx,
        prepared=result.prepared,
    )
