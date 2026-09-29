"""Измерение текста по метрикам шрифта.

На autofit PowerPoint полагаться нельзя: он подбирает кегль сам, по своим
правилам, в момент открытия файла — и подбирает его из любых значений, а не из
типографической шкалы шаблона. Слайд, свёрстанный в расчёте на autofit,
открывается разным у разных людей и нарушает шкалу, что справедливо найдёт
проверка «кегль не из шкалы шаблона».

Поэтому текст меряется здесь, до записи в файл, по метрикам того самого
шрифта, который будет его набирать. Шрифт берётся извлечённым из шаблона
(`parse/fonts.py`); если извлечь не удалось, подставляется системный, и это
записывается в манифест — подставленный шрифт шире или уже настоящего, и
рассчитанная вёрстка перестаёт соответствовать увиденному.

Точность. Считается сумма ширин глифов без кернинга и без сложного шейпинга:
для кириллицы и латиницы это даёт погрешность порядка двух-трёх процентов в
меньшую сторону. Погрешность гасится запасом при подгонке, а не игнорируется.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

from deckwright.schemas import EMU_PER_POINT
from deckwright.schemas.common import FRAME_INSET_X_EMU, FRAME_INSET_Y_EMU

# Запас на кернинг и шейпинг, которых упрощённое измерение не видит.
MEASUREMENT_SLACK = 1.03

# Доля кегля, уходящая на межстрочный интервал по умолчанию.
DEFAULT_LINE_HEIGHT = 1.2

# Внутренние поля текстового фрейма (`FRAME_INSET_*_EMU`) живут в
# `schemas.common`: рамке их объявляет разбор шаблона, мерит по ним фиттер. Не
# учитывать их — значит систематически считать, что в бокс влезает больше.

# Высота последней строки в долях кегля: межстрочный интервал ей не нужен,
# только сам шрифт (ascent + descent у Arial и Liberation Sans ≈ 1.15). Рамка,
# которую шаблон набрал ровно в одну строку, иначе не вмещала ни одной.
LAST_LINE_HEIGHT = 1.15


def frame_inset_x(box) -> int:
    """Поля рамки по горизонтали: объявленные ею или умолчания PowerPoint."""
    inset = getattr(box, "inset_x", None)
    return FRAME_INSET_X_EMU if inset is None else inset


def frame_inset_y(box) -> int:
    """Поля рамки по вертикали: объявленные ею или умолчания PowerPoint."""
    inset = getattr(box, "inset_y", None)
    return FRAME_INSET_Y_EMU if inset is None else inset

# Ширина глифа, которого в шрифте нет, — половина кегля. Грубо, но лучше, чем
# считать такой символ нулевым.
FALLBACK_ADVANCE_RATIO = 0.5


class FontUnavailable(RuntimeError):
    """Шрифт для измерения не найден."""


@dataclass(frozen=True)
class FontMetrics:
    """Метрики одного начертания, достаточные для измерения строки.

    `width_scale` — поправка на то, что мерим не тем, чем будут рисовать.
    Метрически совместимый клон её не требует (у него те же ширины), а вот
    произвольная подстановка требует: замер на живой русской строке даёт
    расхождение до 13 % (DejaVu Sans против Liberation Sans). Запас входит
    в сами метрики, чтобы ни один из вызывающих не мог про него забыть.
    """

    family: str
    units_per_em: int
    advances: dict[int, int]
    path: str
    width_scale: float = 1.0

    def advance(self, char: str) -> int:
        return self.advances.get(
            ord(char), round(self.units_per_em * FALLBACK_ADVANCE_RATIO)
        )

    def width_pt(self, text: str, size_pt: float) -> float:
        """Ширина строки в пунктах при заданном кегле."""
        if not text:
            return 0.0
        total = sum(self.advance(char) for char in text)
        return total / self.units_per_em * size_pt * self.width_scale

    def width_emu(self, text: str, size_pt: float) -> int:
        return round(self.width_pt(text, size_pt) * EMU_PER_POINT)


@lru_cache(maxsize=32)
def load_metrics(path: str) -> FontMetrics:
    """Читает метрики шрифта. Результат кэшируется: файл один на весь прогон."""
    font_path = Path(path)
    if not font_path.exists():
        raise FontUnavailable(f"шрифт не найден: {path}")
    try:
        # У коллекции (`.ttc`) шрифтов несколько, и без номера fontTools
        # отказывается читать её вовсе: так терялись системные шрифты
        # macOS, найденные по имени семейства.
        font = TTFont(str(font_path), lazy=True, fontNumber=0)
        cmap = font.getBestCmap()
        hmtx = font["hmtx"]
        units = font["head"].unitsPerEm
        advances = {
            code: hmtx[glyph][0] for code, glyph in cmap.items() if glyph in hmtx.metrics
        }
        family = str(
            next(
                (record for record in font["name"].names if record.nameID == 1),
                "",
            )
        )
    except Exception as exc:  # повреждённый шрифт — не повод ронять прогон
        raise FontUnavailable(f"{path}: метрики не читаются ({exc})") from exc
    return FontMetrics(family=family, units_per_em=units, advances=advances, path=path)


def wrap(text: str, metrics: FontMetrics, size_pt: float, width_emu: int) -> list[str]:
    """Разбивает строку по словам так, как её перенесёт PowerPoint.

    Слово длиннее строки не режется: PowerPoint его тоже не режет, а
    выпускает за край. Пусть проверка «текст не поместился» это и увидит.
    """
    limit = width_emu / MEASUREMENT_SLACK
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and metrics.width_emu(candidate, size_pt) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def measure_height_emu(
    text: str,
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
    inset_x_emu: int = FRAME_INSET_X_EMU,
) -> int:
    """Высота, которую текст займёт в боксе такой ширины."""
    usable = max(1, width_emu - inset_x_emu)
    lines = wrap(text, metrics, size_pt, usable)
    return round(len(lines) * size_pt * line_height * EMU_PER_POINT)


def fits(
    text: str,
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    height_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> bool:
    usable_height = max(1, height_emu - FRAME_INSET_Y_EMU)
    return measure_height_emu(text, metrics, size_pt, width_emu, line_height) <= usable_height


def characters_that_fit(
    metrics: FontMetrics,
    size_pt: float,
    width_emu: int,
    height_emu: int,
    line_height: float = DEFAULT_LINE_HEIGHT,
) -> int:
    """Сколько примерно символов помещается в бокс.

    Нужно планировщику: дешевле сразу написать текст нужной длины, чем потом
    ужимать его фиттером — ужимание либо мельчит кегль, либо зовёт модель ещё
    раз, и то и другое дороже.

    Считается по средней ширине символа этого шрифта, а не по абстрактной:
    у узкой гарнитуры в ту же строку влезает заметно больше.
    """
    if not metrics.advances:
        return 0
    average = sum(metrics.advances.values()) / len(metrics.advances)
    char_emu = average / metrics.units_per_em * size_pt * EMU_PER_POINT
    if char_emu <= 0:
        return 0

    usable_width = max(1, width_emu - FRAME_INSET_X_EMU)
    usable_height = max(1, height_emu - FRAME_INSET_Y_EMU)
    line_emu = size_pt * line_height * EMU_PER_POINT
    lines = max(1, int(usable_height // line_emu))
    per_line = max(1, int(usable_width / char_emu / MEASUREMENT_SLACK))
    return lines * per_line


# LOCAL-COMPAT (диагностика на macOS, добавлено вне бизнес-логики):
# исходно здесь были только каталоги Linux, и на macOS поиск шрифтов давал
# None при сотнях установленных .ttf — см. отчёт, причина №1.
FALLBACK_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    str(Path.home() / "Library" / "Fonts"),
)

# Метрически совместимые замены: те же ширины глифов, что у оригинала, при
# другом рисунке. Подставленный такой клон не сдвигает вёрстку — строки
# переносятся там же, где у человека с оригинальным шрифтом.
#
# Это не «похожие» шрифты. Carlito сделан как метрическая замена Calibri,
# Caladea — Cambria, семейство Liberation — Arial, Times New Roman и Courier
# New. Подставлять вместо Calibri, скажем, DejaVu Sans нельзя: он заметно
# шире, и расчёт длины разойдётся с действительностью на десятки процентов.
METRIC_CLONES: dict[str, tuple[str, ...]] = {
    "calibri": ("Carlito-Regular.ttf",),
    "cambria": ("Caladea-Regular.ttf",),
    "arial": ("LiberationSans-Regular.ttf",),
    "helvetica": ("LiberationSans-Regular.ttf",),
    "times new roman": ("LiberationSerif-Regular.ttf",),
    "times": ("LiberationSerif-Regular.ttf",),
    "courier new": ("LiberationMono-Regular.ttf",),
    "courier": ("LiberationMono-Regular.ttf",),
}

# Чем подставлять, когда метрического клона для гарнитуры не существует.
GENERIC_FALLBACKS = (
    "DejaVuSans.ttf",
    "LiberationSans-Regular.ttf",
    "FreeSans.ttf",
)


@dataclass(frozen=True)
class FontFile:
    """Один найденный в системе файл шрифта и то, что о нём известно."""

    path: str
    family: str
    bold: bool
    italic: bool
    cyrillic: bool


def normalized_family(name: str) -> str:
    """Имя семейства без регистра, пробелов и знаков.

    «Times New Roman», «TimesNewRoman» и «times-new-roman» — одно семейство;
    файл при этом может называться как угодно, и по имени файла его не найти.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


