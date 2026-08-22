from __future__ import annotations

from collections import defaultdict
from typing import Any


def build_trade_decision_memory(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize realized trades into compact decision feedback for the AI.

    This is intentionally advisory only. It does not create code-side gates; the
    AI receives the lessons and remains responsible for the next trade decision.
    """

    if not records:
        return {
            "sample_size": 0,
            "net_pnl_usdt": 0.0,
            "gross_pnl_usdt": 0.0,
            "fee_impact_usdt": 0.0,
            "lessons": [
                "No realized trade memory yet; rely on current live exchange technicals and fee-aware setup quality.",
            ],
            "winning_patterns": [],
            "losing_patterns": [],
        }

    by_reason: dict[str, dict[str, Any]] = defaultdict(_empty_stats)
    by_direction: dict[str, dict[str, Any]] = defaultdict(_empty_stats)
    by_confidence: dict[str, dict[str, Any]] = defaultdict(_empty_stats)
    examples: dict[str, list[str]] = defaultdict(list)

    total_net = 0.0
    total_gross = 0.0
    for row in records:
        net = _float(row.get("pnl_usdt"))
        gross = _gross_pnl(row)
        total_net += net
        total_gross += gross

        reason = _reason_bucket(row.get("reason"))
        _add_stat(by_reason[reason], net, gross, row)
        _add_stat(by_direction[str(row.get("direction") or "unknown")], net, gross, row)
        _add_stat(by_confidence[_confidence_band(row.get("confidence"))], net, gross, row)

        if len(examples[reason]) < 3:
            examples[reason].append(_entry_note(row.get("entry_reason")))

    losing_patterns = _top_patterns(by_reason, positive=False, examples=examples)
    winning_patterns = _top_patterns(by_reason, positive=True, examples=examples)
    lessons = _lessons(losing_patterns=losing_patterns, winning_patterns=winning_patterns, net_pnl=total_net)

    return {
        "sample_size": len(records),
        "net_pnl_usdt": round(total_net, 4),
        "gross_pnl_usdt": round(total_gross, 4),
        "fee_impact_usdt": round(total_gross - total_net, 4),
        "lessons": lessons,
        "winning_patterns": winning_patterns,
        "losing_patterns": losing_patterns,
        "direction_performance": _compact_bucket_dict(by_direction),
        "confidence_performance": _compact_bucket_dict(by_confidence),
    }


def _empty_stats() -> dict[str, Any]:
    return {"trades": 0, "wins": 0, "losses": 0, "net": 0.0, "gross": 0.0, "volume": 0.0}


def _add_stat(target: dict[str, Any], net: float, gross: float, row: dict[str, Any]) -> None:
    target["trades"] += 1
    target["net"] += net
    target["gross"] += gross
    target["volume"] += _volume(row)
    if net > 0:
        target["wins"] += 1
    elif net < 0:
        target["losses"] += 1


def _top_patterns(
    buckets: dict[str, dict[str, Any]],
    *,
    positive: bool,
    examples: dict[str, list[str]],
) -> list[dict[str, Any]]:
    items = [
        (key, stats)
        for key, stats in buckets.items()
        if (stats["net"] > 0 if positive else stats["net"] < 0)
    ]
    items.sort(key=lambda item: item[1]["net"], reverse=positive)
    result = []
    for key, stats in items[:4]:
        result.append(
            {
                "pattern": key,
                "trades": stats["trades"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "gross_pnl_usdt": round(stats["gross"], 4),
                "fee_impact_usdt": round(stats["gross"] - stats["net"], 4),
                "net_pnl_usdt": round(stats["net"], 4),
                "avg_net_usdt": round(stats["net"] / stats["trades"], 4) if stats["trades"] else 0.0,
                "example_entry_notes": [note for note in examples.get(key, []) if note],
            }
        )
    return result


def _lessons(
    *,
    losing_patterns: list[dict[str, Any]],
    winning_patterns: list[dict[str, Any]],
    net_pnl: float,
) -> list[str]:
    lessons: list[str] = []
    if net_pnl < 0:
        lessons.append(
            "Recent net PnL is negative; prefer fewer, cleaner entries whose expected gross move clearly beats round-trip taker fees."
        )
    for pattern in losing_patterns:
        name = str(pattern["pattern"])
        if name == "stop_loss":
            lessons.append(
                "Recent stop-loss trades are the largest drag; avoid entries that chase exhausted momentum, extreme RSI, or conflicting timeframes."
            )
        elif name == "liquidation_guard":
            lessons.append(
                "Recent liquidation-guard exits are costly; avoid setups with tight liquidation distance or noise larger than the planned stop."
            )
        elif name == "strategy_exit":
            lessons.append(
                "Recent strategy exits show setup decay; require stronger agreement between trend, momentum, and order-book flow before entry."
            )
        else:
            lessons.append(f"Recent {name} exits underperformed; avoid repeating similar entry notes unless current edge is stronger.")
    for pattern in winning_patterns[:2]:
        lessons.append(
            f"Recent {pattern['pattern']} exits performed well; favor setups with similar fee-covering move potential and clean confirmation."
        )
    return _dedupe(lessons)[:6]


def _compact_bucket_dict(buckets: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "net_pnl_usdt": round(stats["net"], 4),
            "avg_net_usdt": round(stats["net"] / stats["trades"], 4) if stats["trades"] else 0.0,
        }
        for key, stats in sorted(buckets.items(), key=lambda item: item[1]["net"])
    }


def _gross_pnl(row: dict[str, Any]) -> float:
    quantity = _float(row.get("quantity"))
    entry = _float(row.get("entry_price"))
    exit_price = _float(row.get("exit_price"))
    direction = str(row.get("direction") or "").upper()
    if direction == "LONG":
        return (exit_price - entry) * quantity
    return (entry - exit_price) * quantity


def _volume(row: dict[str, Any]) -> float:
    quantity = _float(row.get("quantity"))
    return (_float(row.get("entry_price")) * quantity) + (_float(row.get("exit_price")) * quantity)


def _reason_bucket(raw: Any) -> str:
    reason = str(raw or "unknown")
    if reason.startswith("strategy_exit"):
        return "strategy_exit"
    return reason


def _confidence_band(raw: Any) -> str:
    value = max(0.0, min(1.0, _float(raw)))
    if value < 0.4:
        return "0.00-0.40"
    if value < 0.6:
        return "0.40-0.60"
    if value < 0.8:
        return "0.60-0.80"
    return "0.80-1.00"


def _entry_note(raw: Any) -> str:
    text = str(raw or "").strip()
    marker = " note="
    if marker in text:
        text = text.split(marker, 1)[1]
    return text[:220]


def _float(raw: Any) -> float:
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
