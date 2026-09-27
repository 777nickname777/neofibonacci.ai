"""Экспорт `.pdf` → PNG через poppler.

PNG нужны дважды: как превью в интерфейсе и как вход контекстных проверок
аудита, которые смотрят на слайд глазами модели.

Растеризация — самая дорогая часть пересборки. Замер на holdout: колода из
десяти страниц растеризуется 18 с, одна страница — 1.86 с, и накладных
расходов на запуск процесса почти нет. Поэтому итерация цикла исправления,
тронувшая три слайда, обязана растеризовать три страницы, а не десять.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class RasterizeError(RuntimeError):
    """Не удалось получить картинки слайдов."""


def _page_count(pdf_path: Path, timeout_seconds: int) -> int | None:
    """Число страниц по `pdfinfo`; `None`, если спросить не удалось."""
    if shutil.which("pdfinfo") is None:
        return None
    try:
        result = subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _ranges(pages: set[int]) -> list[tuple[int, int]]:
    """Подряд идущие страницы — одним вызовом: 1,2,5 → (1,2) и (5,5)."""
    result: list[tuple[int, int]] = []
    for page in sorted(pages):
        if result and page == result[-1][1] + 1:
            result[-1] = (result[-1][0], page)
        else:
            result.append((page, page))
    return result


def _spans(pages: set[int], workers: int) -> list[tuple[int, int]]:
    """Страницы кусками для параллельной растеризации.

    pdftoppm однопоточный: 13 страниц `zelenie_investicii` подряд — 16 с,
    по 1–1.4 с на страницу. Подряд идущие страницы режутся на куски так,
    чтобы работы хватило на все ядра.
    """
    size = max(1, -(-len(pages) // max(1, workers)))
    result: list[tuple[int, int]] = []
    for first, last in _ranges(pages):
        for start in range(first, last + 1, size):
            result.append((start, min(start + size - 1, last)))
    return result


def _render_spans(
    pdf_path: Path, prefix: Path, dpi: int, timeout_seconds: int, pages: set[int]
) -> None:
    spans = _spans(pages, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=len(spans)) as pool:
        results = list(
            pool.map(lambda span: _render(pdf_path, prefix, dpi, timeout_seconds, span), spans)
        )
    for span, result in zip(spans, results, strict=True):
        # Молча вернуть прошлые картинки нельзя: они от предыдущей сборки, и
        # отчёт был бы про другую колоду.
        if result.returncode != 0:
            raise RasterizeError(
                f"страницы {span[0]}–{span[1]} не перерисованы: "
                f"{(result.stdout or result.stderr or '').strip()[:400]}"
            )


def _render(
    pdf_path: Path, prefix: Path, dpi: int, timeout_seconds: int, span: tuple[int, int] | None
) -> subprocess.CompletedProcess:
    command = ["pdftoppm", "-png", "-r", str(dpi)]
    if span is not None:
        command += ["-f", str(span[0]), "-l", str(span[1])]
    command += [str(pdf_path), str(prefix)]
    try:
        return subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
            command, capture_output=True, text=True, timeout=timeout_seconds, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RasterizeError(f"pdftoppm не уложился в {timeout_seconds} с") from exc


def pdf_to_png(
    pdf_path: str | Path,
    output_dir: str | Path,
    dpi: int = 96,
    timeout_seconds: int = 180,
    only_pages: set[int] | None = None,
) -> list[Path]:
    """Возвращает пути к картинкам слайдов по порядку.

    `only_pages` — номера страниц (с единицы), которые надо перерисовать;
    остальные берутся с прошлой сборки. Так итерация цикла исправления платит
    за изменённые слайды, а не за всю колоду.

    Переиспользование разрешено, только когда прошлых картинок ровно столько
    же, сколько страниц в новом `.pdf`. Если вёрстка разбила слайд надвое,
    нумерация поехала, и старая картинка с номером 4 — это уже другой слайд;
    такой случай честнее перерисовать целиком, чем угадывать.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if shutil.which("pdftoppm") is None:
        raise RasterizeError(
            "pdftoppm не найден. Нужен пакет poppler-utils (см. README)."
        )

    prefix = output_dir / pdf_path.stem
    pattern = f"{pdf_path.stem}-*.png"
    existing = sorted(output_dir.glob(pattern))

    total = _page_count(pdf_path, timeout_seconds)
    reusable = (
        only_pages is not None
        and total is not None
        and len(existing) == total
        and all(1 <= page <= total for page in only_pages)
    )

    if reusable:
        if not only_pages:
            # Не изменилось ничего: перерисовывать нечего, и это законный
            # случай — правка могла не тронуть ни одной страницы.
            return existing
        _render_spans(pdf_path, prefix, dpi, timeout_seconds, only_pages)
    else:
        # Полная растеризация: сначала убираем прошлые картинки. Иначе колода,
        # ставшая короче, оставила бы хвост от предыдущей сборки, и в отчёт
        # уехали бы страницы, которых в `.pdf` уже нет.
        for stale in existing:
            stale.unlink()
        if total:
            _render_spans(pdf_path, prefix, dpi, timeout_seconds, set(range(1, total + 1)))
            result = None
        else:
            result = _render(pdf_path, prefix, dpi, timeout_seconds, None)
        if result is not None and result.returncode != 0:
            raise RasterizeError(
                f"растеризация {pdf_path.name} оборвалась: "
                f"{(result.stdout or result.stderr or '').strip()[:400]}"
            )

    pages = sorted(output_dir.glob(pattern))
    if not pages:
        raise RasterizeError(f"картинки не созданы для {pdf_path.name}")
    return pages
