"""NeTEx fare extraction with explicit provenance tiers.

Why tiers
---------
A survey of the whole BODS archive (``tools/netex_survey.py``) shows the data
is only *partly* canonical:

===============================================  ========
``<Operator id="noc:...">``                        100.0%
``<UserType>``                                     100.0%
``<ValidBetween>``                                 100.0%
``<TypeOfFareProductRef>``                          99.6%
some ``<Amount>``                                   98.8%
files with **no zone element at all**               12.2%
files with **no** ``DistanceMatrixElementPrice``    17.7%
files carrying a ``FareTable``/``Cell`` structure    86.4%
===============================================  ========

and ``<Amount>`` appears under four different parents, only two of which are
fares (``GeographicalIntervalPrice`` and ``DistanceMatrixElementPrice``);
``UsageParameterPrice`` is a booking fee or discount and
``TimeIntervalPrice`` is a duration-priced pass.

So we resolve each fact through an ordered chain and record which link
succeeded:

``tier 1`` canonical
    Read directly from an element that unambiguously owns the fact: a
    ``TypeOfFareProductRef``, a ``UserType`` reached through its tariff, a
    resolved ``GeographicalIntervalPriceRef``, or an ``<Amount>`` written
    inline on the price element itself.
``tier 2`` structural
    Derived by following a weaker but still schema-driven route when the direct
    element is absent: the minimum of a price group, or the only passenger class
    the file mentions.
``tier 3`` dialect
    A recognised but non-preferred encoding: identifier conventions the UK
    profile documents, fare tables, whole-file flat fares.
``tier 4`` heuristic
    The legacy string-matching rules in :mod:`bods_extractor.heuristics`,
    retained verbatim because they were written in response to real, observed
    breakage. They only run when tiers 1-3 produce nothing.

Nothing is discarded; the tiers only change the *order* in which knowledge is
applied, and make the fallback rate measurable per operator.
"""
from __future__ import annotations

import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from lxml import etree

from . import heuristics

# ``<Operator>`` elements for ATOC and Network Rail are boilerplate in every
# BODS fares file. Only ids in the ``noc:`` namespace are real bus operators.
NOC_PREFIX = "noc:"

#: ``TypeOfFareProductRef`` is a closed vocabulary in the UK fares profile.
#: These five values are the only ones present anywhere in the archive.
PRODUCT_TYPE_BY_REF = {
    "fxc:standard_product@trip@single": "single",
    "fxc:standard_product@trip@day_return": "day_return",
    "fxc:standard_product@pass@period": "period",
    "fxc:standard_product@carnet@trips": "carnet",
    "fxc:standard_product@carnet@days": "carnet",
}

#: ``<ProductType>`` is the secondary, less consistently populated source.
PRODUCT_TYPE_BY_ELEMENT = {
    "singleTrip": "single",
    "dayReturnTrip": "day_return",
    "shortTrip": "single",
    "periodPass": "period",
    "dayPass": "period",
    "amountOfPriceUnitProduct": "carnet",
    "tripCarnet": "carnet",
    "multitrip": "carnet",
}

#: Sentinel used for the origin/destination of a fare that applies across a
#: whole network rather than between two zones (12.2% of files).
ANY_ZONE = "ANY"

#: Prices below this are almost always fees, discounts or data errors rather
#: than a bus fare. Kept deliberately low so genuine cheap singles survive.
MIN_PLAUSIBLE_FARE = 0.20


def _text(parent, tag: str) -> str:
    """Return the stripped text of a direct child, or ''."""
    el = parent.find("{*}" + tag)
    return el.text.strip() if el is not None and el.text else ""


def _deep_text(parent, tag: str) -> str:
    """Return the stripped text of the first descendant with this local name."""
    el = next(parent.iter("{*}" + tag), None)
    return el.text.strip() if el is not None and el.text else ""


def _clean_atco(ref: str) -> str:
    """Strip the NeTEx namespace prefix from a scheduled stop point reference."""
    return ref.replace("atco:", "").replace("naptan:", "").strip()


