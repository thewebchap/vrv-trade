"""Deterministic normalisation. Kept separate from extraction.

Conservative by design: when a raw string has more than one plausible
reading, return ambiguous=True rather than guessing silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# --------------------------------------------------------------------------- numbers

EU_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{3})+,\d{1,6}\b")       # 188.926,90
US_PATTERN = re.compile(r"\b\d{1,3}(?:,\d{2,3})+\.\d{1,6}\b")     # 188,926.90 / 1,92,075.04


def detect_number_locale(text: str) -> str | None:
    """'EU' if the document uses '.' thousands + ',' decimals, 'US' for the
    opposite, None if there is no unambiguous evidence either way."""
    eu, us = len(EU_PATTERN.findall(text)), len(US_PATTERN.findall(text))
    if eu and not us:
        return "EU"
    if us and not eu:
        return "US"
    if eu and us:
        return "EU" if eu > 2 * us else "US" if us > 2 * eu else None
    return None


@dataclass
class ParsedNumber:
    value: float | None
    ambiguous: bool = False
    note: str = ""
    alt: float | None = None      # the other plausible reading when ambiguous


def parse_number(raw: str | None, locale: str | None = None) -> ParsedNumber:
    """Parse a printed number. Handles US (1,234.56), EU (1.234,56),
    Indian lakh grouping (1,92,075.04) and common OCR separator slips (47.664.15)."""
    if raw is None:
        return ParsedNumber(None)
    # digits, allowing separators (optionally followed by one OCR-inserted space) between digits
    m = re.search(r"\d(?:\d|[.,]\s?(?=\d))*", str(raw))
    if not m:
        return ParsedNumber(None)
    s = re.sub(r"\s+", "", m.group(0))
    seps = [c for c in s if c in ".,"]
    ambiguous, note, alt = False, "", None

    if not seps:
        v = float(s)
    elif "." in seps and "," in seps:
        dec = s[max(s.rfind("."), s.rfind(","))]
        grp = "," if dec == "." else "."
        head, _, tail = s.rpartition(dec)
        if dec in head:
            ambiguous, note = True, "decimal separator appears more than once"
        v = float(head.replace(grp, "").replace(dec, "") + "." + tail)
    else:
        sep = seps[0]
        groups = s.split(sep)
        if len(groups) > 2:
            if len(groups[-1]) == 3 and all(len(g) in (2, 3) for g in groups[1:]):
                v = float("".join(groups))                       # 1,000,000 / 1.000.000
                if sep == "." and locale != "EU":
                    ambiguous, note = True, f"'{m.group(0)}' could be EU thousands grouping or a misprint"
                    alt = float(groups[0] + "." + "".join(groups[1:]))
            else:                                                # OCR: 47.664.15 -> 47664.15
                v = float("".join(groups[:-1]) + "." + groups[-1])
                note = f"read last '{sep}' as decimal separator"
        else:
            head, tail = groups
            if len(tail) == 3 and len(head) <= 3:
                # 1,370 : thousands (US) or decimals (EU)? Use the document locale.
                if locale == "EU":
                    v = float(f"{head}.{tail}") if sep == "," else float(head + tail)
                elif locale == "US":
                    v = float(head + tail) if sep == "," else float(f"{head}.{tail}")
                else:
                    v, alt = float(head + tail), float(f"{head}.{tail}")
                    if sep == ".":
                        v, alt = alt, v
                    ambiguous, note = True, f"'{m.group(0)}' is ambiguous without a known number locale"
            elif sep == "," and len(tail) > 3 and locale != "EU":
                v = float(f"{head}.{tail}")
                ambiguous, note = True, f"'{m.group(0)}': comma followed by {len(tail)} digits is malformed"
            else:
                v = float(f"{head}.{tail}")
    return ParsedNumber(v, ambiguous, note, alt)


# --------------------------------------------------------------------------- units

WEIGHT_UNITS = {
    "MT": 1.0, "MTS": 1.0, "M/T": 1.0, "TON": 1.0, "TONS": 1.0, "TONNE": 1.0, "TONNES": 1.0,
    "METRIC TON": 1.0, "METRIC TONS": 1.0, "METRIC TONNES": 1.0, "T": 1.0,
    "KG": 0.001, "KGS": 0.001, "KILOGRAM": 0.001, "KILOGRAMS": 0.001,
}


def normalize_weight_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = re.sub(r"[^A-Z/ ]", "", unit.upper()).strip()
    if u in WEIGHT_UNITS:
        return "MT" if WEIGHT_UNITS[u] == 1.0 else "KG"
    for k in sorted(WEIGHT_UNITS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(k)}\b", u):
            return "MT" if WEIGHT_UNITS[k] == 1.0 else "KG"
    return None   # PCS, BALES, etc. are counts, never weights


def to_mt(value: float, unit: str | None) -> float | None:
    u = normalize_weight_unit(unit)
    if u == "MT":
        return value
    if u == "KG":
        return value / 1000.0
    return None


# --------------------------------------------------------------------------- dates

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


@dataclass
class ParsedDate:
    value: date | None
    ambiguous: bool = False
    note: str = ""


def parse_date(raw: str | None) -> ParsedDate:
    if not raw:
        return ParsedDate(None)
    s = raw.upper().replace("•", "-")
    s = re.sub(r"(\d)(ST|ND|RD|TH)\b", r"\1", s)
    try:
        m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b", s)        # 2026-08-12, 2026/8/24
        if m:
            return ParsedDate(date(int(m[1]), int(m[2]), int(m[3])))
        m = re.search(r"\b(\d{1,2})[\s\-/.]*(?:OF\s+)?([A-Z]{3})[A-Z]*[\s\-/.,]*(\d{4})\b", s)  # 19-JUN-2026, 15 of June, 2026
        if m and m[2] in MONTHS:
            return ParsedDate(date(int(m[3]), MONTHS[m[2]], int(m[1])))
        m = re.search(r"\b([A-Z]{3})[A-Z]*\.?\s+(\d{1,2}),?\s*(\d{4})\b", s)      # June 15, 2026
        if m and m[1] in MONTHS:
            return ParsedDate(date(int(m[3]), MONTHS[m[1]], int(m[2])))
        m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", s)          # 15-06-2026, 11/9/2026
        if m:
            a, b, y = int(m[1]), int(m[2]), int(m[3])
            if a > 12:
                return ParsedDate(date(y, b, a))
            if b > 12:
                return ParsedDate(date(y, a, b), note="read as MM/DD/YYYY")
            amb = a != b
            return ParsedDate(date(y, b, a), ambiguous=False,
                              note="assumed DD/MM/YYYY (trade-document convention)" if amb else "")
    except ValueError:
        return ParsedDate(None, True, f"invalid date '{raw}'")
    return ParsedDate(None)


def parse_month_period(raw: str | None) -> tuple[date, date] | None:
    """'June 2026.' -> (2026-06-01, 2026-06-30)."""
    if not raw:
        return None
    m = re.search(r"\b([A-Z]{3})[A-Z]*\.?\s*,?\s*(\d{4})\b", raw.upper())
    if m and m[1] in MONTHS and not re.search(r"\d{1,2}\s*(ST|ND|RD|TH)?\s*(OF\s+)?" + m[1], raw.upper()):
        y, mo = int(m[2]), MONTHS[m[1]]
        nxt = date(y + (mo == 12), mo % 12 + 1, 1)
        return date(y, mo, 1), date.fromordinal(nxt.toordinal() - 1)
    return None


# --------------------------------------------------------------------------- text / identifiers

def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm_id(s: str | None) -> str:
    """Contract / BL / invoice numbers: compare on alphanumerics only."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