# Кириллица: если этих букв в шрифте нет, русский текст им не наберёшь, как
# бы точно ни совпало имя семейства.
_CYRILLIC_PROBE = (0x0410, 0x0430, 0x042F, 0x044F)
_FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
_index: list[FontFile] | None = None


def _describe(path: Path) -> FontFile | None:
    """Семейство, начертание и покрытие кириллицы одного файла шрифта.

    Жалобы fontTools на мелкие несоответствия в системных шрифтах («1 extra
    byte in post.stringData array») к делу не относятся и в вывод прогона не
    идут: шрифт при этом читается.
    """
    logging.getLogger("fontTools").setLevel(logging.ERROR)
    try:
        font = TTFont(str(path), lazy=True, fontNumber=0)
    except Exception:  # не шрифт, повреждён, неподдержанный формат
        return None
    try:
        names = {}
        for record in font["name"].names:
            if record.nameID in (1, 2, 16, 17):
                try:
                    names.setdefault(record.nameID, str(record))
                except Exception:
                    continue
        family = (names.get(16) or names.get(1) or "").strip()
        style = (names.get(17) or names.get(2) or "").strip().lower()
        if not family:
            return None
        try:
            selection = font["OS/2"].fsSelection
            bold = bool(selection & 0x20)
            italic = bool(selection & 0x01)
        except Exception:
            bold, italic = "bold" in style, ("italic" in style or "oblique" in style)
        try:
            cmap = font.getBestCmap()
        except Exception:
            cmap = {}
        cyrillic = all(code in cmap for code in _CYRILLIC_PROBE)
        return FontFile(str(path), family, bold, italic, cyrillic)
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            font.close()