@dataclass
class Operator:
    """Identity of the operator that actually runs the service.

    This is the fix for the central naming problem: BODS groups publish under a
    corporate account (``Go-Ahead Group plc_10``) but each file names its real
    operator, so Carousel Buses can be recovered from a Go-Ahead upload.
    """
    noc: str
    public_code: str = ""
    name: str = ""
    trading_name: str = ""
    town: str = ""
    postcode: str = ""


@dataclass
class Product:
    """A purchasable fare product, resolved to a product type and user class."""
    product_id: str
    name: str = ""
    product_type: str = "unknown"
    user_type: str = "unknown"
    product_type_tier: int = 4
    user_type_tier: int = 4


@dataclass
class Fare:
    """One priced origin/destination pair for one product."""
    origin_zone: str
    destination_zone: str
    product_type: str
    user_type: str
    product_name: str
    price: float
    line_id: str = ""
    source_tier: int = 4
    source_detail: str = ""


@dataclass
class ParsedFile:
    """Everything extracted from a single NeTEx fare file."""
    operator: Operator | None = None
    lines: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    zones: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    fares: list[Fare] = field(default_factory=list)
    valid_from: str = ""
    valid_to: str = ""
    tier_counts: Counter = field(default_factory=Counter)
    zoneless: bool = False
    unpriced: bool = False


# --------------------------------------------------------------------------
# Identity, validity, lines and zones
# --------------------------------------------------------------------------

def extract_operator(root) -> Operator | None:
    """Tier 1. Read the real operator from ``<Operator id="noc:...">``.

    Present in 100% of sampled files. Falls back to nothing rather than
    guessing, because a wrong NOC is worse than an absent one.
    """
    for el in root.iter("{*}Operator"):
        oid = el.get("id", "")
        if not oid.startswith(NOC_PREFIX):
            continue
        address = el.find("{*}Address")
        return Operator(
            noc=oid[len(NOC_PREFIX):].strip(),
            public_code=_text(el, "PublicCode"),
            name=_text(el, "Name"),
            trading_name=_text(el, "TradingName"),
            town=_text(address, "Town") if address is not None else "",
            postcode=_text(address, "PostCode") if address is not None else "",
        )
    return None


def extract_validity(root) -> tuple[str, str]:
    """Tier 1. Read the product's validity window from ``<ValidBetween>``.

    Present in 100% of sampled files. Without this, withdrawn fares from years
    ago are indistinguishable from current ones.
    """
    el = next(root.iter("{*}ValidBetween"), None)
    if el is None:
        return "", ""
    return _text(el, "FromDate"), _text(el, "ToDate")


def extract_lines(root) -> dict[str, tuple[str, str, str]]:
    """Tier 1. Most BODS fares are line-scoped, not network-scoped.

    Recording the line means a fare that only applies on route 103 is not
    silently offered for every journey the operator runs.
    """
    lines = {}
    for el in root.iter("{*}Line"):
        line_id = el.get("id", "")
        if line_id:
            lines[line_id] = (_text(el, "PublicCode"),
                              _text(el, "Name"),
                              _text(el, "Description"))
    return lines


def extract_zones(root) -> dict[str, tuple[str, list[str]]]:
    """Tier 1. Fare zones and their member stops, keeping the human name.

    The zone *name* ("Big Lamp") is what makes an exported CSV checkable
    against an operator's published zone map; the raw id ("fs@12155") is not.
    """
    zones: dict[str, tuple[str, list[str]]] = {}
    for tag in ("FareZone", "TariffZone"):
        for el in root.iter("{*}" + tag):
            zone_id = el.get("id", "")
            if not zone_id:
                continue
            name = _text(el, "Name")
            atcos = [
                _clean_atco(ref.get("ref", ""))
                for ref in el.iter("{*}ScheduledStopPointRef")
                if ref.get("ref")
            ]
            existing_name, existing = zones.get(zone_id, ("", []))
            zones[zone_id] = (name or existing_name,
                              existing + [a for a in atcos if a])
    return zones


# --------------------------------------------------------------------------
# Products: what is being sold, and to whom
# --------------------------------------------------------------------------

