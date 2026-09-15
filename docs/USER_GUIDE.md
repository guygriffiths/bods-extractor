# User guide

*How to use the exported bus fare CSVs.*

Read [METHODOLOGY.md](METHODOLOGY.md) first if you want to know where the data
comes from and how far to trust it. This guide is about using it.

---

## 1. What you have

The export directory contains one set of files per bus operator, plus a manifest.

```
manifest.csv                  <- start here
GNEL_zones.csv                <- Go North East: which stops are in which zones
GNEL_postcodes.csv            <- Go North East: postcode -> stops
GNEL_fares.csv                <- Go North East: zone -> zone -> price
SCNE_zones.csv
SCNE_postcodes.csv
...
```

The four-letter code in each filename (`GNEL`, `SCNE`, `ANEA`…) is the
operator's **National Operator Code**, or NOC. It is the key that ties
everything together.

---

## 2. Looking up a fare

There are two routes into the data. Use whichever suits what you already know.

### Route A — you know the postcodes

This is the closest equivalent to your current method, with one extra step.

1. Take the origin and destination postcodes.
2. **Remove the last character** of each: `NE1 4XD` → `NE1 4X`.
3. Look both up in `<NOC>_postcodes.csv` to get the **stops** at each end.
4. Look those stops up in `<NOC>_zones.csv` to get their **zones**.
5. Find that pair of zones in `<NOC>_fares.csv` to get the price.

```
NE1 5DX  ->  NE1 5D  ->  GNEL_postcodes.csv  ->  3 stops   ->  43 zones
NE9 6AA  ->  NE9 6A  ->  GNEL_postcodes.csv  ->  1 stop    ->  31 zones

GNEL_fares.csv where origin_zone      is any of the 43
                 and destination_zone is any of the 31
                 and product_type     = "single"
                 and user_type        = "adult"
   ->  £2.50
```

**Why the extra step, and why so many zones.** In the published data a "zone"
belongs to a *ticket*, not to a place. Go North East defines a separate set of
zones for every product they sell, so one stop sits in 26 zones on average — one
as a fare stage on the X1, another as part of the network-wide Day Rover, and so
on. There is therefore no single "zone for NE1 5D" to publish, and any file that
claimed otherwise would be picking one arbitrarily. Postcode → stop is
unambiguous, so that is what `_postcodes.csv` gives you, and `_zones.csv`
supplies the rest.

In practice you collect all the zones for each end and take any matching fare
row; where several match they are normally the same price, and if they differ,
take the lowest and note that the journey had more than one applicable ticket.
The `zones_per_stop` column in `manifest.csv` tells you how much of this to
expect for each operator — Wright Bros and Stanley Travel are 1.0, so for them
the old one-zone-per-postcode intuition still holds.

### Route B — you know the bus stops

More precise, and one step shorter. If your routing data gives you stop
identifiers or coordinates, skip straight to `<NOC>_zones.csv` and look the stops
up by `atco_code`. This avoids the postcode approximation entirely.

If you have coordinates but not ATCO codes, `<NOC>_zones.csv` includes latitude
and longitude for every stop, so you can match to the nearest one.

### Route C — ask the database directly, no CSVs at all

Everything above is also available as a live lookup, if you'd rather not read a
file:

```bash
# Two postcodes, no operator or zone lookup needed - it checks every operator
# and every zone combination for you.
bodsDB --db fares_v2.db postcode-fare "NE1 5DX" "NE9 6AA"

# Same, for a specific passenger class
bodsDB --db fares_v2.db postcode-fare "NE1 5DX" "NE9 6AA" --user-type youngPerson

# Two known bus stops (ATCO codes)
bodsDB --db fares_v2.db fare 410000024296 410000025301
```

This queries the same database the CSVs were exported from, so the two are
always consistent with each other. Use whichever suits your integration — the
CSVs for a static/offline copy, the CLI (or the `bods_extractor.fares` Python
API behind it) for a live one.

---

## 3. Going from an operator name to a NOC

Your journey planner will give you an agency name like "Carousel Buses". The
fare files are named by NOC. `manifest.csv` connects the two:

| Column | Example |
|---|---|
| `noc` | `CSLB` |
| `operator_name` | `Carousel Buses` |
| `trading_name` | `Carousel Buses Ltd` |
| `bods_publisher_account` | `Go-Ahead Group plc_10` |

Match your agency name against **both** `operator_name` and `trading_name` —
they often differ, and journey planners are inconsistent about which they report.

Note the fourth column. The BODS publisher account is frequently a parent
company and will **not** match the agency name at all. This is the reason names
failed to line up before. Ignore it for matching; it is there for provenance.

### If the name does not match

Fall back to geography. If you know the boarding and alighting stops, find which
operators have **both** stops in their zones — look up both `atco_code` values in
each operator's `_zones.csv`. On a busy corridor several operators may qualify,
in which case use the name as a tiebreak, or take the cheapest as an
approximation and record that you did.

---

## 4. Column reference

### `manifest.csv` — one row per operator

| Column | Meaning |
|---|---|
| `noc` | National Operator Code. The key for all other files. |
| `operator_name` | Registered operator name. |
| `trading_name` | Name the operator trades under, if different. |
| `bods_publisher_account` | The BODS account the data came from. Provenance only. |
| `zones` | Number of distinct fare zones. |
| `stops` | Number of stops assigned to a zone. |
| `zones_per_stop` | How many zones an average stop belongs to. 1.0 means zones behave like places; 26 means they are per-ticket. |
| `stops_with_postcode` | Of those stops, how many have a postcode within 500 m. |
| `postcodes` | Distinct truncated postcodes in this operator's `_postcodes.csv`. |
| `postcode_stop_rows` | Total rows in that file — a postcode usually reaches several stops. |
| `fare_rows` | Rows in this operator's `_fares.csv`. |
| `flat_fare_products` | Products where every zone pair costs the same, and the price. Non-empty means the fare matrix was collapsed to one row. |
| `tier1_pct` … `tier4_pct` | Share of this operator's fares at each confidence tier. See §5. |