def font_index(refresh: bool = False) -> list[FontFile]:
    """Все шрифты известных каталогов, разобранные по семейству и начертанию.

    Порядок каталогов определён `FALLBACK_FONT_DIRS` и воспроизводим: при
    двух одинаково подходящих файлах побеждает тот, чей каталог раньше.
    """
    global _index
    if _index is not None and not refresh:
        return _index
    found: list[FontFile] = []
    seen: set[str] = set()
    for directory in FALLBACK_FONT_DIRS:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in _FONT_SUFFIXES or str(path) in seen:
                continue
            seen.add(str(path))
            entry = _describe(path)
            if entry is not None:
                found.append(entry)
    _index = found
    return _index


def find_family(
    family: str, bold: bool = False, italic: bool = False, cyrillic: bool = True
) -> FontFile | None:
    """Установленный в системе файл запрошенной гарнитуры и начертания.

    Раньше поиск шёл по именам файлов из заранее заданного списка, и шрифт,
    стоящий в системе под своим именем, оставался ненайденным: `Arial` на
    macOS лежит в `Supplemental/Arial.ttf`, а искали `LiberationSans-Regular.ttf`.

    Уступки делаются по одной и в определённом порядке: сначала точное
    совпадение начертания, потом любое начертание семейства. Покрытие
    кириллицы не уступается: шрифт без неё для русского текста бесполезен,
    и мерить им — обманывать себя.
    """
    wanted = normalized_family(family)
    if not wanted:
        return None
    matches = [item for item in font_index() if normalized_family(item.family) == wanted]
    if cyrillic:
        matches = [item for item in matches if item.cyrillic]
    if not matches:
        return None
    exact = [item for item in matches if item.bold == bold and item.italic == italic]
    plain = [item for item in matches if not item.bold and not item.italic]
    return (exact or plain or matches)[0]


