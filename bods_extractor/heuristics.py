"""Legacy string-matching rules, retained as the last-resort extraction tier.

Provenance
----------
Every rule in this module was written in response to a real fare that came out
visibly wrong. They are kept deliberately intact rather than replaced, because
the BODS archive is only partly canonical (see :mod:`bods_extractor.netex`) and
a measurable minority of files genuinely cannot be read any other way:

* 1.2% of files contain no ``<Amount>`` element anywhere, so the price only
  exists inside an identifier such as ``price_band_1.00``.
* 0.4% of files have no ``TypeOfFareProductRef``, so nothing states whether a
  product is a single or a day pass.
* The £2 national fare cap and various promotional products are policy
  artefacts that appear in no canonical NeTEx field at all.

One deliberate change
---------------------
The original single kill list mixed two different concerns:

1. *Not a fare at all* - booking fees, luggage, reservations, dogs, PlusBus
   add-ons, promotional caps. These must still be excluded.
2. *Not an **adult** fare* - child, student, senior, young person, school
   pupil. These were excluded only because the old pipeline had no concept of
   passenger class, so anything non-adult was noise.

Since ``<UserType>`` is now read canonically from 100% of files, group 2 is
**labelled** rather than discarded. A child single is real data that the client
may well want; it simply must not be mistaken for an adult single. Group 1 is
still dropped. The original terms are preserved below so the reasoning stays
auditable.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Group 1: tokens meaning "this amount is not a passenger fare"
# --------------------------------------------------------------------------
#: Retained from the original kill list. Booking fees and luggage charges are
#: encoded as ``UsageParameterPrice`` amounts (1,471 occurrences in the survey
#: sample) and will otherwise be picked up as if they were fares.
NON_FARE_TOKENS = (
    "reservation", "fee", "luggage", "dog", "animal", "companion",
    "plusbus", "addon", "add_on", "booking", "surcharge", "penalty",
)

#: Promotional and capped products. The £2/£3 national bus fare cap is a
#: government scheme with a fixed end date; including it makes an operator's
#: real fare structure invisible. Excluded by default, recoverable by passing
#: ``include_capped=True``.
CAPPED_TOKENS = (
    "gbp_1", "gbp1", "gbp_2", "gbp2", "gbp_3", "gbp3", "cap", "promo",
)

# --------------------------------------------------------------------------
# Group 2: passenger class tokens. No longer used for exclusion - kept because
# they are still the only signal in the ~0% of files with no <UserType>, and
# because they document what the original rules were compensating for.
# --------------------------------------------------------------------------
USER_TYPE_TOKENS = (
    ("child", ("child", "chd", "_ch_", "kid")),
    ("schoolPupil", ("pupil", "school", "sch_")),
    ("student", ("student", "stu_", "uni")),
    ("senior", ("senior", "oap", "elderly", "concession")),
    ("youngPerson", ("young", "_yp", "yp_", "u19", "u21", "u25", "under")),
    ("infant", ("infant", "baby", "toddler")),
    ("employee", ("staff", "employee")),
    ("disabled", ("disabled", "mobility")),
    ("adult", ("adult", "ad_", "_ad")),
)

#: Product-type tokens, ordered so that the more specific classification wins.
#: The original ordering bug - day passes being tested before singles, so every
#: "Day Single" became a pass - is fixed here by testing period, then return,
#: then single, then pass.
PRODUCT_TYPE_TOKENS = (
    ("period", ("week", "7day", "7-day", "period", "season", "monthly",
                "4week", "4w", "28day", "annual", "termly", "1m")),
    ("carnet", ("carnet", "bundle", "multitrip", "multi-trip", "10trip")),
    ("day_return", ("return", "rtn", "two-way", "2trip", "round-trip", "daysaver")),
    ("single", ("single", "sgl", "one-way", "1-way", "onetrip")),
    ("period", ("day", "24hr", "24-hour", "pass", "allmodes", "rover",
                "hopper", "daily", "explorer")),
)

#: Matches a price embedded in an identifier, e.g. ``price_band_1.00`` or
#: ``price_band_1.00@AdultSingle``.
_PRICE_IN_ID = re.compile(r"(?:price_band_|price_|amount_|fare_)(\d+(?:\.\d+)?)")

#: Above this, a "single" classification is implausible for a UK bus fare and
#: the product is far more likely to be a pass. Retained from the original
#: ``price_val < 5.0`` rule but only consulted when nothing else classified it.
SINGLE_FARE_CEILING = 5.0


def _normalise(text: str) -> str:
    return (text or "").lower()


def is_non_fare(text: str, include_capped: bool = False) -> bool:
    """True if the identifier describes a charge that is not a passenger fare.

    Applied to the product name and identifier, not to the price element, so a
    genuine fare is never dropped because it happens to sit near a fee.
    """
    lowered = _normalise(text)
    if any(token in lowered for token in NON_FARE_TOKENS):
        return True
    if not include_capped and any(token in lowered for token in CAPPED_TOKENS):
        return True
    return False


#: Age-capped products are named inconsistently - ``U16``, ``U19``, ``U22``,
#: ``Under21``, ``21&Under``. Enumerating them was always going to miss one
#: (``U22SGL`` was the one that got away), so match the pattern instead.
UNDER_AGE_PATTERN = re.compile(r"u(?:1[0-9]|2[0-6])(?![0-9])")


def guess_user_type(text: str) -> str | None:
    """Tier 4 passenger class. Only reached when no ``<UserType>`` exists."""
    lowered = _normalise(text)
    for user_type, tokens in USER_TYPE_TOKENS:
        if any(token in lowered for token in tokens):
            return user_type
    if UNDER_AGE_PATTERN.search(lowered):
        return "youngPerson"
    return None


def guess_product_type(text: str) -> str | None:
    """Tier 4 product classification. Only reached when no product ref exists."""
    lowered = _normalise(text)
    for product_type, tokens in PRODUCT_TYPE_TOKENS:
        if any(token in lowered for token in tokens):
            return product_type
    return None


def product_type_from_price(price: float) -> str:
    """Absolute last resort: classify by magnitude.

    A UK single bus fare above about £5 is very unusual, so an unclassified
    product priced higher is more likely a pass. This is the weakest rule in
    the codebase and rows relying on it are marked ``source_tier = 4``.
    """
    return "single" if price < SINGLE_FARE_CEILING else "period"


def price_from_identifier(*identifiers: str) -> float | None:
    """Recover a price encoded in an element id or reference.

    Needed for the 1.2% of files with no ``<Amount>`` element at all, where
    ``price_band_1.00`` is the only place the number appears.
    """
    for identifier in identifiers:
        if not identifier:
            continue
        match = _PRICE_IN_ID.search(identifier)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def commuter_score(text: str) -> int:
    """Original ranking heuristic, preserved for fare *selection*.

    Extraction no longer needs this - product type and user type are read
    canonically - but choosing which of several valid tickets to quote a
    commuter is a judgement call, and this encodes the original judgement:
    prefer adult, network-wide, standard, daily products.
    """
    lowered = _normalise(text)
    score = 0
    if "adult" in lowered or "ad_" in lowered:
        score += 100
    if any(token in lowered for token in ("network", "all", "zone")):
        score += 50
    if any(token in lowered for token in ("standard", "any")):
        score += 50
    if any(token in lowered for token in ("day", "daily")):
        score += 30
    if any(token in lowered for token in ("return", "rtn")):
        score += 30
    return score
