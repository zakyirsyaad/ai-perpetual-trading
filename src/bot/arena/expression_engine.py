from __future__ import annotations

import ast
import math
from statistics import mean, median, pstdev
from typing import Any, Callable


Number = int | float


class FactorExpressionEngine:
    """Safe factor expression evaluator with 69 whitelisted functions."""

    def __init__(self) -> None:
        self._functions = _build_functions()

    def function_names(self) -> list[str]:
        return sorted(self._functions)

    def evaluate(self, expression: str, context: dict[str, Any]) -> float:
        tree = ast.parse(expression, mode="eval")
        _validate_ast(tree)
        env = {**self._functions, **context}
        value = eval(compile(tree, "<factor_expression>", "eval"), {"__builtins__": {}}, env)
        return _last_number(value)


def _validate_ast(tree: ast.AST) -> None:
    allowed = (
        ast.Expression,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.UnaryOp,
        ast.BinOp,
        ast.BoolOp,
        ast.Compare,
        ast.IfExp,
        ast.List,
        ast.Tuple,
        ast.Subscript,
        ast.Slice,
        ast.And,
        ast.Or,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f"Expression node {type(node).__name__} is not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"Name {node.id} is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError("Only direct function calls are allowed")
            if node.func.id.startswith("__"):
                raise ValueError(f"Function {node.func.id} is not allowed")


def _build_functions() -> dict[str, Callable[..., Any]]:
    funcs: dict[str, Callable[..., Any]] = {
        "abs": abs,
        "add": lambda a, b: _last_number(a) + _last_number(b),
        "sub": lambda a, b: _last_number(a) - _last_number(b),
        "mul": lambda a, b: _last_number(a) * _last_number(b),
        "div": lambda a, b: _last_number(a) / _guard_zero(_last_number(b)),
        "neg": lambda a: -_last_number(a),
        "sqrt": lambda a: math.sqrt(max(_last_number(a), 0.0)),
        "log": lambda a: math.log(max(_last_number(a), 1e-12)),
        "exp": lambda a: math.exp(_clamp(_last_number(a), -50, 50)),
        "pow": lambda a, b: _last_number(a) ** _last_number(b),
        "min": lambda *a: min(_last_number(x) for x in a),
        "max": lambda *a: max(_last_number(x) for x in a),
        "clip": lambda a, lo, hi: _clamp(_last_number(a), _last_number(lo), _last_number(hi)),
        "scale": lambda a, lo, hi: _scale(_last_number(a), _last_number(lo), _last_number(hi)),
        "sign": lambda a: 1.0 if _last_number(a) > 0 else -1.0 if _last_number(a) < 0 else 0.0,
        "gt": lambda a, b: 1.0 if _last_number(a) > _last_number(b) else 0.0,
        "gte": lambda a, b: 1.0 if _last_number(a) >= _last_number(b) else 0.0,
        "lt": lambda a, b: 1.0 if _last_number(a) < _last_number(b) else 0.0,
        "lte": lambda a, b: 1.0 if _last_number(a) <= _last_number(b) else 0.0,
        "eq": lambda a, b: 1.0 if _last_number(a) == _last_number(b) else 0.0,
        "and_": lambda a, b: 1.0 if _last_number(a) and _last_number(b) else 0.0,
        "or_": lambda a, b: 1.0 if _last_number(a) or _last_number(b) else 0.0,
        "not_": lambda a: 0.0 if _last_number(a) else 1.0,
        "last": _last_number,
        "first": lambda a: _series(a)[0] if _series(a) else 0.0,
        "mean": lambda a: mean(_series(a)) if _series(a) else 0.0,
        "median": lambda a: median(_series(a)) if _series(a) else 0.0,
        "sum": lambda a: sum(_series(a)),
        "std": lambda a: pstdev(_series(a)) if len(_series(a)) > 1 else 0.0,
        "var": lambda a: (pstdev(_series(a)) ** 2) if len(_series(a)) > 1 else 0.0,
        "min_series": lambda a: min(_series(a)) if _series(a) else 0.0,
        "max_series": lambda a: max(_series(a)) if _series(a) else 0.0,
        "range": lambda a: (max(_series(a)) - min(_series(a))) if _series(a) else 0.0,
        "diff": lambda a, n=1: _last_number(a) - _lag_value(a, int(_last_number(n))),
        "lag": lambda a, n=1: _lag_value(a, int(_last_number(n))),
        "pct_change": lambda a, n=1: _pct_change(a, int(_last_number(n))),
        "returns": lambda a: _returns(a),
        "roc": lambda a, n=1: _pct_change(a, int(_last_number(n))),
        "sma": lambda a, n=5: mean(_tail(a, int(_last_number(n)))) if _tail(a, int(_last_number(n))) else 0.0,
        "ema": lambda a, n=5: _ema(a, int(_last_number(n))),
        "zscore": lambda a: _zscore(a),
        "rank": lambda a: _rank_last(a),
        "percentile": lambda a, p=0.5: _percentile(_series(a), _last_number(p)),
        "quantile": lambda a, q=0.5: _percentile(_series(a), _last_number(q)),
        "corr": lambda a, b: _corr(_series(a), _series(b)),
        "cov": lambda a, b: _cov(_series(a), _series(b)),
        "skew": lambda a: _moment(_series(a), 3),
        "kurt": lambda a: _moment(_series(a), 4),
        "mad": lambda a: mean([abs(x - mean(_series(a))) for x in _series(a)]) if _series(a) else 0.0,
        "tr": lambda h, l, c: abs(_last_number(h) - _last_number(l)) + abs(_last_number(c) - _last_number(l)),
        "atr": lambda h, l, c, n=14: mean([abs(x) for x in _tail(_series(h), int(_last_number(n)))]) if _series(h) else 0.0,
        "rsi": lambda a, n=14: _rsi(a, int(_last_number(n))),
        "macd": lambda a: _ema(a, 12) - _ema(a, 26),
        "bb_width": lambda a, n=20: _bb_width(a, int(_last_number(n))),
        "cross_up": lambda a, b: 1.0 if _lag_value(a, 1) <= _lag_value(b, 1) and _last_number(a) > _last_number(b) else 0.0,
        "cross_down": lambda a, b: 1.0 if _lag_value(a, 1) >= _lag_value(b, 1) and _last_number(a) < _last_number(b) else 0.0,
        "slope": lambda a, n=5: (_last_number(a) - _lag_value(a, int(_last_number(n)))) / max(int(_last_number(n)), 1),
        "accel": lambda a: _last_number(a) - (2 * _lag_value(a, 1)) + _lag_value(a, 2),
        "decay": lambda a, half=5: _last_number(a) * (0.5 ** (1.0 / max(_last_number(half), 1.0))),
        "winsor": lambda a, lo=-3, hi=3: _clamp(_last_number(a), _last_number(lo), _last_number(hi)),
        "neutralize": lambda a, b: _last_number(a) - _last_number(b),
        "safe_div": lambda a, b: _last_number(a) / _guard_zero(_last_number(b)),
        "imbalance": lambda bid, ask: (_last_number(bid) - _last_number(ask)) / _guard_zero(_last_number(bid) + _last_number(ask)),
        "spread_bps": lambda bid, ask: ((_last_number(ask) - _last_number(bid)) / _guard_zero((_last_number(ask) + _last_number(bid)) / 2.0)) * 10000,
        "fee_adjust": lambda edge, fee: _last_number(edge) - _last_number(fee),
        "breakeven_long": lambda entry, fee: _last_number(entry) * (1 + _last_number(fee)) / _guard_zero(1 - _last_number(fee)),
        "breakeven_short": lambda entry, fee: _last_number(entry) * (1 - _last_number(fee)) / (1 + _last_number(fee)),
        "ifelse": lambda cond, a, b: _last_number(a) if _last_number(cond) else _last_number(b),
        "isfinite": lambda a: 1.0 if math.isfinite(_last_number(a)) else 0.0,
    }
    if len(funcs) != 69:
        raise RuntimeError(f"Expected 69 functions, got {len(funcs)}")
    return funcs


def _series(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    return [float(value)]


def _last_number(value: Any) -> float:
    series = _series(value)
    return float(series[-1]) if series else 0.0


def _tail(value: Any, n: int) -> list[float]:
    series = _series(value)
    return series[-max(n, 1):]


def _lag_value(value: Any, n: int) -> float:
    series = _series(value)
    if not series:
        return 0.0
    index = max(len(series) - 1 - max(n, 0), 0)
    return series[index]


def _pct_change(value: Any, n: int) -> float:
    current = _last_number(value)
    previous = _lag_value(value, n)
    return ((current - previous) / _guard_zero(previous)) * 100.0


def _returns(value: Any) -> list[float]:
    series = _series(value)
    return [((series[i] - series[i - 1]) / _guard_zero(series[i - 1])) * 100.0 for i in range(1, len(series))]


def _ema(value: Any, n: int) -> float:
    series = _series(value)
    if not series:
        return 0.0
    alpha = 2.0 / (max(n, 1) + 1.0)
    out = series[0]
    for item in series[1:]:
        out = (item * alpha) + (out * (1.0 - alpha))
    return out


def _zscore(value: Any) -> float:
    series = _series(value)
    if len(series) < 2:
        return 0.0
    std = pstdev(series)
    return (_last_number(series) - mean(series)) / _guard_zero(std)


def _rank_last(value: Any) -> float:
    series = _series(value)
    if len(series) <= 1:
        return 0.5
    ordered = sorted(series)
    return ordered.index(_last_number(series)) / (len(ordered) - 1)


def _percentile(series: list[float], q: float) -> float:
    if not series:
        return 0.0
    q = _clamp(q, 0.0, 1.0)
    ordered = sorted(series)
    idx = q * (len(ordered) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] + ((ordered[hi] - ordered[lo]) * (idx - lo))


def _corr(left: list[float], right: list[float]) -> float:
    n = min(len(left), len(right))
    if n < 2:
        return 0.0
    left = left[-n:]
    right = right[-n:]
    avg_l = mean(left)
    avg_r = mean(right)
    den_l = math.sqrt(sum((x - avg_l) ** 2 for x in left))
    den_r = math.sqrt(sum((x - avg_r) ** 2 for x in right))
    if den_l <= 0 or den_r <= 0:
        return 0.0
    return sum((x - avg_l) * (y - avg_r) for x, y in zip(left, right)) / (den_l * den_r)


def _cov(left: list[float], right: list[float]) -> float:
    n = min(len(left), len(right))
    if n < 2:
        return 0.0
    left = left[-n:]
    right = right[-n:]
    avg_l = mean(left)
    avg_r = mean(right)
    return sum((x - avg_l) * (y - avg_r) for x, y in zip(left, right)) / n


def _moment(series: list[float], power: int) -> float:
    if len(series) < 2:
        return 0.0
    avg = mean(series)
    std = _guard_zero(pstdev(series))
    return mean([((item - avg) / std) ** power for item in series])


def _rsi(value: Any, n: int) -> float:
    changes = [b - a for a, b in zip(_series(value), _series(value)[1:])][-max(n, 1):]
    gains = [max(item, 0.0) for item in changes]
    losses = [abs(min(item, 0.0)) for item in changes]
    if not changes:
        return 50.0
    rs = mean(gains) / _guard_zero(mean(losses))
    return 100.0 - (100.0 / (1.0 + rs))


def _bb_width(value: Any, n: int) -> float:
    tail = _tail(value, n)
    if len(tail) < 2:
        return 0.0
    mid = mean(tail)
    return (4.0 * pstdev(tail) / _guard_zero(mid)) * 100.0


def _scale(value: float, low: float, high: float) -> float:
    if high == low:
        return 0.0
    return _clamp(((value - low) / (high - low) * 2.0) - 1.0)


def _guard_zero(value: float) -> float:
    if abs(value) < 1e-12:
        return 1e-12
    return value


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