def _find_font_file(names: tuple[str, ...]) -> str | None:
    for directory in FALLBACK_FONT_DIRS:
        root = Path(directory)
        if not root.is_dir():
            continue
        for name in names:
            found = next(root.rglob(name), None)
            if found is not None:
                return str(found)
    return None


def _system_font() -> str | None:
    found = _find_font_file(GENERIC_FALLBACKS)
    if found is not None:
        return found
    for directory in FALLBACK_FONT_DIRS:
        root = Path(directory)
        if root.is_dir():
            any_ttf = next(root.rglob("*.ttf"), None)
            if any_ttf is not None:
                return str(any_ttf)
    return None


@dataclass(frozen=True)
class MeasurementSource:
    """Чем меряли текст и насколько этому можно верить."""

    metrics: FontMetrics | None
    requested: str
    used: str
    # Совместим ли подставленный шрифт метрически с запрошенным. Только от
    # этого зависит, нужен ли запас на расхождение ширин.
    metric_compatible: bool
    description: str

    @property
    def substituted(self) -> bool:
        return self.metrics is not None and self.used != self.requested


def metrics_for_spec(spec) -> MeasurementSource:
    """Чем мерить текст этого шаблона.

    Порядок предпочтений: шрифт, извлечённый из шаблона (единственный, что
    даёт настоящие ширины); метрически совместимый клон (даёт те же ширины
    при другом рисунке); что-нибудь ещё (ширины свои, и на это нужен запас).

    Встроенный шрифт берётся только тогда, когда он и есть основная гарнитура
    шаблона. В `vk_education` текст набран Arial, а Play лежит в файле для
    отдельных фигур донора: мерить Play, а писать Arial — разойтись с
    действительностью на ширинах глифов, и PDF рисуется не тем, чем мерено.
    Пока встроенные шрифты не извлекались вовсе, расхождение было незаметно.
    """
    wanted = spec.fonts[0].family if spec.fonts else ""
    for token in spec.fonts:
        if not (token.embedded and token.file_path):
            continue
        if wanted and token.family.strip().lower() != wanted.strip().lower():
            continue
        try:
            metrics = load_metrics(token.file_path)
        except FontUnavailable:
            continue
        return MeasurementSource(
            metrics=metrics,
            requested=token.family,
            used=token.family,
            metric_compatible=True,
            description=f"шрифт шаблона {token.family}",
        )

    installed = find_family(wanted) if wanted else None
    if installed is not None:
        try:
            metrics = load_metrics(installed.path)
        except FontUnavailable:
            metrics = None
        if metrics is not None:
            return MeasurementSource(
                metrics=metrics,
                requested=wanted,
                used=installed.family,
                metric_compatible=True,
                description=f"шрифт {installed.family} найден в системе",
            )

    clone_names = METRIC_CLONES.get(wanted.strip().lower())
    if clone_names:
        path = _find_font_file(clone_names)
        if path is not None:
            try:
                metrics = load_metrics(path)
            except FontUnavailable:
                metrics = None
            if metrics is not None:
                return MeasurementSource(
                    metrics=metrics,
                    requested=wanted,
                    used=metrics.family,
                    metric_compatible=True,
                    description=(
                        f"метрический клон {metrics.family} вместо {wanted}: "
                        "ширины совпадают"
                    ),
                )

    path = _system_font()
    if path is None:
        return MeasurementSource(
            metrics=None,
            requested=wanted,
            used="",
            metric_compatible=False,
            description="шрифтов нет вовсе: измерить текст нечем",
        )
    try:
        metrics = load_metrics(path)
    except FontUnavailable as exc:
        return MeasurementSource(None, wanted, "", False, str(exc))
    # Системный шрифт может оказаться ровно тем, что просил шаблон: DejaVu
    # Sans стоит в образе и встречается в шаблонах. Это не подстановка, и
    # запас на неё требовать не за что.
    if wanted and metrics.family.strip().lower() == wanted.strip().lower():
        return MeasurementSource(
            metrics=metrics,
            requested=wanted,
            used=metrics.family,
            metric_compatible=True,
            description=f"системный шрифт {metrics.family}, он же шрифт шаблона",
        )
    return MeasurementSource(
        metrics=metrics,
        requested=wanted or "?",
        used=metrics.family,
        metric_compatible=False,
        description=(
            f"подстановка {metrics.family} вместо {wanted or 'неизвестной гарнитуры'}: "
            "ширины не совпадают, нужен запас"
        ),
    )


