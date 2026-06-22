from __future__ import annotations

import ast
import multiprocessing
import queue
import time
from dataclasses import dataclass
from typing import Any

from bot.arena.signals import NormalizedSignal
from bot.models import PositionDirection, TradePlan


@dataclass(frozen=True, slots=True)
class ValidationResult:
    is_valid: bool
    errors: list[str]


@dataclass(frozen=True, slots=True)
class ProgramExecutionResult:
    success: bool
    signal: NormalizedSignal | None = None
    trade_plan: TradePlan | None = None
    error: str | None = None
    execution_time_ms: int = 0


_FORBIDDEN_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.With,
    ast.AsyncWith,
    ast.ClassDef,
    ast.Lambda,
    ast.Try,
    ast.Raise,
    ast.Delete,
)
_FORBIDDEN_CALLS = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "globals",
    "locals",
    "open",
    "input",
    "breakpoint",
}
_SAFE_BUILTINS = {
    "abs": abs,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "round": round,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}


def _execute_strategy_in_child(code: str, context: dict[str, Any], result_queue: multiprocessing.Queue) -> None:
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    try:
        compiled = compile(code, "<program_trader>", "exec")
        exec(compiled, namespace, namespace)
        build_signal = namespace.get("build_signal")
        if not callable(build_signal):
            result_queue.put({"success": False, "error": "build_signal(context) is not callable"})
            return
        result_queue.put({"success": True, "raw": build_signal(dict(context))})
    except Exception as exc:
        result_queue.put({"success": False, "error": f"{type(exc).__name__}: {exc}"})


def validate_strategy_code(code: str) -> ValidationResult:
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(False, [f"SyntaxError: {exc.msg}"])

    has_build_signal = False
    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            errors.append(f"{type(node).__name__} is not allowed in program trader strategies")
        if isinstance(node, ast.FunctionDef) and node.name == "build_signal":
            has_build_signal = True
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _FORBIDDEN_CALLS or name.startswith("__"):
                errors.append(f"Call to {name} is not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append(f"Dunder attribute {node.attr} is not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            errors.append(f"Dunder name {node.id} is not allowed")
    if not has_build_signal:
        errors.append("Strategy must define build_signal(context)")
    return ValidationResult(is_valid=not errors, errors=errors)


class ProgramStrategyRunner:
    def __init__(self, timeout_seconds: float = 1.0) -> None:
        self._timeout_seconds = timeout_seconds

    def run_signal(self, code: str, context: dict[str, Any]) -> ProgramExecutionResult:
        return self.run(code, context)

    def run(self, code: str, context: dict[str, Any]) -> ProgramExecutionResult:
        validation = validate_strategy_code(code)
        if not validation.is_valid:
            return ProgramExecutionResult(False, error="Validation failed: " + "; ".join(validation.errors))

        started = time.perf_counter()
        process_context = multiprocessing.get_context("spawn")
        result_queue = process_context.Queue(maxsize=1)
        process = process_context.Process(
            target=_execute_strategy_in_child,
            args=(code, dict(context), result_queue),
        )
        process.start()
        process.join(timeout=self._timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.2)
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)
            return ProgramExecutionResult(False, error=f"Strategy timed out after {self._timeout_seconds:.2f}s")
        try:
            child_result = result_queue.get_nowait()
        except queue.Empty:
            return ProgramExecutionResult(False, error="Strategy exited without returning a result")
        if not child_result.get("success"):
            return ProgramExecutionResult(False, error=str(child_result.get("error") or "Strategy failed"))
        raw = child_result.get("raw")

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            if isinstance(raw, NormalizedSignal):
                return ProgramExecutionResult(True, signal=raw, execution_time_ms=elapsed_ms)
            if isinstance(raw, TradePlan):
                _validate_trade_plan(raw)
                return ProgramExecutionResult(True, trade_plan=raw, execution_time_ms=elapsed_ms)
            if isinstance(raw, dict) and _looks_like_trade_plan(raw):
                plan = _trade_plan_from_dict(raw)
                _validate_trade_plan(plan)
                return ProgramExecutionResult(True, trade_plan=plan, execution_time_ms=elapsed_ms)
            signal = NormalizedSignal.model_validate(raw)
            return ProgramExecutionResult(True, signal=signal, execution_time_ms=elapsed_ms)
        except Exception as exc:
            return ProgramExecutionResult(False, error=f"{type(exc).__name__}: {exc}", execution_time_ms=elapsed_ms)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _looks_like_trade_plan(raw: dict[str, Any]) -> bool:
    return {"symbol", "direction", "usdt_size", "leverage"}.issubset(raw.keys())


def _trade_plan_from_dict(raw: dict[str, Any]) -> TradePlan:
    direction_raw = raw.get("direction")
    direction = direction_raw if isinstance(direction_raw, PositionDirection) else PositionDirection(str(direction_raw))
    plan = TradePlan(
        symbol=str(raw["symbol"]),
        direction=direction,
        usdt_size=float(raw["usdt_size"]),
        leverage=int(raw["leverage"]),
        stop_loss_pct=float(raw.get("stop_loss_pct") or 0.0),
        take_profit_pct=float(raw.get("take_profit_pct") or 0.0),
        decision_reason=str(raw.get("decision_reason") or "program_trader"),
        decision_payload=raw.get("decision_payload") if isinstance(raw.get("decision_payload"), dict) else None,
        confidence=float(raw.get("confidence") or 0.0),
        setup_score=float(raw.get("setup_score") or 0.0),
        max_hold_minutes=int(raw["max_hold_minutes"]) if raw.get("max_hold_minutes") is not None else None,
    )
    return plan


def _validate_trade_plan(plan: TradePlan) -> None:
    if plan.stop_loss_pct <= 0 or plan.take_profit_pct <= 0:
        raise ValueError("TradePlan requires positive stop_loss_pct and take_profit_pct")
    if plan.usdt_size <= 0:
        raise ValueError("TradePlan requires positive usdt_size")
    if plan.leverage <= 0:
        raise ValueError("TradePlan requires positive leverage")
