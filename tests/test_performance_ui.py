"""Контрольные случаи группы 7: скорость и устойчивый интерфейс.

* отмена: новые задачи не ставятся, поздний результат не публикуется;
* постоянный отказ провайдера замечается один раз, а не на каждом слайде;
* кэш разбора не переиспользуется при смене содержимого шаблона;
* смена значимой настройки инвалидирует кэш;
* у идущего прогона неизменяемый снимок параметров;
* повторное нажатие и обновление страницы не плодят прогоны.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from deckwright.config import load_config  # noqa: E402
from deckwright.parse.opener import parse_template  # noqa: E402
from deckwright.workspace import new_run_id, new_session_id  # noqa: E402

CONFIG = ROOT / "configs" / "config.yaml"
TEMPLATE = ROOT / "data" / "templates" / "vk_tech.pptx"


# ── отмена ──────────────────────────────────────────────────────────────


def _state(owner: str, run_id: str):
    import runs

    state = runs.RunState(
        run_id=run_id,
        template_name="t.pptx",
        started_at=datetime.now(UTC),
        owner=owner,
        variants={"balanced": runs.VariantState(name="balanced")},
    )
    with runs._LOCK:
        runs._RUNS[run_id] = state
    return state


def test_cancel_marks_the_run_and_only_for_its_owner():
    import runs

    owner = new_session_id()
    run_id = new_run_id()
    state = _state(owner, run_id)

    assert runs.cancel(run_id, "чужая-сессия") is False, "чужой прогон отменился"
    assert state.cancelled is False

    assert runs.cancel(run_id, owner) is True
    assert state.cancelled is True
    assert state.stage == "отменяю"


def test_a_finished_run_cannot_be_cancelled():
    import runs

    owner = new_session_id()
    run_id = new_run_id()
    state = _state(owner, run_id)
    state.finished = True
    assert runs.cancel(run_id, owner) is False


def test_cancellation_does_not_promise_to_stop_a_request_in_flight():
    """Обещание должно совпадать с делом: уже отправленный запрос не вернуть."""
    import runs

    assert "не опубликует" in (runs.cancel.__doc__ or "")
    source = (ROOT / "app" / "ui.py").read_text("utf-8")
    assert "Отменить прогон" in source
    assert "остановить нельзя" in source


def test_a_late_result_is_not_published_after_cancel():
    """Ответ, пришедший после отмены, не становится результатом прогона."""
    source = (ROOT / "app" / "runs.py").read_text("utf-8")
    assert "if state.cancelled:" in source
    # Результат присваивается только после повторной проверки флага.
    assert "built = complete_variant(laid)" in source
    assert "variant_state.result = built" in source


# ── постоянный отказ провайдера ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Error code: 403 - {'code': 30003, 'message': 'Model disabled.'}",
        "PermissionDeniedError: доступа нет",
        "Error code: 401 - unauthorized",
        "model not found",
    ],
)
def test_permanent_provider_failures_are_recognised(text):
    from deckwright.audit.contextual import runner

    assert runner._is_permanent(RuntimeError(text)), f"не признан постоянным: {text}"


@pytest.mark.parametrize(
    "text",
    ["timed out", "connection reset by peer", "429 too many requests"],
)
def test_temporary_failures_are_not_treated_as_permanent(text):
    """Тайм-аут и лимит лечатся повтором: гасить из-за них аудит нельзя."""
    from deckwright.audit.contextual import runner

    assert not runner._is_permanent(RuntimeError(text))


def test_a_permanent_failure_is_asked_once_per_run():
    """Отказ «модель отключена» не повторяется по каждому слайду.

    На живом прогоне это стоило минут: одиннадцать вопросов × одиннадцать
    слайдов × три варианта заведомо отказных вызовов.
    """
    from deckwright.audit.contextual import runner

    source = Path(runner.__file__).read_text("utf-8")
    assert "_permanent.get(id(client))" in source
    assert "_permanent[id(client)] = str(failure)" in source


# ── кэш разбора шаблона ─────────────────────────────────────────────────


def test_changed_template_content_is_reparsed(tmp_path):
    """Тот же путь и то же имя, другое содержимое — старый кэш не годится."""
    if not TEMPLATE.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    cache = tmp_path / "cache"
    copy = tmp_path / "шаблон.pptx"
    copy.write_bytes(TEMPLATE.read_bytes())
    first = parse_template(copy, cache, cfg.fonts.extract_dir)

    other = ROOT / "data" / "templates" / "vk_workspace.pptx"
    if not other.exists():
        pytest.skip("нет второго шаблона")
    copy.write_bytes(other.read_bytes())
    second = parse_template(copy, cache, cfg.fonts.extract_dir)

    assert first.template_sha256 != second.template_sha256
    assert first.source_name == second.source_name == "шаблон.pptx"
    assert len(first.patterns) != len(second.patterns) or (
        first.slide_width_emu != second.slide_width_emu
    ), "подменённый шаблон разобрался как прежний"


def test_parser_version_is_part_of_the_cache_key():
    """Правка разбора сбрасывает кэш сама: ручной номер версии забывают."""
    from deckwright.parse.opener import _parser_fingerprint

    assert len(_parser_fingerprint()) >= 8


def test_a_warm_parse_matches_a_cold_one(tmp_path):
    """Кэш обязан отдавать ровно то же, что свежий разбор."""
    if not TEMPLATE.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    cache = tmp_path / "cache"
    cold = parse_template(TEMPLATE, cache, cfg.fonts.extract_dir)
    warm = parse_template(TEMPLATE, cache, cfg.fonts.extract_dir)
    assert cold.model_dump() == warm.model_dump()


def test_a_warm_parse_is_faster(tmp_path):
    """Кэш существует не для красоты: разбор шаблона — секунды."""
    if not TEMPLATE.exists():
        pytest.skip("нет шаблона vk_tech")
    cfg = load_config(CONFIG)
    cache = tmp_path / "cache"
    started = time.monotonic()
    parse_template(TEMPLATE, cache, cfg.fonts.extract_dir)
    cold = time.monotonic() - started
    started = time.monotonic()
    parse_template(TEMPLATE, cache, cfg.fonts.extract_dir)
    warm = time.monotonic() - started
    assert warm < cold, f"тёплый разбор не быстрее: {warm:.2f}с против {cold:.2f}с"


# ── состояния интерфейса ────────────────────────────────────────────────


def test_every_run_state_has_a_name():
    """Пользователь обязан видеть, что происходит, а не «идёт работа»."""
    source = (ROOT / "app" / "runs.py").read_text("utf-8")
    for stage in (
        "ожидает",
        "разбираю вход",
        "собираю варианты",
        "применяю исправления",
        "повторяю экспорт PDF",
        "готово",
        "готово частично",
        "готово не всё",
        "отменено",
        "прервано",
    ):
        assert f'"{stage}"' in source, f"нет состояния {stage!r}"


def test_progress_is_shown_by_finished_work_not_by_a_made_up_percent():
    """Выдуманный процент хуже честного «готово 1 из 3»."""
    source = (ROOT / "app" / "ui.py").read_text("utf-8")
    assert "готово вариантов" in source
    assert "state.done_count / total" in source


# ── параллельность экспорта ─────────────────────────────────────────────


def test_parallelism_is_capped_by_the_machine(monkeypatch):
    """Просить больше, чем машина тянет, — менять параллельность на своппинг."""
    from deckwright.render import pdf

    monkeypatch.setattr(pdf.os, "cpu_count", lambda: 2)
    pdf._limit = None
    pdf._limit_size = 0
    pdf.configure_parallelism(8)
    assert pdf._limit_size == 1, f"предел не срезан по ядрам: {pdf._limit_size}"

    monkeypatch.setattr(pdf.os, "cpu_count", lambda: 8)
    pdf._limit = None
    pdf._limit_size = 0
    pdf.configure_parallelism(3)
    assert pdf._limit_size == 3


def test_parallelism_is_never_zero(monkeypatch):
    from deckwright.render import pdf

    pdf._limit = None
    pdf._limit_size = 0
    pdf.configure_parallelism(0)
    assert pdf._limit_size >= 1


def test_deck_is_handed_over_before_the_audit(tmp_path, monkeypatch):
    """Файлы варианта готовы до аудита — и интерфейс получает их сразу.

    Бюджет считается «с загрузки до выгрузки презентации пользователю», а
    аудит по картинкам занимает 80-111 с из 170-262 с прогона (замеры
    run 28-31). Колода к началу аудита уже собрана, проверена на
    целостность и выгружена: держать её до конца проверки значит отдавать
    то же самое, но на полторы минуты позже.
    """
    import json

    sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    from make_template import build_template

    from deckwright.llm.fake import RecordedClient
    from deckwright.pipeline import complete_variant, lay_out_variant
    from deckwright.schemas import ContentPack

    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "configs" / "config.yaml")
    pack = ContentPack.model_validate(
        json.loads((root / "tests/fixtures/content_pack.json").read_text("utf-8"))
    )
    template = build_template(tmp_path / "t.pptx")

    seen: list[dict] = []
    stages: list[str] = []

    def on_export(ready: dict) -> None:
        # Проверяем не «позвали», а «в этот момент файл уже лежит на диске»:
        # кнопка, ведущая в пустоту, хуже отсутствующей кнопки.
        seen.append(
            {
                "pptx": ready["pptx"].is_file() and ready["pptx"].stat().st_size > 0,
                "аудит уже был": "audit" in stages,
            }
        )

    laid = lay_out_variant(
        template,
        pack,
        cfg,
        RecordedClient(root / "tests/fixtures/recorded"),
        "balanced",
        tmp_path / "out",
        run_id="early",
        on_stage=stages.append,
        on_export=on_export,
    )
    result = complete_variant(laid)

    assert seen, "интерфейс не узнал, что файлы готовы"
    assert seen[0]["pptx"], "позвали раньше, чем файл дописан"
    assert not seen[0]["аудит уже был"], "позвали после аудита — смысла нет"
    assert "audit" in stages, "этап аудита так и не начался: проба ничего не проверяет"
    assert result.report is not None, "аудит всё равно обязан пройти"
