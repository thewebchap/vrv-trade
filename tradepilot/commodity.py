"""Physical commodity identity: ISRI grades, metal, form and physical recovery.

ISRI grades describe material, never price. Only grades whose definitions are well
established are listed; an unlisted grade name is simply not recognised (the comparison
then falls back to metal/form checks and, if needed, a semantic opinion that can never
produce a MISMATCH on its own).

Physical recovery ("Insulated Aluminium Wire Scrap with 55% Aluminium recovery",
"Recovery 58% on Average basis of Entire Lot") is a quality characteristic. Commercial
payable-metal factors ("LME CSP x 90%") are pricing terms and are never read here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# grade: (metal, form keywords that define it, short definition)
ISRI = {
    "BARLEY": ("COPPER", {"WIRE"}, "No.1 bare bright copper wire"),
    "BERRY": ("COPPER", {"WIRE"}, "No.1 copper wire"),
    "BIRCH": ("COPPER", {"WIRE"}, "No.2 copper wire"),
    "CANDY": ("COPPER", set(), "No.1 heavy copper solids and tubing"),
    "CLIFF": ("COPPER", set(), "No.2 copper solids and tubing"),
    "DREAM": ("COPPER", set(), "light copper"),
    "DRUID": ("COPPER", {"INSULATED", "WIRE"}, "insulated copper wire scrap"),
    "TWANG": ("ALUMINIUM", {"INSULATED", "WIRE"}, "insulated aluminium wire scrap"),
    "TAINT": ("ALUMINIUM", {"SHEET"}, "clean mixed old alloy sheet aluminium"),
    "TABOR": ("ALUMINIUM", {"SHEET"}, "clean mixed old alloy sheet aluminium"),
    "TENSE": ("ALUMINIUM", {"CASTING"}, "mixed aluminium castings"),
    "ZORBA": ("NONFERROUS", {"SHRED"}, "shredded non-ferrous scrap"),
    "HONEY": ("BRASS", set(), "yellow brass scrap"),
    "EBONY": ("BRASS", set(), "composition or red brass"),
    "RAINS": ("LEAD", {"BATTER"}, "scrap drained/dry whole intact lead batteries"),
}

METAL_WORDS = [("STAINLESS", r"STAINLESS"), ("COPPER", r"\bCOPPER\b|\bCU\b"),
               ("ALUMINIUM", r"ALUMIN(I)?UM|\bALU\b"), ("LEAD", r"\bLEAD\b|\bPB\b"), ("BRASS", r"\bBRASS\b"),
               ("ZINC", r"\bZINC\b"), ("NICKEL", r"\bNICKEL\b"), ("STEEL", r"\bSTEEL\b|\bHMS\b"),
               ("IRON", r"\bIRON\b")]
FORM_WORDS = {"INSULATED": r"INSULAT", "WIRE": r"\bWIRES?\b|CABLE", "BATTER": r"BATTER", "SHEET": r"SHEET",
              "CASTING": r"CASTING", "SHRED": r"SHRED"}


@dataclass
class Commodity:
    text: str
    grades: set[str]
    metal: str | None          # primary metal: the first one named
    forms: set[str]


def describe(text: str | None) -> Commodity:
    u = (text or "").upper()
    grades = {g for g in ISRI if re.search(rf"\b{g}\b", u)}
    hits = sorted((m.start(), name) for name, rx in METAL_WORDS for m in [re.search(rx, u)] if m)
    metal = hits[0][1] if hits else None
    if metal is None and grades:
        metal = ISRI[sorted(grades)[0]][0]
    forms = {f for f, rx in FORM_WORDS.items() if re.search(rx, u)}
    return Commodity(text or "", grades, metal, forms)


def compatible(a: Commodity, b: Commodity) -> tuple[str, str]:
    """('EQ' | 'NEQ' | 'UNKNOWN', reason). Deterministic: grades and metals, not wording."""
    if a.grades and b.grades:
        if a.grades & b.grades:
            return "EQ", f"same ISRI grade {sorted(a.grades & b.grades)}"
        return "NEQ", f"different ISRI grades {sorted(a.grades)} vs {sorted(b.grades)}"
    if a.grades or b.grades:
        graded, plain = (a, b) if a.grades else (b, a)
        g = sorted(graded.grades)[0]
        metal, forms, definition = ISRI[g]
        if plain.metal and metal != "NONFERROUS" and plain.metal != metal:
            return "NEQ", f"ISRI {g} is {definition}, but the other document describes {plain.metal.lower()}"
        if plain.metal == metal and forms <= plain.forms:
            return "EQ", f"'{plain.text[:60]}' matches the ISRI {g} definition ({definition})"
        return "UNKNOWN", f"ISRI {g} ({definition}) vs '{plain.text[:60]}': not determinable from wording"
    if a.metal and b.metal and a.metal != b.metal:
        return "NEQ", f"different metals: {a.metal.lower()} vs {b.metal.lower()}"
    return "UNKNOWN", "no ISRI grade or metal difference to decide on"


RECOVERY = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:(?:OF\s+)?[A-Z]+\s+)?RECOVERY|RECOVERY\s*:?\s*(\d+(?:\.\d+)?)\s*%", re.I)


def physical_recoveries(text: str | None) -> list[float]:
    """Physical recovery percentages stated in a material description / specification line."""
    return [float(m.group(1) or m.group(2)) for m in RECOVERY.finditer(text or "")]


def recovery_is_average(text: str | None) -> bool:
    return bool(re.search(r"AVERAGE|AVG|ENTIRE\s+LOT|\bMIN", (text or "").upper()))
