# How the fare database was built

*A methodology note for the fare-lookup dataset derived from the Bus Open Data
Service.*

---

## 1. What this dataset is

For any two bus stops in the covered area that share a direct route, the dataset
gives the fare between them: the price, the ticket type, the passenger class, the
operator, and the period the price is valid for.

It is **operator-independent**. You do not have to know who runs a route before
asking what it costs. You can ask "what does it cost to travel from this stop to
that stop", and the answer comes back with the operator attached.

It is derived entirely from published open data. Nothing in it is scraped,
estimated from distance, or supplied by an operator privately.

### Three source datasets

| Dataset | Publisher | Role |
|---|---|---|
| **BODS fares archive** (NeTEx XML) | Department for Transport | The fares themselves, and the fare zones each operator defines. |
| **NaPTAN** | Department for Transport | The national bus stop register: ATCO code, name, locality, coordinates. |
| **Code-Point Open** | Ordnance Survey | Postcode centroids, used to attach postcodes to stops. |

The BODS archive is large: 2.9 GB compressed, **183,569 individual fare files**
published across **337 publisher accounts**.

---

## 2. The operator identity problem

This was the first thing that had to be solved, and it is worth explaining
because it affects how you use the data.

BODS organises fare files into folders named after the **account that published
them**, not after the operator that runs the buses. Those are frequently
different things. Inside the folder `Go-Ahead Group plc_10` the archive contains
fares for **24 distinct operators**, including:

| National Operator Code | Name in the data | Trading name |
|---|---|---|
| `GNEL` | Go North East | Go North East |
| `CSLB` | Carousel Buses | Carousel Buses Ltd |

This is exactly the situation that produces the High Wycombe problem: a journey
planner reports the agency as "Carousel Buses", but the BODS folder is called
"Go-Ahead Group plc_10", and the two never match. Similarly `Arriva UK Bus_63`
contains 13 operators, and `Stagecoach Group_15` contains Stagecoach North East
among others.

**The solution** is to ignore folder names entirely and key everything on the
**National Operator Code (NOC)**, which is recorded canonically inside each file
as `<Operator id="noc:GNEL">`. This field is present in **100%** of files
surveyed. Alongside it, the files carry the operator's registered name *and*
their trading name, so:

- `GNEL` → name "Go North East"
- `CSLB` → name "Carousel Buses", trading name "Carousel Buses Ltd"

The export includes both names for every operator, which is what allows a name
reported by a journey planner to be matched back to a NOC. The `manifest.csv`
also records which BODS publisher account each operator's data came from, so the
provenance can be traced back.

> A caution learned the hard way: **filenames cannot be trusted to identify the
> operator.** 31,404 Stagecoach files contain no NOC in their filename at all.
> Only the `<Operator>` element inside the file is authoritative.

---

## 3. How fares are represented, and how completely

The NeTEx standard that BODS uses is expressive, which means the same commercial
fact can be encoded several different ways, and operators use different subsets
of it. Before writing the parser, a survey was run across **3,275 files sampled
from all 336 publisher folders** to establish what is actually present rather
than what the specification permits.

### What is reliably present

| Element | Present in |
|---|---|
| Operator NOC | **100.0%** |
| Passenger class (`UserType`) | **100.0%** |
| Validity period (`ValidBetween`) | **100.0%** |
| Product type reference | **99.6%** |
| Fare product definition | **99.8%** |
| At least one price | **98.8%** |

This is the good news, and it is the reason the dataset can be built at all.
Three of the four things you need — *who*, *for whom*, *when* — are canonically
defined in effectively every file.

The product type is drawn from a controlled vocabulary. Across the entire
archive only five values are ever used:

| Product type | Files |
|---|---|
| Single trip | 2,083 |
| Period pass | 755 |
| Day return | 648 |
| Carnet (bundle of trips) | 6 |
| Carnet (bundle of days) | 2 |

Passenger class is likewise canonical, with eleven values in use: adult, child,
student, senior, infant, "anyone", young person, school pupil, employee,
disabled, and disabled companion.

### What is not reliably present

| Gap | Share of files |
|---|---|
| No fare zone defined at all | **12.2%** (across 91 folders) |
| No zone-to-zone price element | **17.7%** |
| No price of any kind | **1.2%** |
| A zone that contains no stops | 1.7% |

