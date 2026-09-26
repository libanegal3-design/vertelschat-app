"""Prices, SKUs and page tiers: the single source of truth for the app, the shop links and the docs.

Principles (see pricing-strategy.md): one complete core package; extra value is sold at the moment it becomes
relevant; every price is findable before purchase; quantity prices follow real costs (one parcel, one shipment);
no subscriptions, no reactivation, no countdowns.
"""
from __future__ import annotations

TERMS_VERSION = "v2"        # projects created from 1 Oct 2026; "v1" = everything bought before (grandfathered)

BASE_PAGES = 240            # included in every book price
MAX_PAGES = 400             # one volume; above this the book is split into two volumes
TIER_BOUNDS = (240, 320, 400)
TIER_LABEL = {0: "tot 240 pagina's", 1: "241 tot 320 pagina's", 2: "321 tot 400 pagina's"}
TIER_SHORT = {0: "240", 1: "320", 2: "400"}
TIER_SURCHARGE = {0: 0, 1: 1500, 2: 2900}      # cents per copy

# All prices in cents, incl. VAT, shipping within NL/BE included.
VERTELJAAR = 12900
DUO = 22800                  # two storytellers bought together (second at the family rate)
SECOND_STORYTELLER = 9900    # family rate: an extra storyteller in an existing family
COPY_FIRST = 4500            # first paid copy in a parcel
COPY_NEXT = 3900             # every further copy in the same parcel (no extra shipment)
RENEWAL = 7900               # another 12 months of questions + one book credit
RENEWAL_LEGACY = 6900        # grandfathered for v1 projects: another 12 months without book credit
GIFT_PACKAGE = 1795
LEGACY_GIFT_CARD = 495       # sold until Sep 2026, still honoured

# SKU -> (kind, attributes). The webhook understands every SKU ever sold.
SKUS: dict[str, tuple[str, dict]] = {
    "VT-VERTELJAAR": ("storyteller", {"count": 1}),
    "VT-VERTELJAAR-DUO": ("storyteller", {"count": 2}),
    "VT-EXTRA-VERTELLER": ("storyteller", {"count": 1, "tier": "family"}),
    "VT-VERLENGING-BOEK": ("renewal", {"book_credit": 1}),
    "VT-VERLENGING": ("renewal", {"book_credit": 0, "legacy": True}),
    "VT-CADEAUPAKKET": ("gift_package", {"format": "pakket"}),
    "VT-CADEAUKAART": ("gift_package", {"format": "kaart", "legacy": True}),
    "VT-EXTRA-BOEK": ("book_credit", {"legacy": True}),        # v1: prepaid extra copy, used from the app
    "VT-OMVANG-320": ("page_upgrade", {"tier": 1}),
    "VT-OMVANG-400": ("page_upgrade", {"tier": 2}),
}
for _tier, _sfx in ((0, ""), (1, "-320"), (2, "-400")):
    SKUS[f"VT-BOEK-1{_sfx}"] = ("copy", {"position": "first", "tier": _tier})
    SKUS[f"VT-BOEK-N{_sfx}"] = ("copy", {"position": "next", "tier": _tier})
    SKUS[f"VT-FAMILIE-1{_sfx}"] = ("family_copy", {"position": "first", "tier": _tier})
    SKUS[f"VT-FAMILIE-N{_sfx}"] = ("family_copy", {"position": "next", "tier": _tier})

# Shopify product handles (the theme selects the variant from these options).
HANDLES = {"verteljaar": "verteljaar", "extra_storyteller": "extra-verteller", "copy": "boekexemplaar",
           "family_copy": "familie-exemplaar", "page_upgrade": "dikker-boek", "renewal": "nog-een-verteljaar",
           "gift_package": "cadeaupakket"}


def page_tier(pages: int) -> int | None:
    """0 = included size, 1 or 2 = larger book, None = too thick for one volume."""
    if pages <= TIER_BOUNDS[0]:
        return 0
    if pages <= TIER_BOUNDS[1]:
        return 1
    if pages <= TIER_BOUNDS[2]:
        return 2
    return None


def copy_price(position: str, tier: int) -> int:
    return (COPY_FIRST if position == "first" else COPY_NEXT) + TIER_SURCHARGE[tier]


def parcel_total(paid_copies: int, tier: int, credit_copies: int = 0) -> int:
    """Total to pay for one parcel: credit copies cost only their page surcharge; the first paid copy in a parcel
    without a credit copy costs COPY_FIRST, every further copy COPY_NEXT."""
    total = credit_copies * TIER_SURCHARGE[tier]
    for i in range(paid_copies):
        first = credit_copies == 0 and i == 0
        total += copy_price("first" if first else "next", tier)
    return total


def eur(cents: int, *, short: bool = True) -> str:
    whole, rest = divmod(int(cents), 100)
    if short and rest == 0:
        return f"€{whole}"
    return f"€{whole},{rest:02d}"


def price_list() -> list[dict]:
    """What the storefront and the app show under 'Later uitbreiden kan altijd'."""
    return [
        {"key": "copy", "title": "Extra exemplaar van het boek", "price": eur(COPY_FIRST),
         "detail": f"Elk volgend exemplaar in hetzelfde pakket {eur(COPY_NEXT)}. Altijd na te bestellen, ook na jaren."},
        {"key": "family_copy", "title": "Familie bestelt zelf", "price": eur(COPY_FIRST),
         "detail": "Via een privélink betaalt ieder zijn eigen exemplaar. Zonder toegang tot jullie verhalen."},
        {"key": "second", "title": "Tweede verteller", "price": eur(SECOND_STORYTELLER),
         "detail": "Bijvoorbeeld de andere ouder: een eigen jaar en een eigen boek, in dezelfde familie."},
        {"key": "renewal", "title": "Nog een verteljaar", "price": eur(RENEWAL),
         "detail": "Twaalf maanden nieuwe vragen plus een boektegoed. Niet nodig om bij je verhalen te kunnen."},
        {"key": "pages", "title": "Dikker boek", "price": f"+{eur(TIER_SURCHARGE[1])}",
         "detail": f"Per exemplaar voor 241 tot 320 pagina's, +{eur(TIER_SURCHARGE[2])} tot 400. Je ziet het ruim vooraf."},
        {"key": "gift", "title": "Cadeaupakket per post", "price": eur(GIFT_PACKAGE, short=False),
         "detail": "Kaart met persoonlijke QR-code, uitlegboekje voor de verteller, in een doosje door de brievenbus."},
    ]
