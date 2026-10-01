"""Place-name normalisation shared by the gazetteer index and the CSV loader.

Both sides of the join must be normalised by the *same* function or the join
silently under-matches. Measured effect of these rules: they are what lifts the
Census-only match rate from 90.5% to 94.8% (see PLAN.md section 2).
"""
from __future__ import annotations

import re
import unicodedata

# Census NAME carries a legal-status suffix: "Abanda CDP", "Kansas City city".
# We index the full form *and* the form with one such suffix removed. Removing
# only one matters: stripping repeatedly turns "Kansas City city" into "Kansas".
_LSAD = (
    r"(CITY|TOWN|CDP|VILLAGE|BOROUGH|MUNICIPALITY|TOWNSHIP|PLANTATION|GORE"
    r"|URBAN COUNTY|METRO GOVERNMENT|METROPOLITAN GOVERNMENT"
    r"|CONSOLIDATED GOVERNMENT|UNIFIED GOVERNMENT|CORPORATION|RESERVATION"
    r"|COMUNIDAD|ZONA URBANA)"
)
_LSAD_SUFFIX = re.compile(rf"^(.*?)\s+{_LSAD}$")
_PARENTHETICAL = re.compile(r"\((?:BALANCE|HISTORICAL)\)")
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_WS = re.compile(r"\s+")

# Abbreviation pairs that genuinely differ between OPIS and the gazetteers,
# e.g. OPIS "Saint Johns" vs Census "St. Johns".
_ABBREV = (
    # SAINTE must precede SAINT: \bSAINT\b does not match inside "SAINTE".
    # This pair alone resolves the two "Sault Sainte Marie" MI stops against
    # Census "Sault Ste. Marie city".
    (re.compile(r"\bSAINTE\b"), "ST"),
    (re.compile(r"\bSAINT\b"), "ST"),
    (re.compile(r"\bSTE\b"), "ST"),
    (re.compile(r"\bFORT\b"), "FT"),
    (re.compile(r"\bMOUNT\b"), "MT"),
)


def normalize_place(name: str) -> str:
    """Fold a place name to a comparable key.

    Strips accents, uppercases, drops punctuation (so "St. Johns" == "ST JOHNS"),
    and collapses whitespace (the CSV pads City to a fixed width).
    """
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = _PARENTHETICAL.sub(" ", s)
    for pattern, repl in _ABBREV:
        s = pattern.sub(repl, s)
    s = _NON_ALNUM.sub(" ", s)
    return _WS.sub(" ", s).strip()


def place_keys(name: str) -> set[str]:
    """Every lookup key a place name should answer to.

    Four variants: normalised, normalised-minus-one-LSAD-suffix, and each of
    those de-spaced. The de-spaced variant is what makes OPIS "Mc Calla" match
    Census "McCalla" and "Mc Graw" match "McGraw".
    """
    base = normalize_place(name)
    if not base:
        return set()
    keys = {base}
    m = _LSAD_SUFFIX.match(base)
    if m and m.group(1):
        keys.add(m.group(1))
    keys |= {k.replace(" ", "") for k in list(keys)}
    return {k for k in keys if k}


US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY "
    "DC".split()
)
CA_PROVINCES = frozenset("AB BC MB NB NL NS NT NU ON PE QC SK YT".split())


def country_for_state(state_code: str) -> str:
    """'US', 'CA', or 'XX' for unrecognised. The CSV mixes US states and Canadian
    provinces in one State column with no country field of its own."""
    code = (state_code or "").strip().upper()
    if code in US_STATES:
        return "US"
    if code in CA_PROVINCES:
        return "CA"
    return "XX"