And crucially: where a zone-to-zone price *does* exist, **76.9%** of the time it
is a *reference* to a price defined elsewhere in the file rather than a value
written inline. Only **5.4%** carry the amount directly. Resolving those
references correctly is most of the work in the parser.

### The answer to "are fares defined canonically?"

Partly. **Who, for whom, and when are canonical. What it costs usually is. Which
journey it applies to often is not.**

That distinction is why the dataset is built the way it is.

---

## 4. Provenance tiers

Because the data is canonical in some places and inferential in others, **every
single fare row records how confidently it was derived.** This is the central
design decision of the whole project.

| Tier | How the value was obtained | Trust |
|---|---|---|
| **1** | Read from a canonical NeTEx field designed to carry exactly this meaning. | Direct from the operator. |
| **2** | Derived from an unambiguous structure within the same file — e.g. a price written inline, or a file that defines only one passenger class. | Very high; no judgement involved. |
| **3** | Derived from an identifier convention that the UK NeTEx profile documents, e.g. an element named `Trip@AdultSingle-SOP@...`. | High, but a convention rather than a field. |
| **4** | Inferred from free text in identifiers — recognising "child", "dayrover", "return" and so on in an element's name. | Reasonable, but a judgement. |

A fare row is assigned the **weakest** tier of any component used to derive it.
If the price came from a canonical reference but the passenger class had to be
inferred from the product's name, the row is tier 4. The `source_detail` column
names which component set the tier.

### An example of what tier 1 requires

Reading the data canonically is not simply a matter of finding the right field.
A Go North East fare file describes its ticket like this:

```
<PreassignedFareProduct id="Trip@AdultSingle">
  <TypeOfFareProductRef ref="fxc:standard_product@trip@single"/>
  <validableElements>
    <FareStructureElementRef ref="Tariff@AdultSingle@access"/>
    <FareStructureElementRef ref="Tariff@AdultSingle@conditions_of_travel"/>
```

The product references two fare structure elements — and **neither of them says
who the ticket is for**. The passenger class lives in a third element the
product does not reference at all:

```
<Tariff id="Tariff@AdultSingle@Line_GNEL:...">
  <FareStructureElement id="Tariff@AdultSingle@access"/>
  <FareStructureElement id="Tariff@AdultSingle@eligibility">
      <UserType>adult</UserType>
  <FareStructureElement id="Tariff@AdultSingle@conditions_of_travel"/>
```

Recovering "adult" means going from a referenced element *up* to its enclosing
tariff and *back down* to its eligibility sibling. This is defined by the schema,
so the result is properly canonical — but a parser that only follows the explicit
references will not find it, and will have to fall back on reading the word
"Adult" out of the product's name instead.

That distinction accounts for the large majority of rows in this dataset. It is
described here because it illustrates what the tier system is actually measuring:
not whether the data is present, but whether it was read from where the standard
says it lives.

### Why the heuristics were kept

An earlier version of this work used pattern-matching on identifier text
throughout. It would have been tempting to delete all of it once the canonical
fields were being read properly.

That would have been a mistake. Those rules were written as corrections for
cases where the canonical route produced visibly wrong answers, and 12–18% of
files genuinely lack the canonical structure. Deleting them would have discarded
working knowledge and lost real fares.

Instead they were preserved as **tier 4**: they run only when tiers 1–3 have not
produced an answer, and everything they produce is labelled as theirs. You get
the coverage they provide *and* the ability to exclude them. Exporting with
`--max-tier 2` yields a strictly canonical dataset; the default yields the
fullest one.

### One deliberate change to the old rules

The previous rules used a single exclusion list that mixed together two very
different things:

1. **Not a fare at all** — reservation fees, luggage charges, dog fares, PlusBus
   add-ons, booking surcharges, penalties.
2. **Not an *adult* fare** — child, student, senior.

The first category is still excluded: those are genuinely not journey prices, and
the survey confirmed this structurally — amounts attached to `UsageParameterPrice`
elements (1,471 of them) are fees and discounts, not fares, and are never read as
prices.

The second category is now **labelled rather than discarded**. Since the
passenger class is canonically available in 100% of files, a child single is kept
as a child single. This makes the dataset useful for concessionary travel
questions it previously could not answer, and it costs nothing — filter on
`user_type` if you only want adult fares.

Capped and promotional products (the £2 and £3 national fare caps, and similar
short-lived offers) remain excluded by default, because they expire and would
silently misprice a journey after they end. `--include-capped` brings them back.

