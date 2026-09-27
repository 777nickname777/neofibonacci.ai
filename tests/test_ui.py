"""Интерфейс: то, что можно проверить без браузера.

Сам сценарий — загрузка, прогон, выбор, применение — проверяется живым
прогоном страницы в Chromium (см. `progress/handoff.md`, фаза 11). Здесь
проверяется то, что не требует поднятого сервера и потому обязано быть
тестом: разметка оверлея и её соответствие находкам.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import audit_overlay

from deckwright.schemas import (
    Box,
    CheckKind,
    Issue,
    IssueCategory,
    Severity,
)

SLIDE_W = 12192000
SLIDE_H = 6858000


def _issue(check_id: str, slide: int, box: Box | None, severity=Severity.WARNING) -> Issue:
    return Issue(
        check_id=check_id,
        kind=CheckKind.DETERMINISTIC,
        category=IssueCategory.LAYOUT,
        severity=severity,
        slide_index=slide,
        element_ids=["e1"],
        bbox=box,
        message=f"{check_id} на слайде {slide}",
    )


@pytest.fixture
def slide_png(tmp_path):
    """Одноцветная картинка: оверлею важны только её байты."""
    from PIL import Image

    path = tmp_path / "slide-01.png"
    Image.new("RGB", (640, 360), (240, 240, 240)).save(path)
    return path


def test_frame_lands_where_the_finding_is(slide_png):
    """Рамка ставится долями от размера слайда, а не в пикселях.

    Доля не зависит от разрешения растеризации: слайд, отрисованный в 96 и в
    150 dpi, даёт одну и ту же рамку.
    """
    box = Box(x=SLIDE_W // 4, y=SLIDE_H // 2, w=SLIDE_W // 2, h=SLIDE_H // 4)
    html = audit_overlay.render(slide_png, [_issue("layout.overlap", 1, box)], SLIDE_W, SLIDE_H)
    assert "left:25.000%" in html
    assert "top:50.000%" in html
    assert "width:50.000%" in html
    assert "height:25.000%" in html


def test_numbers_match_the_list(slide_png):
    """Номер рамки — это номер строки в списке находок, а не порядок на слайде.

    Список нумеруется по всей колоде, рамки рисуются по слайдам. Без общей
    нумерации рамка №1 на третьем слайде означала бы другой пункт списка.
    """
    box = Box(x=0, y=0, w=1000, h=1000)
    first = _issue("layout.out_of_bounds", 3, box)
    second = _issue("layout.off_grid", 3, box)
    numbering = {first.key: 7, second.key: 12}
    html = audit_overlay.render(
        slide_png, [first, second], SLIDE_W, SLIDE_H, numbering=numbering
    )
    assert ">7</span>" in html
    assert ">12</span>" in html
    assert ">1</span>" not in html


def test_finding_without_a_box_draws_no_frame(slide_png):
    """Рамка вокруг всего слайда соврала бы о том, где проблема."""
    html = audit_overlay.render(slide_png, [_issue("content.no_typos", 1, None)], SLIDE_W, SLIDE_H)
    assert 'class="finding' not in html


def test_chosen_finding_is_visible_on_the_slide(slide_png):
    """Отмеченную находку видно на слайде, а не только в списке."""
    box = Box(x=0, y=0, w=1000, h=1000)
    issue = _issue("layout.out_of_bounds", 1, box)
    plain = audit_overlay.render(slide_png, [issue], SLIDE_W, SLIDE_H)
    chosen = audit_overlay.render(slide_png, [issue], SLIDE_W, SLIDE_H, selected={issue.key})
    assert "finding chosen" in chosen
    assert "finding chosen" not in plain


def test_overlay_makes_no_outside_requests(slide_png):
    """Компонент живёт в изолированном iframe: ссылка на файл из него не
    разрешится, поэтому картинка обязана быть вшита."""
    box = Box(x=0, y=0, w=1000, h=1000)
    html = audit_overlay.render(slide_png, [_issue("layout.overlap", 1, box)], SLIDE_W, SLIDE_H)
    assert "data:image/png;base64," in html
    assert "http://" not in html and "https://" not in html
    assert str(slide_png) not in html


def test_message_is_escaped(slide_png):
    """Текст находки попадает в подсказку и не имеет права ломать разметку."""
    issue = _issue('layout.overlap', 1, Box(x=0, y=0, w=10, h=10))
    issue.message = '<script>alert("1")</script>'
    html = audit_overlay.render(slide_png, [issue], SLIDE_W, SLIDE_H)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── Вход по паролю ───────────────────────────────────────────────────────────


def _app(monkeypatch, password: str | None):
    from streamlit.testing.v1 import AppTest

    if password is None:
        monkeypatch.delenv("DECKWRIGHT_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DECKWRIGHT_PASSWORD", password)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    return AppTest.from_file(str(root / "app" / "ui.py"), default_timeout=60)


def test_password_closes_the_interface(monkeypatch):
    """С паролем в окружении без входа не видно ни загрузки шаблона, ни прогонов."""
    app = _app(monkeypatch, "секрет").run()
    assert len(app.text_input) == 1 and app.text_input[0].label == "Пароль"
    assert not app.sidebar.button, "кнопка «Собрать» доступна без пароля"


def test_wrong_password_is_refused_and_right_one_lets_in(monkeypatch):
    app = _app(monkeypatch, "секрет").run()
    app.text_input[0].input("не тот")
    app.button[0].click().run()
    assert app.error and "Неверный пароль" in app.error[0].value
    assert not app.sidebar.button

    app.text_input[0].input("секрет")
    app.button[0].click().run()
    assert app.sidebar.button, "после верного пароля интерфейс не открылся"


def test_without_password_the_interface_is_open(monkeypatch):
    app = _app(monkeypatch, None).run()
    assert not [field for field in app.text_input if field.label == "Пароль"]
    assert app.sidebar.button


def test_input_accepts_a_brief_and_files_of_any_format(monkeypatch):
    """Вход: поле брифа, загрузка txt/md/docx/pdf/pptx/json, назначение, спикер."""
    app = _app(monkeypatch, None).run()
    assert [area.label for area in app.sidebar.text_area] == ["Бриф или текст"]
    assert {"Кто выступает", "Должность выступающего"} <= {
        field.label for field in app.sidebar.text_input
    }
    purpose = next(box for box in app.sidebar.selectbox if box.label == "Назначение")
    assert purpose.options == [
        "определить по материалам", "фича", "продукт", "проект", "инициатива"
    ]
    # Без входа предлагается демонстрационный пакет; с брифом — уже нет.
    sample = next(box for box in app.sidebar.checkbox if "демонстрационный" in box.label)
    assert sample.value is True
    app.sidebar.text_area[0].input("Покажем фичу автопротокола руководителям.").run()
    sample = next(box for box in app.sidebar.checkbox if "демонстрационный" in box.label)
    assert sample.value is False


# ── Что можно отметить к применению ─────────────────────────────────────────


def test_only_fixable_findings_can_be_picked():
    """Пункт без исправления отметить нельзя: кнопка показывала «(0)».

    Применить можно автоматическую правку и переписывание текста моделью;
    плотность, повтор и вопрос к человеку — только к сведению.
    """
    from app import runs

    from deckwright.schemas import (
        CheckKind,
        FixKind,
        Issue,
        IssueCategory,
        ProposedFix,
        Severity,
    )

    def finding(kind: FixKind, action: str) -> Issue:
        return Issue(
            check_id="density.slide_too_empty",
            kind=CheckKind.DETERMINISTIC,
            category=IssueCategory.DENSITY,
            severity=Severity.INFO,
            slide_index=1,
            message="слайд пустоват",
            fix=ProposedFix(kind=kind, description="—", action=action),
        )

    assert not runs.applicable(finding(FixKind.NONE, ""))
    assert not runs.applicable(finding(FixKind.ASSISTED, "review_contextual_finding"))
    assert runs.applicable(finding(FixKind.AUTOMATIC, "move_inside_margins"))
    assert runs.applicable(finding(FixKind.ASSISTED, "shorten_paragraph"))
