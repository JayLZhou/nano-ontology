"""A tiny, safe expression language for ontology rules and effects.

Expressions look like Python but are *never* passed to ``eval``. They are parsed
with :mod:`ast`, checked against a whitelist of node types, and interpreted by a
small tree walker. Attribute access only reaches dict keys or objects that
implement ``onto_get`` -- never real Python attributes -- so an expression
cannot escape into the host process.

Examples::

    params.amount > 50000
    params.supplier.blacklisted
    params.po.requester != actor.id and actor.role != 'cfo'
    params.supplier.rating in ['A', 'B']

A missing value is ``None``. Arithmetic with ``None`` gives ``None`` and an
ordering comparison with ``None`` is false, so ``params.po.supplier.rating > 3``
does not fire when there is no supplier. Where that matters, say so explicitly:
``params.po.supplier == None or params.po.supplier.rating > 3``.
"""

from __future__ import annotations

import ast
import math
import operator
from functools import lru_cache
from typing import Any, Iterable


class ExprError(ValueError):
    """Raised for invalid expressions or evaluation failures."""


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _upper(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value


def _today() -> str:
    """Today's date as ``YYYY-MM-DD`` (override with $NANO_ONTO_TODAY for tests and replays)."""
    import os
    from datetime import date

    return os.environ.get("NANO_ONTO_TODAY") or date.today().isoformat()


FUNCTIONS: dict[str, Any] = {
    "today": _today,
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "lower": _lower,
    "upper": _upper,
}

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.IfExp,
    ast.Call,
    *_BINOPS.keys(),
    *_CMPOPS.keys(),
)


@lru_cache(maxsize=4096)
def parse(source: str) -> ast.Expression:
    """Parse and whitelist-check an expression. Cached."""
    if not isinstance(source, str) or not source.strip():
        raise ExprError(f"expression must be a non-empty string, got {source!r}")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:  # pragma: no cover - message varies by version
        raise ExprError(f"syntax error in {source!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(f"{type(node).__name__} is not allowed in {source!r}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ExprError(f"name {node.id!r} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError(f"attribute {node.attr!r} is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExprError(
                    f"only these functions may be called: {', '.join(sorted(FUNCTIONS))}"
                )
            if node.keywords:
                raise ExprError("keyword arguments are not allowed")
    return tree


def validate(source: str) -> None:
    """Raise :class:`ExprError` if ``source`` is not a valid expression."""
    parse(source)


def evaluate(source: str, env: dict[str, Any]) -> Any:
    """Evaluate ``source`` against ``env`` (a mapping of root names)."""
    return _eval(parse(source).body, env)


def truthy(source: str, env: dict[str, Any]) -> bool:
    return bool(evaluate(source, env))


def get_member(base: Any, name: str) -> Any:
    """Member access used by the interpreter. Missing members yield ``None``."""
    if base is None:
        return None
    if isinstance(base, dict):
        return base.get(name)
    getter = getattr(type(base), "onto_get", None)
    if getter is not None:
        return getter(base, name)
    raise ExprError(f"cannot read {name!r} from a {type(base).__name__}")


def _eval(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise ExprError(f"unknown name {node.id!r}")
        return env[node.id]
    if isinstance(node, ast.Attribute):
        return get_member(_eval(node.value, env), node.attr)
    if isinstance(node, ast.Subscript):
        base = _eval(node.value, env)
        key = _eval(node.slice, env)
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(key)
        if isinstance(base, (list, tuple, str)) and isinstance(key, int):
            return base[key] if -len(base) <= key < len(base) else None
        return get_member(base, key) if isinstance(key, str) else None
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            value: Any = True
            for item in node.values:
                value = _eval(item, env)
                if not value:
                    return value
            return value
        value = False
        for item in node.values:
            value = _eval(item, env)
            if value:
                return value
        return value
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not operand
        if operand is None:
            return None
        try:
            return -operand if isinstance(node.op, ast.USub) else +operand
        except TypeError as exc:
            raise ExprError(str(exc)) from None
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, env), _eval(node.right, env)
        if left is None or right is None:
            return None
        numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right))
        joins = isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str)
        if not (numbers or joins):  # no 'x' * 10**9, no list arithmetic
            raise ExprError(f"cannot apply {type(node.op).__name__} to {left!r} and {right!r}")
        try:
            result = _BINOPS[type(node.op)](left, right)
        except (ZeroDivisionError, OverflowError) as exc:
            raise ExprError(str(exc)) from None
        if isinstance(result, float) and not math.isfinite(result):  # inf - inf = NaN, which no rule catches
            raise ExprError(f"arithmetic overflow in {type(node.op).__name__}")
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, env)
            if isinstance(op, _ORDERING) and (left is None or right is None):
                return False
            if isinstance(op, (ast.In, ast.NotIn)) and right is None:
                return isinstance(op, ast.NotIn)
            try:
                ok = _CMPOPS[type(op)](left, right)
            except TypeError as exc:
                raise ExprError(f"cannot compare {left!r} and {right!r}: {exc}") from None
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(item, env) for item in node.elts]
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, ast.Call):
        func = FUNCTIONS[node.func.id]  # type: ignore[union-attr]
        args = [_eval(arg, env) for arg in node.args]
        try:
            return func(*args)
        except (TypeError, ValueError) as exc:
            raise ExprError(str(exc)) from None
    raise ExprError(f"unsupported expression node {type(node).__name__}")  # pragma: no cover


def references(source: str) -> set[tuple[str, ...]]:
    """Return the dotted member paths an expression reads.

    ``params.po.requester != actor.id`` -> ``{("params","po","requester"), ("actor","id")}``
    """
    tree = parse(source)
    found: set[tuple[str, ...]] = set()

    def chain(node: ast.AST) -> tuple[str, ...] | None:
        parts: list[str] = []
        while True:
            if isinstance(node, ast.Attribute):
                parts.append(node.attr)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str):
                parts.append(node.slice.value)  # x['name'] reads the same member as x.name
            else:
                break
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return tuple(reversed(parts))
        return None

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.Attribute, ast.Name, ast.Subscript)):
            path = chain(node)
            if path is not None:
                found.add(path)
                return
        if isinstance(node, ast.Call):
            for arg in node.args:
                visit(arg)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree.body)
    return found


def root_names(paths: Iterable[tuple[str, ...]]) -> set[str]:
    return {path[0] for path in paths}