OCR_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "Q": "0"})


def ids_equal_modulo_ocr(a: str, b: str) -> bool:
    return norm_id(a).translate(OCR_CONFUSABLE) == norm_id(b).translate(OCR_CONFUSABLE)


def norm_hs(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


COMPANY_SUFFIXES = [
    (r"\bPVT\.?\b", "PRIVATE"), (r"\bPTE\.?\b", "PRIVATE"), (r"\bLTD\.?\b", "LIMITED"),
    (r"\bCO\.?\b", "COMPANY"), (r"\bCORP\.?\b", "CORPORATION"), (r"\bINC\.?\b", "INCORPORATED"),
    (r"\b(SDN\.?\s*BHD\.?)\b", "SDN BHD"), (r"\(M\)", ""),
]


def norm_company(s: str | None) -> str:
    s = (s or "").upper()
    s = re.sub(r"^\s*(M/S\.?|MESSRS\.?:?)\s*", "", s)
    s = s.split("\n")[0]
    for pat, rep in COMPANY_SUFFIXES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return norm_space(s)


def company_match(a: str | None, b: str | None) -> str:
    """'EXACT' | 'CONTAINED' | 'DIFFERENT' after normalising legal suffixes."""
    na, nb = norm_company(a), norm_company(b)
    if not na or not nb:
        return "DIFFERENT"
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):      # HONGKONG / HONG KONG
        return "EXACT"
    if na in nb or nb in na:
        return "CONTAINED"
    return "DIFFERENT"


CURRENCY_ALIASES = {
    "USD": ["USD", "US$", "US DOLLAR", "US DOLLARS", "U.S. DOLLARS", "DOLARES AMERICANOS",
            "DOLARES", "DÓLARES AMERICANOS", "$"],
    "EUR": ["EUR", "EURO", "EUROS", "€"],
    "SGD": ["SGD", "S$", "SINGAPORE DOLLAR"],
}


