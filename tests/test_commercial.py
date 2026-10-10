"""Quantity and price verification (commercial core)."""
import copy
from datetime import date

import pytest

from conftest import FakeModel, make_doc
from tradepilot.canonical import ContractView, Material, build_invoice
from tradepilot.checks import Ctx
from tradepilot.commercial import (check_contract_price, check_contract_qty, check_expected_amount,
                                   check_quantity_measures, check_shipped_qty)
from tradepilot.extract import ground, label_confirmed
from tradepilot.models import (BILL_OF_LADING, MISMATCH, FOUND, PASS, PURCHASE_CONTRACT, PURCHASE_INVOICE,
                               REVIEW, SALES_INVOICE, Field)


def fld(raw, name="x", doc="d.pdf", label=None):
    return Field(name, raw, raw, status=FOUND, page=1, document=doc, source_text=f"line with {raw}",
                 label_confirmed=label)


def invoice(doc_type, qty, unit, price, amount, currency="USD", method="pdf_text", gross=None):
    d = make_doc("COMMERCIAL INVOICE", f"{doc_type}.pdf", doc_type, method=method)
    d.number_locale = "US"
    d.line_items = [{"description": fld("Refined Lead"), "quantity": fld(qty), "quantity_unit": fld(unit),
                     "unit_price": fld(price), "amount": fld(amount)}]
    d.fields["currency"] = fld(currency)
    if gross:
        d.fields["gross_amount"] = fld(gross)
    return d


def bill(net=None, gross=None, unit="MT", method="ocr"):
    d = make_doc("BILL OF LADING", "bl.pdf", BILL_OF_LADING, method=method)
    if net:
        d.fields["net_weight"] = fld(net, "net_weight", "bl.pdf", label=True)
        d.fields["net_weight_unit"] = fld(unit)
    if gross:
        d.fields["gross_weight"] = fld(gross, "gross_weight", "bl.pdf", label=True)
        d.fields["gross_weight_unit"] = fld(unit)
    return d


def contract(qty=100, tol=5, price=2000.0, price_type="FIXED", currency="USD", price_tol=None):
    doc = make_doc("CONTRACT", "pc.pdf", PURCHASE_CONTRACT)
    m = Material("Refined Lead", None, qty, tol, price, price_type,
                 fields={"quantity": fld(str(qty)), "unit_price": fld(f"{price:,.2f}" if price else "LME formula")},
                 price_basis="per MT")
    return ContractView(doc, [m], qty, tol, None, False, currency=currency, price_tol_pct=price_tol)


def ctx_with(pi=None, bl=None, pc=None, si=None, sc=None):
    """Unless given, the sales side mirrors the purchase side, so a test exercises one pairing."""
    if pi and si is None:
        si = copy.deepcopy(pi)
        si.doc_type, si.filename = SALES_INVOICE, "si.pdf"
    sc = sc if sc is not None else pc
    docs = {d.doc_type: d for d in (pi, bl, si) if d}
    c = Ctx(docs, [], FakeModel(), date(2026, 10, 10))
    c.pi = build_invoice(pi) if pi else None
    c.si = build_invoice(si) if si else None
    c.pc, c.sc = pc, sc
    return c


# ------------------------------------------------------------------ quantities

def test_bl_net_weight_within_contract_tolerance_passes():
    """PC 100 MT +/-5%, BL net 103 MT -> PASS, not an automatic mismatch."""
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "103.000", "MT", "2,000.00", "206,000.00"),
                 bl=bill(net="103.000"), pc=contract())
    r = check_contract_qty(c, "CORE")
    bl_cmp = next(x for x in r.comparisons if x["comparison"].startswith("BL net weight"))
    assert bl_cmp["result"] == PASS and "95.0000 to 105.0000" in bl_cmp["tolerance"]
    assert r.result == PASS


def test_bl_net_weight_above_tolerance_fails():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "106.000", "MT", "2,000.00", "212,000.00"),
                 bl=bill(net="106.000"), pc=contract())
    assert check_contract_qty(c, "CORE").result == MISMATCH


