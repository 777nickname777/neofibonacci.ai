"""Реестр проверок: что вообще умеет аудит и какого рода каждая проверка.

Реестр — не украшение, а требование A14: каждая проверка обязана быть помечена
детерминированной или контекстной, и по её идентификатору должна находиться
строка в `docs/AUDIT.md`. Список здесь и список в документации сверяются
тестом — иначе они разъезжаются на первой же добавленной проверке.

Детерминированная проверка на одном слайде всегда даёт один ответ, поэтому её
находка годится для автоправки. Контекстную выполняет модель, на повторе она
может ответить иначе, и правка по ней требует подтверждения человека.
"""

from __future__ import annotations

from dataclasses import dataclass

from deckwright.schemas import CheckKind, FixKind, IssueCategory, Severity


@dataclass(frozen=True)
class Check:
    """Паспорт проверки."""

    id: str
    kind: CheckKind
    category: IssueCategory
    severity: Severity
    title: str
    # Как чинится находка этой проверки, если чинится вообще.
    fix: FixKind = FixKind.NONE


def _det(
    check_id: str,
    category: IssueCategory,
    severity: Severity,
    title: str,
    fix: FixKind = FixKind.NONE,
) -> Check:
    return Check(check_id, CheckKind.DETERMINISTIC, category, severity, title, fix)


def _ctx(
    check_id: str, category: IssueCategory, severity: Severity, title: str
) -> Check:
    # Контекстная находка никогда не чинится автоматически: ответ модели на
    # повторе может отличаться, и молча переписывать по нему колоду нельзя.
    return Check(check_id, CheckKind.CONTEXTUAL, category, severity, title, FixKind.ASSISTED)


