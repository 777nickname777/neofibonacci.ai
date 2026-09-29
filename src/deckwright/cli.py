"""Точка входа командной строки."""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from deckwright.audit.contextual.runner import SharedAudit
from deckwright.config import load_config
from deckwright.content.ingest import IngestError, IngestInput, ingest
from deckwright.content.readers import UnsupportedInput
from deckwright.environment import run_checks
from deckwright.llm.base import StructuredClient
from deckwright.llm.fake import RecordedClient
from deckwright.pipeline import complete_variant, lay_out_variant, run_variant, stage_times
from deckwright.schemas import ContentPack, DeckPurpose, RunSummary

DEFAULT_CONFIG = Path("configs/config.yaml")


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Готовность окружения: что сломано насмерть, а что просто недоступно.

    Разница не косметическая. Без LibreOffice прогон всё равно соберёт
    `.pptx` и `.html`, и объявлять такое окружение негодным — значит
    отговаривать человека от работы, которая работает.
    """
    soffice = "soffice"
    cfg = None
    if args.config and Path(args.config).exists():
        try:
            cfg = load_config(args.config)
        except Exception as exc:  # конфиг мог быть отредактирован руками
            print(f"✗ конфигурация {args.config}: {exc}", file=sys.stderr)
            return 1
        soffice = cfg.render.soffice_binary
        print(f"✓ конфигурация {args.config}: вариантов {len(cfg.variants)}")

    checks = run_checks(soffice, cfg)
    for check in checks:
        mark = "✓" if check.ok else ("✗" if check.blocking else "!")
        stream = sys.stdout if check.ok else sys.stderr
        print(f"{mark} {check.name}: {check.detail}", file=stream)
        if not check.ok and check.affects:
            print(f"    недоступно: {check.affects}", file=stream)

    blocking = [c.name for c in checks if not c.ok and c.blocking]
    degraded = [c.name for c in checks if not c.ok and not c.blocking]
    if degraded:
        print(
            f"\nРаботает не всё: {', '.join(degraded)}. Остальное — как обычно.",
            file=sys.stderr,
        )
    if blocking:
        print(
            f"\nБез этого прогон не пойдёт: {', '.join(blocking)}. "
            "См. README, раздел «Сетап».",
            file=sys.stderr,
        )
        return 1
    return 0


def _make_client(cfg, recorded_dir: str | None) -> StructuredClient:
    """Записанные ответы, если каталог задан или модель не настроена.

    Прогон без ключа обязан работать: сквозной тест идёт в CI, где живого
    endpoint'а нет, и падать там из-за отсутствия секрета бессмысленно.
    """
    if recorded_dir:
        return RecordedClient(recorded_dir)
    if not cfg.llm.configured:
        raise SystemExit(
            "Модель не настроена: заполните LLM_BASE_URL, LLM_API_KEY и LLM_MODEL "
            "в .env, либо укажите --recorded с каталогом записанных ответов."
        )
    from deckwright.llm.client import LiveClient

    return LiveClient(cfg.llm)


def _make_vlm_client(cfg, recorded_dir: str | None) -> StructuredClient | None:
    """Модель со зрением для контекстного аудита, если она настроена.

    Без неё прогон не падает: контекстные проверки перечисляются в отчёте как
    невыполненные. Но и молча обходиться без неё при настроенном ключе
    нельзя — тогда время генерации меряется без аудита, и бюджет (A18)
    сверяется не с тем прогоном, который увидит пользователь интерфейса.
    """
    if recorded_dir or not cfg.vlm.configured:
        return None
    from deckwright.llm.client import LiveClient

    return LiveClient(cfg.vlm)


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    # Шаблон и контент берутся из конфига, если не заданы аргументами: A22
    # требует воспроизводимого запуска одной командой с конфиг-файлом.
    template = args.template or cfg.run.template
    content = args.content or cfg.run.content
    files = [Path(path) for path in ([content] if content else []) + list(args.input)]
    missing = [name for name, value in (("--template", template),) if value is None]
    if not files and not args.brief.strip():
        missing.append("--content, --input или --brief")
    if missing:
        print(
            f"Не задано: {', '.join(missing)}. Укажите аргументами или пропишите "
            f"run.template и run.content в {args.config}.",
            file=sys.stderr,
        )
        return 2

    # Бюджет — на всю колоду, включая разбор входа: приведение входа к
    # контент-пакету — вызов модели, и он меряется тем же секундомером.
    run_started = time.monotonic()
    client = _make_client(cfg, args.recorded)
    try:
        ingested = ingest(
            IngestInput(
                text=args.brief,
                files=files,
                purpose=DeckPurpose(args.purpose) if args.purpose else None,
                author=args.author,
                author_role=args.author_role,
            ),
            client,
            default_purpose=DeckPurpose(cfg.deck.purpose),
        )
    except (IngestError, UnsupportedInput) as exc:
        print(f"Вход не разобран: {exc}", file=sys.stderr)
        return 2
    pack = ingested.pack
    pack_seconds = time.monotonic() - run_started
    print(
        f"[вход] {len(ingested.sources) or 1} документ(ов), фактов {len(pack.facts)}, "
        f"рядов {len(pack.series)}, цитат {len(pack.quotes)}, тема «{pack.brief.topic}», "
        f"назначение {pack.brief.purpose.value}; разбор {pack_seconds:.1f}с"
    )
    for warning in ingested.warnings:
        print(f"[вход] ⚠ {warning}", file=sys.stderr)
    if args.save_pack:
        Path(args.save_pack).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_pack).write_text(pack.model_dump_json(indent=2), encoding="utf-8")
    vlm_client = _make_vlm_client(cfg, args.recorded)

    variants = [v.name for v in cfg.variants] if args.variant is None else [args.variant]
    output_root = Path(args.output or cfg.run.output_dir)

    # Бюджет ТЗ — на генерацию трёх вариантов вместе, без разбора входа.
    # Сверяется итог по часам, а не сумма манифестов: между вариантами тоже
    # идёт время.
    started_at = datetime.now(UTC)
    run_id = started_at.strftime("run-%Y%m%d-%H%M%S")
    variant_seconds: dict[str, float] = {}
    template_parse_seconds = 0.0

    # Раскладка — по очереди: вариант избегает композиций, взятых предыдущими,
    # и порядок здесь и есть результат. Секунды, модель зовётся один раз — на
    # план. Сборка, аудит и цикл исправления — параллельно: по очереди три
    # варианта на `zelenie_investicii` стоили 364 с из 300.
    # План от варианта не зависит: планируется один раз и переиспользуется —
    # иначе три одинаковых ответа модели стоят втрое дороже и втрое дольше.
    # Текстовый проход аудита идёт по плану и тоже задаётся один раз.
    shared_audit = SharedAudit()
    prepared = None
    laid_out = []
    for variant in variants:
        laid = lay_out_variant(
            template_path=template,
            pack=pack,
            cfg=cfg,
            client=client,
            variant=variant,
            output_dir=output_root / variant,
            fix_mode=args.fix,
            prepared=prepared,
            text_findings=shared_audit,
            vlm_client=vlm_client,
        )
        prepared = laid.prepared
        laid_out.append(laid)
    if args.save_plan:
        Path(args.save_plan).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_plan).write_text(
            prepared.plan.model_dump_json(indent=2), encoding="utf-8"
        )
    # Отказ одного варианта не уносит остальные: собранные колоды остаются на
    # диске и показываются, а упавший называется отдельно. Раньше исключение
    # из `pool.map` обрывало весь прогон вместе с готовыми вариантами.
    def _safe(laid):
        try:
            return complete_variant(laid)
        except Exception as failure:
            return failure

    with ThreadPoolExecutor(max_workers=len(laid_out)) as pool:
        outcomes = list(pool.map(_safe, laid_out))

    failed_variants: list[str] = []
    results = []
    for variant, outcome in zip(variants, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            failed_variants.append(variant)
            print(
                f"[{variant}] ✗ вариант не собран: "
                f"{type(outcome).__name__}: {outcome}",
                file=sys.stderr,
            )
            continue
        results.append(outcome)

    for variant, result in zip(
        [v for v in variants if v not in failed_variants], results, strict=True
    ):
        manifest = result.manifest
        variant_seconds[variant] = manifest.generation_seconds
        template_parse_seconds += manifest.parse_seconds
        stages = ", ".join(f"{t.stage} {t.seconds}с" for t in manifest.timings)
        print(
            f"[{variant}] {result.pptx.name}: {len(result.deck.slides)} слайдов, "
            f"{len(result.pages)} страниц PDF, {result.html.name}"
        )
        print(f"[{variant}] {stages}")
        print(
            f"[{variant}] разбор {manifest.parse_seconds}с, "
            f"генерация {manifest.generation_seconds}с"
        )
        # Счётчики клиента накопительные: у первого варианта в них план, у
        # следующих — ещё и переписывание. Повторы и 429 объясняют время
        # планирования лучше, чем сама цифра.
        for usage in manifest.models:
            if not usage.mocked:
                print(
                    f"[{variant}] модель {usage.role}: вызовов {usage.calls}, "
                    f"повторов {usage.retries}, ответов 429 {usage.rate_limit_hits}, "
                    f"токенов {usage.prompt_tokens}+{usage.completion_tokens}, "
                    f"самый долгий вызов {usage.slowest_call_seconds}с, "
                    f"дублей {usage.hedges} (ответил первым {usage.hedge_wins})"
                )

        # Что цикл исправления сделал с колодой. Молчать об этом нельзя:
        # прогон менял колоду, и человек обязан видеть, что именно.
        for record in manifest.fix_iterations:
            rewritten = (
                f", переписано слайдов {len(record.rewritten_slides)}"
                if record.rewritten_slides
                else ""
            )
            print(
                f"[{variant}] итерация {record.number}: применено "
                f"{len(record.applied)}, находок {record.issues_before} → "
                f"{record.issues_after}{rewritten}, {record.seconds}с"
            )
        _print_report(variant, result.report, manifest)

        for warning in manifest.warnings:
            print(f"[{variant}] ⚠ {warning}", file=sys.stderr)

    # Ответ каждого шага отдельно: по суммарным счётчикам не видно, план
    # или разбор входа съедает время.
    for role, used in (("llm", client), ("vlm", vlm_client)):
        for step, record in sorted(getattr(used, "by_step", {}).items()):
            print(
                f"[шаг] {role} {step}: вызовов {record.calls}, ответ "
                f"{record.completion_tokens} ток. / {record.answer_chars} симв., "
                f"самый долгий {record.slowest_seconds}с"
                + (f"; повтор из-за: {record.last_error}" if record.last_error else "")
            )

    total = time.monotonic() - run_started
    parse_seconds = round(pack_seconds + template_parse_seconds, 3)
    summary = RunSummary(
        run_id=run_id,
        template_name=Path(template).name,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        variant_seconds=variant_seconds,
        parse_seconds=parse_seconds,
        ingest_seconds=round(pack_seconds, 3),
        generation_seconds=round(total - parse_seconds, 3),
        total_seconds=round(total, 3),
        budget_seconds=cfg.run.time_budget_seconds,
        stages=stage_times(pack_seconds, [result.manifest for result in results]),
    )
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "run-summary.json").write_text(
        summary.model_dump_json(indent=2), encoding="utf-8"
    )
    print("[этапы] " + ", ".join(f"{name} {seconds}с" for name, seconds in summary.stages.items()))
    print(
        f"[прогон] разбор входа {summary.parse_seconds}с "
        f"(из них материалы {summary.ingest_seconds}с); "
        f"генерация вариантов ({len(variant_seconds)}) {summary.generation_seconds}с; "
        f"всего {summary.total_seconds}с из {summary.budget_seconds}с "
        f"({'в бюджете' if summary.within_budget else 'ВНЕ БЮДЖЕТА'})"
    )
    # Частичный результат остаётся результатом, но «успехом» не называется:
    # ненулевой код возврата нужен CI и скриптам.
    incomplete = [
        f"{variant}: {', '.join(sorted(result.export_errors))}"
        for variant, result in zip(
            [v for v in variants if v not in failed_variants], results, strict=True
        )
        if result.export_errors
    ]
    if incomplete:
        print("[прогон] ⚠ получены не все форматы — " + "; ".join(incomplete), file=sys.stderr)
    if failed_variants:
        print(
            f"[прогон] ✗ не собраны варианты: {', '.join(failed_variants)}",
            file=sys.stderr,
        )
    return 1 if (failed_variants or incomplete) else 0


def _print_report(variant: str, report, manifest) -> None:
    """Отчёт аудита в терминал: в режиме `review` выбирать будет человек.

    Ключ находки печатается рядом с ней: это то, чем она выбирается, и без
    него режим «остановиться с отчётом» не доведён до конца — выбрать было бы
    нечем.
    """
    # Покрытие называется всегда и рядом с итогом: «находок нет» без
    # знаменателя читается как «всё проверено», а проверено могло быть
    # меньше половины — контекстные без модели, растровые без картинок.
    if not report.issues:
        print(f"[{variant}] аудит: находок нет; {report.coverage}")
    else:
        automatic = len(report.auto_fixable)
        print(
            f"[{variant}] аудит: находок {len(report.issues)} "
            f"(ошибок {report.error_count}, чинится само {automatic}); "
            f"{report.coverage}"
        )
        for issue in report.issues:
            print(
                f"[{variant}]   [{issue.severity.value}] {issue.key}"
                f" — {issue.message} ({issue.fix.kind.value})"
            )
    if report.skipped_checks:
        print(
            f"[{variant}] не выполнено проверок: "
            f"{len(report.skipped_checks)} из {report.checks_total}"
        )
        for check_id, reason in sorted(report.skipped_checks.items()):
            print(f"[{variant}]   {check_id}: {reason}")
    if manifest.fix_mode == "review" and report.auto_fixable:
        print(
            f"[{variant}] режим review: колода не менялась. "
            "Повторите с --fix auto или выберите находки в интерфейсе."
        )
    if manifest.unresolved:
        print(
            f"[{variant}] осталось после предела итераций: "
            f"{', '.join(manifest.unresolved)}"
        )


def _cmd_probe(args: argparse.Namespace) -> int:
    """Один живой вызов модели с полной диагностикой.

    Нужна, чтобы убедиться, что endpoint настроен и отвечает так, как ждёт
    пайплайн, до того как это выяснится посреди генерации колоды. Показывает
    время, токены, повторы из-за невалидного ответа и блоки рассуждений —
    то, по чему видно, годится ли модель и провайдер для бюджета в пять минут.
    """
    import time

    cfg = load_config(args.config)
    if not cfg.llm.configured:
        print(
            "Модель не настроена. Нужны LLM_BASE_URL, LLM_API_KEY и LLM_MODEL "
            "(см. .env.example).",
            file=sys.stderr,
        )
        return 1

    from deckwright.llm.client import LiveClient, probe_endpoint
    from deckwright.plan.planner import build_plan

    pack = ContentPack.model_validate(
        json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    client = LiveClient(cfg.llm)
    slide_count = cfg.deck.slide_count or cfg.deck.min_slides

    print(f"endpoint : {cfg.llm.base_url}")
    print(f"модель   : {cfg.llm.model}")
    params = cfg.llm.step("plan_deck")
    print(
        f"параметры: temperature={params.temperature} max_tokens={params.max_tokens} "
        f"enable_thinking={params.enable_thinking} "
        f"reasoning_effort={params.reasoning_effort or 'не задан'}"
    )
    print(f"слайдов  : {slide_count}\n")

    # Сначала проверяем ключ и наличие модели: 401 или неизвестное имя модели
    # видно сразу и с подсказкой, а не как провал посреди генерации.
    reachable, detail, available = probe_endpoint(cfg.llm)
    print(f"ключ     : {detail}")
    if available:
        print(f"моделей  : {len(available)}")
        if cfg.llm.model in available:
            print(f"модель   : {cfg.llm.model} — доступна")
        else:
            needle = cfg.llm.model.split("/")[-1].lower()[:6]
            close = [m for m in available if needle in m.lower()][:8]
            print(f"модель   : {cfg.llm.model} — В СПИСКЕ НЕТ")
            if close:
                print("           похожие: " + ", ".join(close))
    if not reachable:
        print(
            "\nEndpoint не принял ключ. Что проверить:\n"
            f"  1. Сейчас запрос уходит на {cfg.llm.base_url}.\n"
            "     У SiliconFlow два независимых сервиса, .com и .cn, с разными\n"
            "     аккаунтами: ключ одного на другом всегда отвечает отказом.\n"
            "     Попробуйте тот домен, на котором заведён аккаунт.\n"
            "  2. В значении секрета не должно быть пробелов, переносов строки\n"
            "     и кавычек — только сам ключ.\n"
            "  3. Ключ должен быть активен, а на счету должен быть баланс.",
            file=sys.stderr,
        )
        return 1
    print()

    # Шаблон нужен, чтобы промпт ушёл в модель таким же, каким уходит в
    # настоящем прогоне: с ограничениями длины, посчитанными по его рамкам.
    # Без шаблона проба проверяет не тот промпт, который потом работает.
    spec = None
    if args.template:
        from deckwright.parse.opener import parse_template

        spec = parse_template(
            args.template,
            cache_dir=cfg.template.cache_dir,
            font_dir=cfg.fonts.extract_dir,
        )

    # Один замер ничего не говорит: скорость у провайдера плавает, и разница
    # между 34 и 317 секундами на двух прогонах может оказаться и разницей
    # нагрузки, и разницей в нашем запросе. Гоняем планировщик несколько раз
    # и смотрим на разброс, а не на одно число.
    #
    # Счётчики клиента накапливаются, поэтому по каждому прогону берётся
    # приращение, а не текущее значение.
    def _counters() -> dict[str, int]:
        return {
            "prompt": client.prompt_tokens,
            "completion": client.completion_tokens,
            "retries": client.retries,
            "thinking": client.thinking_blocks,
            "rate_limit": client.rate_limit_hits,
        }

    runs: list[dict[str, float]] = []
    failed = False
    plan = None
    budget = None
    # Та же ёмкость макетов, что уходит в промпт в настоящем прогоне: иначе
    # проба проверяет не тот промпт.
    from deckwright.pipeline import plan_limits

    limits = plan_limits(spec, cfg) if spec is not None else ()

    for attempt in range(1, args.repeats + 1):
        before = _counters()
        started = time.monotonic()
        try:
            plan, _, budget = build_plan(
                pack,
                client,
                slide_count,
                spec=spec,
                max_bullets=cfg.audit.max_bullets_per_slide,
                max_words_per_bullet=cfg.audit.max_words_per_bullet,
                substitution_slack=cfg.fonts.substitution_slack,
                block_limits=limits,
            )
        except Exception as exc:  # диагностика обязана досказать, что произошло
            failed = True
            print(f"ОШИБКА на прогоне {attempt}: {exc}", file=sys.stderr)
            # Дальше не идём: если запрос ломается, остальные прогоны
            # потратят деньги на тот же отказ.
            break
        elapsed = time.monotonic() - started
        after = _counters()
        runs.append(
            {
                "n": attempt,
                "seconds": elapsed,
                **{key: after[key] - before[key] for key in before},
            }
        )

    if budget is not None:
        print(
            f"бюджет длины    : заголовок {budget.title_chars}, "
            f"пункт {budget.bullet_chars} симв; мерили: {budget.measured_with}"
        )
        for kind, items, chars in budget.block_limits:
            print(f"ёмкость макетов : {kind} — {items} × {chars} симв")

    if runs:
        print(f"\nпрогонов        : {len(runs)} из {args.repeats}")
        print(
            "  №   время, с   выход, ток   ток/с   повторов   <think>   429"
        )
        for run in runs:
            speed = run["completion"] / run["seconds"] if run["seconds"] else 0.0
            print(
                f"  {int(run['n']):<3} {run['seconds']:>8.1f}   {int(run['completion']):>10}"
                f"   {speed:>5.1f}   {int(run['retries']):>8}   {int(run['thinking']):>7}"
                f"   {int(run['rate_limit']):>3}"
            )

        times = sorted(run["seconds"] for run in runs)
        speeds = sorted(
            run["completion"] / run["seconds"] if run["seconds"] else 0.0 for run in runs
        )
        print(
            f"\nвремя, с        : мин {times[0]:.1f}  медиана {median(times):.1f}  "
            f"макс {times[-1]:.1f}"
        )
        print(
            f"скорость, ток/с : мин {speeds[0]:.1f}  медиана {median(speeds):.1f}  "
            f"макс {speeds[-1]:.1f}"
        )
        # Разброс важнее среднего: если максимум вдесятеро больше минимума,
        # планировать бюджет по медиане нельзя. На долях секунды отношение
        # считать бессмысленно — там шумит сам замер.
        if times[0] >= 1.0:
            print(f"разброс         : макс/мин = {times[-1] / times[0]:.1f}×")

    print(f"\nвызовов         : {client.calls}")
    print(f"повторов        : {client.retries}  (ответ не прошёл валидацию по схеме)")
    print(f"блоков <think>  : {client.thinking_blocks}")
    print(f"отброшено полей : {sorted(client.dropped_params) or 'нет'}")
    print(
        f"токенов         : вход {client.prompt_tokens}, "
        f"выход {client.completion_tokens}, всего "
        f"{client.prompt_tokens + client.completion_tokens}"
    )

    print(f"ответов 429     : {client.rate_limit_hits}")

    # Цены берутся из конфига; аргументы командной строки их перекрывают, если
    # у провайдера другой тариф.
    price_in = args.price_in if args.price_in is not None else cfg.llm.price_per_1m_input
    price_out = args.price_out if args.price_out is not None else cfg.llm.price_per_1m_output
    if price_in or price_out:
        total = (
            client.prompt_tokens * price_in + client.completion_tokens * price_out
        ) / 1_000_000
        per_call = total / client.calls if client.calls else 0.0
        print(
            f"стоимость       : ${per_call:.6f} за вызов, ${total:.6f} за пробу "
            f"(${price_in}/${price_out} за 1M)"
        )
        print(
            f"  три шаблона   : ${per_call * 3:.4f}  (планирование по разу на шаблон)"
        )
    else:
        print("стоимость       : цены не заданы ни в конфиге, ни аргументами")

    if plan is not None:
        print(f"\nплан: {plan.slide_count} слайдов")
        for slide in plan.slides:
            print(f"  {slide.index}. [{slide.intent.value}] {slide.takeaway_title}")
        if args.save:
            Path(args.save).parent.mkdir(parents=True, exist_ok=True)
            Path(args.save).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
            print(f"\nплан сохранён: {args.save}")

    if client.last_raw:
        head = client.last_raw[:400].replace("\n", " ")
        print(f"\nначало сырого ответа:\n  {head}")

    return 1 if failed else 0


def _cmd_audit_probe(args: argparse.Namespace) -> int:
    """Живой прогон контекстного аудита: замер вместо оценки.

    Колода собирается по записанным ответам — платить за планирование, чтобы
    проверить аудит, незачем. Живой остаётся только та часть, которую и надо
    измерить: вопросы по картинке слайда.
    """
    import json as _json
    import time as _time

    from deckwright.audit import probe as audit_probe
    from deckwright.audit.spoil import spoil
    from deckwright.llm.client import LiveClient, probe_endpoint
    from deckwright.pipeline import run_variant
    from deckwright.render.pdf import pptx_to_pdf
    from deckwright.render.png import pdf_to_png
    from deckwright.render.pptx_writer import render_deck

    cfg = load_config(args.config)
    model = cfg.vlm if cfg.vlm.configured else cfg.llm
    if not model.configured:
        print(
            "Модель со зрением не настроена: заполните VLM_BASE_URL, VLM_API_KEY "
            "и VLM_MODEL (или LLM_*) в .env — см. .env.example.",
            file=sys.stderr,
        )
        return 1

    print(f"endpoint : {model.base_url}")
    print(f"модель   : {model.model}")
    reachable, detail, available = probe_endpoint(model)
    print(f"ключ     : {detail}")
    if available:
        print(f"моделей  : {len(available)}")
        print(
            f"модель   : {model.model} — "
            + ("доступна" if model.model in available else "В СПИСКЕ НЕТ")
        )
    if not reachable:
        print("\nEndpoint не принял ключ: см. подсказки в `deckwright probe`.", file=sys.stderr)
        return 1

    pack = ContentPack.model_validate(
        _json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    output = Path(args.output)
    print("\nсобираю колоду по записанным ответам…")
    built = run_variant(
        template_path=args.template,
        pack=pack,
        cfg=cfg,
        client=RecordedClient(args.recorded),
        variant=args.variant,
        output_dir=output / "clean",
    )
    print(f"  слайдов {len(built.deck.slides)}, картинок {len(built.pages)}")

    check_ids = cfg.audit.checks_by_mode("image")
    client = LiveClient(model)

    started = _time.monotonic()
    clean = audit_probe.run(
        client,
        built.deck,
        built.plan,
        built.pages,
        check_ids,
        limit=args.slides,
        measure_cost=True,
    )

    # Шесть вопросов из одиннадцати задаются по тексту колоды, а не по
    # картинке. Не спросить их значит померить меньше половины аудита.
    text_ids = cfg.audit.checks_by_mode("text")
    if text_ids:
        clean.deck_pass = audit_probe.run_text_pass(client, built.plan, text_ids)

    spoiled_result = None
    damage = []
    if args.spoil:
        print("\nсобираю заведомо испорченную колоду…")
        broken, damage = spoil(built.deck)
        broken_pptx = render_deck(
            broken, built.spec, args.template, output / "spoiled" / "spoiled.pptx"
        )
        broken_pdf = pptx_to_pdf(
            broken_pptx,
            output / "spoiled",
            soffice_binary=cfg.render.soffice_binary,
            timeout_seconds=cfg.render.soffice_timeout_seconds,
        )
        broken_pages = pdf_to_png(
            broken_pdf, output / "spoiled" / "png", dpi=cfg.render.png_dpi
        )
        print(f"  порч внесено: {len(damage)}")
        spoiled_result = audit_probe.run(
            client,
            broken,
            built.plan,
            broken_pages,
            check_ids,
            limit=max(item.slide_index for item in damage) if damage else args.slides,
            measure_cost=False,
        )

    print()
    print(
        audit_probe.format_report(
            clean,
            spoiled_result,
            damage,
            check_ids,
            cfg.audit.contextual_dpi,
            text_ids,
        )
    )
    print()
    print(f"весь прогон          : {_time.monotonic() - started:.1f} с")
    print(f"вызовов              : {client.calls}")
    print(f"повторов             : {client.retries}")
    print(f"блоков <think>       : {client.thinking_blocks}")
    print(f"ответов 429          : {client.rate_limit_hits}")
    print(f"отброшено полей      : {sorted(client.dropped_params) or 'нет'}")
    cost = model.cost_usd(client.prompt_tokens, client.completion_tokens)
    print(f"стоимость            : ${cost:.6f}")

    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text(
            _json.dumps(
                {
                    "dpi": cfg.audit.contextual_dpi,
                    "image_token_cost": clean.image_token_cost,
                    "slides": [vars(s) for s in clean.slides],
                    "spoiled": [vars(s) for s in (spoiled_result.slides if spoiled_result else [])],
                    "damage": [vars(d) for d in damage],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"замеры сохранены     : {args.save}")

    if any(slide.error for slide in clean.slides):
        return 1
    return 0


def _cmd_audit_dpi(args: argparse.Namespace) -> int:
    """Картиночный аудит одних и тех же колод при разных разрешениях.

    Колоды пересобираются по записанному плану: вёрстка детерминирована, и
    это ровно те колоды, что собрал живой прогон. Живые — только вопросы по
    картинкам. Итог — таблица токенов, времени и находок на каждое dpi.
    """
    from deckwright.audit import probe as audit_probe
    from deckwright.llm.client import LiveClient

    cfg = load_config(args.config)
    model = cfg.vlm if cfg.vlm.configured else cfg.llm
    if not model.configured:
        print("Модель со зрением не настроена: VLM_* или LLM_* в .env.", file=sys.stderr)
        return 1
    dpis = [int(value) for value in args.dpi.split(",")]
    client = LiveClient(model)
    check_ids = cfg.audit.checks_by_mode("image")
    pack = ContentPack.model_validate_json(Path(args.content).read_text(encoding="utf-8"))
    reports = []
    for variant in args.variants.split(","):
        built = run_variant(
            template_path=args.template,
            pack=pack,
            cfg=cfg,
            client=RecordedClient(args.recorded),
            variant=variant,
            output_dir=Path(args.output) / Path(args.template).stem / variant,
        )
        passes = audit_probe.compare_resolutions(
            client,
            built.deck,
            built.plan,
            built.pages,
            check_ids,
            base_dpi=cfg.render.png_dpi,
            dpis=dpis,
            workers=cfg.vlm.max_concurrent_calls,
        )
        report = audit_probe.format_resolution_report(
            f"{Path(args.template).stem} / {variant} ({len(built.pages)} слайдов)", passes
        )
        print(report, flush=True)
        reports.append(report)
    if args.save:
        Path(args.save).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save).write_text("\n\n".join(reports) + "\n", encoding="utf-8")
    return 0


def _cmd_rewrite_probe(args: argparse.Namespace) -> int:
    """Живой прогон главного сценария демонстрации.

    Находка «текст не помещается» → выбор человека → модель переписывает →
    колода пересобирается → повторный аудит. Механика закрыта тестами на
    подставной модели; здесь проверяется то, что тестом не проверишь:
    **пишет ли настоящая модель текст, который проходит наши рамки**.

    Отклонение — не провал прогона, а результат: значит на демонстрации
    находка останется, и знать это надо заранее, а не на защите.
    """
    import time as _time

    from deckwright.llm.client import LiveClient, probe_endpoint
    from deckwright.pipeline import apply_selection

    cfg = load_config(args.config)
    if not cfg.llm.configured:
        print(
            "Модель не настроена: нужны LLM_BASE_URL, LLM_API_KEY и LLM_MODEL.",
            file=sys.stderr,
        )
        return 1
    # Переписывание — шаг, который без флага не выполняется вовсе. Здесь он
    # включается явно: прогон ради него и затеян.
    cfg.run.rewrite_assisted = True

    print(f"endpoint : {cfg.llm.base_url}")
    print(f"модель   : {cfg.llm.model}")
    reachable, detail, _ = probe_endpoint(cfg.llm)
    print(f"ключ     : {detail}")
    if not reachable:
        print("\nEndpoint не принял ключ: см. подсказки в `deckwright probe`.", file=sys.stderr)
        return 1

    pack = ContentPack.model_validate(
        json.loads(Path(args.content).read_text(encoding="utf-8"))
    )
    print("\nсобираю колоду по записанным ответам…")
    built = run_variant(
        template_path=args.template,
        pack=pack,
        cfg=cfg,
        client=RecordedClient(args.recorded),
        variant=args.variant,
        output_dir=Path(args.output) / "before",
        fix_mode="review",
    )
    budget = built.prepared.budget
    print(f"  слайдов {len(built.deck.slides)}, находок {len(built.report.issues)}")
    print(
        f"  бюджет шаблона: заголовок {budget.title_chars} симв, "
        f"пункт {budget.bullet_chars} симв, пунктов {budget.max_bullets}"
    )

    targets = [
        issue
        for issue in built.report.issues
        if issue.fix.action in rewrite_actions()
    ][: args.findings or None]
    if not targets:
        print(
            "\nНа этой колоде нечего переписывать: находок с текстовым "
            "исправлением нет. Возьмите шаблон с узкими рамками.",
            file=sys.stderr,
        )
        return 1

    print(f"\nнаходок под переписывание: {len(targets)}")
    for issue in targets:
        print(f"  {issue.key} — {issue.message}")
        slide = next(s for s in built.plan.slides if s.index == issue.slide_index)
        print(f"    было: «{slide.takeaway_title}» ({len(slide.takeaway_title)} симв)")
        for block in slide.blocks:
            for item in block.items:
                print(f"      [{len(item):3}] {item}")

    client = LiveClient(cfg.llm)
    started = _time.monotonic()
    after = apply_selection(built, {issue.key for issue in targets}, client)
    elapsed = _time.monotonic() - started

    print(f"\nпереписывание заняло {elapsed:.1f} с, вызовов {client.calls}")
    for record in after.manifest.fix_iterations:
        print(
            f"  итерация {record.number}: переписано слайдов "
            f"{record.rewritten_slides or 'ни одного'}, находок "
            f"{record.issues_before} → {record.issues_after}"
        )
        for what, reason in record.skipped.items():
            print(f"    ОТКЛОНЕНО {what}: {reason}")

    print("\nчто написала модель:")
    for issue in targets:
        slide = next(s for s in after.plan.slides if s.index == issue.slide_index)
        fits = "влезает" if len(slide.takeaway_title) <= budget.title_chars else "НЕ ВЛЕЗАЕТ"
        print(f"  слайд {issue.slide_index}: «{slide.takeaway_title}» "
              f"({len(slide.takeaway_title)} симв, {fits})")
        for block in slide.blocks:
            for item in block.items:
                mark = "" if len(item) <= budget.bullet_chars else "  ← ДЛИННЕЕ БЮДЖЕТА"
                print(f"      [{len(item):3}] {item}{mark}")

    gone = [
        issue
        for issue in targets
        if not any(
            found.check_id == issue.check_id and found.slide_index == issue.slide_index
            for found in after.report.issues
        )
    ]
    print(f"\nнаходок ушло: {len(gone)} из {len(targets)}")
    print(f"вызовов         : {client.calls}")
    print(f"повторов        : {client.retries}  (ответ не прошёл валидацию по схеме)")
    print(f"блоков <think>  : {client.thinking_blocks}")
    print(f"ответов 429     : {client.rate_limit_hits}")
    print(
        f"токенов         : вход {client.prompt_tokens}, "
        f"выход {client.completion_tokens}"
    )
    print(
        f"стоимость       : ${cfg.llm.cost_usd(client.prompt_tokens, client.completion_tokens):.6f}"
    )
    # Ушли не все — это результат, а не сбой прогона: значит на демонстрации
    # часть находок останется, и лучше знать это заранее.
    return 0


def rewrite_actions() -> frozenset[str]:
    from deckwright.audit.rewrite import REWRITABLE_ACTIONS

    return REWRITABLE_ACTIONS



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckwright",
        description="Собирает презентацию по правилам произвольного .pptx-шаблона.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Проверить системные зависимости и конфигурацию.")
    doctor.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"Путь к config.yaml (по умолчанию {DEFAULT_CONFIG}); "
        "пропускается, если файла нет.",
    )
    doctor.set_defaults(func=_cmd_doctor)

    run = sub.add_parser("run", help="Собрать презентацию по шаблону и контент-пакету.")
    run.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    run.add_argument(
        "--template", default=None, help="Шаблон .pptx; по умолчанию run.template из конфига."
    )
    run.add_argument(
        "--content",
        default=None,
        help="Материалы: контент-пакет в JSON или любой файл входа; по умолчанию "
        "run.content из конфига.",
    )
    run.add_argument(
        "--input",
        action="append",
        default=[],
        help="Ещё файл входа: txt, md, docx, pdf, pptx, json. Можно несколько раз.",
    )
    run.add_argument(
        "--brief", default="", help="Текст брифа или готовый материал — как в поле ввода."
    )
    run.add_argument(
        "--purpose",
        choices=[p.value for p in DeckPurpose],
        default=None,
        help="Назначение: feature, product, project, initiative; по умолчанию deck.purpose.",
    )
    run.add_argument("--author", default="", help="Кто выступает: подпись спикера.")
    run.add_argument("--author-role", default="", help="Должность выступающего.")
    run.add_argument(
        "--save-pack",
        default=None,
        help="Сохранить контент-пакет, полученный из входа, в этот JSON.",
    )
    run.add_argument(
        "--save-plan",
        default=None,
        help="Сохранить план колоды (он общий для вариантов) в этот JSON.",
    )
    run.add_argument("--variant", default=None, help="Один вариант вместо всех из конфига.")
    run.add_argument("--output", default=None, help="Каталог артефактов.")
    run.add_argument(
        "--recorded",
        default=None,
        help="Каталог записанных ответов модели: прогон без сети и без ключа.",
    )
    run.add_argument(
        "--fix",
        choices=("off", "review", "auto"),
        default=None,
        help=(
            "Что делать с находками аудита; по умолчанию режим из config.yaml. "
            "review — показать отчёт и не трогать колоду, auto — применить "
            "автоматические исправления и пересобрать, off — не проверять на "
            "исправимость вовсе. ASSISTED и контекстные находки не применяются "
            "ни в одном режиме: они идут только явным выбором в интерфейсе."
        ),
    )
    run.set_defaults(func=_cmd_run)

    probe = sub.add_parser(
        "probe", help="Один живой вызов модели с диагностикой: время, токены, повторы."
    )
    probe.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    probe.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    probe.add_argument("--save", default=None, help="Куда сохранить полученный план.")
    probe.add_argument(
        "--repeats",
        type=int,
        default=3,
        help=(
            "Сколько раз подряд прогнать планировщик. Один замер ничего не "
            "говорит: скорость у провайдера плавает."
        ),
    )
    probe.add_argument(
        "--template",
        default=None,
        help="Шаблон, по которому считать ограничения длины для промпта.",
    )
    probe.add_argument(
        "--price-in", type=float, default=None, help="Цена за 1M входных токенов."
    )
    probe.add_argument(
        "--price-out", type=float, default=None, help="Цена за 1M выходных токенов."
    )
    probe.set_defaults(func=_cmd_probe)

    audit = sub.add_parser(
        "audit-probe",
        help="Живой прогон контекстного аудита: токены, время, формат, чувствительность.",
    )
    audit.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    audit.add_argument("--template", required=True, help="Шаблон .pptx.")
    audit.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    audit.add_argument(
        "--recorded",
        default="tests/fixtures/recorded",
        help="Записанные ответы для планирования: платить за него незачем.",
    )
    audit.add_argument("--variant", default="balanced", help="Вариант вёрстки.")
    audit.add_argument("--output", default="outputs/audit-probe", help="Куда класть колоды.")
    audit.add_argument(
        "--slides",
        type=int,
        default=0,
        help="Сколько слайдов спросить. 0 — всю колоду.",
    )
    audit.add_argument(
        "--spoil",
        action="store_true",
        help=(
            "Прогнать ещё и по заведомо испорченной колоде. Аудит, который "
            "всегда доволен, проходит все тесты и бесполезен."
        ),
    )
    audit.add_argument("--save", default=None, help="Куда сохранить замеры в JSON.")
    audit.set_defaults(func=_cmd_audit_probe)

    dpi = sub.add_parser(
        "audit-dpi",
        help="Картиночный аудит тех же колод при разных разрешениях: токены и находки.",
    )
    dpi.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    dpi.add_argument("--template", required=True, help="Шаблон .pptx.")
    dpi.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    dpi.add_argument("--recorded", required=True, help="Каталог с записанным plan_deck.json.")
    dpi.add_argument("--variants", default="dense,balanced,airy", help="Варианты через запятую.")
    dpi.add_argument("--dpi", default="96,72,60", help="Разрешения через запятую, первое — база.")
    dpi.add_argument("--output", default="outputs/audit-dpi", help="Куда класть колоды.")
    dpi.add_argument("--save", default=None, help="Куда сохранить таблицу (markdown).")
    dpi.set_defaults(func=_cmd_audit_dpi)

    rewrite = sub.add_parser(
        "rewrite-probe",
        help=(
            "Живой прогон переписывания: находка ASSISTED → модель → "
            "пересборка → повторный аудит."
        ),
    )
    rewrite.add_argument("--config", default=str(DEFAULT_CONFIG), help="Путь к config.yaml.")
    rewrite.add_argument(
        "--template",
        default="data/holdout/zelenie_investicii.pptx",
        help="Шаблон с узкими рамками: на просторном переписывать нечего.",
    )
    rewrite.add_argument("--content", required=True, help="Контент-пакет в JSON.")
    rewrite.add_argument(
        "--recorded",
        default="tests/fixtures/recorded",
        help="Записанные ответы для планирования: платить за него незачем.",
    )
    rewrite.add_argument("--variant", default="balanced", help="Вариант вёрстки.")
    rewrite.add_argument("--output", default="outputs/rewrite-probe", help="Куда класть колоды.")
    rewrite.add_argument(
        "--findings",
        type=int,
        default=1,
        help="Сколько находок переписать. 0 — все найденные.",
    )
    rewrite.set_defaults(func=_cmd_rewrite_probe)


    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
