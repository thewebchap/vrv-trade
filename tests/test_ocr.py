"""OCR character confusion: O/0, I/1/l, S/5, B/8, Z/2 — corrected only where context supports it."""
import pytest

from conftest import FakeModel, make_doc
from tradepilot import checks
from tradepilot.checks import compare_pairs, eq_id, find_identifier
from tradepilot.extract import extract
from tradepilot.models import (BILL_OF_LADING, MISMATCH, FOUND, PASS, REVIEW, SALES_INVOICE, Document, Field,
                               Page)
from tradepilot.normalize import fix_numeric_ocr
from tradepilot.pdf_text import reconcile


# ------------------------------------------------------------------ numeric context

@pytest.mark.parametrize("raw,fixed", [
    ("1O0 MT", "100 MT"),            # O -> 0 between digits
    ("1O0.5O", "100.50"),            # O -> 0 inside and at the end of a decimal
    ("5O%", "50%"),                  # percentage
    ("2O25", "2025"),                # year
    ("3B5", "385"),                  # B -> 8
    ("1Z5 MT", "125 MT"),            # Z -> 2
    ("2S0", "250"),                  # S -> 5
    ("1l0", "110"),                  # l -> 1
    ("1I0", "110"),                  # I -> 1
])
def test_letters_inside_numbers_are_fixed_in_text(raw, fixed):
    out, corrections = fix_numeric_ocr(raw, field_is_numeric=False)
    assert out == fixed and corrections and all(c["reason"].startswith("numeric context") for c in corrections)


@pytest.mark.parametrize("raw,fixed", [
    ("I00", "100"), ("l00", "100"), ("O.5", "0.5"), ("USD 2,O5O.OO", "USD 2,050.00"),
    ("Sep. 2l, 2O26", "Sep. 21, 2026"), ("1O-O6-2O26", "10-06-2026"), ("+/- 1O%", "+/- 10%"),
])
def test_numeric_fields_fix_leading_letters_too(raw, fixed):
    assert fix_numeric_ocr(raw, field_is_numeric=True)[0] == fixed


@pytest.mark.parametrize("text", [
    "TO ORDER", "NOTIFY PARTY: BOLIVIA", "SCT NO VRV/2026/001/S00004",   # words and identifiers
    "VRV/2026/001/ P0O006",            # identifier: only a comparison may correct it
    "GST 05AABCV2866K2Z2",             # alphanumeric code with non-confusable letters
    "INVOICE NO VRV/2026/INVO0002",    # prefix letters + digits: left alone
    "I00 PACKAGES",                    # leading I in free text: not provably a digit
])
def test_text_and_identifiers_are_not_rewritten(text):
    assert fix_numeric_ocr(text, field_is_numeric=False) == (text, [])


def test_month_names_survive_numeric_date_fields():
    assert fix_numeric_ocr("19-JUN-2O26", field_is_numeric=True)[0] == "19-JUN-2026"
    assert fix_numeric_ocr("OCT. 1O, 2026", field_is_numeric=True)[0] == "OCT. 10, 2026"


def test_extracted_field_keeps_raw_and_corrected_values():
    doc = make_doc("BILL OF LADING\nTOTAL NET WEIGHT: 1O0 MT", "bl.pdf", BILL_OF_LADING, method="ocr")
    extract(doc, FakeModel([{"net_weight": "1O0 MT", "net_weight_unit": "MT"}]))
    f = doc.fields["net_weight"]
    assert f.raw_value == "1O0 MT" and f.ocr_corrected == "100 MT" and f.normalized_value == 100
    assert f.ocr_corrections[0] == {"from": "O", "to": "0", "token": "1O0", "reason": "numeric field", "corrected": "100"}


# ------------------------------------------------------------------ multi-pass reconciliation

def test_digit_misread_corrected_by_majority_of_passes():
    text, corr = reconcile("REFINED LEAD 99.199 2,070.00 205,962.93",
                           [("400 dpi", "REFINED LEAD 99.499 2,070.00 205,962.93"),
                            ("200 dpi", "REFINED LEAD 99.499 2.070.00 205,962.93")])
    assert text == "REFINED LEAD 99.499 2,070.00 205,962.93"
    assert corr == [{"from": "99.199", "to": "99.499", "line": 1,
                     "reason": "majority of 3 OCR passes: 400 dpi and 200 dpi both read 99.499"}]


def test_no_vote_without_agreement_or_for_separators_or_identifiers():
    # the other passes disagree with each other: keep the primary reading
    assert reconcile("NET 20031,3 KGS", [("b", "NET 20031,5 KGS"), ("c", "NET 20031,8 KGS")])[1] == []
    # separator-only difference: the number parser handles it, nothing is rewritten
    assert reconcile("PRICE 2,860.00", [("b", "PRICE 2.860.00"), ("c", "PRICE 2.860.00")])[1] == []
    # digits glued to letters are identifiers: never voted on
    assert reconcile("SCT NO S00008", [("b", "SCT NO 500008"), ("c", "SCT NO 500008")])[1] == []


# ------------------------------------------------------------------ identifiers: comparison support

def _doc(name, method, text="x", alt=()):
    return Document(name, name, [Page(1, text, method, list(alt))])


def _id(value, doc):
    return Field("bl_number", value, value, status=FOUND, page=1, document=doc)


@pytest.fixture
def docs():
    checks._DOCS.clear()
    yield checks._DOCS
    checks._DOCS.clear()


