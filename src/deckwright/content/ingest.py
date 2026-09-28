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
from dataclasses import dataclass, field
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

    documents, warnings = render_documents(sources, max_chars)
    prompt = load_prompt(agent_prompt("ingest_content"), prompts_dir)
    purpose = request.purpose.value if request.purpose else "не выбрано"
    answer = client.complete(
        step=prompt.step,
        prompt=prompt.render(purpose=purpose, documents=documents, max_facts=MAX_FACTS),
        schema=IngestAnswer,
    )
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