def _user_profiles_by_element(root) -> dict[str, str]:
    """Map every ``FareStructureElement`` id to the user type it restricts to.

    Two passes, both canonical.

    **Direct.** Some elements carry the ``UserProfile``/``UserType`` themselves,
    hung off a ``GenericParameterAssignment``.

    **Via the enclosing ``Tariff``.** In the UK profile a tariff is split into
    several fare structure elements with distinct jobs - ``@access``,
    ``@eligibility``, ``@conditions_of_travel`` - and only the eligibility one
    names the passenger class::

        <Tariff id="Tariff@AdultSingle@Line_GNEL:...">
          <FareStructureElement id="Tariff@AdultSingle@access"/>
          <FareStructureElement id="Tariff@AdultSingle@eligibility">
            ... <UserType>adult</UserType>
          <FareStructureElement id="Tariff@AdultSingle@conditions_of_travel"/>

    Critically, the product does **not** reference the eligibility element. It
    references ``@access`` and ``@conditions_of_travel`` only. So resolving the
    passenger class means going from the referenced element up to its tariff and
    back down to the eligibility sibling.

    This is structural containment defined by the schema, not a guess about
    naming, so it stays tier 1. Missing it is the difference between reading Go
    North East's passenger classes canonically and having to infer them: it
    covers the large majority of rows in the archive.

    A tariff that mixes passenger classes is left unmapped rather than guessed
    at, and falls through to the tier 2/4 paths in :func:`extract_products`.
    """
    mapping: dict[str, str] = {}

    for element in root.iter("{*}FareStructureElement"):
        element_id = element.get("id", "")
        if not element_id:
            continue
        user_type = _deep_text(element, "UserType")
        if user_type:
            mapping[element_id] = user_type

    for tariff in root.iter("{*}Tariff"):
        siblings = [el.get("id", "") for el in tariff.iter("{*}FareStructureElement")]
        declared = {mapping[el] for el in siblings if mapping.get(el)}
        if len(declared) != 1:
            continue  # ambiguous or silent; do not invent an answer
        user_type = declared.pop()
        for element_id in siblings:
            if element_id:
                mapping.setdefault(element_id, user_type)

    return mapping


def extract_products(root) -> dict[str, Product]:
    """Resolve every fare product to a product type and a user type.

    Tier 1 for both facts where the canonical elements exist; tier 2 when the
    product type has to come from ``<ProductType>`` or the user type from a
    file that only mentions one class of passenger; tier 4 when neither works
    and the legacy string rules take over.
    """
    element_users = _user_profiles_by_element(root)

    # File-level fallback: if the whole file only ever mentions one user type,
    # an unlinked product almost certainly means that one.
    all_user_types = {
        el.text.strip() for el in root.iter("{*}UserType") if el.text and el.text.strip()
    }
    sole_user_type = all_user_types.pop() if len(all_user_types) == 1 else ""

    products: dict[str, Product] = {}
    for tag in ("PreassignedFareProduct", "AmountOfPriceUnitProduct", "FareProduct"):
        for el in root.iter("{*}" + tag):
            product_id = el.get("id", "")
            if not product_id:
                continue
            product = Product(product_id=product_id, name=_text(el, "Name"))

            # --- product type ---
            type_ref = el.find("{*}TypeOfFareProductRef")
            ref = type_ref.get("ref", "") if type_ref is not None else ""
            if ref in PRODUCT_TYPE_BY_REF:
                product.product_type = PRODUCT_TYPE_BY_REF[ref]
                product.product_type_tier = 1
            else:
                element_type = _text(el, "ProductType")
                if element_type in PRODUCT_TYPE_BY_ELEMENT:
                    product.product_type = PRODUCT_TYPE_BY_ELEMENT[element_type]
                    product.product_type_tier = 2
                else:
                    guessed = heuristics.guess_product_type(f"{product_id} {product.name} {ref}")
                    if guessed:
                        product.product_type = guessed
                        product.product_type_tier = 4

            # --- user type ---
            linked = [
                r.get("ref", "")
                for r in el.iter("{*}FareStructureElementRef")
                if r.get("ref")
            ]
            for element_id in linked:
                if element_id in element_users:
                    product.user_type = element_users[element_id]
                    product.user_type_tier = 1
                    break
            else:
                inline = _deep_text(el, "UserType")
                if inline:
                    product.user_type = inline
                    product.user_type_tier = 1
                elif sole_user_type:
                    product.user_type = sole_user_type
                    product.user_type_tier = 2
                else:
                    guessed = heuristics.guess_user_type(f"{product_id} {product.name}")
                    if guessed:
                        product.user_type = guessed
                        product.user_type_tier = 4

            products[product_id] = product
    return products


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------

