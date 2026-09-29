"""Рабочий каталог одного прогона: входы, результаты, диагностика.

Зачем отдельный модуль. Раньше загруженные файлы ложились в один общий
каталог под своим исходным именем (`outputs/uploads/<имя>`), а прогон
разбирал их уже в фоновом потоке. Два человека с файлом `template.pptx` —
самое обычное имя — перезаписывали файл друг другу, и прогон мог разобрать
чужой шаблон. Здесь у каждого прогона свой каталог, и пересечься нечем.

Владение. `owner` — идентификатор сессии браузера. Он не секрет и не замена
авторизации, но решает конкретную задачу: знание чужого `run_id` само по себе
не даёт доступ к его файлам, потому что интерфейс спрашивает прогон **и**
владельца. Пароль на вход (`DECKWRIGHT_PASSWORD`) остаётся отдельным слоем.

Имена файлов. Пользовательское имя не попадает в путь как есть: `../` и
разделители каталогов из него вырезаются, длина ограничивается, расширение
сохраняется. Имя нужно только человеку — на диске файл лежит под безопасным.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Что остаётся от пользовательского имени файла: буквы (в том числе
# кириллица), цифры, пробел, точка, дефис, подчёркивание.
_KEEP = re.compile(r"[^\w .\-]+", re.UNICODE)
_COLLAPSE = re.compile(r"\s+")
MAX_NAME = 120

# Расширения, которые пайплайн умеет читать. Всё остальное на диск не ложится.
ALLOWED_SUFFIXES = frozenset(
    {".pptx", ".potx", ".txt", ".md", ".docx", ".pdf", ".json"}
)


class UnsafeUpload(ValueError):
    """Файл нельзя принять: имя или расширение непригодны."""


def safe_name(raw: str, *, fallback: str = "file") -> str:
    """Безопасное имя файла из пользовательского.

    Берётся только базовое имя — всё, что похоже на путь, отбрасывается,
    поэтому `../../etc/passwd` превращается в `passwd`. Имя нормализуется в
    NFC: macOS отдаёт кириллицу в NFD, и один и тот же файл иначе даёт два
    разных имени на диске.
    """
    name = unicodedata.normalize("NFC", raw or "")
    # И POSIX, и Windows: пользователь мог принести имя с любой машины.
    name = name.replace("\\", "/").split("/")[-1]
    name = _KEEP.sub("_", name)
    name = _COLLAPSE.sub(" ", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > MAX_NAME:
        stem, dot, suffix = name.rpartition(".")
        keep = MAX_NAME - (len(suffix) + 1 if dot else 0)
        name = (stem[:keep] + dot + suffix) if dot else name[:MAX_NAME]
    return name


def new_run_id(prefix: str = "run") -> str:
    """Идентификатор прогона: время для человека, случайность против подбора."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(4)}"


def new_session_id() -> str:
    """Идентификатор сессии браузера."""
    return secrets.token_urlsafe(16)


@dataclass(frozen=True)
class RunWorkspace:
    """Каталоги одного прогона. Ничего не делит с соседями."""

    run_id: str
    owner: str
    root: Path
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(cls, base: str | Path, run_id: str, owner: str) -> RunWorkspace:
        root = Path(base) / run_id
        ws = cls(run_id=run_id, owner=owner, root=root)
        for path in (ws.inputs, ws.outputs, ws.diag):
            path.mkdir(parents=True, exist_ok=True)
        return ws

    # ── каталоги ────────────────────────────────────────────────────────
    @property
    def inputs(self) -> Path:
        """Загруженные пользователем файлы этого прогона."""
        return self.root / "inputs"

    @property
    def outputs(self) -> Path:
        """Готовые колоды по вариантам."""
        return self.root / "variants"

    @property
    def diag(self) -> Path:
        """Журнал прогона и прочая диагностика."""
        return self.root / "diag"

    def variant(self, name: str) -> Path:
        """Каталог одного варианта вёрстки."""
        path = self.outputs / safe_name(name, fallback="variant")
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ── файлы ───────────────────────────────────────────────────────────
    def save_upload(self, raw_name: str, data: bytes) -> Path:
        """Кладёт загруженный файл в каталог прогона под безопасным именем.

        Одинаковые имена в пределах одного прогона не перетирают друг друга:
        второй такой же получает суффикс. Между прогонами пересечься нечем —
        каталоги разные.
        """
        name = safe_name(raw_name, fallback="upload")
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise UnsafeUpload(
                f"файл {raw_name!r}: расширение {suffix or '(нет)'} не поддерживается; "
                f"принимаются {', '.join(sorted(ALLOWED_SUFFIXES))}"
            )
        target = self.inputs / name
        if target.exists():
            stem, dot, ext = name.rpartition(".")
            for n in range(2, 1000):
                candidate = self.inputs / (f"{stem}-{n}.{ext}" if dot else f"{name}-{n}")
                if not candidate.exists():
                    target = candidate
                    break
        # Запись через временный файл рядом: читатель никогда не увидит
        # недописанный вход.
        tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self._assert_inside(target)
        return target

    def _assert_inside(self, path: Path) -> None:
        """Страховка от выхода за пределы каталога прогона."""
        root = self.root.resolve()
        if not path.resolve().is_relative_to(root):
            raise UnsafeUpload(f"путь {path} уводит за пределы каталога прогона")

    # ── уборка ──────────────────────────────────────────────────────────
    def cleanup(self, keep_outputs: bool = True) -> None:
        """Убирает временное этого прогона, не трогая соседние.

        `keep_outputs` бережёт готовые колоды и диагностику: убирать нужно
        входы и обрывки, а не то, ради чего прогон затевался. Удаляется только
        то, что лежит внутри `root`, — ни один соседний прогон не задет.
        """
        shutil.rmtree(self.inputs, ignore_errors=True)
        for part in self.root.rglob("*.part"):
            part.unlink(missing_ok=True)
        if not keep_outputs:
            shutil.rmtree(self.root, ignore_errors=True)
