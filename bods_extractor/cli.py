"""Command line interface.

All presentation lives here; every other module returns data. Run
``bodsDB --help`` for the command list, or see ``docs/USER_GUIDE.md``.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

from . import export as export_module
from . import ingest as ingest_module
from . import postcodes as postcodes_module
from .config import (ATCO_AREAS, DEFAULT_ARCHIVE, DEFAULT_CODEPOINT, DEFAULT_DB,
                     DEFAULT_STOPS_CSV, EXPORT_DIR, ZONE_FILL_RADIUS_M)
from .db import get_connection, init_db
from .fares import FareLookup
from .lookup import plan_journey

load_dotenv()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )


@click.group()
@click.option("--db", type=click.Path(path_type=Path), default=None,
              help=f"Database file (default: {DEFAULT_DB}).")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def cli(ctx, db, verbose):
    """BODS bus fare extractor.

    Builds a local database of UK bus fares from the Bus Open Data Service,
    keyed on stop-to-stop journeys, then exports it as CSV.
    """
    _configure_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["db"] = db


# --------------------------------------------------------------------------
# Building the database
# --------------------------------------------------------------------------

@cli.command("init")
@click.pass_context
def init_command(ctx):
    """Create an empty database with the current schema."""
    init_db(ctx.obj["db"])
    click.echo(f"Initialised {ctx.obj['db'] or DEFAULT_DB}")


@cli.command("ingest-stops")
@click.option("--stops-csv", type=click.Path(exists=True, path_type=Path),
              default=None, help=f"NaPTAN Stops.csv (default: {DEFAULT_STOPS_CSV}).")
@click.option("--include-inactive", is_flag=True,
              help="Keep stops NaPTAN marks as deleted or pending.")
@click.option("--bus-only", is_flag=True,
              help="Keep only BCT on-street bus stops.")
@click.pass_context
def ingest_stops_command(ctx, stops_csv, include_inactive, bus_only):
    """Load the NaPTAN national stop register."""
    count = ingest_module.ingest_stops(
        ctx.obj["db"], stops_csv,
        active_only=not include_inactive, bus_only=bus_only)
    click.echo(f"Loaded {count:,} stops.")


@cli.command("ingest-fares")
@click.option("--archive", type=click.Path(exists=True, path_type=Path), default=None,
              help=f"BODS fares archive zip (default: {DEFAULT_ARCHIVE}).")
@click.option("--folder", "folders", multiple=True,
              help="Restrict to a BODS publisher account folder. Repeatable.")
@click.option("--noc", "nocs", multiple=True,
              help="Restrict to a National Operator Code. Repeatable.")
@click.option("--atco-prefix", default="",
              help="Keep only fares touching this ATCO area, e.g. 4100 for Tyne & Wear.")
@click.option("--scan-all", is_flag=True,
              help="Disable the filename pre-filter when using --noc. Slower but "
                   "catches publishers who omit the NOC from their filenames.")
@click.option("--include-capped", is_flag=True,
              help="Include £2/£3 capped and promotional products.")
@click.option("--limit", type=int, default=None, help="Stop after N files (for testing).")
@click.pass_context
def ingest_fares_command(ctx, archive, folders, nocs, atco_prefix, scan_all,
                         include_capped, limit):
    """Parse the BODS NeTEx fares archive into the database."""
    ingester = ingest_module.FareIngester(
        db_path=ctx.obj["db"], folders=folders, nocs=nocs,
        atco_prefix=atco_prefix, scan_all=scan_all,
        include_capped=include_capped, limit=limit)
    summary = ingester.run(archive)

    click.echo("")
    click.echo(f"Files seen      : {summary.files_seen:,}")
    click.echo(f"Files parsed    : {summary.files_parsed:,}")
    click.echo(f"Files filtered  : {summary.files_skipped_filter:,}")
    click.echo(f"Operators       : {len(summary.operators)}")
    click.echo(f"Zone rows       : {summary.zone_rows:,}")
    click.echo(f"Fare rows       : {summary.fare_rows:,}")
    total = sum(summary.tier_counts.values()) or 1
    click.echo("Provenance      :")
    for tier in (1, 2, 3, 4):
        count = summary.tier_counts.get(tier, 0)
        click.echo(f"  tier {tier}       : {count:>10,}  ({100 * count / total:5.1f}%)")


@cli.command("ingest-postcodes")
@click.option("--codepoint", type=click.Path(exists=True, path_type=Path), default=None,
              help=f"Code-Point Open GeoPackage (default: {DEFAULT_CODEPOINT}).")
@click.option("--atco-prefix", default="",
              help="Only map stops in this ATCO area. Much faster for a regional job.")
@click.pass_context
def ingest_postcodes_command(ctx, codepoint, atco_prefix):
    """Map every bus stop to its nearest postcode."""
    count = postcodes_module.ingest_stop_postcodes(
        ctx.obj["db"], codepoint, atco_prefix=atco_prefix)
    click.echo(f"Mapped {count:,} stops to postcodes.")


@cli.command("discover")
@click.option("--archive", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--folder", "folders", multiple=True, help="Limit to these folders.")
@click.option("--sample", type=int, default=60, help="Files to sample per folder.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def discover_command(archive, folders, sample, as_json):
    """List which operators hide inside each BODS publisher account.

    BODS folder names are corporate accounts, not operators: the Go-Ahead
    account publishes for 24 different NOCs. Run this before a targeted ingest.
    """
    found = ingest_module.discover_operators(archive, sample, folders)
    if as_json:
        click.echo(json.dumps(found, indent=1, sort_keys=True))
        return
    for folder in sorted(found):
        entry = found[folder]
        click.echo(f"\n{folder}  ({entry['files']} archive entries)")
        for noc, name in sorted(entry["nocs"].items()):
            click.echo(f"    {noc:8s} {name}")


# --------------------------------------------------------------------------
# Exporting
# --------------------------------------------------------------------------

@cli.command("export")
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help=f"Output directory (default: {EXPORT_DIR}).")
@click.option("--noc", "nocs", multiple=True, help="Export these NOCs. Repeatable.")
@click.option("--atco-prefix", default="",
              help="Export every operator serving this ATCO area, e.g. 4100. "
                   "Selects which operators are exported; does not truncate "
                   "their zone or fare lists.")
@click.option("--mode", type=click.Choice(["stops", "fill"]), default="stops",
              help="'stops' maps only postcodes containing a stop; 'fill' assigns "
                   "every nearby postcode to its closest zone.")
@click.option("--codepoint", type=click.Path(exists=True, path_type=Path), default=None)
@click.option("--fill-radius", type=float, default=ZONE_FILL_RADIUS_M,
              help="Metres from a zoned stop within which 'fill' will assign a postcode.")
@click.option("--user-type", "user_types", multiple=True,
              help="Restrict to these passenger classes (adult, child, senior, "
                   "student, youngPerson...). Repeatable. Default: all classes.")
@click.option("--product-type", "product_types", multiple=True,
              help="Restrict to these product types (single, day_return, period, carnet).")
@click.option("--max-tier", type=int, default=4,
              help="Drop fares whose provenance tier is weaker than this. "
                   "Use 2 for a high-confidence-only export.")
@click.pass_context
def export_command(ctx, out, nocs, atco_prefix, mode, codepoint, fill_radius,
                   user_types, product_types, max_tier):
    """Write per-operator zone, postcode and fare CSVs plus a manifest."""
    if not user_types or "all" in user_types:
        user_types = None
    out_dir = export_module.export(
        db_path=ctx.obj["db"], out_dir=out, nocs=nocs, atco_prefix=atco_prefix,
        mode=mode, codepoint_path=codepoint, radius_m=fill_radius,
        user_types=user_types, product_types=product_types or None, max_tier=max_tier)
    click.echo(f"\nWrote CSVs to {out_dir}")
    click.echo(f"Start with {out_dir / 'manifest.csv'}")


@cli.command("report")
@click.option("--atco-prefix", default="", help="Restrict coverage stats to an ATCO area.")
@click.pass_context
def report_command(ctx, atco_prefix):
    """Show coverage and data-quality statistics for the database."""
    conn = get_connection(ctx.obj["db"], read_only=True)

    with FareLookup(ctx.obj["db"]) as lookup:
        coverage = lookup.coverage(atco_prefix)
    area_name = ATCO_AREAS.get(atco_prefix, atco_prefix or "Great Britain")
    click.echo(f"\nArea            : {area_name}")
    click.echo(f"Stops           : {coverage['stops']:,}")
    click.echo(f"In a fare zone  : {coverage['stops_in_a_fare_zone']:,} "
               f"({coverage['coverage_pct']}%)")

    totals = conn.execute(
        "SELECT COUNT(*) AS fares, COUNT(DISTINCT noc) AS operators FROM fares").fetchone()
    click.echo(f"\nOperators       : {totals['operators']:,}")
    click.echo(f"Fare rows       : {totals['fares']:,}")

    click.echo("\nProvenance:")
    for row in conn.execute(
            "SELECT source_tier, COUNT(*) AS n FROM fares GROUP BY 1 ORDER BY 1"):
        share = 100 * row["n"] / max(totals["fares"], 1)
        click.echo(f"  tier {row['source_tier']}       : {row['n']:>10,}  ({share:5.1f}%)")

    click.echo("\nTop operators by fare rows:")
    clause = ""
    params: tuple = ()
    if atco_prefix:
        clause = (" WHERE f.noc IN (SELECT DISTINCT noc FROM zones"
                  " WHERE atco_code LIKE ?)")
        params = (atco_prefix + "%",)
    for row in conn.execute(
            "SELECT f.noc, o.name, COUNT(*) AS n FROM fares f"
            " LEFT JOIN operators o ON o.noc = f.noc"
            f"{clause} GROUP BY f.noc ORDER BY n DESC LIMIT 20", params):
        click.echo(f"  {row['noc']:8s} {(row['name'] or '')[:38]:38s} {row['n']:>9,}")
    conn.close()


@cli.command("operators")
@click.argument("name")
@click.pass_context
def operators_command(ctx, name):
    """Find the NOC for an operator name, e.g. 'Carousel Buses'."""
    with FareLookup(ctx.obj["db"]) as lookup:
        matches = lookup.resolve_operator(name)
    if not matches:
        click.echo(f"No operator matched '{name}'.")
        return
    for operator in matches:
        click.echo(f"  {operator.noc:8s} {operator.name}  "
                   f"[trading as: {operator.trading_name or '-'}]")


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

@cli.command("fare")
@click.argument("origin_atco")
@click.argument("destination_atco")
@click.option("--noc", default=None, help="Restrict to one operator.")
@click.option("--user-type", default="adult")
@click.option("--max-tier", type=int, default=4)
@click.pass_context
def fare_command(ctx, origin_atco, destination_atco, noc, user_type, max_tier):
    """Look up fares between two ATCO stop codes. No network access required."""
    with FareLookup(ctx.obj["db"]) as lookup:
        origin = lookup.get_stop(origin_atco)
        destination = lookup.get_stop(destination_atco)
        quotes = lookup.fares_between(origin_atco, destination_atco, noc=noc,
                                      user_type=user_type, max_tier=max_tier)

    click.echo(f"\n{origin.common_name if origin else origin_atco} -> "
               f"{destination.common_name if destination else destination_atco}\n")
    if not quotes:
        click.echo("No fare found. Try --max-tier 4, a different --user-type, "
                   "or check both stops are in the same operator's network.")
        return

    click.echo(f"{'OPERATOR':<26} {'PRODUCT':<11} {'PRICE':>7}  {'MATCH':<15} TIER  NAME")
    # The same fare is often published once per route variant. Collapse them
    # for display; the CSV export keeps every row with its line reference.
    seen = set()
    for quote in quotes:
        key = (quote.noc, quote.product_type, quote.price, quote.product_name)
        if key in seen:
            continue
        seen.add(key)
        click.echo(f"{quote.operator_name[:25]:<26} {quote.product_type:<11} "
                   f"£{quote.price:>6.2f}  {quote.zone_match:<15} {quote.source_tier:^4}  "
                   f"{quote.product_name[:30]}")


@cli.command("journey")
@click.argument("origin")
@click.argument("destination")
@click.option("--api-key", envvar="GOOGLE_MAPS_API_KEY",
              help="Google Maps API key. Reads GOOGLE_MAPS_API_KEY otherwise.")
@click.option("--user-type", default="adult")
@click.pass_context
def journey_command(ctx, origin, destination, api_key, user_type):
    """Demo: route with Google, then price it from the local database.

    Not part of the deliverable - the fares database is usable with any routing
    engine. See docs/USER_GUIDE.md.
    """
    if not api_key:
        raise click.ClickException(
            "No Google Maps API key. Set GOOGLE_MAPS_API_KEY or pass --api-key.")

    journey = plan_journey(origin, destination, api_key,
                           db_path=ctx.obj["db"], user_type=user_type)
    if not journey.success:
        raise click.ClickException(journey.error)

    click.echo(f"\n{journey.origin} -> {journey.destination}")
    click.echo(f"{journey.distance_km:.1f} km, about {journey.duration_mins} minutes\n")

    for index, leg in enumerate(journey.legs, start=1):
        mode = "train" if leg.is_train else "bus"
        click.echo(f"  {index}. [{mode}] {leg.route_name} ({leg.agency_name})")
        click.echo(f"     {leg.origin_stop_name} -> {leg.destination_stop_name}")
        if leg.matched_noc:
            click.echo(f"     operator: {leg.matched_noc} (matched by {leg.match_method})")
        quote = leg.best_quote
        if quote:
            click.echo(f"     fare: £{quote.price:.2f} {quote.product_type} "
                       f"({quote.product_name}) [tier {quote.source_tier}]")
        elif not leg.is_train:
            click.echo("     fare: no data")

    if journey.unmatched_stops:
        click.echo("\nStops that could not be matched to an ATCO code:")
        for name in journey.unmatched_stops:
            click.echo(f"  - {name}")

    click.echo(f"\nTotal (one ticket per operator): £{journey.total_cost:.2f}"
               + ("  [incomplete: some legs have no fare data]"
                  if journey.is_incomplete else ""))
    carbon = journey.carbon
    click.echo(f"CO2: {carbon['bus_kg']} kg by bus vs {carbon['car_kg']} kg by car "
               f"({carbon['saved_kg']} kg saved)")


if __name__ == "__main__":
    cli()
