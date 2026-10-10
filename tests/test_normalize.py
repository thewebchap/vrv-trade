from datetime import date

import pytest

from tradepilot import normalize as N


@pytest.mark.parametrize("raw,locale,value,ambiguous", [
    ("26.001,5", "EU", 26001.5, False),            # EU thousands + decimal comma
    ("USD 7,26600", "EU", 7.266, False),           # EU price per KG
    ("USD 188.926,90", "EU", 188926.90, False),
    ("1,92,075.04", None, 192075.04, False),       # Indian lakh grouping
    ("$2,060.00", None, 2060.0, False),
    ("47.664.15", None, 47664.15, False),          # OCR read the comma as a dot
    ("24, 573. 00KGS", None, 24573.0, False),      # OCR inserted spaces
    ("USD 1,370", "EU", 1.37, False),              # locale resolves a 3-digit group
    ("17,450", None, 17450.0, True),               # no locale: ambiguous
    ("USD 50,0000", None, 50.0, True),             # malformed grouping
])
def test_parse_number(raw, locale, value, ambiguous):
    p = N.parse_number(raw, locale)
    assert p.value == pytest.approx(value)
    assert p.ambiguous is ambiguous


def test_ambiguous_number_keeps_alternative_reading():
    p = N.parse_number("99.499.000 MT")
    assert p.ambiguous and p.alt == pytest.approx(99.499)


def test_detect_locale():
    assert N.detect_number_locale("TOTAL USD 188.926,90") == "EU"
    assert N.detect_number_locale("TOTAL USD 188,926.90") == "US"
    assert N.detect_number_locale("26 MT") is None


@pytest.mark.parametrize("raw,expected", [
    ("19-JUN-2026", date(2026, 6, 19)), ("19 Jun 2026", date(2026, 6, 19)),
    ("15-06-2026", date(2026, 6, 15)), ("2026/8/24", date(2026, 8, 24)),
    ("2026-08-12", date(2026, 8, 12)), ("28.08.2026", date(2026, 8, 28)),
    ("15th of June, 2026", date(2026, 6, 15)), ("26/8/2026", date(2026, 8, 26)),
    ("Aug. 26, 2026", date(2026, 8, 26)),
])
def test_parse_date(raw, expected):
    assert N.parse_date(raw).value == expected


def test_day_month_assumption_is_recorded():
    d = N.parse_date("11/9/2026")
    assert d.value == date(2026, 9, 11) and "DD/MM" in d.note


def test_month_period():
    assert N.parse_month_period("June 2026.") == (date(2026, 6, 1), date(2026, 6, 30))
    assert N.parse_month_period("Within 45 days from Contract Date") is None


@pytest.mark.parametrize("unit,norm", [("MT", "MT"), ("MTS", "MT"), ("M/T", "MT"), ("metric tonnes", "MT"),
                                       ("KGS", "KG"), ("Kilograms", "KG"), ("PCS", None), ("BALES", None)])
def test_weight_units(unit, norm):
    assert N.normalize_weight_unit(unit) == norm


def test_unit_conversion():
    assert N.to_mt(100_000, "KG") == pytest.approx(100)
    assert N.to_mt(26.0015, "MTS") == pytest.approx(26.0015)
    assert N.to_mt(60, "PACKAGES") is None       # counts are never converted to weight


@pytest.mark.parametrize("words,amount", [
    ("One Hundred and Seventy-Five Thousand, Seven Hundred and Fourteen Dollars, Eighteen Cents Only/-", 175714.18),
    ("USD: ONE HUNDRED SEVENTY THREE THOUSAND FIVE HUNDRED SIXTY NINE AND SIXTY FOUR CENTS ONLY.", 173569.64),
    ("Fifty-Four Thousand Nine Hundred Ninety and Cents Fifty Only.", 54990.50),
    ("USD : ONE HUNDRED EIGHTY FOUR THOUSAND EIGHT HUNDRED EIGHT AND ZERO CENTS ONLY.", 184808.00),
])
def test_amount_in_words(words, amount):
    assert N.words_to_amount(words) == pytest.approx(amount)


def test_company_and_terms():
    assert N.company_match("M/S.BHAGYANAGAR COPPER PVT LTD", "Bhagyanagar Copper Private Limited") == "EXACT"
    assert N.company_match("YONG HENG ALUMINIUM (M) SDN. BHD.", "NEW WING YING TRADING LIMITED") == "DIFFERENT"
    assert N.norm_incoterm("CNF Chennai") == "CFR" and N.norm_incoterm("CIF Nhava Sheva") == "CIF"
    assert N.norm_currency("DOLARES AMERICANOS") == "USD"
    assert N.norm_hs("7404 0012") == N.norm_hs("HS CODE -74040012") == "74040012"
    assert N.ids_equal_modulo_ocr("VRV/2026/001/ P0O006", "VRV/2026/001/P00006")


def test_ocr_homoglyphs_are_mapped_to_latin():
    from tradepilot.pdf_text import fix_homoglyphs
    assert fix_homoglyphs("КМTCPKW1305043") == "KMTCPKW1305043"
