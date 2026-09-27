"""Клонирование фигур со слайда-донора вместе с их связями.

Шаблон используется как донор: композиция не рисуется заново, а копируется
поддеревом фигур с заменой текста. Копирование XML фигуры — только половина
дела, потому что картинки, диаграммы и гиперссылки в XML не лежат. Там стоит
`r:embed="rId5"`, а сам `rId5` объявлен в файле связей слайда.

Ошибиться здесь можно двумя способами, и второй хуже первого.

**Висящая ссылка.** Связь не перенесли, `rId5` на новом слайде не объявлен.
PowerPoint скажет «требуется восстановление», LibreOffice смолчит. Ловится
`package_check.check_package`.

**Совпавшая ссылка.** `rId5` на новом слайде уже есть, но ведёт на другую
часть. Воспроизведено на `vk_workspace`: `rId4` на тринадцатом слайде указывает
на `image29.png`, а на третьем — на `image17.png`. Файл при этом структурно
безупречен, просто на слайде чужая картинка. Ни одна проверка целостности
такого не заметит.

Поэтому связи здесь **всегда перенумеровываются**: для каждой ссылки донора
ищется или заводится связь на ту же часть в принимающем слайде, и атрибут
переписывается на новый идентификатор. Совпадение идентификаторов перестаёт
что-либо значить, и второй класс ошибок исчезает по построению.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from lxml import etree
from pptx.opc.package import XmlPart
from pptx.presentation import Presentation as PresentationObject
from pptx.slide import Slide

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"

# Атрибуты, которыми фигура ссылается на связь своей части.
REL_ATTRS: tuple[str, ...] = tuple(
    f"{{{R_NS}}}{name}" for name in ("id", "embed", "link", "pict", "dm", "lo", "qs", "cs")
)

_CNVPR_ID = "id"


class CloneError(RuntimeError):
    """Фигуру нельзя перенести, не потеряв её содержимое."""


@dataclass(frozen=True)
class ClonedRef:
    """Перенесённая ссылка: чем была у донора, чем стала у приёмника."""

    donor_rid: str
    new_rid: str
    target: str


def purge_slides(prs: PresentationObject) -> int:
    """Снимает все слайды, оставляя мастера, layout'ы, тему и шрифты.

    Убрать запись из `sldIdLst` недостаточно: сама часть слайда остаётся в
    пакете, и при сохранении `zipfile` пишет дубликаты имён — файл бьётся.
    Проверено: наивный вариант даёт `Duplicate name: ppt/slides/slide1.xml`
    и нечитаемый `.pptx`. Нужен ещё `drop_rel`, который заодно уносит media
    удалённых слайдов: на `vk_workspace` это 13.4 МБ против 9.2 МБ.
    """
    sld_id_lst = prs.slides._sldIdLst
    removed = 0
    for sld_id in list(sld_id_lst):
        prs.part.drop_rel(sld_id.rId)
        sld_id_lst.remove(sld_id)
        removed += 1
    return removed


def _rel_target(part: XmlPart, rid: str) -> tuple[object, str, bool]:
    """(цель, тип отношения, внешняя ли) для связи донора."""
    try:
        rel = part.rels[rid]
    except KeyError as exc:
        raise CloneError(
            f"у донорской части {part.partname} нет связи {rid}: "
            "шаблон повреждён ещё до клонирования"
        ) from exc
    target = rel.target_ref if rel.is_external else rel.target_part
    return target, rel.reltype, rel.is_external


def _next_shape_id(slide: Slide) -> int:
    """Следующий свободный id фигуры на слайде.

    Клонированная фигура приносит `p:cNvPr/@id` донора. Внутри одного слайда
    эти идентификаторы обязаны быть уникальны, иначе PowerPoint ругается на
    повтор — а донорская фигура почти наверняка столкнётся с чем-то из декора
    layout'а или с соседним клоном.
    """
    used = {
        int(value)
        for value in slide.shapes._spTree.xpath(".//*[local-name()='cNvPr']/@id")
        if value.isdigit()
    }
    return max(used, default=1) + 1


def clone_shape(
    donor_element: etree._Element,
    donor_part: XmlPart,
    slide: Slide,
) -> tuple[etree._Element, list[ClonedRef]]:
    """Копирует фигуру донора на слайд, перенося и перенумеровывая её связи.

    Возвращает вставленный элемент и список перенесённых ссылок — по нему
    `verify_clone` убеждается, что каждая ссылка ведёт туда же, куда у донора.
    """
    element = copy.deepcopy(donor_element)
    moved = _relink(element, donor_part, slide.part)

    # Уникальные идентификаторы фигур внутри принимающего слайда.
    next_id = _next_shape_id(slide)
    for cnv_pr in element.xpath(".//*[local-name()='cNvPr']"):
        cnv_pr.set(_CNVPR_ID, str(next_id))
        next_id += 1

    slide.shapes._spTree.append(element)
    return element, moved


def clone_background(donor_bg: etree._Element, donor_part: XmlPart, slide: Slide) -> etree._Element:
    """Переносит собственный фон донора (`p:bg`) на слайд вместе со связями.

    Фон, заданный на слайде, а не в мастере, композицией не приезжает: он
    лежит не в дереве фигур. Без него бланк «Дорожная карта» собирался на
    белом мастере вместо своей заливки F7F9F8, а тёмный финал — на белом.
    """
    element = copy.deepcopy(donor_bg)
    _relink(element, donor_part, slide.part)
    csld = slide._element.find(f"{{{P_NS}}}cSld")
    old = csld.find(f"{{{P_NS}}}bg")
    if old is not None:
        csld.remove(old)
    # `p:bg` — первый ребёнок `p:cSld`, до дерева фигур.
    csld.insert(0, element)
    return element


def _relink(element: etree._Element, donor_part: XmlPart, target_part) -> list[ClonedRef]:
    """Перенумеровывает ссылки перенесённого узла на связи принимающей части."""
    moved: list[ClonedRef] = []
    # Кэш на время одной фигуры: одна и та же картинка в группе не должна
    # порождать несколько связей на одну часть.
    remapped: dict[str, str] = {}

    for node in element.iter():
        for attr in REL_ATTRS:
            donor_rid = node.get(attr)
            if not donor_rid:
                continue
            if donor_rid not in remapped:
                target, reltype, is_external = _rel_target(donor_part, donor_rid)
                remapped[donor_rid] = target_part.relate_to(
                    target, reltype, is_external=is_external
                )
                moved.append(
                    ClonedRef(
                        donor_rid=donor_rid,
                        new_rid=remapped[donor_rid],
                        target=str(target if is_external else target.partname),
                    )
                )
            node.set(attr, remapped[donor_rid])
    return moved


def verify_clone(
    element: etree._Element,
    slide: Slide,
    donor_part: XmlPart,
    donor_element: etree._Element,
) -> list[str]:
    """Сверяет, что каждая ссылка клона ведёт на ту же часть, что у донора.

    Проверка целостности пакета этого не заметит: совпавший по имени, но
    указывающий не туда `rId` даёт структурно корректный файл с чужой
    картинкой. Здесь сравниваются именно цели.
    """
    donor_targets: list[str] = []
    for node in donor_element.iter():
        for attr in REL_ATTRS:
            rid = node.get(attr)
            if rid:
                target, _, is_external = _rel_target(donor_part, rid)
                donor_targets.append(str(target if is_external else target.partname))

    clone_targets: list[str] = []
    for node in element.iter():
        for attr in REL_ATTRS:
            rid = node.get(attr)
            if not rid:
                continue
            rel = slide.part.rels.get(rid)
            if rel is None:
                clone_targets.append(f"<нет связи {rid}>")
                continue
            clone_targets.append(
                str(rel.target_ref if rel.is_external else rel.target_part.partname)
            )

    if donor_targets != clone_targets:
        return [
            "ссылки клона ведут не туда: "
            f"у донора {donor_targets}, у клона {clone_targets}"
        ]
    return []
