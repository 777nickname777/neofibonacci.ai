"""Веб-интерфейс: шаблон и контент на входе, три варианта и аудит на выходе.

Сценарий один и короткий: загрузить шаблон, дать бриф, запустить, посмотреть
три варианта, отметить находки аудита прямо на слайде, применить, скачать.

Два решения, которые видно в коде.

**Прогон идёт в фоне.** Пять минут в обработчике кнопки означали бы
заблокированную страницу и потерю результата при обновлении. Прогон живёт в
потоке (`app/runs.py`), его `run_id` — в параметрах адреса, и обновление
страницы возвращает к тому же прогону.

**Вход по паролю, если он задан.** Сервис, выложенный по адресу, тратит
ключ модели на каждый прогон. `DECKWRIGHT_PASSWORD` в окружении закрывает
интерфейс паролем; не задан — вход открыт (локальный запуск).

**Находки показываются на слайде, а не только списком.** «Элемент заходит в
поля» без рамки — загадка; номер рамки совпадает с номером в списке
(`app/audit_overlay.py`).
"""

from __future__ import annotations

import hmac
import json
import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # запуск через `streamlit run app/ui.py`
    sys.path.insert(0, str(ROOT))

from app import audit_overlay, runs  # noqa: E402

from deckwright.config import load_config  # noqa: E402
from deckwright.content.ingest import IngestInput  # noqa: E402
from deckwright.llm.fake import RecordedClient  # noqa: E402
from deckwright.schemas import ContentPack, DeckPurpose, FixKind, Severity  # noqa: E402
from deckwright.workspace import (  # noqa: E402
    RunWorkspace,
    UnsafeUpload,
    new_run_id,
    new_session_id,
)

CONFIG = ROOT / "configs" / "config.yaml"
SAMPLE_PACK = ROOT / "tests" / "fixtures" / "content_pack.json"
RECORDED = ROOT / "tests" / "fixtures" / "recorded"

SEVERITY_MARK = {Severity.ERROR: "●", Severity.WARNING: "●", Severity.INFO: "●"}
FIX_EXPLANATION = {
    FixKind.AUTOMATIC: "применится само",
    FixKind.ASSISTED: "требует решения человека",
    FixKind.MANUAL: "чинится руками",
    FixKind.NONE: "информационная",
}


PASSWORD_ENV = "DECKWRIGHT_PASSWORD"


def _require_password() -> bool:
    """Пускает дальше, только если пароль не задан или введён верно.

    Сравнение — `hmac.compare_digest`: время ответа не выдаёт, сколько
    символов совпало. Отметка о входе живёт в сессии браузера: обновление
    страницы пароль заново не спрашивает, новая вкладка — спрашивает.
    """
    expected = os.environ.get(PASSWORD_ENV, "")
    if not expected or st.session_state.get("authorized"):
        return True
    with st.form("login"):
        entered = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти")
    if submitted:
        if hmac.compare_digest(entered.encode("utf-8"), expected.encode("utf-8")):
            st.session_state["authorized"] = True
            st.rerun()
        st.error("Неверный пароль.")
    return False


def _session_id() -> str:
    """Идентификатор этой вкладки браузера.

    Живёт в `st.session_state`, то есть создаётся один раз на сессию и
    переживает перезапуски скрипта. По нему прогон получает владельца:
    реестр прогонов общий на процесс, и без владельца номер чужого прогона
    открывал бы чужие результаты.
    """
    if "session_id" not in st.session_state:
        st.session_state["session_id"] = new_session_id()
    return st.session_state["session_id"]


def _save_upload(uploaded, workspace: RunWorkspace) -> Path:
    """Кладёт загруженный файл в каталог этого прогона.

    Имя проходит через `workspace.save_upload`: `../` вырезается, расширение
    проверяется, одинаковые имена не перетирают друг друга. Раньше файл
    ложился в общий `outputs/uploads/<исходное имя>` — и два человека с
    `template.pptx` перезаписывали шаблон друг другу.
    """
    return workspace.save_upload(uploaded.name, bytes(uploaded.getbuffer()))