def norm_currency(s: str | None) -> str | None:
    if not s:
        return None
    u = norm_space(s.upper())
    for code, aliases in CURRENCY_ALIASES.items():
        if u in aliases or any(re.search(rf"(^|\W){re.escape(a)}(\W|$)", u) for a in aliases if len(a) > 1):
            return code
    return None


INCOTERMS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DPU", "DDP"]


CFR_ALIASES = re.compile(r"(?<![A-Z0-9])(?:CNF|C\s*&\s*F|C\s+AND\s+F|C\s*\+\s*F)(?![A-Z0-9])", re.I)
INCOTERM_CODE = re.compile(r"(?<![A-Z0-9])(" + "|".join(INCOTERMS) + r")(?![A-Z0-9])")
INCOTERMS_EDITION = re.compile(r"\(?\s*\bINCOTERMS?\b\s*®?\s*(?:20\d\d)?\s*\)?", re.I)
# Words that start the next field when a delivery term shares a line with other data.
PLACE_END = re.compile(r"\s+(?:PAYMENT|PORT\s+OF|SHIPMENT|PRICE|QUANTITY|TOTAL|AMOUNT|VESSEL|INSURANCE|PARTIAL|"
                        r"DATE|INVOICE|CONTRACT|HS\s*CODE)\b|[;|]", re.I)


def parse_delivery_term(s: str | None) -> dict:
    """A delivery term split into its Incoterm code and the named place that follows it.

        "CIF"               -> {"incoterm": "CIF", "named_place": None}
        "CIF Chennai Port"  -> {"incoterm": "CIF", "named_place": "Chennai Port"}
        "C & F Chennai"     -> {"incoterm": "CFR", "named_place": "Chennai"}
    The place is delivery-location information; it never changes the Incoterm. Text naming two different
    Incoterms ("CIF/CFR Chennai") has no single code, so incoterm is None. "raw" keeps the original text."""
    raw = norm_space(s or "") or None
    out = {"incoterm": None, "named_place": None, "raw": raw}
    if not raw:
        return out
    t = INCOTERMS_EDITION.sub(" ", CFR_ALIASES.sub("CFR", raw))
    hits = list(INCOTERM_CODE.finditer(t.upper()))
    if len({m.group(1) for m in hits}) != 1:
        return out
    out["incoterm"] = hits[0].group(1)
    place = PLACE_END.split(t[hits[0].end():])[0]
    place = re.sub(r"^[\s,:\-–/.]+|[\s,:\-–/.]+$", "", place)
    out["named_place"] = norm_space(place) or None
    return out


def norm_incoterm(s: str | None) -> str | None:
    """The Incoterm code only (CFR for CNF / C&F); None when absent or ambiguous."""
    return parse_delivery_term(s)["incoterm"]


def norm_place(s: str | None) -> str:
    u = (s or "").upper()
    u = re.sub(r"\(.*?\)", " ", u)
    u = re.sub(r"[^A-Z ]", " ", u)
    return norm_space(u)


def place_tokens(s: str | None) -> set[str]:
    stop = {"PORT", "OF", "THE", "FINAL", "DESTINATION", "ICD", "CIF", "CFR", "CNF", "FOB",
            "INDIA", "CHILE", "MALAYSIA", "AUSTRALIA", "NIGERIA", "ANY", "IN"}
    return {t for t in norm_place(s).split() if len(t) > 2 and t not in stop}


# --------------------------------------------------------------------------- amount in words