# Метрически совместимые замены по имени семейства, а не файла: начертание
# ищется в системе тем же поиском, что и оригинал, и Carlito Bold находится
# там, где список файлов знал только Carlito-Regular.
_CLONE_FAMILY: dict[str, str] = {
    "calibri": "Carlito",
    "cambria": "Caladea",
    "arial": "Liberation Sans",
    "helvetica": "Liberation Sans",
    "times new roman": "Liberation Serif",
    "times": "Liberation Serif",
    "courier new": "Liberation Mono",
    "courier": "Liberation Mono",
}

# Слова начертания в имени семейства: «Calibri Light» — это Calibri, набранный
# светлым. Клона у «Calibri Light» не существует, а у «Calibri» есть, и он
# несравнимо ближе, чем первый попавшийся системный шрифт.
_WEIGHT_WORDS = (
    "thin", "extralight", "ultralight", "light", "semilight", "book", "regular",
    "medium", "semibold", "demibold", "bold", "extrabold", "ultrabold", "black",
    "heavy", "italic", "oblique",
)


def base_family(name: str) -> str:
    """Имя семейства без слов начертания: «Calibri Light» → «Calibri»."""
    words = [w for w in name.replace("-", " ").split() if w.lower() not in _WEIGHT_WORDS]
    return " ".join(words) or name


# Гарнитуры, у которых равная ширина знака — часть смысла: ими набирают код
# и таблицы цифр. Подставлять вместо них пропорциональный шрифт — менять не
# только ширины, но и вид: колонки перестают стоять колонками.
_MONOSPACE_HINTS = ("mono", "consolas", "courier", "code", "menlo", "consola")

# Чем заменять моноширинную гарнитуру, для которой клона не нашлось.
MONOSPACE_FALLBACKS = (
    "LiberationMono-Regular.ttf",
    "DejaVuSansMono.ttf",
    "Menlo.ttc",
)


def looks_monospace(family: str) -> bool:
    """Похоже ли имя семейства на моноширинное."""
    lowered = normalized_family(family)
    return any(hint in lowered for hint in _MONOSPACE_HINTS)


def _clone_for(family: str, bold: bool, italic: bool) -> FontFile | None:
    """Метрически совместимая замена нужного начертания, если она известна."""
    for candidate in (family, base_family(family)):
        clone = _CLONE_FAMILY.get(candidate.strip().lower())
        if not clone:
            continue
        found = find_family(clone, bold=bold, italic=italic)
        if found is not None:
            return found
    return None


def _embedded(spec, family: str, bold: bool, italic: bool) -> FontMetrics | None:
    """Шрифт этого семейства и начертания, извлечённый из самого шаблона."""
    wanted = normalized_family(family)
    for token in getattr(spec, "fonts", []) or []:
        if not (token.embedded and token.file_path):
            continue
        if normalized_family(token.family) != wanted:
            continue
        if bool(getattr(token, "bold", False)) != bold:
            continue
        if bool(getattr(token, "italic", False)) != italic:
            continue
        try:
            return load_metrics(token.file_path)
        except FontUnavailable:
            continue
    return None


