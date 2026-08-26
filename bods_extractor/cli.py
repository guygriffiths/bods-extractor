import click
import os
import sys
from dotenv import load_dotenv
from .db import init_db
from .ingest import IngestionEngine
from .lookup import LookupEngine

# Load environment variables from .env file
load_dotenv()

@click.group()
def cli():
    """BODS Fare Profiler CLI"""
    pass

@cli.command()
@click.option('--operator', help='Filter by operator NOC or Name (e.g. "Reading Buses")')
def ingest(operator):
    """Ingest stops and BODS NeTEx XML into the local SQLite database."""
    init_db()
    engine = IngestionEngine(target_operator=operator)
    
    click.echo("1/2: Ingesting NaPTAN Stops...")
    engine.ingest_naptan('data/raw/Stops.csv')
    
    click.echo("2/2: Parsing BODS NeTEx XML...")
    engine.process_archive('data/raw/bodds_fares_archive_20260803.zip')
    click.echo("Ingestion complete!")

@cli.command()
@click.argument('origin_postcode')
@click.argument('dest_postcode')
@click.option('--api-key', envvar='GOOGLE_MAPS_API_KEY', help='Google Maps API Key')
def lookup(origin_postcode, dest_postcode, api_key):
    """Calculate the optimal commuter fare between two postcodes."""
    if not api_key:
        click.echo("[!] Error: GOOGLE_MAPS_API_KEY environment variable not set. Check your .env file.")
        sys.exit(1)

    engine = LookupEngine()
    result = engine.find_commuter_fare(origin_postcode, dest_postcode, api_key)

    if not result.get('success'):
        click.echo(f"[!] {result.get('error', 'Unknown error occurred.')}")
        sys.exit(1)

    click.echo("\n=======================================================")
    click.echo(f"🚍 BODS Fare Profiler: {result['origin_postcode']} -> {result['dest_postcode']}")
    click.echo("=======================================================\n")
    
    click.echo("--- Route ---")
    for leg in result['legs']:
        icon = "🚆" if leg['is_train'] else "🚌"
        click.echo(f"{icon} {leg['route_name']} ({leg['agency_name']}) - {leg['distance_km']:.2f} km - stop: {leg['matched_stop']}")
        
    if result['unmatched_stops']:
        click.echo("\n[!] Warning: The following stops could not be matched to an ATCO code:")
        for s in result['unmatched_stops']:
            click.echo(f"  - {s}")
            
    click.echo(f"\nTotal distance: {result['distance_km']:.2f} km")
    click.echo(f"Duration:       ~{result['duration_mins']} mins")
    
    click.echo("\n--- Fares (one ticket per operator used) ---")
    for fare in result['fare_breakdown']:
        if fare['price'] is None:
            click.echo(f"{fare['agency_name']}: [!] NO DATA IN DATABASE")
        else:
            click.echo(f"{fare['agency_name']}: {fare['ticket_type']} - £{fare['price']:.2f}")
            
    if result.get('is_incomplete'):
        click.echo(f"\nTotal cost: £{result['total_cost']:.2f} * (INCOMPLETE: Missing operator data)")
    else:
        click.echo(f"\nTotal cost: £{result['total_cost']:.2f}")
        
    click.echo("\n--- Environmental Impact ---")
    click.echo(f"Car CO2:     {result['car_co2']} kg")
    click.echo(f"Bus CO2:     {result['bus_co2']} kg")
    click.echo(f"Saved:       {result['saved_co2']} kg 🍃")
    click.echo("=======================================================\n")