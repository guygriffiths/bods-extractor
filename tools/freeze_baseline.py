"""Freeze the current database's answers so the rewrite can be diffed against it.

The pre-refactor ingester encodes a lot of hard-won, hand-tuned knowledge about
malformed BODS data. Rather than trust that a cleaner implementation reproduces
it, we snapshot concrete (origin stop, destination stop) -> fare answers from
the old database and replay them against the new one.

The snapshot is deliberately schema-independent: it records ATCO code pairs and
prices, not zone ids or operator folder names, because both of those change in
the new schema.

Usage::

    python tools/freeze_baseline.py --db data/output/fares.db \\
        --atco-prefix 4100 --out tests/baseline_4100.json

Then after re-ingesting::

    python tools/compare_baseline.py --db data/output/fares_v2.db \\
        --baseline tests/baseline_4100.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def freeze(db_path: Path, atco_prefix: str, per_operator: int, out_path: Path) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = conn.cursor()

    # Which publisher folders touch the area of interest.
    cur.execute(
        "SELECT DISTINCT operator_id FROM zones WHERE atco_code LIKE ?",
        (atco_prefix + "%",),
    )
    operators = [row[0] for row in cur.fetchall()]
    print(f"[i] {len(operators)} publisher folder(s) with {atco_prefix}* stops")

    # One representative ATCO per (operator, zone) so a zone pair becomes a
    # stop pair. MIN() keeps it deterministic across runs.
    print("[i] building zone -> representative stop map...")
    cur.execute(
        "SELECT operator_id, zone_id, MIN(atco_code) FROM zones "
        "WHERE atco_code LIKE ? GROUP BY operator_id, zone_id",
        (atco_prefix + "%",),
    )
    representative: dict[tuple[str, str], str] = {
        (op, zone): atco for op, zone, atco in cur.fetchall()
    }
    print(f"[i] {len(representative)} zones have a local stop")

    cases = []
    for operator in operators:
        cur.execute(
            """
            SELECT DISTINCT origin_zone, destination_zone, ticket_type,
                            product_name, price, commuter_score
            FROM fare_matrix
            WHERE operator_id = ? AND price >= 1.00
            ORDER BY origin_zone, destination_zone, ticket_type, price
            """,
            (operator,),
        )
        taken = 0
        for origin_zone, dest_zone, ticket_type, product, price, score in cur:
            origin_atco = representative.get((operator, origin_zone))
            dest_atco = representative.get((operator, dest_zone))
            if not origin_atco or not dest_atco or origin_atco == dest_atco:
                continue
            cases.append({
                "legacy_operator_folder": operator,
                "origin_atco": origin_atco,
                "destination_atco": dest_atco,
                "legacy_ticket_type": ticket_type,
                "legacy_product_name": product,
                "legacy_price": price,
                "legacy_commuter_score": score,
            })
            taken += 1
            if taken >= per_operator:
                break
        print(f"    {operator}: {taken} case(s)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "source_db": str(db_path),
        "atco_prefix": atco_prefix,
        "schema": "legacy",
        "case_count": len(cases),
        "cases": cases,
    }, indent=1))
    print(f"\n[i] wrote {len(cases)} baseline case(s) to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("data/output/fares.db"))
    parser.add_argument("--atco-prefix", default="4100")
    parser.add_argument("--per-operator", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path("tests/baseline_4100.json"))
    args = parser.parse_args()
    freeze(args.db, args.atco_prefix, args.per_operator, args.out)


if __name__ == "__main__":
    main()