def test_gross_weight_is_never_used_as_net_weight():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "99.111", "MT", "2,050.00", "203,177.55"),
                 bl=bill(gross="99.111"))
    r = check_shipped_qty(c, "CORE")
    assert r.result == REVIEW and "gross is not net" in r.reason


def test_net_above_gross_fails():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "26.0015", "MT", "7,266.00", "188,926.90"),
                 bl=bill(net="26.0615", gross="26.0015"))
    assert check_quantity_measures(c, "CORE").result == MISMATCH


def test_counts_are_not_compared_with_weights():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "56", "PCS", "3,805.85", "213,127.60"), bl=bill(net="103.460"))
    assert c.pi.lines[0].basis == "COUNT" and c.pi.qty_mt is None
    assert check_quantity_measures(c, "CORE").result == REVIEW


def test_quantity_comparison_respects_printed_precision():
    pi = invoice(PURCHASE_INVOICE, "20,031.5", "KG", "9.588", "192,062.02")
    assert check_shipped_qty(ctx_with(pi=pi, bl=bill(net="20,031.5", unit="KG")), "CORE").result == PASS
    # 0.2 kg apart, both printed to 0.1 kg: a real difference, but one digit on an OCR'd BL -> review
    assert check_shipped_qty(ctx_with(pi=pi, bl=bill(net="20,031.3", unit="KG")), "CORE").result == REVIEW
    # same difference on a native-text BL -> fail
    assert check_shipped_qty(ctx_with(pi=pi, bl=bill(net="20,031.3", unit="KG", method="pdf_text")), "CORE").result == MISMATCH


# ------------------------------------------------------------------ prices and totals

def test_expected_total_matches_invoice():
    """100 MT x USD 2,000/MT = 200,000 = invoice total -> PASS."""
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "100.000", "MT", "2,000.00", "200,000.00"), pc=contract())
    r = check_expected_amount(c, "CORE")
    assert r.result == PASS
    assert "100.0000 MT x 2,000.00 USD/MT" in r.comparisons[0]["left"]["computation"]


def test_expected_total_mismatch_fails():
    """Invoice says 205,000 for 100 MT at the contract price of 2,000 -> MISMATCH with the difference."""
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "100.000", "MT", "2,050.00", "205,000.00"), pc=contract())
    r = check_expected_amount(c, "CORE")
    assert r.result == MISMATCH and "+5,000.00" in r.comparisons[0]["calculation"]
    assert check_contract_price(c, "CORE").result == MISMATCH


def test_price_tolerance_stated_in_contract_is_applied():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "100.000", "MT", "2,010.00", "201,000.00"), pc=contract(price_tol=1))
    assert check_contract_price(c, "CORE").result == PASS
    assert check_expected_amount(c, "CORE").result == PASS


def test_price_per_kg_is_normalised_to_per_mt():
    pi = invoice(PURCHASE_INVOICE, "100,000", "KG", "2.00", "200,000.00")
    c = ctx_with(pi=pi, pc=contract())
    assert check_contract_price(c, "CORE").result == PASS
    assert "per KG" in c.pi.lines[0].price_basis


def test_currency_mismatch_fails():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "100.000", "MT", "2,000.00", "200,000.00", currency="EUR"),
                 pc=contract(currency="USD"))
    assert check_contract_price(c, "CORE").result == MISMATCH


def test_unreadable_formula_needs_review():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "20.000", "MT", "9,000.00", "180,000.00"),
                 pc=contract(price=None, price_type="FORMULA"))     # FORMULA, but no formula text to read
    assert check_contract_price(c, "CORE").result == REVIEW


def test_invoice_charging_more_than_shipped_fails():
    """Invoice quantity 105 MT but BL net 100 MT: value at contract price on the shipped weight differs."""
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "105.000", "MT", "2,000.00", "210,000.00"),
                 bl=bill(net="100.0", method="pdf_text"), pc=contract())
    r = check_expected_amount(c, "CORE")
    assert r.result == MISMATCH and any(x["comparison"].startswith("PI: BL net weight") for x in r.comparisons)