def test_ocr_value_corrected_by_native_document(docs):
    docs.update({"si.pdf": _doc("si.pdf", "ocr"), "sc.pdf": _doc("sc.pdf", "pdf_text")})
    verdict, why = eq_id(_id("VRV/2026/001/500008", "si.pdf"), _id("VRV/2026/001/ S00008", "sc.pdf"))
    assert verdict == "EQ" and "native text is authoritative" in why


def test_both_ocr_resolved_when_passes_disagree(docs):
    docs.update({"bl.pdf": _doc("bl.pdf", "ocr", "SPA OWCOAN1MA", alt=["SPA OWCOANIMA"]),
                 "si.pdf": _doc("si.pdf", "ocr", "VOYAGE NO OWCOANIMA")})
    assert eq_id(_id("OWCOAN1MA", "bl.pdf"), _id("OWCOANIMA", "si.pdf"))[0] == "EQ"


def test_both_ocr_and_stable_stays_review(docs):
    docs.update({"bl.pdf": _doc("bl.pdf", "ocr", "OWCOAN1MA", alt=["OWCOAN1MA"]),
                 "si.pdf": _doc("si.pdf", "ocr", "OWCOANIMA", alt=["OWCOANIMA"])})
    res = compare_pairs([("SI", _id("OWCOANIMA", "si.pdf"), "BL", _id("OWCOAN1MA", "bl.pdf"), True)], eq_id)[0]
    assert res == REVIEW


def test_both_native_confusable_difference_is_a_real_mismatch(docs):
    docs.update({"pi.pdf": _doc("pi.pdf", "pdf_text"), "pc.pdf": _doc("pc.pdf", "pdf_text")})
    res = compare_pairs([("PI", _id("P0O006", "pi.pdf"), "PC", _id("P00006", "pc.pdf"), True)], eq_id)[0]
    assert res == MISMATCH


def test_identifier_found_when_ocr_garbled_its_label():
    bl = _doc("bl.pdf", "ocr", "MAEU ALM. 275364984\nSHIPPER SOUTH RECYCLING")
    bl.doc_type = BILL_OF_LADING
    f = find_identifier(bl, "275364984", "bl_number")
    assert f.status == FOUND and f.extraction_method == "cross-document identifier search" and f.page == 1


def test_identifier_search_tolerates_confusables_only_on_ocr_pages():
    si = _doc("si.pdf", "ocr", "SCT NO VRV/2026/001/500008")
    si.doc_type = SALES_INVOICE
    f = find_identifier(si, "VRV/2026/001/ S00008", "contract_reference")
    assert f and f.ocr_corrections
    native = _doc("si2.pdf", "pdf_text", "SCT NO VRV/2026/001/500008")
    assert find_identifier(native, "VRV/2026/001/ S00008", "contract_reference") is None


def test_line_recovered_only_when_both_other_passes_read_it():
    primary = "N/M 60 PACKAGES\nTOTAL NET WEIGHT: 26.0015 MTS"
    both = "N/M 60 PACKAGES\nCOPPER SCRAP DRUID AS PER ISRI\nTOTAL NET WEIGHT: 26.0015 MTS"
    text, corr = reconcile(primary, [("400 dpi", both), ("200 dpi", both)])
    assert text.splitlines()[1] == "COPPER SCRAP DRUID AS PER ISRI"
    assert corr[0]["from"] is None and "read by both" in corr[0]["reason"]
    only_one = "N/M 60 PACKAGES\nEE 2065: 4020596 RE 15R\nTOTAL NET WEIGHT: 26.0015 MTS"
    assert reconcile(primary, [("400 dpi", only_one), ("200 dpi", primary)])[0] == primary


@pytest.mark.parametrize("text", ["S00011B", "FASA 2B", "2019|", "CLAUSE 1o", "41|", "30B"])
def test_trailing_letters_are_never_converted_in_text(text):
    assert fix_numeric_ocr(text, field_is_numeric=False) == (text, [])


def test_vote_ignores_different_numbers_and_multi_digit_changes():
    # the other passes list the numbers in a different order: not a misread
    assert reconcile('SEAL" 99,111.000', [("b", 'SEAL" 100.0000'), ("c", 'SEAL" 100.0000')])[1] == []
    # two digits differ: a different number, not a misread
    assert reconcile("BOARD 21", [("b", "BOARD 19"), ("c", "BOARD 19")])[1] == []
    # digit count differs
    assert reconcile("PAGE 343", [("b", "PAGE 43"), ("c", "PAGE 43")])[1] == []


def test_bl_number_without_digits_is_replaced_by_identifier_search(docs):
    from datetime import date
    from conftest import FakeModel
    from tradepilot.checks import Ctx, check_bl_refs
    bl = _doc("bl.pdf", "ocr", "MAERSK SCAC MAEU\nBAMO. 275410609")
    bl.doc_type = BILL_OF_LADING
    bl.fields["bl_number"] = _id("MAEU", "bl.pdf")
    si = _doc("si.pdf", "ocr", "BL NO 275410609")
    si.doc_type = SALES_INVOICE
    si.fields["bl_number"] = _id("275410609", "si.pdf")
    docs.update({"bl.pdf": bl, "si.pdf": si})
    ctx = Ctx({BILL_OF_LADING: bl, SALES_INVOICE: si}, [], FakeModel(), date(2026, 10, 10))
    assert check_bl_refs(ctx, "X").result == PASS