def _make_client(cfg, recorded: bool):
    """Записанные ответы или живая модель.

    Демонстрация без ключа обязана работать: иначе интерфейс нечем показать.
    """
    if recorded:
        return RecordedClient(RECORDED), None
    from deckwright.llm.client import LiveClient

    vlm = LiveClient(cfg.vlm) if cfg.vlm.configured else None
    return LiveClient(cfg.llm), vlm


PURPOSES = {
    "": "определить по материалам",
    "feature": "фича",
    "product": "продукт",
    "project": "проект",
    "initiative": "инициатива",
}


def _sidebar(cfg):
    """Вход прогона. Возвращает параметры запуска или None.

    Материалы — в любом виде: текст в поле (короткий бриф или готовый
    текст) и файлы txt, md, docx, pdf, pptx, json. JSON в нашей схеме
    контент-пакета берётся как есть, всё остальное приводится к ней моделью.
    """
    st.sidebar.header("Что собираем")

    template = st.sidebar.file_uploader("Шаблон презентации", type=["pptx", "potx"])
    text = st.sidebar.text_area(
        "Бриф или текст",
        height=160,
        placeholder=(
            "Пара предложений о том, что и кому показываем, — сервис сам решит, "
            "сколько слайдов и что на них. Или вставьте готовый текст."
        ),
    )
    files = st.sidebar.file_uploader(
        "Материалы",
        type=["txt", "md", "docx", "pdf", "pptx", "json"],
        accept_multiple_files=True,
        help="Документы, таблицы, выгрузки. Цифры на слайдах берутся только отсюда.",
    )
    purpose = st.sidebar.selectbox(
        "Назначение", options=list(PURPOSES), format_func=PURPOSES.get
    )
    author = st.sidebar.text_input("Кто выступает", placeholder="Имя Фамилия")
    author_role = st.sidebar.text_input("Должность выступающего")
    has_input = bool(text.strip() or files)
    use_sample = st.sidebar.checkbox(
        "Взять демонстрационный контент-пакет", value=not has_input
    )

    names = [variant.name for variant in cfg.variants]
    chosen = st.sidebar.multiselect("Варианты вёрстки", names, default=names)

    fix_mode = st.sidebar.radio(
        "Что делать с находками аудита",
        options=("review", "auto", "off"),
        index=0,
        format_func=lambda mode: {
            "review": "показать и дать выбрать (по ТЗ)",
            "auto": "применить автоматические самому",
            "off": "не трогать",
        }[mode],
        help=(
            "Находки, требующие решения человека, и ответы модели не "
            "применяются ни в одном режиме — только по вашему выбору ниже."
        ),
    )

    # Переписывание текста моделью. Без этого переключателя режим включался
    # только правкой `config.yaml`: интерфейс показывал галочки у находок,
    # которые заведомо нечем было исправить, и кнопка «Применить» по ним
    # молча ничего не делала.
    can_rewrite = cfg.llm.configured
    rewrite = st.sidebar.checkbox(
        "Переписывать текст моделью (assisted)",
        value=cfg.run.rewrite_assisted and can_rewrite,
        disabled=not can_rewrite,
        help=(
            "Находки «сократить», «разнести пункты», «текст не помещается» "
            "механической операции не имеют: это решение о содержании. "
            "С этим флагом отмеченная находка уходит в модель, которая "
            "переписывает текст слайда; числа, имена и единицы при этом "
            "защищены. Без ключа модели режим недоступен."
        ),
    )
    if not can_rewrite:
        st.sidebar.caption(
            "Переписывание недоступно: не задан ключ модели (LLM_* в .env). "
            "Отмеченные текстовые находки останутся в отчёте с причиной."
        )

    recorded = st.sidebar.checkbox(
        "Записанные ответы модели",
        value=not cfg.llm.configured,
        help=(
            "Прогон без ключа и без сети: план берётся из записи. Работает только "
            "с демонстрационным контент-пакетом — разобрать новый вход без модели нельзя."
        ),
    )
    if not recorded and not cfg.llm.configured:
        st.sidebar.warning("Модель не настроена: заполните .env или включите записанные ответы.")

    ready = template is not None and (has_input or use_sample) and chosen
    if not st.sidebar.button("Собрать", type="primary", disabled=not ready):
        return None

    # Каталог прогона заводится здесь: до нажатия кнопки его не существует, а
    # после — все входы ложатся только в него.
    workspace = RunWorkspace.create(
        Path(cfg.run.output_dir), new_run_id(), _session_id()
    )
    if use_sample and not has_input:
        request = ContentPack.model_validate(json.loads(SAMPLE_PACK.read_text("utf-8")))
        if author or author_role or purpose:
            updates = {"author": author, "author_role": author_role}
            if purpose:
                updates["purpose"] = DeckPurpose(purpose)
            request = request.model_copy(
                update={"brief": request.brief.model_copy(update=updates)}
            )
    else:
        try:
            saved = [_save_upload(upload, workspace) for upload in files or []]
        except UnsafeUpload as refused:
            st.sidebar.error(str(refused))
            workspace.cleanup(keep_outputs=False)
            return None
        request = IngestInput(
            text=text,
            files=saved,
            purpose=DeckPurpose(purpose) if purpose else None,
            author=author,
            author_role=author_role,
        )
    try:
        template_path = _save_upload(template, workspace)
    except UnsafeUpload as refused:
        st.sidebar.error(str(refused))
        workspace.cleanup(keep_outputs=False)
        return None
    return {
        "template": template_path,
        "request": request,
        "variants": chosen,
        "fix_mode": fix_mode,
        "rewrite_assisted": rewrite,
        "recorded": recorded,
        "workspace": workspace,
    }


