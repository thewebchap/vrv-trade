"""Commercial legs: purchase and sales terms are compared within their own leg only."""
from datetime import date

import pytest

from conftest import FakeModel, make_doc
from test_business_rules import ADV20, ADV30, NONE, _with_advances, contract, full, invoice
from tradepilot.checks import (Ctx, check_advances, check_contract_refs, check_currency, check_gp, check_incoterms,
                               compare_pairs, eq_id, incoterm_statements)
from tradepilot.commercial import check_contract_price, check_expected_amount
from tradepilot.models import (FOUND, MISMATCH, PASS, PASS_NOTE, PURCHASE_CONTRACT, PURCHASE_INVOICE, REVIEW,
                               SALES_CONTRACT, SALES_INVOICE, Field)
from tradepilot.scope import CrossLegComparison, ensure_comparable

BOILERPLATE = ("E. TITLE AND RISK\nRisk of loss shall pass at the point determined by the applicable Incoterms 2020 rule "
               "specified in this Contract (for example, FOB,\nCFR or CIF). Notwithstanding the passing of risk")


def legs(pc_terms, pi_terms, sc_terms, si_terms):
    """Documents whose text carries the given delivery-term lines."""
    docs = {}
    for dt, text in ((PURCHASE_CONTRACT, pc_terms), (PURCHASE_INVOICE, pi_terms),
                     (SALES_CONTRACT, sc_terms), (SALES_INVOICE, si_terms)):
        docs[dt] = make_doc(text, f"{dt}.pdf", dt)
    return Ctx(docs, [], FakeModel(), date(2026, 10, 10))


def per_leg(result):
    return {c["scope"]: c["result"] for c in result.comparisons}


# 1
def test_purchase_cfr_and_sales_cif_is_a_normal_trade():
    c = legs("A. TERMS\nDelivery Terms : CNF Chennai\n" + BOILERPLATE, "Shipment Term: CNF CHENNAI, INDIA",
             "A. TERMS\nDelivery Terms : CIF Chennai\n" + BOILERPLATE, "TERMS OF DELIVERY : CIF")
    r = check_incoterms(c, "X")
    assert r.result == PASS and per_leg(r) == {"PURCHASE": PASS, "SALES": PASS}
    assert "differs" not in r.reason.lower() and "not compared with each other" in r.reason


# 2
def test_purchase_contract_cfr_vs_purchase_invoice_cif_is_a_mismatch():
    c = legs("Delivery Terms : CFR Chennai", "DELIVERY TERMS CIF Chennai", "Delivery Terms : CIF Chennai", "TERMS OF DELIVERY : CIF")
    r = check_incoterms(c, "X")
    assert r.result == MISMATCH and per_leg(r) == {"PURCHASE": MISMATCH, "SALES": PASS}


# 3
def test_sales_contract_cif_vs_sales_invoice_cfr_is_a_mismatch():
    c = legs("Delivery Terms : CFR Chennai", "Shipment Term: CFR", "Delivery Terms : CIF Chennai", "TERMS OF DELIVERY : CFR")
    r = check_incoterms(c, "X")
    assert r.result == MISMATCH and per_leg(r) == {"PURCHASE": PASS, "SALES": MISMATCH}


# 4
def test_different_prices_per_leg_are_allowed():
    c = full()          # buys at 6,500/MT, sells at 7,200/MT, each matching its own contract
    for chk in (check_contract_price, check_expected_amount):
        r = chk(c, "X")
        assert r.result == PASS and {cp["scope"] for cp in r.comparisons} <= {"PURCHASE", "SALES", "CROSS_TRADE"}
        assert all(not (cp["scope"] == "CROSS_TRADE" and "price" in cp["comparison"].lower() and "BL" not in cp["comparison"])
                   for cp in r.comparisons)


# 5, 6
@pytest.mark.parametrize("pc,pi,sc,si", [(ADV20, "19,500.00", NONE, None), (NONE, None, ADV30, "32,400.00")])
def test_advance_on_one_leg_only_is_allowed(pc, pi, sc, si):
    assert check_advances(_with_advances(pc, pi, sc, si), "X").result in (PASS, PASS_NOTE)


# 7
def test_boilerplate_incoterms_do_not_count_as_operative():
    doc = make_doc("A. TERMS\nDelivery Terms : CIF Mundra\nPort of Destination : CIF Mundra\n" + BOILERPLATE, "sc.pdf",
                   SALES_CONTRACT)
    st = incoterm_statements(doc)
    assert st["status"] == "DETERMINED" and st["term"] == "CIF" and len(st["ignored"]) == 2
    c = legs("Delivery Terms : CFR Chennai\n" + BOILERPLATE, "Shipment Term: CFR",
             "Delivery Terms : CIF Mundra\n" + BOILERPLATE, "TERMS OF DELIVERY : CIF")
    assert check_incoterms(c, "X").result == PASS


