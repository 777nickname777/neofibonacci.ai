"""Стратегия вёрстки: одна ось различий между тремя вариантами колоды.

ТЗ требует три варианта одной презентации. Три ветки кода дали бы три
источника ошибок и три места, где чинить каждую находку аудита, поэтому
вариант — это **параметр**, а не ветка: пайплайн один, пресеты разные
(`configs/variants/*.yaml`).

Здесь конфигурационный `LayoutStrategy` превращается в решения, которыми
пользуются матчер и фиттер: с какого кегля начинать, какие классы композиций
предпочитать, во что обращать числовые ряды и что делать, когда текст не
влез даже на минимальной ступени шкалы.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from deckwright.schemas import BlockKind, PatternClass, SlotRole, TemplateSpec


class OverflowAction(StrEnum):
    """Что делать, когда минимальная ступень шкалы не спасла.

    Уменьшать кегль дальше нельзя: проверка «кегль не из типографической
    шкалы шаблона» справедливо найдёт промежуточное значение, и чинить
    придётся то же самое, но позже.
    """

    SPLIT = "split"
    SHORTEN = "shorten"


# Во что обращается числовой ряд при каждом режиме подачи данных. `auto`
# решается по содержанию блока, поэтому его здесь нет.
_SERIES_ROLE: dict[str, SlotRole] = {
    "table": SlotRole.TABLE,
    "chart": SlotRole.CHART,
    "factoid": SlotRole.KPI_VALUE,
}

# Какое место нужно блоку плана. Числовой ряд разрешается отдельно: он
# зависит от режима подачи данных, а остальные — нет.
_BLOCK_ROLE: dict[BlockKind, SlotRole] = {
    BlockKind.PARAGRAPH: SlotRole.BODY,
    BlockKind.BULLETS: SlotRole.BULLETS,
    BlockKind.STEPS: SlotRole.BULLETS,
    BlockKind.KPI: SlotRole.KPI_VALUE,
    BlockKind.TABLE: SlotRole.TABLE,
    BlockKind.QUOTE: SlotRole.QUOTE,
    BlockKind.IMAGE: SlotRole.IMAGE,
}

# Плотный вариант режет слайд надвое, воздушный просит сократить текст: у
# первого слайдов и так много, у второго они просторные и лишний слайд
# ломает ритм. Середина делит.
_OVERFLOW_BY_BIAS: dict[str, OverflowAction] = {
    "smaller": OverflowAction.SPLIT,
    "mid": OverflowAction.SPLIT,
    "larger": OverflowAction.SHORTEN,
}

# Сдвиг стартового кегля по ступеням шкалы относительно того, которым
# шаблон набирает этот слот.
_SCALE_SHIFT: dict[str, int] = {"smaller": -1, "mid": 0, "larger": 1}


def scale_ladder(spec: TemplateSpec) -> list[float]:
    """Ступени, по которым фиттеру разрешено ходить.

    Это типографическая шкала шаблона плюс кегли, которыми шаблон набирает
    свои слоты. Объединение нужно потому, что шкала добывается по статистике
    употребления на слайдах, а кегль слота бывает объявлен только в мастере:
    у чужого шаблона заголовок набран 44 pt, которых в шкале нет вовсе. Без
    объединения шагать было бы не от чего — стартовое значение не лежало бы
    на лестнице.

    Ничего постороннего сюда не попадает: оба источника — сам шаблон.
    """
    sizes = set(spec.type_scale_pt)
    for layout in spec.layouts:
        for slot in layout.slots:
            if slot.style is not None:
                sizes.add(slot.style.size_pt)
    for pattern in spec.content_patterns:
        for slot in pattern.slots:
            if slot.style is not None:
                sizes.add(slot.style.size_pt)
    return sorted(size for size in sizes if size > 0) or [18.0]


def _declared_sizes(spec: TemplateSpec, role: SlotRole) -> list[float]:
    """Кегли, которыми сам шаблон набирает слоты этой роли."""
    return [
        slot.style.size_pt
        for source in (
            (slot for layout in spec.layouts for slot in layout.slots),
            (slot for pattern in spec.content_patterns for slot in pattern.slots),
            (
                slot
                for pattern in spec.content_patterns
                for repeater in pattern.repeaters
                for slot in repeater.item_slots
            ),
        )
        for slot in source
        if slot.role is role and slot.style is not None and slot.style.size_pt > 0
    ]


def role_typical(spec: TemplateSpec, role: SlotRole) -> float:
    """Кегль, которым шаблон обычно набирает эту роль: медиана объявленных.

    Предел роли (`role_floor`) отвечает на «мельче нельзя», а не на «так
    читают». Текст, влезший только на самой нижней ступени, формально
    помещается, а на слайде это подпись в 7 pt: так на `vk_tech` пять пунктов
    повестки уезжали в верхнюю микроподпись. Композиция, где текст влезает
    типичным кеглем, предпочитается той, где он влезает только минимальным.
    """
    sizes = sorted(_declared_sizes(spec, role))
    if not sizes and role in _BODY_LIKE:
        # Своих слотов у роли нет — ни в одном из трёх шаблонов датасета нет
        # слота «список». Такой текст набирается как тело, и читаться обязан
        # так же: без этого пять пунктов повестки уезжали в подпись 7 pt.
        sizes = sorted(_declared_sizes(spec, SlotRole.BODY))
    return sizes[len(sizes) // 2] if sizes else 0.0


_BODY_LIKE = frozenset({SlotRole.BULLETS, SlotRole.CAPTION, SlotRole.QUOTE})


# Высота слайда, для которой заданы минимальные кегли конфига: 7.5″ —
# стандартный 16:9 в PowerPoint. Для слайда другой высоты те же пункты
# занимают другую долю листа, поэтому предел пересчитывается.
_REFERENCE_SLIDE_H_EMU = 6_858_000

# {роль: минимальный читаемый кегль, pt}. Заполняется из `type.min_size_pt`
# при загрузке конфига — тем же приёмом, что и предел параллельных
# конвертаций, чтобы интерфейс, CLI и тесты жили по одним числам.
_MIN_SIZE_PT: dict[SlotRole, float] = {}


def configure_min_sizes(sizes: dict[str, float]) -> None:
    """Задаёт нижние границы читаемости по ролям. Пустой словарь — снять."""
    _MIN_SIZE_PT.clear()
    for name, size in (sizes or {}).items():
        _MIN_SIZE_PT[SlotRole(name)] = float(size)


def readable_floor(spec: TemplateSpec, role: SlotRole) -> float:
    """Нижняя граница читаемости для роли — с оглядкой на сам шаблон.

    Это **требование к результату, а не запрет фиттеру**. Проба показала,
    почему: с жёсткой границей `vk_tech` переставал брать композицию из
    двух карточек — текст в неё крупным кеглем не влезал, — и уходил в
    композицию со схемой, где надпись ложилась поверх круговой графики и
    выезжала за нижний край. Читаемый кегль ценой развалившегося слайда не
    выигрыш. Поэтому граница проверяется аудитом
    (`template.text_too_small`), а вёрстка по-прежнему живёт по шкале
    шаблона.

    Требовать от шаблона кегля, которым он никогда не набирает такие места,
    значит верстать не его: у плотного справочника тело текста бывает 12 pt
    по замыслу. Поэтому берётся меньшее из числа конфига и самого крупного
    кегля, которым шаблон эту роль набирает, — предел поднимается ровно
    настолько, насколько шаблон это позволяет.
    """
    wanted = _MIN_SIZE_PT.get(role)
    declared = _declared_sizes(spec, role)
    if not declared and role in _BODY_LIKE:
        # Роль без своих слотов живёт по телу текста — и по его границе
        # читаемости тоже: иначе пункты повестки, севшие в место подписи,
        # разрешалось бы набрать мельче, чем сам текст.
        declared = _declared_sizes(spec, SlotRole.BODY)
        wanted = _MIN_SIZE_PT.get(SlotRole.BODY, wanted)
    if not wanted or not declared:
        return 0.0
    height = getattr(spec, "slide_height_emu", 0) or _REFERENCE_SLIDE_H_EMU
    wanted *= height / _REFERENCE_SLIDE_H_EMU
    # Мерка шаблона — типичный кегль роли, а не самый крупный: у `vk_tech`
    # заголовки объявлены от 18 до 54 pt, и равняться на 54 значит требовать
    # от подписи кегля, которым шаблон набирает обложку. Требование выше
    # типичного ломает собственную вёрстку шаблона: на пробе оно обрезало
    # текст на пятнадцати элементах из тридцати.
    ordered = sorted(declared)
    typical = ordered[len(ordered) // 2]
    return min(wanted, typical)


def role_floor(spec: TemplateSpec, role: SlotRole) -> float:
    """Ниже какого кегля фиттеру нельзя опускаться для этой роли.

    Это **наименьший кегль, которым сам шаблон набирает слоты такой роли**.
    Шкала целиком для такого предела не годится: она считается по статистике
    всего шаблона и вбирает в себя декор и микроподписи. У `vk_tech` в шкале
    есть ступень 4.14 pt — фиттер, честно спустившийся до неё, выдал бы
    нечитаемый слайд, формально соблюдя закон и нарушив его смысл. Тело
    текста этот же шаблон набирает не мельче 7 pt, заголовок — не мельче
    12.25 pt, и это и есть его собственный ответ на вопрос «насколько мелко
    здесь можно».

    Роли, у которой своих слотов в шаблоне нет, предел достаётся от тела
    текста — как и типичный кегль. Без этого роль без слотов получала предел
    0, и шкала открывалась целиком: у `vk_tech` подпись разрешалось ставить
    ступенью 4.14 pt, которую шкала набрала из декора.
    """
    sizes = _declared_sizes(spec, role)
    if not sizes and role in _BODY_LIKE:
        sizes = _declared_sizes(spec, SlotRole.BODY)
    return min(sizes) if sizes else 0.0


# Насколько мельче собственного кегля композиции разрешено набирать слот.
# Три пятых — та же мерка, по которой выбирается композиция («не мельче 3/5
# типичного»): ниже неё место перестаёт читаться как то, чем оно задумано.
SLOT_FLOOR_SHARE = 0.6


def slot_floor(spec: TemplateSpec, role: SlotRole, declared: float) -> float:
    """Ниже какого кегля нельзя опускать текст конкретного слота.

    `role_floor` отвечает за роль целиком и потому берёт минимум по всему
    шаблону. У `vk_tech` заголовки объявлены от 18 до 54 pt, и заголовок в
    54 pt разрешалось ужать втрое — до микроподписи в 18 pt, формально не
    выходя за предел роли. Иерархия у каждой композиции своя: мелкий
    колонтитул не задаёт минимум заголовку, и предел считается от кегля
    самого слота.

    Предел роли остаётся нижней границей: спускаться ниже того, чем шаблон
    вообще набирает такие слоты, не за чем и по собственному кеглю слота.
    """
    floor = role_floor(spec, role)
    if declared > 0:
        floor = max(floor, SLOT_FLOOR_SHARE * declared)
    return floor


def ladder_for_role(spec: TemplateSpec, role: SlotRole) -> list[float]:
    """Ступени, доступные фиттеру для этой роли: шкала не ниже предела роли."""
    floor = role_floor(spec, role)
    ladder = [size for size in scale_ladder(spec) if size >= floor]
    return ladder or scale_ladder(spec)


def ladder_for_slot(spec: TemplateSpec, role: SlotRole, declared: float) -> list[float]:
    """Ступени, доступные фиттеру для конкретного слота.

    Верх шкалы остаётся общим: крупнее объявленного текст ставить можно —
    это решает стратегия варианта. Режется низ.
    """
    floor = slot_floor(spec, role, declared)
    ladder = [size for size in scale_ladder(spec) if size >= floor]
    return ladder or ladder_for_role(spec, role)


@dataclass(frozen=True)
class Strategy:
    """Решения вёрстки для одного варианта."""

    name: str
    slot_fill_target: float
    type_scale_bias: str
    data_viz_mode: str
    blocks_per_slide: int
    pattern_preference: tuple[PatternClass, ...]

    @classmethod
    def from_config(cls, variant) -> Strategy:
        """Пресет из `configs/variants/*.yaml`.

        Неизвестное имя класса композиции в `pattern_preference` не роняет
        прогон и не подменяется тихо: оно просто не участвует в ранжировании.
        Классы — подсказка, а выбор паттерна идёт по структуре.
        """
        raw = variant.strategy
        known = {item.value for item in PatternClass}
        preference = tuple(
            PatternClass(name) for name in raw.pattern_preference if name in known
        )
        return cls(
            name=variant.name,
            slot_fill_target=raw.slot_fill_target,
            type_scale_bias=raw.type_scale_bias,
            data_viz_mode=raw.data_viz_mode,
            blocks_per_slide=raw.blocks_per_slide,
            pattern_preference=preference,
        )

    @property
    def on_overflow(self) -> OverflowAction:
        return _OVERFLOW_BY_BIAS[self.type_scale_bias]

    def start_size(self, ladder: list[float], slot_size_pt: float) -> float:
        """Кегль, с которого фиттер начинает подбор.

        Отсчёт идёт от того, которым шаблон набирает этот слот, и сдвигается
        на ступень вверх или вниз по его же шкале. Брать позицию в шкале
        напрямую нельзя: верх шкалы занят обложечными размерами, и рабочий
        заголовок по ним выходит втрое мельче настоящего.
        """
        if not ladder:
            return slot_size_pt
        # Ближайшая ступень к тому, что объявил шаблон: слот мог быть набран
        # кеглем, которого в шкале нет.
        index = min(
            range(len(ladder)), key=lambda i: abs(ladder[i] - slot_size_pt)
        )
        shifted = index + _SCALE_SHIFT[self.type_scale_bias]
        return ladder[max(0, min(shifted, len(ladder) - 1))]

    def role_for(self, block) -> SlotRole:
        """Какое место нужно этому блоку плана.

        Числовой ряд — единственное, что зависит от варианта: плотный
        показывает его таблицей, воздушный — крупным числом, сбалансированный
        решает по содержанию.
        """
        if block.kind is not BlockKind.SERIES:
            return _BLOCK_ROLE[block.kind]
        # Один ряд — динамика по месяцам, доли каналов — рисуется графиком в
        # любом варианте: плотный сводил их в таблицы, и в колоде о продажах
        # не было ни одной диаграммы. Таблицей плотный показывает сравнение
        # нескольких рядов.
        if self.data_viz_mode == "table" and len(block.series_ids) <= 1:
            return SlotRole.CHART
        if self.data_viz_mode in _SERIES_ROLE:
            return _SERIES_ROLE[self.data_viz_mode]
        # auto: один ряд рисуется графиком, несколько сравниваются таблицей.
        return SlotRole.CHART if len(block.series_ids) <= 1 else SlotRole.TABLE