def extract_price_points(root) -> dict[str, float]:
    """Tier 1. ``GeographicalIntervalPrice`` id -> amount.

    This is the element that genuinely holds a fare. Deliberately does *not*
    scan ``UsageParameterPrice`` (booking fees, discounts) or
    ``TimeIntervalPrice`` (duration-priced passes); those are handled
    separately so they cannot contaminate point-to-point fares.
    """
    prices = {}
    for el in root.iter("{*}GeographicalIntervalPrice"):
        price_id = el.get("id", "")
        amount = _text(el, "Amount")
        if price_id and amount:
            try:
                prices[price_id] = float(amount)
            except ValueError:
                continue
    return prices


def extract_price_groups(root) -> dict[str, list[str]]:
    """Map ``PriceGroup`` id -> the price ids it contains.

    Used by the tier 2 route, where a distance matrix element references a
    price *group* rather than naming a price directly.
    """
    groups: dict[str, list[str]] = {}
    for el in root.iter("{*}PriceGroup"):
        group_id = el.get("id", "")
        if not group_id:
            continue
        members = [
            m.get("id", "") for m in el.iter("{*}GeographicalIntervalPrice") if m.get("id")
        ]
        groups[group_id] = members
    return groups


def extract_matrix(root) -> dict[str, tuple[str, str, list[str]]]:
    """Map ``DistanceMatrixElement`` id -> (origin zone, dest zone, price groups)."""
    matrix = {}
    for el in root.iter("{*}DistanceMatrixElement"):
        element_id = el.get("id", "")
        if not element_id:
            continue
        start = el.find(".//{*}StartTariffZoneRef")
        end = el.find(".//{*}EndTariffZoneRef")
        groups = [
            g.get("ref", "") for g in el.iter("{*}PriceGroupRef") if g.get("ref")
        ]
        matrix[element_id] = (
            start.get("ref", "") if start is not None else "",
            end.get("ref", "") if end is not None else "",
            groups,
        )
    return matrix


#: Element names that reference a fare product.
_PRODUCT_REF_TAGS = (
    "PreassignedFareProductRef", "FareProductRef", "AmountOfPriceUnitProductRef",
)


def _product_ref_on(node, products: dict[str, Product]) -> Product | None:
    """Return the product referenced by a direct child or ``pricesFor`` child.

    Note the explicit ``is not None`` tests. An lxml element with no children
    is falsy, and every ``*Ref`` element is childless, so the tempting
    ``a.find(x) or a.find(y)`` idiom silently discards a valid first match.
    """
    for tag in _PRODUCT_REF_TAGS:
        for path in ("{*}" + tag, "{*}pricesFor/{*}" + tag):
            ref_el = node.find(path)
            if ref_el is not None:
                ref = ref_el.get("ref", "")
                if ref in products:
                    return products[ref]
    return None


def _product_for_price(element, products: dict[str, Product]) -> tuple[Product | None, int]:
    """Work out which product a price element belongs to.

    Returns the product and the tier of the linkage.

    Tier 1 covers both a reference inside the price element itself and one
    inherited from the enclosing ``FareTable``. The UK fares profile puts
    ``pricesFor/PreassignedFareProductRef`` on the table and lets the cells
    inherit it, so an inherited reference is just as canonical as a local one.
    Tier 3 is reserved for reading the product out of the id string, or
    deducing it because the file describes exactly one product.
    """
    # Tier 1a: an explicit reference inside this price element.
    for tag in _PRODUCT_REF_TAGS:
        ref_el = element.find(".//{*}" + tag)
        if ref_el is not None and ref_el.get("ref", "") in products:
            return products[ref_el.get("ref")], 1

    # Tier 1b: inherited from an enclosing fare table.
    ancestor = element.getparent()
    depth = 0
    while ancestor is not None and depth < 8:
        product = _product_ref_on(ancestor, products)
        if product is not None:
            return product, 1
        ancestor = ancestor.getparent()
        depth += 1

    # Tier 3: the id convention, e.g. "Trip@AdultSingle-SOP@Line_103N@12154+12155"
    # whose product id is the part before "-SOP@".
    element_id = element.get("id", "")
    if element_id:
        candidate = element_id.split("-SOP@")[0]
        if candidate in products:
            return products[candidate], 3
        for product_id in products:
            if element_id.startswith(product_id):
                return products[product_id], 3

    # Single-product file: unambiguous by elimination.
    if len(products) == 1:
        return next(iter(products.values())), 3
    return None, 4