def _input_summary(state: runs.RunState) -> None:
    """Что сервис понял из входа: тема, назначение, сколько фактов и что отброшено."""
    pack = state.pack
    if pack is None:
        return
    with st.expander(
        f"Материалы: «{pack.brief.topic}» · {PURPOSES.get(pack.brief.purpose.value)} · "
        f"фактов {len(pack.facts)}, рядов {len(pack.series)}, цитат {len(pack.quotes)} · "
        f"разбор {state.ingest_seconds} с"
    ):
        for fact in pack.facts:
            where = f" ({fact.locator})" if fact.locator else ""
            st.markdown(f"- {fact.text}{where}")
        for series in pack.series:
            points = ", ".join(f"{p.label}: {p.value:g}" for p in series.points)
            st.markdown(f"- ряд «{series.name}»: {points}")
        for warning in state.ingest_warnings:
            st.warning(warning)


@st.fragment(run_every=2)
def _live_progress(state: runs.RunState, cfg) -> None:
    """Прогресс обновляется сам, не трогая остальную страницу.

    Раньше вся страница перезапускалась каждые две секунды, пока шёл прогон.
    Это не только мигание: перезапуск приходится ровно на то время, когда
    человек отмечает находки уже готового варианта, и отметка терялась.
    Фрагмент перерисовывает только себя.
    """
    _progress(state, cfg)
    if state.finished:
        # Прогон закончился — перерисовать страницу целиком, чтобы появились
        # вкладки с результатами, и больше не опрашивать.
        st.rerun(scope="app")


