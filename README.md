# bods_extractor

Builds a queryable database of UK bus fares from the [Bus Open Data Service](https://www.bus-data.dft.gov.uk/)
fares archive, and exports it as per-operator CSVs.

The question it answers is: **given two bus stops served by a direct route, what
does the journey cost?** — without needing to know in advance which operator runs it.

- **Client-facing documentation** lives in [docs/METHODOLOGY.md](docs/METHODOLOGY.md)
  (how it works, what the data can and cannot support) and
  [docs/USER_GUIDE.md](docs/USER_GUIDE.md) (how to use the CSVs).
- This file is for whoever maintains or rebuilds the database.

---

## Quick start

```bash
python3 -m venv .venv
./.venv/bin/pip install -e .
```

Place the source data under `data/raw/` (the repo's `data` is a symlink; nothing
large is committed):

| File | Source | Approx. size |
|---|---|---|
| `bodds_fares_archive_YYYYMMDD.zip` | BODS "Download all fares data" | 2.9 GB |
| `Stops.csv` | [NaPTAN](https://beta-naptan.dft.gov.uk/download) national stop register | 101 MB |
| `codepo_gb.gpkg` | Ordnance Survey Code-Point Open (GeoPackage) | 277 MB |

Then build a regional database — Tyne & Wear (ATCO area `4100`) as the worked example:

```bash
DB=data/output/fares_v2.db

bodsDB --db $DB ingest-stops
bodsDB --db $DB ingest-fares      --atco-prefix 4100
bodsDB --db $DB ingest-postcodes  --atco-prefix 4100
bodsDB --db $DB export            --atco-prefix 4100 --mode fill \
                                  --out data/export/newcastle/
```

Check what you got:

```bash
bodsDB --db $DB report --atco-prefix 4100
bodsDB --db $DB fare 4100Z0000001 4100Z0000002
```

Paths default to `data/raw` / `data/output` and can be overridden with the
environment variables in [`bods_extractor/config.py`](bods_extractor/config.py)
(`BODS_DATA_DIR`, `BODS_ARCHIVE`, `BODS_DB`, and so on).

---

## Commands

| Command | Purpose |
|---|---|
| `ingest-stops` | Load the NaPTAN stop register (ATCO code → name, locality, coordinates). |
| `discover` | Show which operators (NOCs) hide inside each BODS publisher account. |
| `ingest-fares` | Parse the NeTEx archive into zones and fares. |
| `ingest-postcodes` | Match every stop to its nearest Code-Point postcode. |
| `export` | Write per-operator zone / postcode / fare CSVs plus a manifest. |
| `report` | Coverage and provenance statistics. |
| `operators` | Resolve a trading name to a NOC. |
| `fare` | Look up fares between two ATCO codes. Offline. |
| `journey` | Demo only: Google Directions + the local database. |

Every command takes `--help`.

### Scoping an ingest

`ingest-fares` reads the whole archive by default and keeps whatever matches
your filters:

- `--atco-prefix 4100` — keep only fares touching stops in that ATCO
  administrative area. This is the right filter for "all the Newcastle operators",
  because it does not require knowing their names first.
- `--noc GNEL --noc SCNE` — keep only these operators. Adds a cheap filename
  pre-filter; pass `--scan-all` to disable it (see the caveat in
  `FareIngester._filename_may_match` — Stagecoach's filenames do not contain
  their NOC).
- `--folder "Go-Ahead Group plc_10"` — restrict to a publisher account. Fastest,
  but you must already know which accounts matter. Use `discover` to find out.

Re-running an ingest replaces only the operators it touches
(`db.reset_operators`), so you can add an operator without rebuilding everything.

---

## Architecture

```
config.py       Paths and tunable constants. Everything is env-overridable.
db.py           Schema, connections, scoped resets.
netex.py        NeTEx XML -> operators, lines, zones, products, fares.  <- the core
heuristics.py   Fallback inference from identifier strings. Tier 4 only.
ingest.py       Archive iteration, filtering, batched writes.
geo.py          Coordinate transforms, spatial index, GeoPackage blob decoding.
postcodes.py    Stop -> nearest postcode via Code-Point Open.
fares.py        Read-only query API.                                    <- the deliverable
export.py       CSV writer.
lookup.py       Demo: composes directions.py with fares.py.
directions.py   Demo: Google Directions adapter.
cli.py          All presentation.
```

`fares.py` has no network dependency and does not import `directions.py`. The
Google integration is a demonstration front-end; the client's own routing
implementation substitutes for it directly.

### Schema

```
stops           atco_code PK, lat, lng, easting, northing, common_name, locality, ...
stop_postcodes  atco_code PK, postcode, postcode_trunc, distance_m
operators       noc PK, public_code, name, trading_name, town, postcode
operator_sources  noc, publisher_folder, file_count
lines           noc, line_id, public_code, line_name, description
zones           noc, zone_id, zone_name, atco_code
fares           noc, line_id, origin_zone, destination_zone, product_type,
                user_type, product_name, price, valid_from, valid_to,
                source_tier, source_detail
ingest_stats    per-operator, per-folder parse counts
```

Everything is keyed on **NOC** (National Operator Code), not on the BODS
publisher folder name. This matters: the folder `Go-Ahead Group plc_10` contains
24 different operators, including both Go North East and Carousel Buses.

The `fares` primary key spans all the identifying columns, so duplicate rows —
of which the archive contains a great many — collapse on insert.

### Provenance tiers

Every fare row carries `source_tier` (1 = best) and a human-readable
`source_detail`. This is the mechanism that lets heuristic inference coexist with
canonical data instead of replacing it:

| Tier | Meaning |
|---|---|
| 1 | Read from a canonical NeTEx field (`TypeOfFareProductRef`, `UserType`, `GeographicalIntervalPriceRef`). |
| 2 | Derived from an unambiguous local structure (inline `Amount`, a file with exactly one user type). |
| 3 | Derived from a documented NeTEx-profile identifier convention (`Trip@AdultSingle-SOP@...`). |
| 4 | Inferred from free-text identifiers by `heuristics.py`. |

Filter with `--max-tier 2` for a high-confidence-only export. See
[docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the reasoning and the measured
tier distribution.

---

## Development

```bash
./.venv/bin/python -m compileall bods_extractor      # syntax
python tools/netex_survey.py --sample 10             # what's actually in the archive
python tools/freeze_baseline.py                      # snapshot answers from an old DB
python tools/compare_baseline.py                     # replay them against a new one
```

`tools/netex_survey.py` is the tool to reach for before changing `netex.py`.
It reports which NeTEx elements actually occur and how often, across a sample of
every publisher folder. Most of the design decisions in `netex.py` are
justified by its output rather than by the specification.

`tools/freeze_baseline.py` / `compare_baseline.py` guard against regressions
when the parser changes: freeze fare answers from a known-good database, then
replay the same stop pairs against a rebuilt one and diff.

### Gotchas

- **An lxml element with no children is falsy.** `node.find(a) or node.find(b)`
  silently discards a valid match from `a`. Always test `is not None`.
- **Filenames are not a reliable NOC source.** 31,404 Stagecoach files parse to a
  fake NOC of `FX` if you trust the filename. Only `<Operator id="noc:...">` is
  authoritative.
- **Small samples mislead.** Sampling 40 files per folder reported Go North East,
  Stagecoach North East and both Arriva north-east operators as absent from the
  archive. They are all present in quantity.
- **`ingest-fares` without `--folder` reads all 183,569 XML files.** Expect a long
  run. Use `--limit` while developing.

### Security

`.env` is git-ignored, but a Google Maps API key was committed in `c1b5a13`
before that was fixed. **It is still in the history and must be rotated.**
Rewriting the history would not be sufficient on its own — revoke the key.
