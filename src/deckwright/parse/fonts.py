"""Извлечение встроенных в шаблон шрифтов.

Шрифты внутри `.pptx` лежат в `ppt/fonts/*.fntdata` и сжаты. Это не TTF:
первые четыре байта — размер файла, дальше заголовок EOT версии 0x00020002 с
флагом сжатия MicroType Express. Сигнатуры `\\x00\\x01\\x00\\x00` в файле нет,
срезом заголовка шрифт не достать.

Зачем это нужно. Шрифта шаблона в системе почти наверняка нет: `Play` из всех
трёх шаблонов датасета отсутствует в базовом образе, и LibreOffice молча
подставляет свой. Тогда расходятся три вещи сразу — PDF, картинки слайдов для
аудита и измерение текста по метрикам, на котором держится детерминированное
разрешение переполнения. Подставленный шрифт шире или уже настоящего, и
рассчитанная вёрстка перестаёт соответствовать тому, что увидит человек.

Распаковка идёт через `libeot` — в дистрибутивах Linux это пакет `libeot0`.
Отдельного исполняемого файла `eot2ttf` в пакетах нет, поэтому вызов идёт
напрямую через `ctypes`. Имя файла библиотеки платформозависимо, и искать её
приходится по списку — см. `libeot_candidates`.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Имя файла разделяемой библиотеки зависит от платформы: в дистрибутивах Linux
# это `libeot.so.0`, в macOS — `libeot.0.dylib`. Одно жёстко вписанное имя
# делало встроенные шрифты недоступными на второй платформе молча: разбор не
# падал, а тихо переходил на подставленный шрифт, и вёрстка считалась по чужим
# ширинам. Порядок проверки: явный путь из окружения, затем то, что нашёл
# линковщик, затем известные имена.
LIBEOT_ENV_VAR = "DECKWRIGHT_LIBEOT"
LIBEOT_SONAMES = ("libeot.so.0", "libeot.so", "libeot.0.dylib", "libeot.dylib")
LIBEOT_SYMBOL = "EOT2ttf_buffer"

_u8, _u16, _u32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32


class _EUDCInfo(ctypes.Structure):
    _fields_ = [
        ("exists", ctypes.c_bool),
        ("codePage", _u32),
        ("flags", _u32),
        ("fontDataSize", _u32),
        ("fontData", ctypes.POINTER(_u8)),
    ]


class _RootString(ctypes.Structure):
    _fields_ = [("rootStringSize", _u16), ("rootString", ctypes.POINTER(_u16))]


class _EOTMetadata(ctypes.Structure):
    """Раскладка `struct EOTMetadata` из заголовков libeot.

    Порядок полей обязан совпадать с библиотекой: ctypes ничего не проверяет,
    и расхождение даёт не ошибку, а мусор в именах и размерах.
    """

    _fields_ = [
        ("totalSize", _u32),
        ("version", ctypes.c_int),
        ("flags", _u32),
        ("panose", _u8 * 10),
        ("charset", ctypes.c_int),
        ("italic", ctypes.c_bool),
        ("weight", _u32),
        ("permissions", _u16),
        ("unicodeRange", _u32 * 4),
        ("codePageRange", _u32 * 2),
        ("checkSumAdjustment", _u32),
        ("familyNameSize", _u16),
        ("familyName", ctypes.POINTER(_u16)),
        ("styleNameSize", _u16),
        ("styleName", ctypes.POINTER(_u16)),
        ("versionNameSize", _u16),
        ("versionName", ctypes.POINTER(_u16)),
        ("fullNameSize", _u16),
        ("fullName", ctypes.POINTER(_u16)),
        ("numRootStrings", ctypes.c_uint),
        ("rootStrings", ctypes.POINTER(_RootString)),
        ("fontDataSize", _u32),
        ("fontDataOffset", ctypes.c_uint),
        ("eudcInfo", _EUDCInfo),
        ("do_not_use_size", _u16),
        ("do_not_use", ctypes.POINTER(_u16)),
    ]


@dataclass(frozen=True)
class ExtractedFont:
    family: str
    style: str
    path: Path

    @property
    def bold(self) -> bool:
        return "bold" in self.style.lower()

    @property
    def italic(self) -> bool:
        return "italic" in self.style.lower() or "oblique" in self.style.lower()


class FontExtractionError(RuntimeError):
    """Встроенный шрифт не распаковался."""


def libeot_candidates() -> tuple[str, ...]:
    """Имена и пути, по которым библиотеку стоит искать, в порядке проверки."""
    names: list[str] = []
    explicit = os.environ.get(LIBEOT_ENV_VAR, "").strip()
    if explicit:
        names.append(explicit)
    found = ctypes.util.find_library("eot")
    if found:
        names.append(found)
    names.extend(LIBEOT_SONAMES)
    return tuple(dict.fromkeys(names))


def load_libeot() -> tuple[ctypes.CDLL, str]:
    """Загруженная библиотека и имя, по которому она нашлась.

    Причины всех неудачных попыток сохраняются в сообщении: «библиотеки нет» и
    «есть, но другой архитектуры» — разные беды с разным лечением, и без текста
    ошибки их не различить.
    """
    reasons: list[str] = []
    for name in libeot_candidates():
        try:
            return ctypes.CDLL(name), name
        except OSError as exc:
            reasons.append(f"{name}: {exc}")
    raise FontExtractionError(
        "libeot недоступна: без неё встроенные шрифты не извлечь, и вёрстка "
        "будет считаться по метрикам подставленного шрифта. Попытки — "
        + "; ".join(reasons)
    )


def _library() -> ctypes.CDLL:
    lib, _ = load_libeot()
    lib.EOT2ttf_buffer.argtypes = [
        ctypes.POINTER(_u8),
        ctypes.c_uint,
        ctypes.POINTER(_EOTMetadata),
        ctypes.POINTER(ctypes.POINTER(_u8)),
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.EOT2ttf_buffer.restype = ctypes.c_int
    lib.EOTfreeBuffer.argtypes = [ctypes.POINTER(_u8)]
    return lib


def _utf16(pointer, size_bytes: int) -> str:
    if not pointer or size_bytes <= 0:
        return ""
    return "".join(chr(pointer[i]) for i in range(size_bytes // 2))


def eot_to_ttf(data: bytes) -> tuple[bytes, str, str]:
    """Распаковывает EOT в TTF. Возвращает (шрифт, семейство, начертание)."""
    lib = _library()
    buffer = (_u8 * len(data)).from_buffer_copy(data)
    metadata = _EOTMetadata()
    out = ctypes.POINTER(_u8)()
    size = ctypes.c_uint()

    code = lib.EOT2ttf_buffer(
        buffer, len(data), ctypes.byref(metadata), ctypes.byref(out), ctypes.byref(size)
    )
    if code != 0 or not size.value:
        raise FontExtractionError(f"libeot вернула код {code}")

    try:
        ttf = bytes(bytearray(out[i] for i in range(size.value)))
    finally:
        lib.EOTfreeBuffer(out)

    return (
        ttf,
        _utf16(metadata.familyName, metadata.familyNameSize),
        _utf16(metadata.styleName, metadata.styleNameSize),
    )


def extract_embedded_fonts(
    template_path: str | Path, target_dir: str | Path
) -> tuple[list[ExtractedFont], list[str]]:
    """Достаёт все встроенные шрифты шаблона. Возвращает (шрифты, предупреждения).

    Неудача по одному шрифту не отменяет остальные: лучше иметь верные метрики
    для трёх начертаний из четырёх, чем ни для одного.
    """
    template_path, target_dir = Path(template_path), Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    fonts: list[ExtractedFont] = []
    warnings: list[str] = []

    with zipfile.ZipFile(template_path) as package:
        entries = [name for name in package.namelist() if name.startswith("ppt/fonts/")]
        for entry in sorted(entries):
            try:
                ttf, family, style = eot_to_ttf(package.read(entry))
            except FontExtractionError as exc:
                warnings.append(f"{entry}: {exc}")
                continue
            if not family:
                warnings.append(f"{entry}: распаковался, но без имени семейства")
                continue
            path = target_dir / f"{family}-{style or 'Regular'}.ttf".replace(" ", "_")
            path.write_bytes(ttf)
            fonts.append(ExtractedFont(family=family, style=style or "Regular", path=path))

    return fonts, warnings