def _progress(state: runs.RunState, cfg) -> None:
    total = len(state.variants)
    st.progress(
        min(1.0, state.done_count / total if total else 1.0),
        text=(
            f"{state.stage}: готово вариантов {state.done_count} из {total}, "
            f"{state.elapsed} с из {cfg.run.time_budget_seconds} с"
        ),
    )
    columns = st.columns(total or 1)
    for column, variant_state in zip(columns, state.variants.values(), strict=False):
        column.metric(variant_state.name, variant_state.stage)
    _stage_times(state)

    # Отмена. Идущий вызов модели прервать нельзя — он уже в пути, — поэтому
    # кнопка обещает ровно то, что делает: новых задач не будет, а поздний
    # ответ не опубликуется. Обещать мгновенную остановку было бы неправдой.
    if not state.finished and not state.cancelled:
        if st.button(
            "Отменить прогон",
            key=f"cancel-{state.run_id}",
            help=(
                "Новые задачи не ставятся, поздний ответ модели не публикуется. "
                "Уже отправленный запрос остановить нельзя — он оплачен."
            ),
        ):
            runs.cancel(state.run_id, _session_id())
            st.rerun()
    elif state.cancelled and not state.finished:
        st.caption("отмена принята: ждём, пока завершатся уже отправленные запросы")
    if state.elapsed > cfg.run.time_budget_seconds:
        st.warning(
            f"прогон идёт {state.elapsed} с при бюджете {cfg.run.time_budget_seconds} с"
        )


def _stage_times(state: runs.RunState) -> None:
    """Время по этапам: разбор шаблона, разбор входа, план, генерация, аудит.

    Сумма этапов меньше общего времени: варианты собираются и проверяются
    параллельно, и у этих этапов показан самый долгий вариант.
    """
    stages = state.stages()
    if not stages:
        return
    columns = st.columns(len(stages))
    for column, (name, seconds) in zip(columns, stages.items(), strict=True):
        column.metric(name, f"{seconds:.1f} с")


def _pick_key(state: runs.RunState, variant: str, issue_key: str, number: int) -> str:
    """Ключ виджета-галочки. Один на два места: отметить и снять."""
    return f"pick-{state.run_id}-{variant}-{issue_key}-{number}"


def _findings_panel(state: runs.RunState, variant_state: runs.VariantState, client) -> None:
    """Список находок с выбором. Номера совпадают с рамками на слайде."""
    report = variant_state.result.report
    # «Находок нет» без знаменателя читается как «всё проверено». Покрытие
    # стоит рядом с итогом, а не прячется в раскрывашке ниже.
    if not report.issues:
        st.success(f"Находок нет. {report.coverage.capitalize()}.")
    else:
        st.caption(
            f"находок {len(report.issues)}, из них ошибок {report.error_count}; "
            f"применяется само — {len(report.auto_fixable)}; {report.coverage}"
        )

    for number, issue in enumerate(report.issues, start=1):
        columns = st.columns([1, 14])
        if not runs.applicable(issue):
            # Отметить нечего применять: галочки нет, есть подпись.
            columns[0].checkbox(
                f"{number}",
                value=False,
                disabled=True,
                key=_pick_key(state, variant_state.name, issue.key, number),
                label_visibility="collapsed",
            )
            variant_state.selected.discard(issue.key)
            columns[1].markdown(
                f"**{number}. {issue.check_id}** · слайд {issue.slide_index} · "
                f"{issue.severity.value} · только к сведению  \n"
                f"{issue.message}"
            )
            continue
        # `value=` здесь не передаётся намеренно. Вместе с `key=` он означает
        # «сбросить виджет к этому значению на каждом перезапуске скрипта», а
        # перезапуск случается на каждое нажатие — галочка снималась сама, и
        # выбрать находку было невозможно. Состояние держит session_state по
        # ключу, а `selected` собирается из него.
        chosen = columns[0].checkbox(
            f"{number}",
            key=_pick_key(state, variant_state.name, issue.key, number),
            label_visibility="collapsed",
        )
        if chosen:
            variant_state.selected.add(issue.key)
        else:
            variant_state.selected.discard(issue.key)
        columns[1].markdown(
            f"**{number}. {issue.check_id}** · слайд {issue.slide_index} · "
            f"{issue.severity.value} · {FIX_EXPLANATION[issue.fix.kind]}  \n"
            f"{issue.message}"
        )

    if report.skipped_checks:
        with st.expander(
            f"Не выполнено проверок: {len(report.skipped_checks)} из {report.checks_total}"
        ):
            for check_id, reason in sorted(report.skipped_checks.items()):
                st.write(f"`{check_id}` — {reason}")

    # Отмеченное, что этот прогон применить не может: текстовые находки при
    # выключенном переписывании. Кнопка остаётся рабочей — механические
    # правки применятся, — но обещание даётся честное.
    chosen = [issue for issue in report.issues if issue.key in variant_state.selected]
    helpless = [
        issue
        for issue in chosen
        if issue.fix.kind is FixKind.ASSISTED
        and not (state.cfg is not None and state.cfg.run.rewrite_assisted)
    ]
    if helpless:
        st.info(
            f"Из отмеченного {len(helpless)} требует переписывания текста, а режим "
            "выключен: эти находки останутся в отчёте с причиной. Включите "
            "«Переписывать текст моделью» в панели слева и соберите заново."
        )

    disabled = variant_state.applying or not variant_state.selected
    if st.button(
        f"Применить отмеченное ({len(variant_state.selected)})",
        key=f"apply-{state.run_id}-{variant_state.name}",
        disabled=disabled,
    ):
        # Галочки здесь не снимаются: Streamlit запрещает менять состояние
        # виджета после того, как виджет создан, и попытка обрывала весь
        # обработчик — применение не начиналось вовсе. Этого и не нужно:
        # исправленная находка исчезает из отчёта вместе со своей галочкой,
        # а оставшаяся отмеченной означает, что она осталась.
        runs.apply(state, variant_state.name, client)
        st.rerun()

    if variant_state.applying:
        _await_fix(variant_state)
    else:
        _last_fix_outcome(variant_state)