def metrics_for_style(
    spec,
    family: str,
    bold: bool = False,
    italic: bool = False,
    substitution_slack: float = 1.0,
) -> MeasurementSource:
    """Чем мерить текст именно этого семейства и начертания.

    Порядок тот же, что у гарнитуры колоды: шрифт из самого шаблона, затем
    установленный в системе того же начертания, затем метрический клон, и
    только потом что-нибудь ещё — с запасом на расхождение ширин.

    Начертание — не украшение: полужирный набор той же строки шире обычного
    на 7-8 %, и мерить его обычным значит систематически обещать, что текст
    влезет.
    """
    requested = family or ""
    embedded = _embedded(spec, requested, bold, italic) if requested else None
    if embedded is not None:
        return MeasurementSource(
            metrics=embedded,
            requested=requested,
            used=requested,
            metric_compatible=True,
            description=f"шрифт шаблона {requested}",
        )

    installed = find_family(requested, bold=bold, italic=italic) if requested else None
    if installed is not None:
        try:
            metrics = load_metrics(installed.path)
        except FontUnavailable:
            metrics = None
        if metrics is not None:
            exact = installed.bold == bold and installed.italic == italic
            return MeasurementSource(
                metrics=metrics,
                requested=requested,
                used=installed.family,
                metric_compatible=exact,
                description=(
                    f"шрифт {installed.family} найден в системе"
                    if exact
                    else f"{installed.family}: нужного начертания в системе нет"
                ),
            )

    clone = _clone_for(requested, bold, italic) if requested else None
    if clone is not None:
        try:
            metrics = load_metrics(clone.path)
        except FontUnavailable:
            metrics = None
        if metrics is not None:
            exact = clone.bold == bold and clone.italic == italic
            # Клон совпадает по ширинам с тем начертанием, которое он
            # повторяет. Нашлось не то начертание — совпадение кончилось.
            same_family = base_family(requested).strip().lower() in _CLONE_FAMILY
            return MeasurementSource(
                metrics=metrics,
                requested=requested,
                used=metrics.family,
                metric_compatible=exact and same_family,
                description=(
                    f"метрический клон {metrics.family} вместо {requested}"
                    + ("" if exact else ", начертание другое")
                ),
            )

    # Моноширинную заменяем моноширинной: иначе таблица цифр, набранная
    # Consolas, меряется пропорциональным шрифтом и в колонки не встаёт.
    path = (
        _find_font_file(MONOSPACE_FALLBACKS) if looks_monospace(requested) else None
    ) or _system_font()
    if path is None:
        return MeasurementSource(
            metrics=None,
            requested=requested,
            used="",
            metric_compatible=False,
            description="шрифтов нет вовсе: измерить текст нечем",
        )
    try:
        metrics = load_metrics(path)
    except FontUnavailable as exc:
        return MeasurementSource(None, requested, "", False, str(exc))
    same = bool(requested) and normalized_family(metrics.family) == normalized_family(
        requested
    )
    return MeasurementSource(
        metrics=metrics,
        requested=requested or "?",
        used=metrics.family,
        metric_compatible=same and not (bold or italic),
        description=(
            f"системный шрифт {metrics.family}"
            if same
            else f"подстановка {metrics.family} вместо {requested or 'неизвестной гарнитуры'}"
        ),
    )


def with_slack(source: MeasurementSource, substitution_slack: float) -> MeasurementSource:
    """Тот же источник, но с запасом на чужие ширины, если он нужен.

    Запас применяется ровно один раз и только к несовместимой подстановке:
    метрический клон его не получает, потому что переносит строки там же,
    где оригинал.
    """
    if source.metrics is None or source.metric_compatible:
        return source
    if not 0 < substitution_slack < 1:
        return source
    return replace(
        source,
        metrics=replace(source.metrics, width_scale=1.0 / substitution_slack),
        description=f"{source.description}; запас на ширины {substitution_slack:g}",
    )