# 8
def test_two_operative_terms_in_one_contract_need_review_with_evidence():
    c = legs("Delivery Terms : CFR Chennai", "Shipment Term: CFR",
             "A. TERMS\nDelivery Terms : CIF Chennai\nC. SHIPMENT\nDelivery Term : CNF Chennai", "TERMS OF DELIVERY : CIF")
    r = check_incoterms(c, "X")
    assert r.result == REVIEW and per_leg(r)["SALES"] == REVIEW and per_leg(r)["PURCHASE"] == PASS
    assert "more than one operative delivery term" in r.reason and "The sales invoice states CIF" in r.reason
    assert sum(1 for e in r.evidence if e.get("note") == "operative (labelled)") >= 3


def test_invoice_without_any_delivery_term_is_a_note_not_a_review():
    c = legs("Delivery Terms : CFR Chennai", "COMMERCIAL INVOICE\nDELIVERY TERMS\nTOTAL USD 10",
             "Delivery Terms : CIF Chennai", "TERMS OF DELIVERY : CIF")
    assert per_leg(check_incoterms(c, "X"))["PURCHASE"] == PASS_NOTE


def test_currency_is_checked_per_leg_and_may_differ_between_legs():
    c = full(si=invoice(SALES_INVOICE), sc=contract(SALES_CONTRACT))
    c.docs[PURCHASE_CONTRACT].fields["currency"] = Field("currency", "USD", "USD", status=FOUND,
                                                         document_type=PURCHASE_CONTRACT, page=1)
    for dt in (SALES_CONTRACT, SALES_INVOICE):
        c.docs[dt].fields["currency"] = Field("currency", "EUR", "EUR", status=FOUND, document_type=dt, page=1)
    r = check_currency(c, "X")
    assert r.result == PASS and "[PURCHASE] contract USD = invoice USD" in r.reason   # USD purchase leg,
    assert "[SALES] contract EUR = invoice EUR" in r.reason                              # EUR sales leg: allowed
    c.docs[SALES_INVOICE].fields["currency"] = Field("currency", "USD", "USD", status=FOUND, document_type=SALES_INVOICE, page=1)
    assert check_currency(c, "X").result == MISMATCH


def test_leg_specific_fields_are_never_compared_across_legs():
    with pytest.raises(CrossLegComparison):
        ensure_comparable("delivery_terms", PURCHASE_CONTRACT, SALES_CONTRACT)
    with pytest.raises(CrossLegComparison):
        a = Field("unit_price", "6500", status=FOUND, page=1, document_type=PURCHASE_INVOICE)
        b = Field("unit_price", "7200", status=FOUND, page=1, document_type=SALES_INVOICE)
        compare_pairs([("PI", a, "SI", b, True)], eq_id)
    assert ensure_comparable("quantity", PURCHASE_INVOICE, "BILL_OF_LADING") == "CROSS_TRADE"


def test_contract_references_are_checked_within_each_leg():
    c = full()
    c.docs[PURCHASE_INVOICE].fields["contract_reference"] = Field("contract_reference", "P001", status=FOUND, page=1,
                                                                  document_type=PURCHASE_INVOICE)
    c.docs[PURCHASE_CONTRACT].fields["contract_number"] = Field("contract_number", "P001", status=FOUND, page=1,
                                                                document_type=PURCHASE_CONTRACT)
    c.docs[SALES_INVOICE].fields["contract_reference"] = Field("contract_reference", "S009", status=FOUND, page=1,
                                                               document_type=SALES_INVOICE)
    c.docs[SALES_CONTRACT].fields["contract_number"] = Field("contract_number", "S009", status=FOUND, page=1,
                                                             document_type=SALES_CONTRACT)
    r = check_contract_refs(c, "X")
    assert r.result == PASS and {cp["scope"] for cp in r.comparisons} == {"PURCHASE", "SALES"}


def test_gp_margin_rule_is_cross_trade_by_checklist_and_does_not_call_legs_a_mismatch():
    c = full()
    c.docs[PURCHASE_CONTRACT].fields["delivery_terms"] = Field("delivery_terms", "CFR Chennai", status=FOUND, page=1)
    c.docs[SALES_CONTRACT].fields["delivery_terms"] = Field("delivery_terms", "CIF Chennai", status=FOUND, page=1)
    r = check_gp(c, "COMMON-8", 2.0)
    assert r.result == PASS and "which is normal" in r.reason