def _resolve_amount(element, price_points: dict[str, float],
                    price_groups: dict[str, list[str]],
                    matrix_entry) -> tuple[float | None, int, str]:
    """Find the money for one price element, cheapest-confidence-first.

    Returns ``(amount, tier, detail)``.
    """
    # Tier 1: a named GeographicalIntervalPrice reference. This is the route
    # used by 76.9% of files, where the price element holds a ref not a number.
    ref_el = element.find(".//{*}GeographicalIntervalPriceRef")
    if ref_el is not None:
        ref = ref_el.get("ref", "")
        if ref in price_points:
            return price_points[ref], 1, "GeographicalIntervalPriceRef"

    # Tier 1: an amount written directly on the price element (5.4% of files,
    # but the house style of some large operators - all of Stagecoach's fares
    # arrive this way). The UK profile prefers a reference, but an inline
    # <Amount> is the most direct possible statement of a price: there is no
    # indirection to resolve and nothing to get wrong. Grading it below a
    # reference would misrepresent whole operators as low-confidence.
    amount = _text(element, "Amount")
    if amount:
        try:
            return float(amount), 1, "inline Amount"
        except ValueError:
            pass

    # Tier 2: follow the distance matrix element's price group. A group can
    # hold several prices (one per user class); take the lowest so we never
    # overstate a fare, and say so in the detail column.
    if matrix_entry:
        _, _, group_refs = matrix_entry
        candidates = [
            price_points[pid]
            for ref in group_refs
            for pid in price_groups.get(ref, [])
            if pid in price_points
        ]
        if candidates:
            return min(candidates), 2, "PriceGroupRef (min of group)"

    # Tier 4: the legacy id-embedded price, e.g. "price_band_1.00". Retained
    # because 1.2% of files carry no <Amount> element anywhere at all.
    guessed = heuristics.price_from_identifier(
        element.get("id", ""),
        ref_el.get("ref", "") if ref_el is not None else "",
    )
    if guessed is not None:
        return guessed, 4, "price parsed from identifier"

    return None, 4, ""


