#!/usr/bin/env python3
"""Offline counterfactual replay for the planner-social plugin.

This is not a live A/B test. It reads the plugin's decision log and reports how
many decisions would have crossed a given utility threshold, alongside what the
Planner actually chose. Use it to compare parameter sets on the same history.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="planner_social.sqlite3")
    parser.add_argument("--threshold", type=float, default=0.38)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    connection = sqlite3.connect(args.db)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT action, utility, threshold, forced, reward, addressed FROM decisions "
        "ORDER BY created_at DESC LIMIT ?",
        (max(1, args.limit),),
    ).fetchall()
    connection.close()
    total = len(rows)
    actual_replies = sum(1 for row in rows if row["action"] == "reply")
    forced = sum(1 for row in rows if row["forced"])
    negatives = sum(1 for row in rows if row["reward"] == -1)
    addressed_replies = sum(1 for row in rows if row["action"] == "reply" and row["addressed"])
    above = [row for row in rows if float(row["utility"] or 0) >= args.threshold]
    report = {
        "db": str(args.db),
        "sample": total,
        "threshold": args.threshold,
        "actual": {
            "replies": actual_replies,
            "reply_rate": round(actual_replies / total, 4) if total else 0,
            "forced": forced,
            "negative": negatives,
            "negative_rate": round(negatives / max(actual_replies, 1), 4),
            "addressed_replies": addressed_replies,
        },
        "counterfactual": {
            "would_reply": len(above),
            "would_reply_rate": round(len(above) / total, 4) if total else 0,
            "would_force": sum(1 for row in above if row["action"] in {"stop", "wait"}),
        },
        "caveat": "Offline counterfactual on logged utilities; not a live A/B and not causal.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
