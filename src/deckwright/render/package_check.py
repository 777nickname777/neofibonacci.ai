"""Проверка целостности пакета OOXML.

Зачем отдельная проверка. Клонирование фигур со слайда-донора тащит за собой
ссылки: картинка, диаграмма, гиперссылка живут не в XML слайда, а в его файле
связей, а в XML стоит только `r:embed="rId5"`. Если фигуру скопировать, а связь
не перенести, `rId5` повиснет в пустоте.

Коварство в том, что **LibreOffice это проглатывает молча**: и конвертация в
PDF, и рендер в PNG проходят, картинка просто не появляется. А PowerPoint на
том же файле показывает «обнаружены неполадки, требуется восстановление». То
есть весь наш путь проверки (pptx → pdf → png → аудит по картинке) слеп ровно к
этому классу поломок, и без явной проверки они доедут до жюри.

Проверяется пять вещей:

1. каждый `r:id`/`r:embed`/`r:link`, упомянутый в XML части, объявлен в её
   файле связей;
2. каждая внутренняя связь указывает на часть, которая в пакете есть;
3. каждая часть пакета имеет тип содержимого — иначе PowerPoint её не примет;
4. ни одно имя части не записано в архив дважды;
5. ни один `PartName` не объявлен дважды в `[Content_Types].xml`, и ни один
   `Id` не повторяется внутри одного файла связей.

Последние две появились по живому дефекту. Клонированный с донора график и
наш нативный получали одно имя `/ppt/charts/chart1.xml` (см.
`clone.ensure_unique_partnames`), и в архив уходили две записи. Проверка
начиналась с `set(zf.namelist())` — множество схлопывало дубли, и файл, на
котором LibreOffice отвечает «source file could not be loaded», объявлялся
целым.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

# Атрибуты, которыми части ссылаются на свои связи.
_REL_ATTRS = frozenset(
    f"{{{R_NS}}}{name}" for name in ("id", "embed", "link", "pict", "dm", "lo", "qs", "cs")
)

_CONTENT_TYPES = "[Content_Types].xml"


@dataclass
class IntegrityReport:
    """Результат проверки. Пустой `problems` — файл целостен."""

    problems: list[str] = field(default_factory=list)
    parts_checked: int = 0
    rels_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_broken(self, path: Path) -> None:
        if self.problems:
            listed = "\n  - ".join(self.problems)
            raise PackageIntegrityError(f"{path.name}: пакет повреждён\n  - {listed}")


class PackageIntegrityError(RuntimeError):
    """Пакет откроется в PowerPoint с ошибкой восстановления."""


def _rels_path_for(part: str) -> str:
    head, tail = posixpath.split(part)
    return posixpath.join(head, "_rels", tail + ".rels")


def _read_rels(
    zf: zipfile.ZipFile, part: str
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """({rId: (Target, TargetMode)}, повторённые Id) для части.

    Повторы возвращаются отдельно: словарь их схлопывает, а именно они и
    выдают разъехавшийся граф частей.
    """
    rels_path = _rels_path_for(part)
    try:
        raw = zf.read(rels_path)
    except KeyError:
        return {}, set()
    root = etree.fromstring(raw)
    rels: dict[str, tuple[str, str]] = {}
    duplicates: set[str] = set()
    for rel in root.findall(f"{{{RELS_NS}}}Relationship"):
        rid = rel.get("Id")
        if rid in rels:
            duplicates.add(rid)
        rels[rid] = (rel.get("Target", ""), rel.get("TargetMode", "Internal"))
    return rels, duplicates


def _referenced_ids(raw: bytes) -> set[str]:
    """rId, упомянутые в XML части."""
    root = etree.fromstring(raw)
    found: set[str] = set()
    for element in root.iter():
        for attr, value in element.attrib.items():
            if attr in _REL_ATTRS and value:
                found.add(value)
    return found


def _content_type_index(
    zf: zipfile.ZipFile,
) -> tuple[set[str], set[str], list[str]]:
    """(расширения Default, части Override, Override списком) из [Content_Types].xml.

    Список нужен, чтобы увидеть повтор: множество его прячет, а повторённый
    Override — признак того, что две разные части назвались одинаково.
    """
    try:
        root = etree.fromstring(zf.read(_CONTENT_TYPES))
    except KeyError:
        return set(), set(), []
    defaults = {
        (d.get("Extension") or "").lower() for d in root.findall(f"{{{CT_NS}}}Default")
    }
    override_list = [
        (o.get("PartName") or "").lstrip("/") for o in root.findall(f"{{{CT_NS}}}Override")
    ]
    return defaults, set(override_list), override_list


def check_package(path: str | Path) -> IntegrityReport:
    """Проверяет .pptx на висящие ссылки и части без типа содержимого."""
    path = Path(path)
    report = IntegrityReport()

    with zipfile.ZipFile(path) as zf:
        listed = zf.namelist()
        names = set(listed)
        # Одно имя дважды в архиве. Раньше эта проверка начиналась с
        # `set(namelist())`, и множество молча схлопывало дубли — файл
        # объявлялся целым. LibreOffice на таком отвечает «source file could
        # not be loaded», PowerPoint требует восстановления, а мы отдавали
        # его как готовый результат. Воспроизведено на `zelenie_investicii`,
        # вариант balanced: `ppt/charts/chart1.xml` записан дважды.
        if len(listed) != len(names):
            seen: set[str] = set()
            twice = sorted({n for n in listed if n in seen or seen.add(n)})
            report.problems.append(
                "части записаны в архив дважды: " + ", ".join(twice)
            )
        defaults, overrides, override_list = _content_type_index(zf)
        if _CONTENT_TYPES not in names:
            report.problems.append(f"нет {_CONTENT_TYPES}")
            return report

        xml_parts = [n for n in sorted(names) if n.endswith(".xml") and "/_rels/" not in n]

        # Один и тот же PartName объявлен типом содержимого дважды. Это
        # вторая примета коллизии имён: два разных объекта части, сохраняясь,
        # пишут по своему Override.
        if len(override_list) != len(set(override_list)):
            seen_ct: set[str] = set()
            twice_ct = sorted(
                {n for n in override_list if n in seen_ct or seen_ct.add(n)}
            )
            report.problems.append(
                f"{_CONTENT_TYPES}: PartName объявлен дважды: " + ", ".join(twice_ct)
            )

        for part in xml_parts:
            report.parts_checked += 1
            rels, duplicate_ids = _read_rels(zf, part)
            report.rels_checked += len(rels)

            # Повторяющийся Id внутри одного файла связей: какая из двух
            # целей сработает, зависит от читателя, и это уже не документ.
            if duplicate_ids:
                report.problems.append(
                    f"{_rels_path_for(part)}: Id объявлен дважды: "
                    + ", ".join(sorted(duplicate_ids))
                )

            # 1. Ссылки из XML обязаны быть объявлены в связях этой части.
            for rid in sorted(_referenced_ids(zf.read(part))):
                if rid not in rels:
                    report.problems.append(
                        f"{part}: ссылка {rid} не объявлена в {_rels_path_for(part)}"
                    )

            # 2. Внутренние связи обязаны указывать на существующие части.
            base = posixpath.dirname(part)
            for rid, (target, mode) in sorted(rels.items()):
                if mode == "External" or not target:
                    continue
                resolved = (
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join(base, target))
                )
                if resolved not in names:
                    report.problems.append(
                        f"{_rels_path_for(part)}: {rid} указывает на {resolved!r}, "
                        "такой части в пакете нет"
                    )

        # 3. Каждая часть пакета обязана иметь тип содержимого.
        for name in sorted(names):
            if name == _CONTENT_TYPES or name.endswith("/"):
                continue
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if name not in overrides and extension not in defaults:
                report.problems.append(f"{name}: нет типа содержимого")

    return report