class Fonts:
    """Метрики по гарнитуре и начертанию — один резолвер на разбор шаблона.

    Раньше метрики выбирались один раз на всю колоду по `spec.fonts[0]`, и
    любой текст мерился ими: заголовок, набранный заголовочной гарнитурой
    темы, — основной; полужирный — обычным начертанием. Здесь каждая пара
    «семейство + начертание» разрешается отдельно и кэшируется, а запас на
    несовместимую подстановку применяется ровно один раз, внутри метрик.
    """

    def __init__(self, spec, substitution_slack: float = 1.0):
        self._spec = spec
        self._slack = substitution_slack
        self._cache: dict[tuple[str, bool, bool], MeasurementSource] = {}
        fonts = getattr(spec, "fonts", None) or []
        self.primary = fonts[0].family if fonts else ""

    def source(
        self, family: str = "", bold: bool = False, italic: bool = False
    ) -> MeasurementSource:
        wanted = family or self.primary
        key = (normalized_family(wanted), bool(bold), bool(italic))
        if key not in self._cache:
            self._cache[key] = with_slack(
                metrics_for_style(self._spec, wanted, bold=bold, italic=italic),
                self._slack,
            )
        return self._cache[key]

    def metrics(
        self, family: str = "", bold: bool = False, italic: bool = False
    ) -> FontMetrics | None:
        return self.source(family, bold, italic).metrics

    def for_style(self, style) -> FontMetrics | None:
        """Метрики того начертания, которым текст и будет написан."""
        if style is None:
            return self.metrics()
        return self.metrics(
            getattr(style, "font_family", "") or "",
            bool(getattr(style, "bold", False)),
            bool(getattr(style, "italic", False)),
        )

    def for_slot(self, slot, bold: bool | None = None) -> FontMetrics | None:
        """Метрики места шаблона; `bold` перебивает объявленное начертание."""
        style = getattr(slot, "style", None) if slot is not None else None
        family = getattr(style, "font_family", "") or ""
        weight = bool(getattr(style, "bold", False)) if bold is None else bold
        italic = bool(getattr(style, "italic", False))
        return self.metrics(family, weight, italic)

    def has_face(self, family: str = "", bold: bool = False, italic: bool = False) -> bool:
        """Есть ли у этого семейства нужное начертание отдельным шрифтом.

        Нет — значит просить его у рендера бессмысленно: он нарисует
        синтетический жир или подставит чужой шрифт, а мерили мы обычное
        начертание. `Arial Black` — ровно этот случай: вес уже в самом
        семействе, отдельного полужирного у него нет, и просьба о нём
        заменяла шрифт целиком (в PDF появлялся LinuxLibertine).
        """
        if not (bold or italic):
            return True
        wanted = family or self.primary
        if not wanted:
            return True
        found = find_family(wanted, bold=bold, italic=italic)
        if found is not None:
            return found.bold == bold and found.italic == italic
        clone = _clone_for(wanted, bold, italic)
        if clone is not None:
            return clone.bold == bold and clone.italic == italic
        return False

    @property
    def usable(self) -> bool:
        return self.metrics() is not None

    def report(self) -> list[str]:
        """Чем мерили, по одной строке на разрешённое начертание."""
        lines = []
        for (_family, bold, italic), source in sorted(self._cache.items()):
            mark = "".join(x for x, on in (("ж", bold), ("к", italic)) if on)
            lines.append(
                f"{source.requested}{' ' + mark if mark else ''} → {source.description}"
            )
        return lines


def deck_fonts(deck) -> list[tuple[str, bool, bool]]:
    """Все пары «гарнитура + начертание», которыми набрана колода."""
    found: set[tuple[str, bool, bool]] = set()

    def remember(style) -> None:
        if style is None:
            return
        family = getattr(style, "font_family", "") or ""
        if family:
            found.add((family, bool(style.bold), bool(style.italic)))

    for slide in getattr(deck, "slides", []) or []:
        for element in slide.all_elements():
            if element.text is not None:
                for paragraph in element.text.paragraphs:
                    remember(paragraph.style)
            if element.table is not None:
                remember(element.table.cell_style)
                remember(element.table.header_style)
            if element.chart is not None:
                remember(element.chart.label_style)
    return sorted(found)


def deck_substitutions(deck, spec, substitution_slack: float = 1.0) -> list[dict[str, str]]:
    """Чем на самом деле будет написана каждая гарнитура колоды.

    Нужно двоим сразу. Манифесту — чтобы подстановка была названа, а не
    обнаружена потом в PDF. И конвертеру — чтобы он рисовал ровно тем, чем
    мерил фиттер: без этого текст меряется Carlito, а LibreOffice берёт для
    «Calibri» что-нибудь своё, и переносы строк расходятся с расчётом.
    """
    resolver = Fonts(spec, substitution_slack)
    seen: dict[tuple[str, str], dict[str, str]] = {}
    for family, bold, italic in deck_fonts(deck):
        source = resolver.source(family, bold, italic)
        if source.metrics is None:
            continue
        if normalized_family(source.used) == normalized_family(family):
            continue
        seen.setdefault(
            (family, source.used),
            {
                "requested": family,
                "used": source.used,
                "reason": source.description,
                "metric_compatible": "да" if source.metric_compatible else "нет",
            },
        )
    return list(seen.values())