CHECKS: tuple[Check, ...] = (
    # ── Геометрия ────────────────────────────────────────────────────────
    _det(
        "layout.out_of_bounds",
        IssueCategory.LAYOUT,
        Severity.ERROR,
        "Элемент выходит за границы слайда",
        FixKind.AUTOMATIC,
    ),
    _det(
        "layout.overlap",
        IssueCategory.LAYOUT,
        Severity.ERROR,
        "Два элемента накладываются друг на друга",
    ),
    _det(
        "layout.text_over_decor",
        IssueCategory.LAYOUT,
        Severity.ERROR,
        "Текст накладывается на графику шаблона: линию, иконку, картинку",
    ),
    _det(
        "layout.text_overflow",
        IssueCategory.LAYOUT,
        Severity.WARNING,
        "Текст не помещается в свою рамку",
        FixKind.ASSISTED,
    ),
    _det(
        "layout.text_without_place",
        IssueCategory.LAYOUT,
        Severity.WARNING,
        "Тексту слайда нет места в композиции шаблона",
        FixKind.ASSISTED,
    ),
    _det(
        "layout.margin_violation",
        IssueCategory.LAYOUT,
        Severity.WARNING,
        "Элемент заходит в поля шаблона",
        FixKind.AUTOMATIC,
    ),
    _det(
        "layout.off_grid",
        IssueCategory.LAYOUT,
        Severity.INFO,
        "Элемент не выровнен по сетке шаблона",
        FixKind.AUTOMATIC,
    ),
    _det(
        "layout.image_stretched",
        IssueCategory.LAYOUT,
        Severity.WARNING,
        "Картинка растянута: пропорции не совпадают с исходными",
        FixKind.AUTOMATIC,
    ),
    # ── Верность шаблону ─────────────────────────────────────────────────
    _det(
        "template.font_not_in_template",
        IssueCategory.TEMPLATE,
        Severity.ERROR,
        "Гарнитура не из шаблона",
    ),
    _det(
        "template.size_not_in_scale",
        IssueCategory.TEMPLATE,
        Severity.ERROR,
        "Кегль не из типографической шкалы шаблона",
    ),
    _det(
        "template.color_not_in_palette",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Цвет не из палитры шаблона",
    ),
    _det(
        "template.low_contrast",
        IssueCategory.TEMPLATE,
        Severity.ERROR,
        "Контраст текста с фоном ниже 4.5:1",
    ),
    _det(
        "template.brand_pair_contrast",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Контраст ниже порога у пары «цвет по подложке», которой пишет сам шаблон",
    ),
    _det(
        "template.text_color_off_template",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Цвет текста не тот, которым шаблон пишет на этой заливке",
    ),
    _det(
        "template.background_off_donor",
        IssueCategory.TEMPLATE,
        Severity.ERROR,
        "Фон слайда не такой, как у слайда-донора шаблона",
    ),
    _det(
        "template.unknown_layout",
        IssueCategory.TEMPLATE,
        Severity.ERROR,
        "Слайд ссылается на layout, которого нет в шаблоне",
    ),
    _det(
        "template.recurring_element_moved",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Логотип или колонтитул стоит не там, где в шаблоне",
        FixKind.AUTOMATIC,
    ),
    # ── Плотность ────────────────────────────────────────────────────────
    _det(
        "density.too_many_bullets",
        IssueCategory.DENSITY,
        Severity.WARNING,
        "Пунктов на слайде больше порога",
        FixKind.ASSISTED,
    ),
    _det(
        "density.bullet_too_long",
        IssueCategory.DENSITY,
        Severity.WARNING,
        "Пункт длиннее порога по числу слов",
        FixKind.ASSISTED,
    ),
    _det(
        "density.title_only",
        IssueCategory.DENSITY,
        Severity.ERROR,
        "Слайд содержания — один заголовок: содержание потерялось",
    ),
    _det(
        "density.slide_too_empty",
        IssueCategory.DENSITY,
        Severity.INFO,
        "Слайд заполнен меньше порога",
    ),
    _det(
        "density.slide_too_full",
        IssueCategory.DENSITY,
        Severity.WARNING,
        "Слайд заполнен больше порога",
    ),
    _det(
        "density.table_too_big",
        IssueCategory.DENSITY,
        Severity.WARNING,
        "Таблица больше 7 строк или 5 колонок",
    ),
    _det(
        "density.too_many_series",
        IssueCategory.DENSITY,
        Severity.WARNING,
        "На диаграмме больше пяти серий",
    ),
    # ── Целостность ──────────────────────────────────────────────────────
    _det(
        "content.placeholder_left",
        IssueCategory.INTEGRITY,
        Severity.ERROR,
        "Остался текст-заглушка шаблона",
    ),
    _det(
        "integrity.chart_unlabelled",
        IssueCategory.INTEGRITY,
        Severity.WARNING,
        "У диаграммы нет подписей осей, единиц или легенды",
    ),
    _det(
        "template.contrast_unverified",
        IssueCategory.TEMPLATE,
        Severity.INFO,
        "Контраст текста не измерен: под ним изображение или неизвестный фон",
    ),
    _det(
        "template.too_many_fonts",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "В колоде больше двух гарнитур",
    ),
    _det(
        "integrity.package_broken",
        IssueCategory.INTEGRITY,
        Severity.ERROR,
        "Пакет .pptx повреждён: битые связи или части",
    ),
    _det(
        "integrity.slide_is_single_image",
        IssueCategory.INTEGRITY,
        Severity.ERROR,
        "Слайд состоит из одной картинки",
    ),
    _det(
        "integrity.donor_data_leftover",
        IssueCategory.INTEGRITY,
        Severity.ERROR,
        "На слайде остались данные донора: рыбный график или чужое число",
    ),
    _det(
        "integrity.donor_photo_left",
        IssueCategory.INTEGRITY,
        Severity.ERROR,
        "На слайде осталось фото шаблона: иллюстрация чужой темы",
    ),
    _det(
        "integrity.duplicate_slides",
        IssueCategory.INTEGRITY,
        Severity.WARNING,
        "Два слайда повторяют друг друга",
    ),
    # ── Содержание: числа ────────────────────────────────────────────────
    _det(
        "content.figure_not_in_sources",
        IssueCategory.CONTENT,
        Severity.ERROR,
        "Число не подтверждается контент-пакетом",
    ),
    _det(
        "template.text_too_small",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Кегль ниже читаемого минимума роли",
    ),
    _det(
        "template.text_color_split",
        IssueCategory.TEMPLATE,
        Severity.WARNING,
        "Текст слайда не сведён к одному цвету",
    ),
    _det(
        "content.figure_cites_wrong_fact",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Число взято из входа, но сослано не на тот источник",
    ),
    _det(
        "content.undeclared_number",
        IssueCategory.CONTENT,
        Severity.ERROR,
        "На слайде число, которого нет во входе",
    ),
    _det(
        "content.derived_figure_wrong",
        IssueCategory.CONTENT,
        Severity.ERROR,
        "Производное число не сходится с собственной формулой",
    ),
    # ── Контекстные: вопросы Приложения 1 ────────────────────────────────
    _ctx(
        "content.body_matches_title",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Содержание слайда отвечает его заголовку",
    ),
    _ctx(
        "content.has_content",
        IssueCategory.CONTENT,
        Severity.ERROR,
        "На слайде есть содержание, а не только оформление",
    ),
    _ctx(
        "content.visuals_on_topic",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Картинки и иконки относятся к теме слайда",
    ),
    _ctx(
        "content.no_prompt_leftovers",
        IssueCategory.CONTENT,
        Severity.ERROR,
        "На слайде не осталось подсказок и рыбного текста шаблона",
    ),
    _ctx(
        "content.table_rows_serve_point",
        IssueCategory.CONTENT,
        Severity.INFO,
        "Строки таблицы работают на мысль слайда",
    ),
    _ctx(
        "content.title_is_takeaway",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Заголовок формулирует вывод, а не называет тему",
    ),
    _ctx(
        "content.one_sentence_summary",
        IssueCategory.CONTENT,
        Severity.INFO,
        "Слайд сводится к одной фразе",
    ),
    _ctx(
        "content.figures_in_sources",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Числа на слайде прослеживаются до источника",
    ),
    _ctx(
        "content.no_typos",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "В тексте нет опечаток",
    ),
    _ctx(
        "content.single_language",
        IssueCategory.CONTENT,
        Severity.WARNING,
        "Колода написана на одном языке",
    ),
    _ctx(
        "content.slides_connected",
        IssueCategory.CONTENT,
        Severity.INFO,
        "Соседние слайды связаны по смыслу",
    ),
)

BY_ID: dict[str, Check] = {check.id: check for check in CHECKS}


def check(check_id: str) -> Check:
    """Паспорт проверки по идентификатору.

    Неизвестный идентификатор — ошибка программиста, а не пользователя:
    находка без паспорта не попадёт ни в документацию, ни в UI.
    """
    try:
        return BY_ID[check_id]
    except KeyError as missing:
        raise KeyError(
            f"проверка {check_id!r} не объявлена в реестре; "
            "добавьте её в CHECKS и в docs/AUDIT.md"
        ) from missing


def deterministic_ids() -> list[str]:
    return [c.id for c in CHECKS if c.kind is CheckKind.DETERMINISTIC]


def contextual_ids() -> list[str]:
    return [c.id for c in CHECKS if c.kind is CheckKind.CONTEXTUAL]