def _last_fix_outcome(variant_state: runs.VariantState) -> None:
    """Что сделала последняя попытка исправления и чего не сделала.

    Молчание здесь читается как «кнопка не работает»: у находки может не быть
    механического исправления вовсе, и тогда правильный ответ — сказать это,
    а не ничего не изменить.
    """
    records = variant_state.result.manifest.fix_iterations
    if not records:
        return
    last = records[-1]
    if last.applied:
        st.success(f"Применено исправлений: {len(last.applied)}, колода пересобрана.")
    if last.skipped:
        with st.expander(f"Не применено: {len(last.skipped)} — почему"):
            for what, reason in last.skipped.items():
                st.write(f"`{what}` — {reason}")


@st.fragment(run_every=2)
def _await_fix(variant_state: runs.VariantState) -> None:
    """Ждёт конца пересборки и обновляет страницу сам.

    Без этого страница застывала на «применяю» навсегда: исправление идёт в
    потоке, а Streamlit перерисовывает страницу только в ответ на действие
    человека — и результат появлялся лишь после случайного нажатия.
    """
    if variant_state.applying:
        st.info("Применяю исправления и пересобираю колоду…")
    else:
        st.rerun(scope="app")


def _downloads(state: runs.RunState, variant_state: runs.VariantState, cfg) -> None:
    """Кнопки скачивания — только для проверенных, дописанных файлов.

    `available_formats()` отдаёт то, что действительно лежит на диске и
    непусто. Формат, который не получился, не превращается в битую кнопку:
    про него говорится прямо и предлагается повторить ровно его.
    """
    result = variant_state.result
    available = result.available_formats()
    columns = st.columns(3)
    for column, fmt in zip(columns, ("pptx", "pdf", "html"), strict=True):
        path = available.get(fmt)
        if path is not None:
            column.download_button(
                f"Скачать .{fmt}",
                data=path.read_bytes(),
                file_name=path.name,
                key=f"dl-{state.run_id}-{variant_state.name}-{fmt}",
            )
        else:
            column.button(
                f".{fmt} недоступен",
                disabled=True,
                key=f"dl-off-{state.run_id}-{variant_state.name}-{fmt}",
            )

    if not result.export_errors:
        return

    missing = ", ".join(sorted(result.export_errors))
    st.warning(
        f"Готово не всё: не получен {missing}. Колода собрана и скачивается — "
        "не получился только экспорт."
    )
    with st.expander("Почему не получилось"):
        for fmt, reason in sorted(result.export_errors.items()):
            st.write(f"**{fmt}** — {reason}")
    if variant_state.exporting:
        _await_export(variant_state)
    elif "pdf" in result.export_errors and st.button(
        "Повторить только экспорт PDF",
        key=f"retry-{state.run_id}-{variant_state.name}",
        help="Колода не пересобирается и модель не вызывается: берётся "
             "готовый .pptx и конвертируется заново.",
    ):
        runs.retry_export(state, variant_state.name, cfg)
        st.rerun()


