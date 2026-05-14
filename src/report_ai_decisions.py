from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize trading performance grouped by AI/rule entry reason."
    )
    parser.add_argument(
        "--db",
        default="bot_data/trades.db",
        help="Path to SQLite database (default: bot_data/trades.db)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum number of grouped reasons to display.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(closed_trades)")
    columns = {str(row[1]) for row in cur.fetchall()}
    has_entry_reason = "entry_reason" in columns

    reason_expr = (
        "COALESCE(NULLIF(entry_reason, ''), 'unknown')"
        if has_entry_reason
        else "'legacy_no_reason'"
    )

    cur.execute(
        f"""
        SELECT
            {reason_expr} AS decision_source,
            COUNT(*) AS trades,
            SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN pnl_usdt = 0 THEN 1 ELSE 0 END) AS breakeven,
            ROUND(SUM(pnl_usdt), 4) AS total_pnl,
            ROUND(AVG(pnl_usdt), 4) AS avg_pnl
        FROM closed_trades
        GROUP BY decision_source
        ORDER BY trades DESC
        LIMIT ?
        """,
        (args.limit,),
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT
            COUNT(*) AS total_trades,
            ROUND(SUM(pnl_usdt), 4) AS total_pnl,
            ROUND(AVG(pnl_usdt), 4) AS avg_pnl
        FROM closed_trades
        """
    )
    total = cur.fetchone()
    conn.close()

    total_trades = int(total[0] or 0)
    print("")
    print("===== AI / Entry Reason Report =====")
    print(f"Database: {db_path}")
    print(f"Total trades: {total_trades}")
    print(f"Total PnL:    {float(total[1] or 0):.4f} USDT")
    print(f"Average PnL:  {float(total[2] or 0):.4f} USDT")
    print("")
    print(
        "Reason | Trades | Wins | Losses | Breakeven | Win rate | Total PnL | Avg PnL"
    )
    print("-" * 95)

    for reason, trades, wins, losses, breakeven, total_pnl, avg_pnl in rows:
        win_rate = (wins / trades * 100) if trades else 0
        print(
            f"{reason} | {trades} | {wins} | {losses} | {breakeven} | "
            f"{win_rate:.2f}% | {float(total_pnl or 0):.4f} | {float(avg_pnl or 0):.4f}"
        )

    if not rows:
        print("No closed trades found.")
    print("====================================")
    print("")


if __name__ == "__main__":
    main()

