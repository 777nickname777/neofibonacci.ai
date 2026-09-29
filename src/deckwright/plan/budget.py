"""Бюджеты длины текста, выведенные из самого шаблона.

Зачем это планировщику. Заголовок, не влезший в свою рамку, обходится дорого:
фиттер либо мельчит кегль, либо зовёт модель сократить текст ещё раз — лишние
секунды в бюджете пяти минут и лишние токены в счёте. Написать текст нужной
длины сразу дешевле, чем ужимать потом.

Числа берутся не из головы, а из геометрии шаблона: сколько символов влезает
в его заголовочную рамку при кегле из его же шкалы. У разных шаблонов это
разные числа — у `vk_tech` заголовочная рамка 7.4 дюйма при кегле 18, у
`vk_education` — 11.9 дюйма при 32, и одна и та же формулировка ведёт себя в
них по-разному.

Пороги плотности приходят из Приложения 1 ТЗ через конфиг: не больше шести
буллетов на слайде, буллет не длиннее пятнадцати слов.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from deckwright.layout.text_metrics import (
    MeasurementSource,
    characters_that_fit,
    metrics_for_spec,
)
from deckwright.schemas import Box, SlotRole, TemplateSpec

# Запас от края рамки: писать в упор нельзя, иначе любая неточность измерения
# выносит текст за границу.
BUDGET_SAFETY = 0.9

# Какую долю продемонстрированных длин бюджет обязан покрывать.
DEMONSTRATION_PERCENTILE = 0.8

# Если у шаблона не нашлось ни одной заголовочной рамки, берём долю слайда.
DEFAULT_TITLE_WIDTH_SHARE = 0.8
DEFAULT_TITLE_HEIGHT_SHARE = 0.15
DEFAULT_BODY_WIDTH_SHARE = 0.8
DEFAULT_BODY_HEIGHT_SHARE = 0.5


@dataclass(frozen=True)
class LengthBudget:
    """Сколько текста уместно писать в этот шаблон."""

    title_chars: int
    subtitle_chars: int
    bullet_chars: int
    max_bullets: int
    max_words_per_bullet: int
    # Чем мерили: шрифтом шаблона, метрическим клоном или чужой гарнитурой.
    measured_with: str
    metric_compatible: bool = True
    # Ёмкость по видам блоков, снятая с макетов шаблона: {вид: (пунктов,
    # символов в пункте)}. Считает слой вёрстки тем же предсказателем, по
    # которому потом выбирает макет; здесь это только числа.
    block_limits: tuple[tuple[str, int, int], ...] = ()

    def curve_for(self, kind: str) -> list[tuple[int, int]]:
        """Точки ёмкости вида блока: [(пунктов, символов в пункте), …]."""
        return [(items, chars) for name, items, chars in self.block_limits if name == kind]

    @staticmethod
    def _curve_text(curve: list[tuple[int, int]], forms: tuple[str, str, str]) -> str:
        """«1–2 пункта до 110 символов; 3 пункта до 74; 4–5 пунктов до 50»."""
        parts, previous = [], 0
        for items, chars in curve:
            span = f"{previous + 1}–{items}" if items > previous + 1 else f"{items}"
            parts.append(f"{span} {_plural(items, forms)} до {chars} символов")
            previous = items
        return "; ".join(parts)

    def as_prompt_lines(self) -> str:
        """Ограничения в том виде, в каком они уходят в промпт.

        Если ёмкость макетов известна, она заменяет общий порог «до шести
        пунктов»: живой план #12 этому порогу подчинился и всё равно
        переполнил 37 слайдов из 90. Ёмкость — кривая: чем меньше пунктов,
        тем длиннее каждый, и выбор между ними остаётся модели.
        """
        if self.title_chars > 0:
            lines = [
                f"- заголовок слайда: не длиннее {self.title_chars} символов",
                f"- подзаголовок: не длиннее {self.subtitle_chars} символов",
            ]
        else:
            # Ноль — это «не измерено», а не «ноль символов». Названный
            # числом, он уходит в промпт требованием, которое нельзя
            # выполнить.
            lines = [
                "- длина заголовка и подзаголовка не измерена: пиши коротко, "
                "одной мыслью",
            ]
        listed = self.curve_for("bullets")
        steps = self.curve_for("steps")
        paragraph = self.curve_for("paragraph")
        if listed:
            lines.append(
                "- список (bullets), в пункте не больше "
                f"{self.max_words_per_bullet} слов: "
                + self._curve_text(listed, ("пункт", "пункта", "пунктов"))
            )
        if steps:
            lines.append("- шаги (steps): " + self._curve_text(steps, ("шаг", "шага", "шагов")))
        if paragraph:
            lines.append(f"- абзац (paragraph): не длиннее {paragraph[-1][1]} символов")
        if not listed:
            if self.bullet_chars > 0:
                lines.append(
                    f"- пункт списка: не длиннее {self.bullet_chars} символов "
                    f"и не длиннее {self.max_words_per_bullet} слов"
                )
            else:
                lines.append(
                    "- длина пункта не измерена: не длиннее "
                    f"{self.max_words_per_bullet} слов"
                )
            lines.append(f"- пунктов на слайде: не больше {self.max_bullets}")
        else:
            lines.append(
                "- длиннее или больше, чем сказано, в блок не помещается: выбери "
                "число пунктов под свой текст, разнеси содержание на два слайда "
                "или оставь главное"
            )
        for intent, name in (("cover", "титульный слайд"), ("closing", "завершающий слайд")):
            title = self.curve_for(f"{intent}_title")
            text = self.curve_for(f"{intent}_text")
            parts = []
            if title:
                parts.append(f"заголовок не длиннее {title[0][1]} символов")
            if text and text[0][1]:
                parts.append(
                    f"подзаголовок под ним — один абзац не длиннее {text[0][1]} символов"
                )
            elif text:
                parts.append("места под подзаголовок нет — только заголовок, без блоков")
            if parts:
                lines.append(f"- {name}: " + ", ".join(parts))
        return "\n".join(lines)


def _plural(number: int, forms: tuple[str, str, str]) -> str:
    """Форма слова после числа: 1 пункт, 3 пункта, 5 пунктов."""
    tail = number % 100
    if 11 <= tail <= 14:
        return forms[2]
    last = number % 10
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def _demonstrated_length(spec: TemplateSpec, role: SlotRole) -> int:
    """Сколько символов шаблон сам пишет в слоты этой роли.

    Самое прямое свидетельство из всех: рыбный текст в шаблоне показывает,
    на какую длину рассчитывал дизайнер. У `vk_tech` в заголовочной рамке
    написано «Заголовок в две или в одну строчку» — тридцать четыре символа,
    при том что расчёт по метрикам даёт двадцать. Расчёт осторожен и считает
    строки с округлением вниз; демонстрация точна, потому что она уже
    свёрстана и отрисована.

    Берём максимум из двух оценок: расчёт страхует шаблон без рыбного текста
    (старая презентация пользователя), демонстрация — расчёт от чрезмерной
    осторожности.
    """
    lengths = sorted(
        len(slot.placeholder_text.strip())
        for source in (
            (slot for layout in spec.layouts for slot in layout.slots),
            (slot for pattern in spec.content_patterns for slot in pattern.slots),
        )
        for slot in source
        if slot.role is role and slot.placeholder_text.strip()
    )
    if not lengths:
        return 0
    # Не медиана: рамка обязана держать самый длинный заголовок, который
    # дизайнер счёл приемлемым, а не средний. Но и не максимум — одна
    # случайно длинная подпись не должна задавать бюджет всей колоде.
    index = min(len(lengths) - 1, int(len(lengths) * DEMONSTRATION_PERCENTILE))
    return lengths[index]


def _median_box(boxes: list[Box], fallback: Box) -> Box:
    """Типичная рамка среди найденных. Медиана, а не среднее: одна рамка во
    весь слайд не должна утягивать оценку за собой."""
    if not boxes:
        return fallback
    return Box(
        x=fallback.x,
        y=fallback.y,
        w=max(1, round(median(box.w for box in boxes))),
        h=max(1, round(median(box.h for box in boxes))),
    )


def _boxes_for(spec: TemplateSpec, role: SlotRole) -> list[Box]:
    """Рамки этой роли: сначала из layout'ов, и только если их нет — из слайдов.

    Смешивать источники нельзя: медиана по объединению берёт ширину у одних
    рамок, а высоту у других, и получается рамка, которой в шаблоне нет. На
    `vk_tech` это давало заголовочную рамку 4.29 на 0.36 дюйма — ширину от
    layout'ов, высоту от подписей внутри карточек.

    Layout объявляет рамку явно и потому авторитетнее; слайды-примеры идут в
    дело, когда шаблон рамок не объявляет вовсе — а это обычный случай.
    """
    from_layouts = [
        slot.box
        for layout in spec.layouts
        for slot in layout.slots
        if slot.role is role
    ]
    if from_layouts:
        return from_layouts
    return [
        slot.box
        for pattern in spec.content_patterns
        for slot in pattern.slots
        if slot.role is role
    ]


def _scale_size(spec: TemplateSpec, position: float) -> float:
    """Кегль из шкалы шаблона по относительному положению в ней."""
    scale = spec.type_scale_pt or [18.0]
    index = min(len(scale) - 1, max(0, round((len(scale) - 1) * position)))
    return scale[index]


def _slot_size(spec: TemplateSpec, role: SlotRole, position: float) -> float:
    """Кегль, которым шаблон набирает слоты этой роли.

    Берётся медиана реальных кеглей, а не позиция в шкале: верх шкалы занят
    обложечными размерами, и бюджет рабочего заголовка по ним выходит втрое
    меньше настоящего. К шкале обращаемся, только если слоты кегля не
    сообщили.
    """
    sizes = [
        slot.style.size_pt
        for layout in spec.layouts
        for slot in layout.slots
        if slot.role is role and slot.style is not None
    ] or [
        slot.style.size_pt
        for pattern in spec.content_patterns
        for slot in pattern.slots
        if slot.role is role and slot.style is not None
    ]
    return median(sizes) if sizes else _scale_size(spec, position)


def compute_budget(
    spec: TemplateSpec,
    max_bullets: int,
    max_words_per_bullet: int,
    source: MeasurementSource | None = None,
    substitution_slack: float = 0.8,
) -> LengthBudget:
    """Бюджеты длины для этого шаблона.

    Запас на подстановку применяется **только** к шрифту с другими ширинами.
    Метрический клон в запасе не нуждается: у Carlito те же ширины, что у
    Calibri, и текст переносится там же, где у человека с оригиналом. Ужимать
    бюджет в таком случае значит без причины требовать от модели более
    коротких формулировок.
    """
    source = source or metrics_for_spec(spec)
    metrics, measured_with = source.metrics, source.description
    if metrics is None:
        # Мерить нечем. Выдумывать длины нельзя, но и нули — не «неизвестно»:
        # в промпт они уходили строкой «заголовок не длиннее 0 символов», и
        # модель читала это как требование.
        #
        # Запасная мерка есть, и она не выдумка: длина текста, которым сам
        # дизайнер заполнил слоты шаблона. Это измерение шаблона, а не
        # расчёт по шрифту. Нет и его — длины остаются неизвестными, и
        # промпт так и говорит.
        shown_title = _demonstrated_length(spec, SlotRole.TITLE)
        shown_body = _demonstrated_length(spec, SlotRole.BODY)
        if shown_title or shown_body:
            return LengthBudget(
                title_chars=shown_title,
                subtitle_chars=round(shown_title * 0.8),
                bullet_chars=shown_body,
                max_bullets=max_bullets,
                max_words_per_bullet=max_words_per_bullet,
                measured_with=(
                    f"{measured_with}; длины сняты с текста самого шаблона"
                ),
                metric_compatible=False,
            )
        return LengthBudget(
            0, 0, 0, max_bullets, max_words_per_bullet, measured_with, False
        )
    slack = 1.0 if source.metric_compatible else substitution_slack

    width, height = spec.slide_width_emu, spec.slide_height_emu
    title_box = _median_box(
        _boxes_for(spec, SlotRole.TITLE),
        Box(
            x=0,
            y=0,
            w=round(width * DEFAULT_TITLE_WIDTH_SHARE),
            h=round(height * DEFAULT_TITLE_HEIGHT_SHARE),
        ),
    )
    body_box = _median_box(
        _boxes_for(spec, SlotRole.BODY),
        Box(
            x=0,
            y=0,
            w=round(width * DEFAULT_BODY_WIDTH_SHARE),
            h=round(height * DEFAULT_BODY_HEIGHT_SHARE),
        ),
    )

    title_size = _slot_size(spec, SlotRole.TITLE, 0.85)
    body_size = _slot_size(spec, SlotRole.BODY, 0.45)

    title_chars = max(
        round(
            characters_that_fit(metrics, title_size, title_box.w, title_box.h)
            * BUDGET_SAFETY
        ),
        _demonstrated_length(spec, SlotRole.TITLE),
    )
    body_chars = max(
        round(
            characters_that_fit(metrics, body_size, body_box.w, body_box.h) * BUDGET_SAFETY
        ),
        # Тело показывает длину одного пункта, а не всего списка.
        _demonstrated_length(spec, SlotRole.BODY) * max_bullets,
    )

    title_chars = round(title_chars * slack)
    body_chars = round(body_chars * slack)

    return LengthBudget(
        title_chars=max(20, title_chars),
        # Подзаголовок живёт в той же рамке, но мельче и короче заголовка.
        subtitle_chars=max(20, round(title_chars * 0.8)),
        # Тело делится между пунктами: шесть пунктов по всей высоте не влезут.
        bullet_chars=max(20, body_chars // max(1, max_bullets)),
        max_bullets=max_bullets,
        max_words_per_bullet=max_words_per_bullet,
        measured_with=measured_with,
        metric_compatible=source.metric_compatible,
    )