---

## 5. Fare zones and postcodes

### How operators define zones

There is no single national zoning scheme. Each operator defines its own, and the
survey found four different bases in use:

| Basis | Files |
|---|---|
| Zone-based | 1,982 |
| Point to point | 1,455 |
| Zone to zone | 1,374 |
| Flat fare | 141 |

Some operators draw genuine geographic zones with meaningful names — "Big Lamp",
"Gateshead Interchange". Others emit **one zone per stop**, which is really a
point-to-point matrix wearing a zone-shaped hat. Both are handled: the dataset
records zone membership at stop level regardless, so a stop-to-stop query works
either way.

A small number of operators charge a **flat fare** — every origin and destination
pair has the same price. Where the export detects this (every price identical
across at least 25 zone pairs), it collapses the matrix to a single row rather
than emitting hundreds of thousands of identical ones. The `manifest.csv` records
which operators and products this applied to.

### A zone belongs to a ticket, not to a place

This is the single most important structural fact in the dataset, and it is not
obvious from the file format.

A NeTEx fare zone is scoped to a **fare product**. Every ticket an operator sells
carries its own zone definitions, so a stop belongs to many zones at once:

| Operator | Stops | Zones | Zones per stop |
|---|---|---|---|
| Jim Hughes | 1,534 | 53 | 27.0 |
| Go North East | 6,663 | 1,178 | 26.2 |
| Arriva Northumbria | 2,107 | 934 | 16.8 |
| Stagecoach North East | 4,313 | 1,659 | 6.6 |
| Wright Bros | 165 | 13 | 1.0 |
| Stanley Travel | 90 | 29 | 1.0 |

One Go North East stop reaches 47 zones. Some are single fare stages
(`fs@11110`, "St Marys Place"); others are network-wide
(`fs@1DDiscoveryAD@Network`, covering the whole operator, for the Discovery day
ticket). They are not alternative opinions about where the stop is — they are all
correct, for different tickets.

The consequence is that **"which zone is postcode NE1 5D in?" has no answer.**
An earlier version of this export answered it anyway, by picking the
best-evidenced zone. That was wrong: it silently chose between a fare stage and a
network pass, and it made 100% of postcodes look like boundary conflicts.

So the export does not do that. `<NOC>_postcodes.csv` maps postcode → **stop**,
which is unambiguous and stable, and `<NOC>_zones.csv` carries the full
many-to-many stop → zone membership. The lookup path is:

```
postcode -> stop      (<NOC>_postcodes.csv)
stop     -> zone(s)   (<NOC>_zones.csv)
zone pair -> price    (<NOC>_fares.csv)
```

This is one step longer than a flat postcode-to-zone table, and it is the only
version that is true to the data. For operators with `zones_per_stop` of 1.0 the
extra step collapses to nothing.

### Attaching postcodes

Every stop is matched to its nearest Code-Point Open postcode centroid within
500 m, using the Ordnance Survey national grid rather than latitude and
longitude, so distances are true metres. For Tyne & Wear the median stop is 46 m
from its postcode centroid, and 99% are within 231 m.

Postcodes are then **truncated by removing the final character** — `NE1 4XD`
becomes `NE1 4X` — matching the existing convention in the client's
`postcodes.csv`. This groups roughly 10–15 adjacent addresses into one lookup
key, which is about the granularity at which fare zones actually vary.

The export offers two modes, because they suit different purposes:

**`stops` mode** includes only truncated postcodes that actually contain a bus
stop. Every row is directly evidenced. It is sparse — a postcode 300 m from the
nearest stop simply is not listed.

**`fill` mode** additionally attaches every Code-Point postcode within 1 km of a
stop to its nearest stop. This produces a dense file with usable coverage. It is
inference, but of a mild and inspectable kind. **Direct evidence is never
overridden by inference** — if a stop sits in a postcode, that stop wins.

The `assignment` column in `<NOC>_postcodes.csv` records which of the two
produced each row, so the two can always be separated.

A truncated postcode usually reaches several stops, and all of them are listed,
nearest first. That is not ambiguity to be resolved — they are genuinely all
places you could board.
---

## 6. Coverage, and what is missing

### Is the BODS archive complete?

**No, and this should be stated plainly.**

| Measure | Result |
|---|---|
| Active GB bus stops appearing in at least one operator's fare zones | **253,309 of 387,724 — 65.3%** |
| Tyne & Wear (ATCO area `4100`) active stops in a fare zone | **5,940 of 6,365 — 93.3%** |

