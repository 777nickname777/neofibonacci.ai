"""Любой вход → `ContentPack`.

Вход — то, что пришло от пользователя: текст из поля ввода (короткий бриф
или готовый материал) и файлы. Путь один:

1. JSON в нашей схеме контент-пакета принимается как есть, без модели.
2. Всё остальное читается детерминированно (`content.readers`) и отдаётся
   модели одним вызовом: она выбирает тему, факты, ряды и цитаты.
3. Каждое число ответа сверяется с текстом входа (`content.grounding`).
   Факт, ряд или цитата, которых во входе нет, отбрасываются, и это пишется
   в предупреждения: цифры на слайдах — только из входа.

Идентификаторы документов, фактов, рядов и цитат проставляет код, а не
модель: модель ошибается в них чаще всего и дешевле всего.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from pydantic import ValidationError

from deckwright.config import agent_prompt
from deckwright.content.grounding import is_grounded, normalize, source_numbers, ungrounded
from deckwright.content.readers import SourceText, read_file, read_inline
from deckwright.llm.base import StructuredClient
from deckwright.plan.planner import Prompt, load_prompt
from deckwright.schemas import (
    Brief,
    ContentPack,
    DeckPurpose,
    Fact,
    IngestAnswer,
    Quote,
    Series,
    SourceDoc,
)

# Сколько символов входа уходит модели. Токенов у русского текста примерно
# втрое меньше символов: 24 тысячи символов — около 8 тысяч токенов, пятая
# часть минутного лимита аккаунта. Остальное обрезается, и об этом пишется.
MAX_CHARS = 24_000
# Сколько фактов просить. Ответ модели — самая долгая часть разбора входа:
# 34 факта с местами на docx-отчёте стоили 82 с из 300 на колоду. Двадцати
# хватает на 10–15 слайдов: на слайд идёт один-два факта.
MAX_FACTS = 20
# Целые до этого числа в тексте факта — счёт («три этапа»), а не факт.
SMALL_COUNT = 10


class IngestError(ValueError):
    """Вход пуст или не читается."""


@dataclass
class IngestInput:
    """Что прислал пользователь."""

    text: str = ""
    files: list[Path] = field(default_factory=list)
    purpose: DeckPurpose | None = None
    author: str = ""
    author_role: str = ""
    audience: str = ""


@dataclass
class IngestResult:
    pack: ContentPack
    sources: list[SourceText]
    warnings: list[str]
    prompt: Prompt | None = None
    # Что отброшено сверкой с входом: {вид: число}.
    dropped: dict[str, int] = field(default_factory=dict)


def ingest(
    request: IngestInput,
    client: StructuredClient | None,
    default_purpose: DeckPurpose = DeckPurpose.PRODUCT,
    max_chars: int = MAX_CHARS,
    prompts_dir: str | Path | None = None,
) -> IngestResult:
    """Приводит вход к контент-пакету."""
    ready = _ready_pack(request)
    if ready is not None:
        return IngestResult(pack=ready, sources=[], warnings=[])

    sources: list[SourceText] = []
    if request.text.strip():
        sources.append(read_inline(request.text.strip(), "d0"))
    for number, path in enumerate(request.files, start=1):
        sources.append(read_file(path, f"d{number}"))
    sources = [source for source in sources if source.fragments or source.tables]
    if not sources:
        raise IngestError("вход пуст: нет ни текста, ни файлов с текстом")
    if client is None:
        raise IngestError("для разбора входа нужна модель, а клиент не задан")

    prompt = load_prompt(agent_prompt("ingest_content"), prompts_dir)
    purpose = request.purpose.value if request.purpose else "не выбрано"
    chunks, warnings = split_sources(sources, max_chars)
    # У каждой части просим столько фактов, чтобы после слияния набралось
    # нужное число и осталось чем заменить повторы.
    per_chunk = MAX_FACTS if len(chunks) == 1 else max(4, -(-MAX_FACTS // len(chunks)) + 2)
    answers = []
    for chunk in chunks:
        documents, cut = render_documents(chunk, max_chars)
        warnings.extend(cut)
        answers.append(
            client.complete(
                step=prompt.step,
                prompt=prompt.render(
                    purpose=purpose, documents=documents, max_facts=per_chunk
                ),
                schema=IngestAnswer,
            )
        )
    if len(chunks) > 1:
        warnings.append(
            f"вход прочитан по частям: запросов к модели {len(chunks)}, "
            "границы частей — между фрагментами документов"
        )
    answer = merge_answers(answers, MAX_FACTS)
    pack, dropped, grounding_warnings = to_pack(answer, sources, request, default_purpose)
    return IngestResult(
        pack=pack,
        sources=sources,
        warnings=warnings + grounding_warnings,
        prompt=prompt,
        dropped=dropped,
    )


def _ready_pack(request: IngestInput) -> ContentPack | None:
    """Один JSON в нашей схеме и никакого текста — готовый контент-пакет."""
    if request.text.strip() or len(request.files) != 1:
        return None
    path = request.files[0]
    if path.suffix.lower() != ".json":
        return None
    try:
        pack = ContentPack.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (ValueError, ValidationError):
        return None
    updates = {
        key: value
        for key, value in (
            ("purpose", request.purpose),
            ("author", request.author),
            ("author_role", request.author_role),
            ("audience", request.audience),
        )
        if value
    }
    if updates:
        pack = pack.model_copy(update={"brief": pack.brief.model_copy(update=updates)})
    return pack


def _header(source: SourceText) -> str:
    return f"[{source.doc_id}] {source.name} ({source.kind})"


def _fragment_text(fragment) -> str:
    where = f"({fragment.locator}) " if fragment.locator else ""
    return f"{where}{fragment.text}"


def _table_text(table) -> str:
    where = f"({table.locator}) " if table.locator else ""
    return where + "Таблица:\n" + "\n".join(" | ".join(row) for row in table.rows)


def _slice(source: SourceText, fragments: list[int], tables: list[int]) -> SourceText:
    """Тот же документ, но только с этими фрагментами и таблицами."""
    return replace(
        source,
        fragments=[source.fragments[i] for i in fragments],
        tables=[source.tables[i] for i in tables],
    )


def split_sources(
    sources: list[SourceText], max_chars: int
) -> tuple[list[list[SourceText]], list[str]]:
    """Вход, разбитый на части, каждая из которых влезает в бюджет запроса.

    Режется по смысловым границам — между фрагментами и таблицами, которые
    уже выделил разбор файла, — и никогда посреди фрагмента. Прежний срез
    оставлял начало и выбрасывал остальное молча: середина и конец длинного
    отчёта до модели не доходили вовсе, а предупреждение об этом читал не
    тот, кто принимал решение по колоде.

    Документ, не влезающий целиком, продолжается в следующей части под тем же
    `doc_id`: ссылки на источник от этого не портятся.
    """
    warnings: list[str] = []
    chunks: list[list[SourceText]] = []
    current: list[SourceText] = []
    used = 0
    # Бриф — не материал, который делится, а указание: что показываем и кому.
    # Он повторяется в каждой части, иначе вторая половина отчёта разбиралась
    # бы без темы и без назначения колоды.
    brief = [source for source in sources if source.kind == "inline"]
    sources = [source for source in sources if source.kind != "inline"]
    if brief and not sources:
        return [brief], warnings
    used = sum(len(_header(item)) + len(_fragment_text(f)) + 1
               for item in brief for f in item.fragments)

    reserved = used

    def flush() -> None:
        nonlocal current, used
        if current:
            chunks.append(brief + current)
        current, used = [], reserved

    for source in sources:
        head = len(_header(source)) + 1
        entries = [
            ("f", index, len(_fragment_text(item)) + 1)
            for index, item in enumerate(source.fragments)
        ]
        entries += [
            ("t", index, len(_table_text(item)) + 1)
            for index, item in enumerate(source.tables)
        ]
        fragments: list[int] = []
        tables: list[int] = []
        started = False
        for kind, index, size in entries:
            if head + size > max_chars:
                # Один фрагмент длиннее целого бюджета: единственное место,
                # где граница окажется не смысловой. Обрезку сделает
                # `render_documents`, и она о ней скажет.
                warnings.append(
                    f"{source.name}: фрагмент в {size} символов длиннее бюджета "
                    f"запроса ({max_chars}) и будет обрезан внутри"
                )
            cost = size + (0 if started else head)
            if used + cost > max_chars and (fragments or tables or current):
                if fragments or tables:
                    current.append(_slice(source, fragments, tables))
                    fragments, tables = [], []
                flush()
                started = False
                cost = size + head
            (fragments if kind == "f" else tables).append(index)
            used += cost
            started = True
        if fragments or tables:
            current.append(_slice(source, fragments, tables))
    flush()
    return chunks or [brief + sources], warnings


def merge_answers(answers: list[IngestAnswer], max_facts: int) -> IngestAnswer:
    """Ответы по частям входа — в один, без потери середины.

    Факты, ряды и цитаты берутся по кругу: первый из каждой части, потом
    второй из каждой и так далее. Срезав объединённый список подряд, мы
    вернули бы ровно ту беду, ради которой вход и делится: всё с начала,
    ничего из конца.
    """
    if len(answers) == 1:
        return answers[0]
    first = answers[0]
    return first.model_copy(
        update={
            "facts": _round_robin([a.facts for a in answers], max_facts, _text_key),
            "series": _round_robin([a.series for a in answers], max_facts, _name_key),
            "quotes": _round_robin([a.quotes for a in answers], max_facts, _text_key),
            "topic": next((a.topic for a in answers if a.topic.strip()), first.topic),
            "goal": next((a.goal for a in answers if a.goal.strip()), first.goal),
            "audience": next(
                (a.audience for a in answers if a.audience.strip()), first.audience
            ),
        }
    )


def _text_key(item) -> str:
    return normalize(getattr(item, "text", ""))[:80]


def _name_key(item) -> str:
    return normalize(getattr(item, "name", ""))[:80]


def _round_robin(groups: list[list], limit: int, key) -> list:
    """По одному из каждой части по кругу, без повторов, до предела."""
    taken: list = []
    seen: set[str] = set()
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            mark = key(item)
            if mark and mark in seen:
                continue
            seen.add(mark)
            taken.append(item)
            if len(taken) >= limit:
                return taken
    return taken


def render_documents(sources: list[SourceText], max_chars: int) -> tuple[str, list[str]]:
    """Текст документов для запроса, в пределах `max_chars`.

    Бюджет делится между документами поровну, а недобранное коротким
    документом отдаётся длинным: короткий бриф не должен отнимать место у
    отчёта, а длинный отчёт — вытеснять бриф.
    """
    blocks = [_render(source) for source in sources]
    budget = max_chars
    shares: dict[int, int] = {}
    pending = sorted(range(len(blocks)), key=lambda i: len(blocks[i]))
    while pending:
        share = budget // len(pending)
        index = pending.pop(0)
        shares[index] = min(len(blocks[index]), share)
        budget -= shares[index]
    warnings = []
    parts = []
    for index, (source, block) in enumerate(zip(sources, blocks, strict=True)):
        if len(block) > shares[index]:
            warnings.append(
                f"{source.name}: модели передано {shares[index]} из {len(block)} "
                "символов, остальное не читалось"
            )
            block = block[: shares[index]] + "\n(…текст обрезан)"
        parts.append(block)
    return "\n\n".join(parts), warnings


def _render(source: SourceText) -> str:
    lines = [f"[{source.doc_id}] {source.name} ({source.kind})"]
    for fragment in source.fragments:
        where = f"({fragment.locator}) " if fragment.locator else ""
        lines.append(f"{where}{fragment.text}")
    for table in source.tables:
        where = f"({table.locator}) " if table.locator else ""
        lines.append(f"{where}Таблица:")
        lines.extend(" | ".join(row) for row in table.rows)
    return "\n".join(lines)


def to_pack(
    answer: IngestAnswer,
    sources: list[SourceText],
    request: IngestInput,
    default_purpose: DeckPurpose,
) -> tuple[ContentPack, dict[str, int], list[str]]:
    """Ответ модели → контент-пакет, с отбрасыванием чисел, которых во входе нет.

    Число сверяется со всем входом, а не только с документом, на который
    сослалась модель: ссылку она путает, а число — нет. Выдуманное число
    ищется в масштабах записи (проценты, тысячи), поэтому «34 %» и 0.34
    одно число, а 35 % при 34 % во входе — выдумка.
    """
    known = {source.doc_id for source in sources}
    fallback = sources[0].doc_id
    numbers = set().union(*(source_numbers(source.text) for source in sources))
    corpus = normalize("\n".join(source.text for source in sources))
    dropped = {"facts": 0, "series": 0, "quotes": 0}
    warnings: list[str] = []

    def doc(doc_id: str) -> str:
        return doc_id if doc_id in known else fallback

    facts = []
    for item in answer.facts:
        invented = ungrounded(item.text, numbers, small=SMALL_COUNT)
        if item.value is not None and not is_grounded(item.value, numbers):
            invented.append(item.value)
        if invented:
            dropped["facts"] += 1
            warnings.append(
                f"факт отброшен — чисел {_numbers(invented)} во входе нет: {item.text[:80]}"
            )
            continue
        facts.append(
            Fact(
                id=f"f{len(facts) + 1}",
                text=item.text,
                source_doc_id=doc(item.doc_id),
                locator=item.locator,
                value=item.value,
                unit=item.unit,
            )
        )

    series = []
    for item in answer.series:
        invented = [p.value for p in item.points if not is_grounded(p.value, numbers)]
        if invented:
            dropped["series"] += 1
            warnings.append(
                f"ряд «{item.name}» отброшен — значений {_numbers(invented)} во входе нет"
            )
            continue
        # Подписи вместо данных: из таблицы дизайн-PDF питча Fibonacci модель
        # собрала ряд 2023…2026 со значениями 2023…2026 — столбцы высотой в
        # год. Каждое значение есть во входе, сверка чисел его пропускала.
        if len(item.points) > 1 and all(
            p.value in source_numbers(p.label) for p in item.points
        ):
            dropped["series"] += 1
            warnings.append(f"ряд «{item.name}» отброшен — значения повторяют подписи")
            continue
        series.append(
            Series(
                id=f"s{len(series) + 1}",
                name=item.name,
                unit=item.unit,
                points=item.points,
                source_doc_id=doc(item.doc_id),
                locator=item.locator,
                shape=item.shape,
            )
        )

    quotes = []
    for item in answer.quotes:
        if normalize(item.text) not in corpus:
            dropped["quotes"] += 1
            warnings.append(f"цитата отброшена — во входе её нет дословно: {item.text[:80]}")
            continue
        quotes.append(
            Quote(
                id=f"q{len(quotes) + 1}",
                text=item.text,
                author=item.author,
                role=item.role,
                source_doc_id=doc(item.doc_id),
            )
        )

    inline = next((source for source in sources if source.kind == "inline"), None)
    brief = Brief(
        topic=answer.topic.strip()[:120],
        purpose=request.purpose or answer.purpose or default_purpose,
        audience=request.audience or answer.audience,
        goal=answer.goal,
        language=answer.language,
        author=request.author,
        author_role=request.author_role,
        request=inline.text[:2000] if inline is not None else "",
    )
    pack = ContentPack(
        brief=brief,
        documents=[
            SourceDoc(id=s.doc_id, name=s.name, kind=s.kind, char_count=s.char_count)
            for s in sources
        ],
        facts=facts,
        series=series,
        quotes=quotes,
    )
    return pack, dropped, warnings


def _numbers(values: list[float]) -> str:
    return ", ".join(f"{value:g}" for value in values)