@st.fragment(run_every=2)
def _await_export(variant_state: runs.VariantState) -> None:
    """Ждёт конца повторного экспорта, не перезапуская всю страницу."""
    if variant_state.exporting:
        st.info("Повторяю экспорт PDF…")
    else:
        st.rerun(scope="app")


def _early_downloads(state: runs.RunState, variant_state: runs.VariantState) -> None:
    """Готовые файлы варианта, пока идёт аудит.

    Колода собрана, проверена на целостность и выгружена до того, как
    начнётся проверка качества: аудит по картинкам — 80-111 с из 170-262 с
    прогона (замеры run 28-31). Пользователю незачем ждать полторы минуты
    ради файла, который уже лежит на диске; находки дописываются следом,
    в ту же вкладку.
    """
    st.caption(
        f"{variant_state.name}: колода готова, идёт проверка качества "
        "— находки появятся здесь"
    )
    columns = st.columns(3)
    for column, fmt in zip(columns, ("pptx", "pdf", "html"), strict=True):
        path = variant_state.files.get(fmt)
        if path is None:
            column.button(
                f".{fmt} недоступен",
                disabled=True,
                key=f"early-off-{state.run_id}-{variant_state.name}-{fmt}",
            )
            continue
        column.download_button(
            f"Скачать .{fmt}",
            data=path.read_bytes(),
            file_name=path.name,
            key=f"early-dl-{state.run_id}-{variant_state.name}-{fmt}",
        )
    if variant_state.early_export_errors:
        missing = ", ".join(sorted(variant_state.early_export_errors))
        st.warning(f"Готово не всё: не получен {missing}.")


def _variant_tab(state: runs.RunState, variant_state: runs.VariantState, client, cfg) -> None:
    result = variant_state.result
    if result is None:
        if variant_state.error:
            # Вариант не собрался. Сказать почему и куда смотреть — иначе
            # вкладка выглядит вечно «ожидающей», а остальные варианты
            # при этом готовы и скачиваются.
            st.error(f"{variant_state.name}: {variant_state.stage}")
            st.caption(variant_state.error)
            if state.workspace is not None:
                st.caption(
                    "Подробности: "
                    f"`{state.workspace.diag / (variant_state.name + '.error.txt')}`"
                )
        elif variant_state.files:
            _early_downloads(state, variant_state)
        else:
            st.info(f"{variant_state.name}: {variant_state.stage}")
        return
    if variant_state.error:
        st.error(variant_state.error)

    manifest = result.manifest
    columns = st.columns(4)
    columns[0].metric("слайдов", len(result.deck.slides))
    columns[1].metric("находок", len(result.report.issues))
    columns[2].metric("ошибок", result.report.error_count)
    columns[3].metric("время, с", manifest.total_seconds)

    _downloads(state, variant_state, cfg)

    slides, findings = st.columns([3, 2])
    with slides:
        numbering = {
            issue.key: number for number, issue in enumerate(result.report.issues, start=1)
        }
        for index, page in enumerate(result.pages, start=1):
            on_slide = [issue for issue in result.report.issues if issue.slide_index == index]
            st.caption(f"Слайд {index} · находок {len(on_slide)}")
            # `st.iframe`, а не снятый с поддержки `components.v1.html`.
            # HTML здесь наш собственный: картинка слайда и рамки по находкам,
            # ничего пользовательского внутрь не попадает.
            st.iframe(
                audit_overlay.render(
                    page,
                    on_slide,
                    result.deck.slide_width_emu,
                    result.deck.slide_height_emu,
                    selected=variant_state.selected,
                    numbering=numbering,
                ),
                height=audit_overlay.height_for(16, 9, 640),
            )
    with findings:
        _findings_panel(state, variant_state, client)

    with st.expander("Паспорт прогона"):
        st.caption(
            "Версии промптов, модель, параметры, хэш шаблона и seed — то, чем "
            "результат воспроизводится."
        )
        st.json(json.loads(manifest.model_dump_json()), expanded=False)