Tyne & Wear is very well covered by national standards. Nationally, though, a
third of active bus stops have no published fare data from any operator.

Both figures count only stops NaPTAN marks as **active**; 47,460 inactive or
pending stops are excluded, since a fare for a decommissioned stop is not a gap.

### Known gaps and caveats

- **Not every operator publishes fares to BODS.** Fares publication has been
  less consistently complied with than timetable publication. An operator absent
  from the dataset has not necessarily stopped running.
- **Metro and ferry are not included.** This is a bus fares dataset. Nexus
  Tyne and Wear Metro fares are not in the BODS fares archive, so any journey
  involving the Metro will be under-priced. Rail legs are identified and skipped
  rather than guessed at.
- **Multi-operator and network tickets are not modelled.** The dataset prices
  individual operators' own tickets. A Network One ticket covering several Tyne
  and Wear operators is a different product and is not represented. Summing
  single fares across operators will therefore **overestimate** the cost of a
  multi-operator journey.
- **Line-level applicability is recorded but not enforced.** Where a fare is
  attached to a specific route, the line reference is kept, but a stop-to-stop
  query does not verify that a direct service actually runs. Confirming
  serviceability is the routing engine's job.
- **Validity periods are recorded, not filtered.** `valid_from` and `valid_to`
  are exported. Filter on them if you need fares current at a given date.
- **The archive is a snapshot.** Fares change. The dataset is dated by the
  archive it was built from and should be rebuilt periodically; the process is a
  single command.

### Verification

Fare answers from the previous version of this work were frozen as a baseline of
**1,706 stop-to-stop cases** across all 12 publisher accounts serving Tyne &
Wear, before the parser was rewritten. The rebuilt dataset is replayed against
these. The purpose is not to assume the old answers were right, but to ensure
that no difference between old and new goes unnoticed and unexplained.

| Outcome | Cases | Share |
|---|---|---|
| Same price | 1,183 | 69.3% |
| Within 50p | 194 | 11.4% |
| Differs by more than 50p | 329 | 19.3% |
| **No fare found at all** | **0** | **0%** |

No case lost coverage: every stop pair the old dataset could price, the new one
can still price. Of the 523 cases where the price changed, three quarters are
explained by a single fault in the old version:

| Cause | Cases | Share of differences |
|---|---|---|
| Old row was a **child** fare recorded as adult | 222 | 42.4% |
| Old row was a **young person** fare recorded as adult | 153 | 29.3% |
| Old row was a **student** fare recorded as adult | 12 | 2.3% |
| New price higher | 65 | 12.4% |
| New price lower | 71 | 13.6% |

The old pipeline had no concept of passenger class, so a product named
`Trip@U22SGL` — an under-22 single — was recorded simply as "Single" and returned
when an adult fare was asked for. For Stockton Road to Ryhope Road, the old
dataset said £1.00; the new one says £2.50 for an adult and £1.00 for a young
person, and both are in the file. These are corrections, not regressions.

The remaining 136 cases are genuine price differences, consistent with the
archive being a later snapshot than the one the old dataset was built from.

---

## 7. Summary of design decisions

| Decision | Reason |
|---|---|
| Key everything on NOC, never on publisher folder name | Folder names are corporate accounts; one contains 24 operators. |
| Record a provenance tier on every fare row | The data is canonical in parts and inferential in others; the consumer should be able to tell which. |
| Keep the previous heuristics as tier 4 rather than deleting them | They encode real corrections, and 12–18% of files lack canonical structure. |
| Split the old exclusion list into "not a fare" vs "not an adult fare" | The first is correct to exclude; the second is now canonically labelled instead. |
| Record every passenger class rather than adult only | The old dataset conflated them, which mispriced 387 of 1,706 test cases. |
| Exclude capped/promotional fares by default | They expire, and would silently misprice journeys afterwards. |
| Map postcodes to **stops**, not to zones | A zone belongs to a ticket, not a place; a stop can be in 47 zones at once, so postcode-to-zone has no correct answer. |
| Offer both `stops` and `fill` postcode modes | Precision and density are different needs; the column says which you got. |
| Collapse detected flat-fare matrices | Avoids hundreds of thousands of identical rows. |
| Report coverage as a headline number | 65.3% nationally is a material limitation and should not be buried. |
