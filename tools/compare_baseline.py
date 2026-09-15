"""Replay frozen baseline cases against a rebuilt database and diff the answers.

Companion to ``tools/freeze_baseline.py``. The baseline records concrete
(origin stop, destination stop) -> price answers produced by the previous
implementation. This script asks the new database the same questions and
reports where the two disagree.

The point is *not* that the old answers were right. Some of them were known to
be wrong - that is why the rewrite happened. The point is that no difference
should go unnoticed and unexplained.

Usage::

    python tools/compare_baseline.py --db data/output/fares_v2.db \\
        --baseline tests/baseline_4100.json --show 20
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bods_extractor import heuristics  # noqa: E402
from bods_extractor.fares import FareLookup  # noqa: E402

#: Prices within this many pounds are treated as agreeing. Covers rounding
#: differences between a price read as pence and one read as pounds.
PRICE_TOLERANCE = 0.005

#: The legacy schema's ticket_type vocabulary mapped onto the new product_type
#: vocabulary, so a naming change is not reported as a fare change.
LEGACY_TICKET_TYPES = {
    "single": "single",
    "return": "day_return",
    "day": "period",
    "day_ticket": "period",
    "period": "period",
    "weekly": "period",
    "unknown": None,
}


def _explain(case, quote) -> str:
    """Attribute a divergence to a cause, so it can be reviewed not just counted.

    The dominant cause is known and expected: the previous pipeline had no
    concept of passenger class, so a product named ``Trip@U22SGL`` (an under-22
    single) was recorded as a plain "Single" and compared against the adult
    fare. Those rows are corrections, not regressions.
    """
    legacy_name = case.get("legacy_product_name") or ""
    implied = heuristics.guess_user_type(legacy_name)
    if implied and implied != "adult":
        return f"legacy row was a {implied} fare recorded as adult"
    if quote is None:
        return "no fare in new database"
    if quote.price > case["legacy_price"]:
        return "new price higher"
    return "new price lower"


def compare(db_path: Path, baseline_path: Path, show: int, user_type: str) -> int:
    baseline = json.loads(baseline_path.read_text())
    cases = baseline["cases"]
    print(f"[i] replaying {len(cases)} case(s) from {baseline_path}")
    print(f"[i] against {db_path}\n")

    outcomes = Counter()
    causes = Counter()
    divergences = []

    with FareLookup(db_path) as lookup:
        for case in cases:
            quotes = lookup.fares_between(
                case["origin_atco"], case["destination_atco"], user_type=user_type)

            if not quotes:
                outcomes["lost: no fare in new db"] += 1
                causes[_explain(case, None)] += 1
                divergences.append((case, None, "no fare found"))
                continue

            legacy_price = case["legacy_price"]
            wanted = LEGACY_TICKET_TYPES.get(
                (case["legacy_ticket_type"] or "").lower())
            candidates = [q for q in quotes if q.product_type == wanted] or quotes

            exact = [q for q in candidates
                     if abs(q.price - legacy_price) <= PRICE_TOLERANCE]
            if exact:
                outcomes["agree"] += 1
                continue

            nearest = min(candidates, key=lambda q: abs(q.price - legacy_price))
            delta = nearest.price - legacy_price
            if abs(delta) <= 0.51:
                outcomes["close (within 50p)"] += 1
            else:
                outcomes["differ"] += 1
            causes[_explain(case, nearest)] += 1
            divergences.append((case, nearest, f"£{legacy_price:.2f} -> £{nearest.price:.2f}"))

    total = sum(outcomes.values()) or 1
    print("Outcome:")
    for label, count in outcomes.most_common():
        print(f"  {label:<28} {count:>6,}  ({100 * count / total:5.1f}%)")

    if causes:
        divergent = sum(causes.values()) or 1
        print("\nWhy the differences arise:")
        for label, count in causes.most_common():
            print(f"  {label:<46} {count:>6,}  ({100 * count / divergent:5.1f}%)")

    if divergences and show:
        print(f"\nFirst {min(show, len(divergences))} divergence(s):\n")
        header = f"{'ORIGIN':<14} {'DEST':<14} {'LEGACY TYPE':<12} {'CHANGE':<22} DETAIL"
        print(header)
        print("-" * len(header))
        for case, quote, note in divergences[:show]:
            detail = ""
            if quote is not None:
                detail = (f"{quote.noc} {quote.product_type}/{quote.user_type} "
                          f"tier{quote.source_tier} \"{quote.product_name[:28]}\"")
            print(f"{case['origin_atco']:<14} {case['destination_atco']:<14} "
                  f"{(case['legacy_ticket_type'] or '-'):<12} {note:<22} {detail}")
        print("\nLegacy product names for the above, for context:")
        for case, _, _ in divergences[:show]:
            print(f"  {case['origin_atco']} -> {case['destination_atco']}: "
                  f"\"{case['legacy_product_name']}\" "
                  f"(from {case['legacy_operator_folder']})")

    # Non-zero exit if anything was lost outright, so this can gate a rebuild.
    return 1 if outcomes["lost: no fare in new db"] else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=Path("data/output/fares_v2.db"))
    parser.add_argument("--baseline", type=Path,
                        default=Path("tests/baseline_4100.json"))
    parser.add_argument("--user-type", default="adult")
    parser.add_argument("--show", type=int, default=15,
                        help="How many divergences to print.")
    args = parser.parse_args()
    raise SystemExit(compare(args.db, args.baseline, args.show, args.user_type))


if __name__ == "__main__":
    main()
