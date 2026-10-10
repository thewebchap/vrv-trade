"""Delivery terms are compared on the Incoterm code; the named place after it is location information."""
import pytest
from test_scopes import BOILERPLATE, legs, per_leg

from tradepilot.checks import check_incoterms, incoterm_statements
from tradepilot.models import MISMATCH, PASS, PURCHASE_CONTRACT, REVIEW
from tradepilot.normalize import INCOTERMS, parse_delivery_term

from conftest import make_doc


@pytest.mark.parametrize("text,code,place", [
    ("CIF", "CIF", None),
    ("CIF Chennai", "CIF", "Chennai"),
    ("CIF Chennai Port", "CIF", "Chennai Port"),
    ("CNF CHENNAI, INDIA", "CFR", "CHENNAI, INDIA"),
    ("C & F Chennai", "CFR", "Chennai"),
    ("CIF Chennai, India (Incoterms 2020)", "CIF", "Chennai, India"),
    ("FOB Fasano", "FOB", "Fasano"),                     # FAS inside a place name is not an Incoterm
    ("CIF/CFR Chennai", None, None),                     # two different codes: no single Incoterm
    ("Chennai", None, None),
])
def test_delivery_term_is_split_into_incoterm_and_named_place(text, code, place):
    p = parse_delivery_term(text)
    assert (p["incoterm"], p["named_place"], p["raw"]) == (code, place, text)


def test_all_common_incoterms_recognised():
    assert set(INCOTERMS) == {"EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF"}
    for code in INCOTERMS:
        assert parse_delivery_term(f"{code} Rotterdam") == {"incoterm": code, "named_place": "Rotterdam",
                                                            "raw": f"{code} Rotterdam"}


def purchase_leg(pc, pi):
    """Purchase leg under test; the sales leg is a fixed, consistent CIF pair."""
    return check_incoterms(legs(f"Delivery Terms : {pc}", f"DELIVERY TERMS : {pi}",
                                "Delivery Terms : CIF", "TERMS OF DELIVERY : CIF"), "X")


@pytest.mark.parametrize("a,b", [("CIF", "CIF Chennai"), ("CIF Chennai", "CIF Chennai Port"),
                                 ("CFR", "CFR Chennai"), ("FOB", "FOB Singapore"), ("CIF", "CIF Nhava Sheva")])
def test_same_incoterm_with_or_without_location_passes(a, b):
    r = purchase_leg(a, b)
    assert per_leg(r)["PURCHASE"] == PASS and r.result == PASS


@pytest.mark.parametrize("a,b", [("CIF", "FOB"), ("CIF Chennai", "CFR Chennai"), ("FOB", "CFR")])
def test_different_incoterm_codes_are_flagged(a, b):
    r = purchase_leg(a, b)
    assert per_leg(r)["PURCHASE"] == MISMATCH and "different Incoterms" in r.reason


def test_purchase_contract_cfr_vs_invoice_cfr_chennai():
    r = check_incoterms(legs("Delivery Terms : CFR", "DELIVERY TERMS : CFR Chennai",
                             "Delivery Terms : CIF", "TERMS OF DELIVERY : CIF"), "X")
    assert per_leg(r)["PURCHASE"] == PASS


def test_sales_contract_cif_vs_invoice_cif_chennai():
    r = check_incoterms(legs("Delivery Terms : CFR", "DELIVERY TERMS : CFR",
                             "Delivery Terms : CIF", "TERMS OF DELIVERY : CIF Chennai"), "X")
    assert per_leg(r)["SALES"] == PASS


def test_purchase_cfr_and_sales_cif_are_not_compared():
    r = check_incoterms(legs("Delivery Terms : CFR Chennai", "DELIVERY TERMS : CFR",
                             "Delivery Terms : CIF Chennai", "TERMS OF DELIVERY : CIF"), "X")
    assert r.result == PASS and per_leg(r) == {"PURCHASE": PASS, "SALES": PASS}
    assert all(cp["scope"] in ("PURCHASE", "SALES") for cp in r.comparisons)


def test_raw_values_kept_in_evidence_codes_compared():
    r = purchase_leg("CNF Chennai Port", "C & F Chennai")
    assert per_leg(r)["PURCHASE"] == PASS
    cp = next(c for c in r.comparisons if c["scope"] == "PURCHASE")
    assert (cp["a_value"], cp["b_value"]) == ("CFR", "CFR")
    assert (cp["a_raw"], cp["b_raw"]) == ("CNF Chennai Port", "C & F Chennai")
    assert (cp["a_named_place"], cp["b_named_place"]) == ("Chennai Port", "Chennai")
    ev = [e for e in r.evidence if e.get("scope") == "PURCHASE" and e.get("incoterm")]
    assert {e["raw_value"] for e in ev} == {"CNF Chennai Port", "C & F Chennai"}
    assert {e["normalized_value"] for e in ev} == {"CFR"}


def test_same_code_with_different_places_in_one_contract_is_not_a_conflict():
    doc = make_doc("Delivery Terms : CIF Chennai\nDelivery Term : CIF Chennai Port", "pc.pdf", PURCHASE_CONTRACT)
    st = incoterm_statements(doc)
    assert st["status"] == "DETERMINED" and st["term"] == "CIF"


def test_reference_mention_of_another_incoterm_is_not_a_conflict():
    doc = make_doc("Delivery Terms : CIF Chennai\n" + BOILERPLATE, "pc.pdf", PURCHASE_CONTRACT)
    st = incoterm_statements(doc)
    assert st["status"] == "DETERMINED" and st["term"] == "CIF" and st["ignored"]


def test_two_different_operative_incoterms_in_one_contract_need_review():
    r = check_incoterms(legs("Delivery Terms : CIF Chennai\nDelivery Term : FOB Singapore", "DELIVERY TERMS : CIF",
                             "Delivery Terms : CIF", "TERMS OF DELIVERY : CIF"), "X")
    assert per_leg(r)["PURCHASE"] == REVIEW


def test_named_place_stops_at_the_next_field():
    p = parse_delivery_term("CIF Nhava Sheva - ICD Hyderabad, India HS Code: 74040012")
    assert p["incoterm"] == "CIF" and p["named_place"] == "Nhava Sheva - ICD Hyderabad, India"
