"""`Issue` — находка аудита. Выход слоя audit, вход UI и слоя fix.

Каждая находка обязана нести то, что перечислено в критерии A15 спеки:
идентификатор проверки, тип, серьёзность, слайд, bbox, описание и предлагаемое
исправление. `bbox` — то, вокруг чего UI рисует рамку поверх PNG слайда, а
номер находки совпадает с номером рамки.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deckwright.schemas.common import Box


class CheckKind(StrEnum):
    """Детерминированная проверка на одном слайде всегда даёт один ответ.

    Контекстная выполняется моделью и на повторном прогоне может ответить иначе,
    поэтому её результат несёт `confidence` и не используется для автоправки.
    """

    DETERMINISTIC = "deterministic"
    CONTEXTUAL = "contextual"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class IssueCategory(StrEnum):
    """Разделы Приложения 1 ТЗ."""

    LAYOUT = "layout"
    TEMPLATE = "template"
    DENSITY = "density"
    INTEGRITY = "integrity"
    CONTENT = "content"


class FixKind(StrEnum):
    """Как чинится находка.

    `AUTOMATIC` значит, что слой fix умеет применить исправление сам и
    результат предсказуем. `MANUAL` — исправление требует решения человека.
    """

    AUTOMATIC = "automatic"
    ASSISTED = "assisted"
    MANUAL = "manual"
    NONE = "none"


class ProposedFix(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FixKind
    description: str = ""
    # Имя операции слоя fix и её параметры. Пустой словарь допустим только
    # для MANUAL и NONE — автоправке нужно знать, что именно делать.
    action: str = ""
    params: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _automatic_fix_knows_what_to_do(self) -> ProposedFix:
        if self.kind in (FixKind.AUTOMATIC, FixKind.ASSISTED) and not self.action:
            raise ValueError(f"исправление типа {self.kind} без имени операции")
        return self


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Идентификатор проверки, например "layout.out_of_bounds". По нему находка
    # связывается со строкой в docs/AUDIT.md.
    check_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    kind: CheckKind
    category: IssueCategory
    severity: Severity
    slide_index: int = Field(ge=1)
    # Элементы SlideIR, которых касается находка.
    element_ids: list[str] = Field(default_factory=list)
    bbox: Box | None = None
    message: str = Field(min_length=1)
    fix: ProposedFix = Field(default_factory=lambda: ProposedFix(kind=FixKind.NONE))
    # Уверенность модели. Для детерминированной проверки всегда 1.0.
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @property
    def key(self) -> str:
        """Устойчивое имя находки: по нему её выбирает пользователь.

        Идентификатора у находки нет и быть не может: она не хранится между
        прогонами, а вычисляется заново. Ключ собирается из того, что
        воспроизводится при повторном аудите той же колоды, — номера слайда,
        проверки и элемента, которого она касается.
        """
        element = self.element_ids[0] if self.element_ids else ""
        return f"{self.slide_index}:{self.check_id}:{element}"

    @model_validator(mode="after")
    def _deterministic_is_certain(self) -> Issue:
        if self.kind is CheckKind.DETERMINISTIC and self.confidence != 1.0:
            raise ValueError(
                f"{self.check_id}: детерминированная проверка не может быть "
                f"неуверенной (confidence={self.confidence})"
            )
        return self


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variant: str
    issues: list[Issue] = Field(default_factory=list)
    # Проверки, которые не выполнялись, и почему: например, контекстные при
    # отсутствии доступа к модели. Честнее, чем молча вернуть пустой список.
    skipped_checks: dict[str, str] = Field(default_factory=dict)
    # Сколько проверок вообще есть в реестре. Без знаменателя «нарушений не
    # найдено» читается как «всё проверено», хотя половина могла не
    # выполняться: контекстные без модели, растровые без картинок.
    checks_total: int = 0

    @property
    def checks_run(self) -> int:
        """Сколько проверок действительно выполнено."""
        return max(0, self.checks_total - len(self.skipped_checks))

    @property
    def coverage(self) -> str:
        """Покрытие одной строкой — для отчёта, интерфейса и журнала."""
        if not self.checks_total:
            return "покрытие неизвестно"
        line = f"проверок выполнено {self.checks_run} из {self.checks_total}"
        if self.skipped_checks:
            line += f", пропущено {len(self.skipped_checks)}"
        return line

    def for_slide(self, index: int) -> list[Issue]:
        return [i for i in self.issues if i.slide_index == index]

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity is Severity.ERROR)

    @property
    def auto_fixable(self) -> list[Issue]:
        return [i for i in self.issues if i.fix.kind is FixKind.AUTOMATIC]

    @property
    def assisted(self) -> list[Issue]:
        """Находки, которые применяются только по явному выбору человека."""
        return [i for i in self.issues if i.fix.kind is FixKind.ASSISTED]

    def select(self, keys: set[str]) -> list[Issue]:
        """Находки по ключам выбора. Незнакомый ключ молча игнорируется:
        отчёт мог быть пересобран, и находки в нём уже другие."""
        return [i for i in self.issues if i.key in keys]
