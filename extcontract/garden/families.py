"""Crop-family normalisation used by the spring garden handover.

The rule is about the *family* (crop rotation group), not the display crop
name.  The canonical scenario uses Chinese family names ``禾`` and ``豆``; the
small alias table also lets English-language fixtures say ``grain`` and
``bean`` without weakening exact matching for unknown families.
"""

from __future__ import annotations

_FAMILY_ALIASES = {
    "禾": "grain",
    "禾科": "grain",
    "禾本": "grain",
    "禾本科": "grain",
    "grain": "grain",
    "cereal": "grain",
    "grass": "grain",
    "豆": "bean",
    "豆科": "bean",
    "bean": "bean",
    "legume": "bean",
    "leguminous": "bean",
    "soy": "bean",
    "soybean": "bean",
}


def family_key(value: object) -> str:
    """Return the stable comparison key for a reported crop family."""

    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    folded = text.casefold()
    return _FAMILY_ALIASES.get(text, _FAMILY_ALIASES.get(folded, folded))


def same_family(left: object, right: object) -> bool:
    a = family_key(left)
    b = family_key(right)
    return bool(a) and a == b