def main() -> None:
    st.set_page_config(page_title="deckwright", layout="wide")
    st.title("deckwright")
    st.caption(
        "Читает шаблон как набор правил и собирает по нему новые слайды: "
        "три варианта вёрстки, аудит поверх слайда, экспорт в три формата."
    )

    if not _require_password():
        return

    cfg = load_config(CONFIG)
    if cfg.llm.configured and not os.environ.get(PASSWORD_ENV):
        st.warning(
            f"Ключ модели настроен, а пароль ({PASSWORD_ENV}) — нет: любой, "
            "кто знает адрес, тратит ключ. Для публичного адреса задайте пароль."
        )
    request = _sidebar(cfg)

    if request is not None:
        client, vlm = _make_client(cfg, request["recorded"])
        # Выбор пользователя сильнее умолчания конфига и живёт ровно один
        # прогон: снимок настроек прогона неизменяем.
        cfg = cfg.model_copy(
            update={
                "run": cfg.run.model_copy(
                    update={"rewrite_assisted": bool(request["rewrite_assisted"])}
                )
            }
        )
        state = runs.start(
            template_path=request["template"],
            request=request["request"],
            cfg=cfg,
            client=client,
            variants=request["variants"],
            workspace=request["workspace"],
            vlm_client=vlm,
            fix_mode=request["fix_mode"],
        )
        # run_id в адресе: обновление страницы возвращает к тому же прогону,
        # а не начинает новый.
        st.query_params["run"] = state.run_id
        st.rerun()

    # Прогон ищется в пределах своей сессии: и по номеру из адреса, и как
    # последний. Чужой номер сюда не приведёт — `runs.get` сверяет владельца.
    owner = _session_id()
    run_id = st.query_params.get("run")
    state = runs.get(run_id, owner) if run_id else runs.latest(owner)
    if state is None:
        if run_id:
            st.warning(
                "Прогон не найден в этой сессии. Ссылка на прогон работает "
                "только в той вкладке, где он запущен: результаты не общие."
            )
            # Чужой номер в адресе только мешает — убираем, чтобы страница
            # вернулась к обычному виду.
            st.query_params.clear()
        st.info(
            "Загрузите шаблон, впишите бриф или приложите материалы слева, "
            "затем нажмите «Собрать»."
        )
        return

    st.subheader(f"Прогон {state.run_id} · {state.template_name}")
    if state.finished:
        _progress(state, cfg)
    else:
        _live_progress(state, cfg)
    if state.error:
        st.error(state.error)
    _input_summary(state)

    ready = state.ready()
    if ready:
        client = RecordedClient(RECORDED) if not cfg.llm.configured else None
        tabs = st.tabs([variant_state.name for variant_state in state.variants.values()])
        for tab, variant_state in zip(tabs, state.variants.values(), strict=False):
            with tab:
                _variant_tab(state, variant_state, client, cfg)




main()