def extract_fares(root, products, zones, lines,
                  include_capped: bool = False) -> tuple[list[Fare], Counter, bool]:
    """Build the priced origin/destination rows for one file."""
    price_points = extract_price_points(root)
    price_groups = extract_price_groups(root)
    matrix = extract_matrix(root)
    tiers = Counter()
    fares: list[Fare] = []

    line_id = next(iter(lines), "")

    def add(origin, destination, product, price, tier, detail):
        if price is None or price < MIN_PLAUSIBLE_FARE:
            return
        label = f"{product.product_id} {product.name}" if product else ""
        # Drop booking fees, luggage charges and capped promotional products.
        # Passenger class is *not* filtered here - it is recorded in
        # `user_type` instead. See bods_extractor.heuristics for why.
        if label and heuristics.is_non_fare(label, include_capped=include_capped):
            return
        product_type = product.product_type if product else "unknown"
        if product_type == "unknown":
            # Weakest rule in the codebase; always lands the row in tier 4.
            product_type = heuristics.product_type_from_price(price)
            tier = 4
            detail = (detail + "; type inferred from price").lstrip("; ")
        fares.append(Fare(
            origin_zone=origin or ANY_ZONE,
            destination_zone=destination or ANY_ZONE,
            product_type=product_type,
            user_type=product.user_type if product else "unknown",
            product_name=(product.name or product.product_id) if product else "",
            price=round(price, 2),
            line_id=line_id,
            source_tier=tier,
            source_detail=detail,
        ))
        tiers[tier] += 1

    # ---- primary route: explicit per-pair prices ------------------------
    priced_pairs = set()
    for element in root.iter("{*}DistanceMatrixElementPrice"):
        ref_el = element.find(".//{*}DistanceMatrixElementRef")
        matrix_id = ref_el.get("ref", "") if ref_el is not None else ""
        matrix_entry = matrix.get(matrix_id)
        origin, destination = (matrix_entry[0], matrix_entry[1]) if matrix_entry else ("", "")

        product, link_tier = _product_for_price(element, products)
        amount, price_tier, detail = _resolve_amount(element, price_points,
                                                     price_groups, matrix_entry)
        if amount is None:
            continue

        # A row is only as trustworthy as its weakest link, so record both the
        # overall tier and which link was responsible for it.
        contributions = {
            "price": price_tier,
            "product link": link_tier,
            "product type": product.product_type_tier if product else 4,
            "user type": product.user_type_tier if product else 4,
        }
        tier = max(contributions.values())
        if tier > price_tier:
            weakest = ", ".join(k for k, v in contributions.items() if v == tier)
            detail = f"{detail} (tier set by {weakest})"
        add(origin, destination, product, amount, tier, detail)
        priced_pairs.add(matrix_id)

    # ---- secondary route: matrix elements with a price group but no
    # explicit price element (the 17.7% with no DistanceMatrixElementPrice) --
    for matrix_id, entry in matrix.items():
        if matrix_id in priced_pairs:
            continue
        origin, destination, group_refs = entry
        candidates = [
            (pid, price_points[pid])
            for ref in group_refs
            for pid in price_groups.get(ref, [])
            if pid in price_points
        ]
        if not candidates:
            continue
        for price_id, amount in candidates:
            # Price ids conventionally carry the product suffix, e.g.
            # "price_band_1.00@AdultSingle", which recovers the product.
            product = None
            suffix = price_id.split("@")[-1] if "@" in price_id else ""
            for candidate_product in products.values():
                if suffix and (suffix in candidate_product.product_id
                               or suffix in candidate_product.name.replace(" ", "")):
                    product = candidate_product
                    break
            if product is None and len(products) == 1:
                product = next(iter(products.values()))
            tier = max(2, product.product_type_tier if product else 4)
            add(origin, destination, product, amount, tier, "PriceGroup without price element")

    # ---- flat fares: files with no zonal structure at all (12.2%) --------
    zoneless = not zones
    if zoneless and not fares:
        # A network-wide flat fare. The price still lives in a
        # GeographicalIntervalPrice, it just is not tied to a zone pair.
        for price_id, amount in price_points.items():
            product = None
            for candidate_product in products.values():
                if candidate_product.product_id in price_id or price_id.endswith(
                        candidate_product.product_id.split("@")[-1]):
                    product = candidate_product
                    break
            if product is None and len(products) == 1:
                product = next(iter(products.values()))
            add(ANY_ZONE, ANY_ZONE, product, amount, 3, "flat fare (no zones in file)")

    return fares, tiers, zoneless


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_file(xml_bytes: bytes, include_capped: bool = False) -> ParsedFile | None:
    """Parse one NeTEx fare file into structured, provenance-tagged records.

    Returns ``None`` for files that are unparseable or carry no ``noc:``
    operator, which in practice means they are not bus fare data.
    """
    try:
        root = etree.parse(io.BytesIO(xml_bytes)).getroot()
    except etree.XMLSyntaxError:
        return None

    operator = extract_operator(root)
    if operator is None or not operator.noc:
        return None

    result = ParsedFile(operator=operator)
    result.valid_from, result.valid_to = extract_validity(root)
    result.lines = extract_lines(root)
    result.zones = extract_zones(root)

    products = extract_products(root)
    result.fares, result.tier_counts, result.zoneless = extract_fares(
        root, products, result.zones, result.lines, include_capped=include_capped)
    result.unpriced = not result.fares
    return result
