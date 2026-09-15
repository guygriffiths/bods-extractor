"""Diagnostic survey of a BODS NeTEx fares archive.

Answers the question "how canonical is this month's data?" by sampling XML
files from every publisher folder and measuring which authoritative NeTEx
elements are actually present.

This exists because the ingester deliberately falls back to heuristics when
canonical fields are missing (see :mod:`bods_extractor.netex`). Re-running this
against each new BODS archive tells you whether those fallbacks are still
needed, and whether a new dialect has appeared that nothing handles.

Usage::

    python tools/netex_survey.py                     # 12 files per folder
    python tools/netex_survey.py --sample 40         # deeper sample
    python tools/netex_survey.py --archive path.zip --json out.json

Replaces the old ad-hoc ``diagnose_netex.py`` / ``diagnose_all_operators.py``.
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import sys
import zipfile
from pathlib import Path

from lxml import etree

# Repo root on sys.path so this runs without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bods_extractor.config import DEFAULT_ARCHIVE  # noqa: E402


def _first(root, tag):
    """Return the first descendant with the given local name, or None."""
    return next(root.iter("{*}" + tag), None)


def _localname(el) -> str:
    return el.tag.split("}")[-1] if isinstance(el.tag, str) else "?"


def survey_file(xml_bytes: bytes, acc: "SurveyAccumulator") -> None:
    """Measure which canonical NeTEx fields one fare file provides."""
    try:
        root = etree.parse(io.BytesIO(xml_bytes)).getroot()
    except etree.XMLSyntaxError:
        acc.stats["parse_error"] += 1
        return

    acc.stats["files"] += 1

    # --- operator identity -------------------------------------------------
    # Every file also carries boilerplate ATOC / Network Rail <Operator>
    # elements, so only 'noc:'-prefixed ids count as a real bus operator.
    if any(el.get("id", "").startswith("noc:") for el in root.iter("{*}Operator")):
        acc.stats["has_noc"] += 1

    # --- product type ------------------------------------------------------
    saw_product_ref = False
    for el in root.iter("{*}TypeOfFareProductRef"):
        ref = el.get("ref", "")
        if ref:
            acc.product_refs[ref] += 1
            saw_product_ref = True
    if saw_product_ref:
        acc.stats["has_typeoffareproductref"] += 1
    if _first(root, "ProductType") is not None:
        acc.stats["has_producttype"] += 1
    if _first(root, "PreassignedFareProduct") is not None:
        acc.stats["has_preassigned_product"] += 1

    # --- passenger class ---------------------------------------------------
    saw_user_type = False
    for el in root.iter("{*}UserType"):
        if el.text:
            acc.user_types[el.text.strip()] += 1
            saw_user_type = True
    if saw_user_type:
        acc.stats["has_usertype"] += 1
    elif _first(root, "UserProfile") is not None:
        acc.stats["userprofile_without_usertype"] += 1

    # --- price carriers ----------------------------------------------------
    # <Amount> lives under several different parents. Only some of them are
    # fares: UsageParameterPrice is a fee/discount, TimeIntervalPrice is a
    # duration-based pass price. Counting the parents tells us how much
    # contamination the ingester has to filter out.
    amount_count = 0
    for el in root.iter("{*}Amount"):
        parent = el.getparent()
        acc.amount_parents[_localname(parent) if parent is not None else "?"] += 1
        amount_count += 1
    acc.stats["has_amount" if amount_count else "no_amount_anywhere"] += 1

    geo_prices = list(root.iter("{*}GeographicalIntervalPrice"))
    if geo_prices:
        acc.stats["has_geographicalintervalprice"] += 1
        if any(g.find("{*}Amount") is not None for g in geo_prices):
            acc.stats["geoprice_has_inline_amount"] += 1

    matrix_prices = list(root.iter("{*}DistanceMatrixElementPrice"))
    if matrix_prices:
        acc.stats["has_distancematrixelementprice"] += 1
        if any(p.find("{*}Amount") is not None for p in matrix_prices):
            acc.stats["matrixprice_inline_amount"] += 1
        else:
            acc.stats["matrixprice_ref_only"] += 1

    if _first(root, "FareTable") is not None:
        acc.stats["has_faretable"] += 1
    if _first(root, "Cell") is not None:
        acc.stats["has_cell"] += 1
    if _first(root, "TimeIntervalPrice") is not None:
        acc.stats["has_timeintervalprice"] += 1
    if _first(root, "UsageParameterPrice") is not None:
        acc.stats["has_usageparameterprice"] += 1

    # --- zones -------------------------------------------------------------
    zones = list(root.iter("{*}FareZone")) + list(root.iter("{*}TariffZone"))
    if not zones:
        # Usually a genuine flat-fare product with no zonal structure at all.
        acc.stats["no_zone_element"] += 1
    elif any(z.find(".//{*}ScheduledStopPointRef") is not None for z in zones):
        acc.stats["zone_has_stops"] += 1
    else:
        # Zones declared but with no stop members: unusable for ATCO matching.
        acc.stats["zone_without_stops"] += 1

    # --- tariff shape and validity ----------------------------------------
    for el in root.iter("{*}TariffBasis"):
        if el.text:
            acc.tariff_basis[el.text.strip()] += 1
    for el in root.iter("{*}FareStructureType"):
        if el.text:
            acc.fare_structure[el.text.strip()] += 1
    if _first(root, "ValidBetween") is not None:
        acc.stats["has_validbetween"] += 1


class SurveyAccumulator:
    """Counters shared across every file in the survey."""

    def __init__(self) -> None:
        self.stats = collections.Counter()
        self.product_refs = collections.Counter()
        self.user_types = collections.Counter()
        self.amount_parents = collections.Counter()
        self.tariff_basis = collections.Counter()
        self.fare_structure = collections.Counter()
        self.per_folder: dict[str, dict] = {}


def iter_folder_xml(archive: zipfile.ZipFile, entries: list[str], limit: int):
    """Yield up to ``limit`` XML payloads from one publisher folder.

    BODS packs each publisher's data either as loose XML or as nested zips, so
    both have to be handled.
    """
    yielded = 0
    for name in entries:
        if yielded >= limit:
            return
        try:
            if name.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(archive.read(name))) as nested:
                    for inner in nested.namelist():
                        if yielded >= limit:
                            return
                        if inner.endswith(".xml"):
                            yield nested.read(inner)
                            yielded += 1
            elif name.endswith(".xml"):
                yield archive.read(name)
                yielded += 1
        except (zipfile.BadZipFile, OSError):
            continue


def run(archive_path: Path, sample: int, json_path: Path | None) -> None:
    acc = SurveyAccumulator()
    archive = zipfile.ZipFile(archive_path)

    folders: dict[str, list[str]] = collections.OrderedDict()
    for name in archive.namelist():
        if "/" in name:
            folders.setdefault(name.split("/")[0], []).append(name)

    for index, (folder, entries) in enumerate(folders.items(), start=1):
        before = acc.stats["files"]
        snapshot = collections.Counter(acc.stats)
        for xml_bytes in iter_folder_xml(archive, entries, sample):
            survey_file(xml_bytes, acc)
        if acc.stats["files"] > before:
            delta = acc.stats - snapshot
            acc.per_folder[folder] = dict(delta)
        sys.stderr.write(f"\r{index}/{len(folders)} folders, {acc.stats['files']} files")
    sys.stderr.write("\n")

    total = acc.stats["files"] or 1
    print(f"\n=== {len(acc.per_folder)} folders, {acc.stats['files']} XML files "
          f"(max {sample} per folder) ===\n")

    print("--- field presence (% of sampled files) ---")
    for key in sorted(acc.stats):
        if key == "files":
            continue
        print(f"  {100 * acc.stats[key] / total:6.1f}%  {acc.stats[key]:7d}  {key}")

    for title, counter in (
        ("parent element of <Amount>", acc.amount_parents),
        ("TypeOfFareProductRef values", acc.product_refs),
        ("UserType values", acc.user_types),
        ("TariffBasis values", acc.tariff_basis),
        ("FareStructureType values", acc.fare_structure),
    ):
        print(f"\n--- {title} ---")
        for key, count in counter.most_common(30):
            print(f"  {count:8d}  {key}")

    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({
            "archive": str(archive_path),
            "sample_per_folder": sample,
            "stats": dict(acc.stats),
            "amount_parents": dict(acc.amount_parents),
            "product_refs": dict(acc.product_refs),
            "user_types": dict(acc.user_types),
            "tariff_basis": dict(acc.tariff_basis),
            "fare_structure": dict(acc.fare_structure),
            "per_folder": acc.per_folder,
        }, indent=1))
        print(f"\n[i] wrote {json_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE,
                        help="BODS fares archive zip")
    parser.add_argument("--sample", type=int, default=12,
                        help="max XML files to inspect per publisher folder")
    parser.add_argument("--json", type=Path, default=Path("data/output/netex_survey.json"),
                        help="where to write the machine-readable report")
    args = parser.parse_args()
    run(args.archive, args.sample, args.json)


if __name__ == "__main__":
    main()
