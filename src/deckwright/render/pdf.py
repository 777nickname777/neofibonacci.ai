"""Экспорт `.pptx` → `.pdf` через LibreOffice headless.

LibreOffice запускается отдельным процессом с собственным профилем: без
`-env:UserInstallation` параллельные запуски дерутся за общий профиль в
домашнем каталоге и подвисают. Профиль каждой попытки свой и удаляется вместе
с ней — пользовательский профиль LibreOffice не трогается никогда.

Важное ограничение, которое нельзя забыть: LibreOffice **молчит** о битых
ссылках внутри пакета. Файл с висящим `r:embed` конвертируется без единой
жалобы, картинка просто не рисуется, а PowerPoint на том же файле требует
восстановления. Поэтому целостность проверяется отдельно, в
`package_check.check_package`, а не выводится из факта успешной конвертации.

Второе: шрифты, извлечённые из шаблона, LibreOffice сам не видит — он ищет
их через fontconfig в системных каталогах. Без подсказки `vk_tech` рисовался
DejaVu Sans вместо Play: шире на глаз, и текст, который фиттер честно
уложил по метрикам Play, на картинке рвал слова посередине. Каталоги шрифтов
передаются через собственный `fonts.conf` в профиле запуска; системный
конфиг подключается им же, так что остальные шрифты никуда не деваются.

## Почему конвертация устроена именно так

**Результат собирается в приватном каталоге, а не в каталоге колоды.**
`--outdir` пишет файл по ходу дела, и пока LibreOffice не закончил, в
каталоге лежит недописанный `.pdf`. Интерфейс показывает кнопку скачивания по
факту существования файла — и отдавал бы обрезанный. Готовый файл переносится
на место одним `os.replace`, то есть либо старого, либо целого нового.

**Код возврата проверяется.** Раньше проверялось только существование файла с
нужным именем. Если в каталоге лежал `.pdf` от прошлого прогона, а
конвертация падала, наружу уходил **старый файл как успешный результат**.
Воспроизведено: `soffice` с кодом 1, ничего не записавший, — и на выходе
32-байтный файл прошлого прогона.

**Результат проверяется как PDF, а не как «файл на месте»**: непустой,
с сигнатурой `%PDF`, читается `pdfinfo`, и число страниц совпадает с
ожидаемым, если его передали.

**Повтор — только для временных отказов.** Отсутствующий бинарник и
повреждённый вход повторять бессмысленно: результат будет тот же. Временный
отказ (нет кода возврата, пустой вывод, сорванный таймаут) повторяется с
полностью чистыми временными ресурсами, а причина первой попытки
сохраняется — иначе в отчёте остаётся только последняя, самая бедная.

**Параллельность ограничена.** Каждый экземпляр LibreOffice — сотни мегабайт;
три варианта колоды плюс соседние прогоны выбирали память машины. Предел
общий на процесс и настраивается (`render.max_parallel_conversions`).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

# Сигнатура PDF: первые байты любого валидного файла.
_PDF_MAGIC = b"%PDF"

# Сколько конвертаций идут одновременно в этом процессе. Ноль/None — без
# предела. Меняется через `configure_parallelism`, которую зовёт пайплайн.
_DEFAULT_PARALLEL = 2
_limit_lock = threading.Lock()
_limit: threading.Semaphore | None = None
_limit_size = 0


def configure_parallelism(max_parallel: int) -> None:
    """Задаёт предел одновременных конвертаций на процесс.

    Зовётся один раз при загрузке конфига. Повторный вызов с тем же числом
    ничего не делает: пересоздавать семафор под работающими конвертациями
    нельзя — ждущие останутся на старом.
    """
    global _limit, _limit_size
    # Каждая конвертация — свой процесс LibreOffice со своим профилем.
    # Просить больше, чем машина может вытянуть, значит менять параллельность
    # на своппинг: на слабом раннере три soffice медленнее двух. Половина
    # ядер — граница, за которой выигрыш на этой машине пропадал.
    ceiling = max(1, (os.cpu_count() or 2) // 2)
    size = max(1, min(int(max_parallel or 1), ceiling))
    with _limit_lock:
        if _limit is not None and _limit_size == size:
            return
        _limit = threading.Semaphore(size)
        _limit_size = size


def _gate() -> threading.Semaphore:
    global _limit, _limit_size
    with _limit_lock:
        if _limit is None:
            _limit = threading.Semaphore(_DEFAULT_PARALLEL)
            _limit_size = _DEFAULT_PARALLEL
        return _limit


class ConversionError(RuntimeError):
    """LibreOffice не смог сконвертировать файл.

    `kind` объясняет, что именно случилось, и определяет, есть ли смысл
    повторять: интерфейсу это нужно, чтобы предложить осмысленное следующее
    действие вместо «попробуйте ещё раз».
    """

    #: причина не изменится от повтора
    PERMANENT = frozenset({"missing_binary", "missing_input", "bad_input"})

    def __init__(
        self,
        message: str,
        *,
        kind: str = "transient",
        attempts: int = 1,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.kind not in self.PERMANENT


@dataclass
class ConversionReport:
    """Что происходило при конвертации — для диагностики прогона."""

    source: str
    target: str = ""
    ok: bool = False
    attempts: int = 0
    seconds: float = 0.0
    pages: int | None = None
    error_kind: str = ""
    error: str = ""
    notes: list[str] = field(default_factory=list)


def pptx_to_pdf(
    pptx_path: str | Path,
    output_dir: str | Path,
    soffice_binary: str = "soffice",
    timeout_seconds: int = 180,
    font_dirs: Iterable[str | Path] = (),
    # Пары «гарнитура шаблона → чем её мерил фиттер»: конвертер обязан
    # рисовать тем же, чем считали, иначе переносы разойдутся с расчётом.
    font_aliases: Iterable[tuple[str, str]] = (),
    expected_pages: int | None = None,
    max_attempts: int = 3,
    report: ConversionReport | None = None,
) -> Path:
    """Конвертирует `.pptx` в `.pdf` рядом с ним и возвращает путь к файлу.

    `expected_pages` — сколько страниц обязано получиться; расхождение значит,
    что конвертация потеряла или удвоила слайд, и это отказ, а не мелочь.
    `report` заполняется по ходу: попытки, время, страницы, причина отказа.
    """
    pptx_path = Path(pptx_path)
    output_dir = Path(output_dir)
    target = output_dir / f"{pptx_path.stem}.pdf"
    rep = report if report is not None else ConversionReport(source=str(pptx_path))
    rep.source = str(pptx_path)
    rep.target = str(target)
    started = time.monotonic()

    if not pptx_path.is_file():
        raise ConversionError(
            f"нечего конвертировать: {pptx_path} не существует",
            kind="missing_input",
        )
    binary = shutil.which(soffice_binary)
    if binary is None:
        raise ConversionError(
            f"{soffice_binary} не найден в PATH. На macOS LibreOffice ставится "
            "приложением: укажите render.soffice_binary полным путём "
            "(/Applications/LibreOffice.app/Contents/MacOS/soffice) или "
            "поставьте симлинк. Экспорт в PDF без него недоступен; "
            "собранный .pptx при этом остаётся годным.",
            kind="missing_binary",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    attempts = max(1, int(max_attempts))
    first_failure: ConversionError | None = None

    for attempt in range(1, attempts + 1):
        rep.attempts = attempt
        try:
            produced = _convert_once(
                pptx_path, binary, timeout_seconds, font_dirs, font_aliases,
                expected_pages, rep,
                output_dir,
            )
        except ConversionError as failure:
            if first_failure is None:
                first_failure = failure
            if not failure.retryable or attempt == attempts:
                rep.ok = False
                rep.seconds = round(time.monotonic() - started, 3)
                rep.error_kind = first_failure.kind
                rep.error = str(first_failure)
                raise ConversionError(
                    str(first_failure),
                    kind=first_failure.kind,
                    attempts=attempt,
                    detail=first_failure.detail,
                ) from failure
            rep.notes.append(f"попытка {attempt}: {failure.kind} — {failure}")
            # Пауза растёт: мгновенный повтор упирается в ту же занятую память.
            time.sleep(min(2.0 * attempt, 5.0))
            continue

        # Готовый файл переносится на место целиком: либо старый, либо новый.
        # `produced` лежит в том же каталоге, значит `os.replace` атомарен и
        # не упирается в границу файловых систем.
        try:
            os.replace(produced, target)
        finally:
            Path(produced).unlink(missing_ok=True)
        rep.ok = True
        rep.seconds = round(time.monotonic() - started, 3)
        return target

    raise AssertionError("недостижимо")  # pragma: no cover


def _convert_once(
    pptx_path: Path,
    binary: str,
    timeout_seconds: int,
    font_dirs: Iterable[str | Path],
    font_aliases: Iterable[tuple[str, str]],
    expected_pages: int | None,
    rep: ConversionReport,
    output_dir: Path,
) -> Path:
    """Одна попытка: свой профиль, свой каталог вывода, свой процесс.

    Возвращает путь к проверенному файлу, **уже лежащему в `output_dir`** под
    временным именем: так замена целевого файла остаётся атомарной, а
    недописанный результат никогда не попадает под имя, которое читает
    интерфейс.
    """
    # Профиль и выход — в одном временном каталоге: он уносит за собой всё,
    # что LibreOffice успел создать, при любом исходе. Семафор держится ровно
    # на время работы процесса.
    with _gate(), tempfile.TemporaryDirectory(prefix="deckwright-lo-") as scratch:
        root = Path(scratch)
        profile = root / "profile"
        outdir = root / "out"
        profile.mkdir()
        outdir.mkdir()
        command = [
            binary,
            "--headless",
            "--norestore",
            "--invisible",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(pptx_path),
        ]
        env = _with_fonts(profile, font_dirs, font_aliases)
        code, out, err, timed_out = _run(command, env, timeout_seconds)

        produced = outdir / f"{pptx_path.stem}.pdf"
        tail = (err or out or "").strip()[-400:]

        if timed_out:
            raise ConversionError(
                f"LibreOffice не уложился в {timeout_seconds} с "
                f"на {pptx_path.name}",
                kind="timeout",
                detail=tail,
            )
        if code != 0:
            raise ConversionError(
                f"LibreOffice завершился с кодом {code} на {pptx_path.name}",
                kind=_classify(code, tail),
                detail=tail,
            )
        if not produced.exists():
            raise ConversionError(
                f"LibreOffice отчитался об успехе, но PDF для "
                f"{pptx_path.name} не создан",
                kind="transient",
                detail=tail,
            )

        _validate(produced, expected_pages, rep, timeout_seconds)

        # Переносим проверенный файл в каталог колоды под временным именем:
        # оттуда его заберёт атомарный `os.replace`. Имя уникально по
        # процессу и потоку — соседний прогон в том же каталоге не мешает.
        staged = output_dir / (
            f".{pptx_path.stem}.{os.getpid()}.{threading.get_ident():x}.part"
        )
        shutil.move(str(produced), str(staged))
        return staged


# Фразы, которыми LibreOffice говорит именно про негодный вход. Всё
# остальное — повод повторить: процесс, убитый по памяти, и битый документ
# оба дают ненулевой код, но лечатся по-разному.
_BAD_INPUT_MARKERS = (
    "source file could not be loaded",
    "no export filter",
    "does not exist",
    "Error: Please reverify input parameters",
)


def _classify(code: int | None, tail: str) -> str:
    """Временный отказ или негодный вход.

    Асимметрия намеренная: лишний повтор негодного входа стоит секунд, а
    неповторённый временный отказ стоит всего прогона — именно так пропал
    экспорт трёх вариантов `zelenie_investicii`. Поэтому «битым входом»
    считается только то, про что LibreOffice сказал прямо; отрицательный код
    (процесс убит сигналом, в том числе по памяти) — всегда временный.
    """
    if code is not None and code < 0:
        return "transient"
    lowered = tail.lower()
    if any(marker.lower() in lowered for marker in _BAD_INPUT_MARKERS):
        return "bad_input"
    return "transient"


def _run(
    command: list[str], env: dict[str, str] | None, timeout_seconds: int
) -> tuple[int | None, str, str, bool]:
    """Запускает процесс своей группой и гасит только её.

    `start_new_session` даёт процессу собственную группу, поэтому по таймауту
    гасится LibreOffice вместе со своими потомками и ничего больше. Ни
    `killall`, ни поиск чужих `soffice` по имени здесь не нужны и запрещены:
    у пользователя может быть открыт свой LibreOffice.
    """
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout_seconds)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        _terminate(proc)
        out, err = proc.communicate()
        return proc.returncode, out or "", err or "", True
    finally:
        if proc.poll() is None:  # pragma: no cover — страховка от зависшего потомка
            _terminate(proc)


def _terminate(proc: subprocess.Popen) -> None:
    """Гасит группу процесса: сначала вежливо, потом наверняка."""
    try:
        group = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
        return
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)


def _validate(
    pdf: Path, expected_pages: int | None, rep: ConversionReport, timeout_seconds: int
) -> None:
    """PDF ли это вообще и тот ли, которого ждали."""
    size = pdf.stat().st_size
    if size == 0:
        raise ConversionError(
            f"PDF {pdf.name} пуст (0 байт)", kind="transient"
        )
    with pdf.open("rb") as fh:
        head = fh.read(len(_PDF_MAGIC))
    if head != _PDF_MAGIC:
        raise ConversionError(
            f"файл {pdf.name} не начинается с {_PDF_MAGIC.decode()}: "
            "это не PDF",
            kind="transient",
        )
    pages = _page_count(pdf, timeout_seconds)
    rep.pages = pages
    if pages is None:
        # `pdfinfo` может отсутствовать — это не повод считать экспорт
        # провалившимся, но и не повод молчать.
        rep.notes.append("pdfinfo недоступен: число страниц не проверено")
        return
    if pages <= 0:
        raise ConversionError(
            f"PDF {pdf.name} не читается pdfinfo: страниц {pages}",
            kind="transient",
        )
    if expected_pages is not None and pages != expected_pages:
        raise ConversionError(
            f"в PDF {pages} страниц, а слайдов {expected_pages}: "
            "конвертация потеряла или удвоила слайд",
            kind="transient",
        )


def _page_count(pdf_path: Path, timeout_seconds: int) -> int | None:
    """Число страниц по `pdfinfo`; `None`, если спросить нечем."""
    if shutil.which("pdfinfo") is None:
        return None
    try:
        result = subprocess.run(  # аргументы фиксированы, не из пользовательского ввода
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=max(10, min(timeout_seconds, 60)),
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return -1
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _with_fonts(
    profile: Path,
    font_dirs: Iterable[str | Path],
    aliases: Iterable[tuple[str, str]] = (),
) -> dict[str, str] | None:
    """Окружение, в котором fontconfig видит шрифты шаблона и наши замены.

    `aliases` — пары «гарнитура шаблона → чем её мерил фиттер». Без них
    конвертер подставляет своё: текст посчитан по Carlito, а нарисован чем
    угодно, и переносы строк расходятся с расчётом на ширинах глифов.
    Псевдоним не меняет сам `.pptx`: в файле остаётся гарнитура шаблона, и
    на машине, где она есть, колода рисуется ею.
    """
    dirs = [Path(d).resolve() for d in font_dirs if Path(d).is_dir()]
    pairs = [
        (str(a), str(b))
        for a, b in aliases
        if a and b and str(a).strip().lower() != str(b).strip().lower()
    ]
    if not dirs and not pairs:
        return None
    conf = profile / "fonts.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    entries = "".join(f"<dir>{escape(str(d))}</dir>" for d in dirs)
    # Системный конфиг подключается по обоим привычным путям: на Linux он в
    # /etc/fonts, у Homebrew на macOS — в своём префиксе. Оба помечены
    # ignore_missing, так что лишний просто не применится.
    includes = "".join(
        f'<include ignore_missing="yes">{escape(p)}</include>'
        for p in ("/etc/fonts/fonts.conf", "/opt/homebrew/etc/fonts/fonts.conf",
                  "/usr/local/etc/fonts/fonts.conf")
    )
    rules = "".join(
        '<match target="pattern">'
        f'<test name="family"><string>{escape(requested)}</string></test>'
        f'<edit name="family" mode="prepend" binding="strong">'
        f"<string>{escape(used)}</string></edit></match>"
        for requested, used in pairs
    )
    conf.write_text(
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        f"{includes}{entries}{rules}"
        f"<cachedir>{escape(str(profile / 'fc-cache'))}</cachedir>"
        "</fontconfig>",
        encoding="utf-8",
    )
    return {**os.environ, "FONTCONFIG_FILE": str(conf)}