_UNITS = {w: i for i, w in enumerate(
    "ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN FOURTEEN "
    "FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("TWENTY THIRTY FORTY FIFTY SIXTY SEVENTY EIGHTY NINETY".split())}
_SCALES = {"HUNDRED": 100, "THOUSAND": 1_000, "LAKH": 100_000, "LAKHS": 100_000,
           "MILLION": 1_000_000, "CRORE": 10_000_000}


def _words_to_int(words: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]; seen = True
        elif w in _TENS:
            current += _TENS[w]; seen = True
        elif w == "HUNDRED":
            current = max(current, 1) * 100; seen = True
        elif w in _SCALES:
            total += max(current, 1) * _SCALES[w]; current = 0; seen = True
        elif w in ("AND", "A"):
            continue
        else:
            return None
    return total + current if seen else None


def words_to_amount(text: str | None) -> float | None:
    """'One Hundred and Seventy-Five Thousand ... Dollars, Eighteen Cents Only' -> 175714.18"""
    if not text:
        return None
    u = re.sub(r"[^A-Z ]", " ", text.upper().replace("-", " "))
    u = re.sub(r"\bDOLLARS?\b", " DOLLARS ", u)
    u = re.sub(r"\b(USD|US|ONLY|SAY|AMOUNT|IN|WORDS|TOTAL)\b", " ", u)
    toks = u.split()
    cents_word = "CENTS" if "CENTS" in toks else "CENT" if "CENT" in toks else None
    if not cents_word:
        main = _words_to_int([t for t in toks if t != "DOLLARS"])
        return float(main) if main is not None else None
    idx = toks.index(cents_word)
    before, after = toks[:idx], toks[idx + 1:]
    if "DOLLARS" in before:                       # "... FOURTEEN DOLLARS, EIGHTEEN CENTS"
        d = before.index("DOLLARS")
        main_part, cents_words = before[:d], before[d + 1:] or after
    elif after and _words_to_int(after) is not None:   # "... AND CENTS FIFTY"
        main_part, cents_words = before, after
    elif "AND" in before:                         # "... AND SIXTY FOUR CENTS"
        cut = len(before) - 1 - before[::-1].index("AND")
        main_part, cents_words = before[:cut], before[cut + 1:]
    else:
        return None
    main_part = [t for t in main_part if t != "DOLLARS"]
    while main_part and main_part[-1] == "AND":
        main_part = main_part[:-1]
    main, cents = _words_to_int(main_part), _words_to_int(cents_words)
    if main is None or cents is None or cents > 99:
        return None
    return main + cents / 100


# --------------------------------------------------------------------------- OCR character confusion
# Letters OCR commonly returns for digits. Applied only in numeric context, never to text.
OCR_DIGIT_FOR = {"O": "0", "o": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2", "z": "2"}
_TOKEN = re.compile(r"[A-Za-z0-9|](?:[A-Za-z0-9|]|[.,](?=[A-Za-z0-9|]))*%?")


def _numeric_like(tok: str, field_is_numeric: bool) -> bool:
    """Only digits, separators and confusable letters. In free text the digits must also be at least
    as many as the letters; in a known numeric field one digit is enough ('2,O5O.OO').
    'S00004' passes this test, so fix_numeric_ocr also decides WHICH letters may change."""
    core = tok.rstrip("%")
    letters = [c for c in core if c.isalpha() or c == "|"]
    digits = sum(c.isdigit() for c in core)
    if digits < 1 or not all(c in OCR_DIGIT_FOR for c in letters):
        return False
    return field_is_numeric or digits >= len(letters)


def fix_numeric_ocr(text: str, field_is_numeric: bool) -> tuple[str, list[dict]]:
    """Replace OCR letter/digit confusions in numeric context; returns (fixed text, corrections).

    field_is_numeric=False (free page text): only letters sitting INSIDE a number change
      ('1O0' -> '100', '2O25' -> '2025', '1O0.5O' -> '100.50', '5O%' -> '50%'); a letter that starts a
      token is left alone, so identifiers such as 'S00004' or 'P0O006' are never rewritten here.
    field_is_numeric=True (a quantity/price/amount/weight/percentage/date value): every confusable
      letter in a numeric-looking token changes, including a leading one ('I00' -> '100').
    """
    out, corrections, pos = [], [], 0
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        out.append(text[pos:m.start()])
        pos = m.end()
        if not _numeric_like(tok, field_is_numeric):
            out.append(tok)
            continue
        chars = list(tok)
        sep_seen = re.search(r"\d[.,]", tok)
        i = 0
        while i < len(chars):
            if chars[i] not in OCR_DIGIT_FOR:
                i += 1
                continue
            j = i
            while j < len(chars) and chars[j] in OCR_DIGIT_FOR:   # a run of confusable letters
                j += 1
            left = chars[i - 1] if i > 0 else ""
            right = chars[j] if j < len(chars) else ""
            if field_is_numeric:
                ok = True
            else:
                # inside a number ('1O0', '2O25'), before '%' ('5O%'), or ending a decimal part ('100.5O');
                # never at the start, and never a trailing letter of an identifier ('S00011B', 'FASA 2B')
                inside = left.isdigit() and right != "" and (right.isdigit() or right in ".,")
                percent = left.isdigit() and right == "%"
                decimal_end = left.isdigit() and right == "" and bool(sep_seen) and sep_seen.start() < i and j - i <= 2
                ok = inside or percent or decimal_end
            if ok:
                for k in range(i, j):
                    corrections.append({"from": chars[k], "to": OCR_DIGIT_FOR[chars[k]], "token": tok,
                                        "reason": "numeric field" if field_is_numeric else "numeric context (inside a number)"})
                    chars[k] = OCR_DIGIT_FOR[chars[k]]
            i = j
        fixed = "".join(chars)
        if fixed != tok:
            for cr in corrections:
                if cr["token"] == tok:
                    cr["corrected"] = fixed
        out.append(fixed)
    out.append(text[pos:])
    return "".join(out), corrections