def test_every_comparison_carries_full_evidence():
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "103.000", "MT", "2,000.00", "206,000.00"),
                 bl=bill(net="103.000"), pc=contract())
    for check in (check_contract_qty(c, "CORE"), check_contract_price(c, "CORE"), check_expected_amount(c, "CORE")):
        for comp in check.comparisons:
            assert {"comparison", "left", "right", "tolerance", "calculation", "result"} <= comp.keys()
            assert comp["left"].get("unit") and comp["right"].get("unit")


# ------------------------------------------------------------------ extraction safeguards

def test_net_weight_label_is_required():
    assert label_confirmed("net_weight", ["Container No. Description Gross Weight", "(KGS)", "99,111.000"]) is False
    assert label_confirmed("net_weight", ["TOTAL NET WEIGHT: 26.0015 MTS"]) is True
    assert label_confirmed("gross_weight", ["Description of Goods Gross Weight", "(KGS)", "99,111.000"]) is True


def test_numbers_are_grounded_as_numbers():
    doc = make_doc("26061.5 KGS\nTOTAL GROSS WEIGHT: 26.0615 MTS", "bl.pdf", BILL_OF_LADING)
    page, line, _ = ground("26.0615", doc, "gross_weight")
    assert line == "TOTAL GROSS WEIGHT: 26.0615 MTS"


def test_ambiguous_bl_weight_without_agreeing_invoice_is_not_used():
    """'100.000' with no known number locale could be 100 or 100,000; nothing corroborates either."""
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "105.000", "MT", "2,000.00", "210,000.00"),
                 bl=bill(net="100.000", method="pdf_text"), pc=contract())
    assert check_shipped_qty(c, "CORE").result == REVIEW


def test_per_shipment_quantity_needs_its_own_label():
    ctx_line = ["Recovery 58% on Average basis of Entire Lot", "• Quantity : 26 MT Approximately +/- 5%"]
    assert label_confirmed("quantity_per_shipment", ctx_line) is False
    assert label_confirmed("quantity_per_shipment", ["Quantity : 200 MT +/- 2% (100 MT each BL)"]) is True
    assert label_confirmed("quantity_per_shipment", ["Loading per Container : 25 MT"]) is False


def test_missing_unit_inferred_only_from_unique_bl_agreement():
    from tradepilot.commercial import infer_missing_units
    pi = invoice(PURCHASE_INVOICE, "13.788", "MT", "1,892.67", "26,096.13")
    del pi.line_items[0]["quantity_unit"]
    c = ctx_with(pi=pi, bl=bill(net="13,788", unit="KG"), si=invoice(SALES_INVOICE, "13.788", "MT", "2,000.00", "27,576.00"))
    c.docs[BILL_OF_LADING].number_locale = "US"
    assert c.pi.qty_mt is None
    infer_missing_units(c)
    assert c.pi.qty_mt == pytest.approx(13.788) and "read as MT" in c.pi.lines[0].unit_inferred
    pi_vs_bl = next(x for x in check_shipped_qty(c, "CORE").comparisons if x["comparison"] == "PI quantity vs BL net quantity")
    assert pi_vs_bl["result"] == REVIEW and "circular" in pi_vs_bl["detail"]


def test_invoice_without_quantity_column_uses_labelled_net_weight():
    d = make_doc("COMMERCIAL INVOICE", "pi.pdf", PURCHASE_INVOICE)
    d.number_locale = "US"
    d.line_items = [{"description": fld("REFINED LEAD"), "amount": fld("USD 203,177.55")}]
    d.fields["net_weight"] = fld("99.111 MT", "net_weight", "pi.pdf", label=True)
    iv = build_invoice(d)
    line = iv.lines[0]
    assert iv.qty_mt == pytest.approx(99.111) and line.basis == "NET"
    assert line.price_per_mt == pytest.approx(2050.0) and "derived" in line.price_basis


def test_net_above_a_per_container_gross_is_review_not_fail():
    bl = bill(net="99.4990", gross="24.5730")
    bl.pages[0].text = "GROSS WEIGHT : 24,573.00KGS\nGROSS WEIGHT : 24,875.00KGS\nNET WEIGHT 99,499.00 KGS"
    c = ctx_with(pi=invoice(PURCHASE_INVOICE, "99.499", "MT", "2,050.00", "203,972.95"), bl=bl)
    assert check_quantity_measures(c, "CORE").result == REVIEW