### `<NOC>_postcodes.csv`

One row per postcode–stop pair. A truncated postcode normally reaches several
stops; all of them are listed, nearest first, because all are valid places to
board.

| Column | Meaning |
|---|---|
| `postcode_trunc` | Postcode with the final character removed, e.g. `NE1 4X`. |
| `atco_code` | The stop. Look this up in `_zones.csv` to get its zones. |
| `stop_name` | Stop name, for sanity-checking. |
| `locality` | Town or district the stop is in. |
| `distance_m` | Metres from the postcode to the stop. Smaller is better. |
| `assignment` | `stop` = a bus stop is physically in this postcode. `fill` = inferred from a stop within 1 km. Prefer `stop` rows where you have a choice. |

### `<NOC>_fares.csv`

| Column | Meaning |
|---|---|
| `origin_zone`, `destination_zone` | Zone identifiers. `ANY` means the fare applies to all journeys — see flat fares below. |
| `origin_zone_name`, `destination_zone_name` | Human-readable names where available. |
| `product_type` | `single`, `day_return`, `period`, or `carnet`. |
| `user_type` | `adult`, `child`, `student`, `senior`, `youngPerson`, `infant`, `schoolPupil`, `disabled`, `anyone`, … |
| `product_name` | The operator's own name for the ticket, e.g. "Buzzfare Adult Single". |
| `price` | Price in pounds. |
| `line_id` | Route the fare applies to, where it is route-specific. Blank means network-wide. |
| `valid_from`, `valid_to` | Period the price is valid for. |
| `source_tier` | Confidence, 1 (best) to 4. See §5. |
| `source_detail` | How the value was derived, in words. |

**Fares are directional.** Look up origin→destination as given; do not assume the
reverse is present with the same price, though it usually is.

**If `origin_zone` and `destination_zone` are both `ANY`**, the operator charges a
flat fare: that price applies to every journey on their network, and there is one
row per product and passenger class rather than a matrix.

### `<NOC>_zones.csv`

Zone membership at stop level: `zone_id`, `zone_name`, `atco_code`, `stop_name`,
`locality`, `latitude`, `longitude`, `postcode`, `postcode_trunc`, and
`postcode_distance_m` (how far the stop is from that postcode's centroid — a
large value means the postcode match is weak).

A stop appears once per zone it belongs to, so expect the same `atco_code` on
many rows. This is the join table between `_postcodes.csv` and `_fares.csv`, and
it is also the auditable record: use it to check the export against an
operator's published zone map.

Worth reviewing for any operator where `postcode_conflicts` in the manifest is
high relative to `postcode_rows`. Usually a genuine zone boundary running through
a postcode; occasionally a sign of a misread zone.

---

## 5. Confidence tiers

Every fare carries a `source_tier` from 1 to 4:

| Tier | Meaning | Use it? |
|---|---|---|
| **1** | Read directly from a field the data standard defines for exactly this purpose. | Yes, without reservation. |
| **2** | Derived unambiguously from the file's own structure. | Yes. |
| **3** | Derived from a naming convention the UK data standard documents. | Yes, with awareness. |
| **4** | Inferred by recognising words like "child" or "return" in an identifier. | Usually right, but check before relying on it for anything consequential. |

A fare is given the **weakest** tier of anything used to derive it, so a tier 4
row may still have a perfectly canonical price — the tier might have been set by
the passenger class. The `source_detail` column says which part was responsible.

Check `tier1_pct` in the manifest per operator. Operators differ a lot in how
completely they publish.

---

## 6. Things to watch for

**Metro and rail are not included.** The Tyne and Wear Metro does not publish
fares to BODS. Any journey using the Metro will come out cheaper than reality.
Rail legs should be excluded from a fare total, not treated as free.

**Do not simply add up fares across operators.** A journey using two operators
often has a multi-operator ticket (Network One, for example) that is cheaper than
two singles. Those tickets are not in this dataset. Summing singles gives an
upper bound, not a price.

**One ticket per operator, not per leg.** If a journey uses the same operator
twice, one ticket normally covers both legs. Take the highest applicable fare for
that operator rather than adding the legs together.

**Not every stop has fare data.** Across Tyne and Wear, 93.3% of active stops
appear in at least one operator's fare zones; nationally it is 65.3%. Absence of
a fare means absence of published data, not a free journey. Handle "no fare
found" as a distinct outcome, not as zero.

**Check the validity dates.** Fares change. `valid_from` and `valid_to` tell you
what a price was current for. The dataset as a whole is a snapshot of the archive
it was built from.

**Capped and promotional fares are excluded** by default — the £2 and £3 national
caps and similar time-limited offers. They expire, and including them would
silently misprice journeys afterwards. If a published fare looks higher than what
a passenger currently pays, a cap is the likely explanation.

---

## 7. Querying it directly

If you would rather query than join CSVs, the underlying SQLite database supports
stop-to-stop lookups without any of the postcode approximation:

```bash
bodsDB fare 4100Z0000001 4100Z0000002 --user-type adult
```

```
Monument -> Gateshead Interchange

OPERATOR                   PRODUCT       PRICE  MATCH           TIER  NAME
Go North East              single       £ 2.40  point_to_point   1    Adult Single
Stagecoach North East      single       £ 2.60  point_to_point   1    Single
```

`bodsDB operators "Carousel Buses"` resolves a name to a NOC.
`bodsDB report --atco-prefix 4100` prints coverage statistics.
All of these work offline against the local database file.
