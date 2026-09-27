"""Проверка чисел на слайде против исходных материалов.

Проверка Приложения 1 «все цифры и факты со слайда есть в исходных
материалах» в лоб выглядит поиском подстроки — и в лоб она неверна. Живая
модель на первом же прогоне написала «время обнаружения сократилось в 4.6
раза»: числа 4.6 в контент-пакете нет и быть не может, оно выведено из 42 и 9.
Поиском подстроки это помечается выдумкой, хотя это правильно посчитанное
следствие.

Отсюда два вида чисел и два способа проверки. Процитированное сверяется со
значением факта. Выведенное пересчитывается по формуле, которую планировщик
обязан был приложить. Проверка остаётся детерминированной в обоих случаях.

Формула вычисляется разбором дерева выражения, а не `eval`: на вход приходит
текст от модели, и выполнять его как код нельзя ни при каких обстоятельствах.
Разрешены только имена фактов, числа и четыре действия.
"""

from __future__ import annotations

import ast
import re

from deckwright.schemas import ContentPack, Figure, FigureKind

# Насколько посчитанное может разойтись с написанным. Округление до одного
# знака — обычная практика в презентациях: 42/9 = 4.666… пишется как «4.7»
# или «4.6», и то и другое честно.
TOLERANCE = 0.06

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

_ALLOWED_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div)


class FormulaError(ValueError):
    """Формула не вычисляется по правилам."""


def parse_number(text: str) -> float | None:
    """Первое число в строке. «в 4.6 раза» → 4.6, «34 %» → 34."""
    found = _NUMBER.search(text.replace(" ", " "))
    return float(found.group(0).replace(",", ".")) if found else None


def numbers_in(text: str) -> set[float]:
    """Все числа строки. Нужно проверке «переписывание не ввело новых чисел».

    Запятая и точка считаются одним разделителем: «4,6» и «4.6» — одно число,
    написанное по-разному, и разница написания не должна выглядеть выдуманным
    фактом.
    """
    return {
        float(found.replace(",", "."))
        for found in _NUMBER.findall(text.replace("\u00a0", " "))
    }


def _evaluate(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, values)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        raise FormulaError(f"в формуле недопустимое значение {node.value!r}")
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"формула ссылается на неизвестный факт {node.id!r}")
        return values[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = _evaluate(node.operand, values)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINARY):
        left, right = _evaluate(node.left, values), _evaluate(node.right, values)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise FormulaError("деление на ноль")
        return left / right
    raise FormulaError(
        "в формуле разрешены только имена фактов, числа и четыре действия"
    )


def evaluate_formula(formula: str, values: dict[str, float]) -> float:
    """Вычисляет выражение над значениями фактов."""
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"формула {formula!r} не разбирается") from exc
    return _evaluate(tree, values)


def verify(figure: Figure, pack: ContentPack) -> str | None:
    """Проверяет число. Возвращает описание расхождения или None, если сошлось.

    Отсутствие числового значения у факта — не нарушение: не каждый факт
    числовой. Проверить такое число нечем, и проверка честно об этом молчит,
    вместо того чтобы объявить его выдумкой.
    """
    written = parse_number(figure.text)
    if written is None:
        return None

    facts = {fact.id: fact for fact in pack.facts}
    # Число с графика модель честно ссылает на ряд: «412 МВт·ч» — точка ряда
    # s1, а не факт. Ряд — такой же источник, его точки сверяются как значения.
    series = {item.id: [point.value for point in item.points] for item in pack.series}
    missing = [
        fact_id for fact_id in figure.fact_ids if fact_id not in facts and fact_id not in series
    ]
    if missing:
        return f"число {figure.text!r} ссылается на отсутствующие факты: {', '.join(missing)}"

    if figure.kind is FigureKind.CITED:
        cited = [
            value
            for fact_id in figure.fact_ids
            for value in (
                series[fact_id] if fact_id in series else [facts[fact_id].value]
            )
            if value is not None
        ]
        if not cited:
            return None
        # Знак — запись направления: «−23 %» на слайде и «снизилось на 23 %»
        # в факте — одно число. Сверка входа (`grounding`) знак тоже не читает.
        if not any(
            abs(abs(written) - abs(value)) <= abs(value) * TOLERANCE for value in cited
        ):
            return (
                f"число {figure.text!r} объявлено процитированным, но в фактах "
                f"{', '.join(figure.fact_ids)} таких значений нет: {cited}"
            )
        return None

    # Точки ряда в формулу входят числами («41 - 32»), имени у точки нет;
    # по имени подставляются только факты.
    fact_ids = [fact_id for fact_id in figure.fact_ids if fact_id in facts]
    values = {
        fact_id: facts[fact_id].value
        for fact_id in fact_ids
        if facts[fact_id].value is not None
    }
    if len(values) != len(set(fact_ids)):
        return (
            f"число {figure.text!r} выведено из фактов без числовых значений: "
            f"пересчитать нечем"
        )
    try:
        computed = evaluate_formula(figure.formula, values)
    except FormulaError as exc:
        return f"число {figure.text!r}: {exc}"

    if abs(written - computed) > max(abs(computed) * TOLERANCE, 0.05):
        return (
            f"число {figure.text!r} не сходится с формулой {figure.formula}: "
            f"посчитано {computed:.3g}"
        )
    return None
