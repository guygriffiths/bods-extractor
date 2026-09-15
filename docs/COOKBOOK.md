# Developer cookbook

*Every command the CLI supports, as a working example. Copy-pasteable — every
example on this page has been run against a real database and returns real
data, using stops and operators that actually exist in the archive.*

See [README.md](../README.md) for the architecture and schema, and
[METHODOLOGY.md](METHODOLOGY.md) / [USER_GUIDE.md](USER_GUIDE.md) for the
client-facing explanation of the data itself.

All examples assume you're in the repo root with the venv active:

```bash
source .venv/bin/activate
```

---

## Building the database

```bash
# Create an empty database with the current schema.
# Rarely needed on its own - 'build' calls this as its first step.
# The --db argument can be added to any command but this is the default
bodsDB --db data/output/fares.db init

# Build everything in one step: init, then stops, fares and postcodes,
# always for the whole of Great Britain. This is the command to actually
# assemble a real database - expect roughly an hour for the full archive.
bodsDB build
```

### Scoped ingestion, for debugging only

These exist so you can iterate on the parser against one operator or area
without waiting for a full national run. Using them piecemeal to *assemble* a
database will leave every other operator's data stale or missing - use `build`
for that.

```bash
# Load bus stops from NaPTAN on their own (build calls this too).
bodsDB ingest-stops --stops-csv data/raw/Stops.csv

# Re-parse fares for everything touching Tyne & Wear only.
bodsDB ingest-fares \
  --archive data/raw/bodds_fares_archive_20260803.zip --atco-prefix 4100

# Re-parse fares for one specific operator instead of a whole area.
bodsDB ingest-fares \
  --archive data/raw/bodds_fares_archive_20260803.zip --noc GNEL

# Stop after the first 500 files - useful while developing netex.py.
bodsDB ingest-fares \
  --archive data/raw/bodds_fares_archive_20260803.zip --limit 500

# Attach postcodes to stops, for Tyne & Wear only (much faster than national).
bodsDB ingest-postcodes \
  --codepoint data/raw/codepo_gb.gpkg --atco-prefix 4100
```

### Finding out which operators hide inside a publisher account

```bash
bodsDB discover --folder "Go-Ahead Group plc_10"
```

> **Note:** `discover` samples a limited number of files per folder (60 by
> default) to keep the preview fast, and a big publisher account can hold
> thousands of files behind a handful of nested sub-archives. A small sample
> can under-report which NOCs are present - this is the same "small samples
> mislead" trap documented in the README. Treat `discover`'s output as a
> preview, not a census; the real ingest (`build` / `ingest-fares`) recurses
> into nested zips properly and will find everything.

---

## Exporting CSVs

```bash
# Every operator with a zoned stop in Tyne & Wear (ATCO area 4100).
bodsDB export \
  --atco-prefix 4100 --mode fill --out data/export/newcastle/

# Just one operator.
bodsDB export --noc GNEL --out /tmp/gnel_export

# High-confidence rows only (tier 1-2), adults only.
bodsDB export --noc GNEL --out /tmp/gnel_strict \
  --max-tier 2 --user-type adult
```

---

## Querying without touching a CSV

```bash
# Coverage and provenance stats for an area.
bodsDB report --atco-prefix 4100

# Find the NOC behind an operator's public-facing name.
bodsDB operators "Go North East"

# Fares between two known bus stops (ATCO codes).
bodsDB fare 410000024296 410000025301

# Same, restricted to one operator and passenger class.
bodsDB fare 07605093 07605083 --noc ANEA --user-type child

# Fares between two postcodes - no ATCO code or NOC needed. Resolves each
# postcode to every stop it reaches, then checks every operator and every
# zone combination for you.
bodsDB postcode-fare "NE1 5DX" "NE9 6AA"

# Same, for a young-person fare.
bodsDB postcode-fare "NE1 5DX" "NE9 6AA" --user-type youngPerson

# Plan a real journey via Google Maps and price each leg. Demo only - not
# part of the deliverable, but useful for sanity-checking against a real route.
bodsDB journey \
  "Newcastle Central Station" "Gateshead Interchange" --api-key YOUR_KEY

# Verbose logging works on any command.
bodsDB -v report --atco-prefix 4100
```

---

## Development tools

```bash
# Syntax / import check.
./.venv/bin/python -m compileall bods_extractor

# What NeTEx elements actually occur in the archive, and how often - the
# tool to reach for before changing netex.py.
./.venv/bin/python tools/netex_survey.py --sample 10

# Freeze fare answers from a known-good database, to guard against
# regressions when the parser changes.
./.venv/bin/python tools/freeze_baseline.py

# Replay the frozen cases against a rebuilt database and diff.
./.venv/bin/python tools/compare_baseline.py \
  --baseline tests/baseline_4100.json
```
