"""Business rules from the TradePilot master specification (tests numbered as in Part 18)."""
import copy
import socket
from datetime import date

import pymupdf
import pytest

from conftest import FakeModel, make_doc
from tradepilot.canonical import ContractView, Material, build_invoice
from tradepilot.checks import (Ctx, check_adjustment_notes, check_advances, check_goods, check_invoice_arithmetic,
                               check_mandatory_docs, check_parties, check_supporting_docs, contract_presentation_docs)
from tradepilot.classify import classify, split_combined
from tradepilot.commercial import check_contract_price, check_contract_qty, check_expected_amount, check_shipped_qty
from tradepilot.extract import label_confirmed, payment_clause
from tradepilot.models import (BILL_OF_LADING, FAIL, FOUND, MISMATCH, PASS, PASS_NOTE, PURCHASE_CONTRACT,
                               PURCHASE_INVOICE, REVIEW, SALES_CONTRACT, SALES_INVOICE, SEVERITY, Document, Field, Page)
from tradepilot.normalize import fix_numeric_ocr
from tradepilot.pricing import parse_formula


# ------------------------------------------------------------------ builders

def fld(raw, name="x", doc="d.pdf", label=None, norm=None):
    return Field(name, raw, norm if norm is not None else raw, status=FOUND, page=1, document=doc,
                 source_text=f"line with {raw}", label_confirmed=label)


def invoice(doc_type, qty="15.000", price="7,200.00", amount="108,000.00", unit="MT", desc="Copper Scrap Druid",
            advance=None, gross=None, net=None, text="COMMERCIAL INVOICE", method="pdf_text"):
    d = make_doc(text, f"{doc_type}.pdf", doc_type, method=method)
    d.number_locale = "US"
    d.line_items = [{"description": fld(desc), "quantity": fld(qty), "quantity_unit": fld(unit),
                     "unit_price": fld(price), "amount": fld(amount)}]
    d.fields["currency"] = fld("USD")
    for k, v in (("advance_deducted", advance), ("gross_amount", gross), ("net_amount_payable", net)):
        if v:
            d.fields[k] = fld(v)
    return d


def contract(doc_type, qty=15.0, tol=10.0, price=7200.0, formula=None, clause="T/T 100% against scan copy of DBL",
             desc="Copper Scrap Druid as per ISRI"):
    doc = make_doc("CONTRACT", f"{doc_type}.pdf", doc_type)
    doc.fields["payment_clause"] = fld(clause, "payment_clause")
    fs = [parse_formula(formula)] if formula else []
    m = Material(desc, None, qty, tol, None if formula else price, "FORMULA" if formula else "FIXED",
                 fields={"quantity": fld(str(qty)), "unit_price": fld(f"{price:,.2f}")}, price_basis="per MT", formulas=fs)
    return doc, ContractView(doc, [m], qty, tol, None, False, currency="USD")


def ctx(pi=None, si=None, pc=None, sc=None, bl=None, all_docs=None, supporting=()):
    docs = {}
    for d in (pi, si, bl):
        if d:
            docs[d.doc_type] = d
    for pair in (pc, sc):
        if pair:
            docs[pair[0].doc_type] = pair[0]
    c = Ctx(docs, [], FakeModel(), date(2026, 10, 10), all_docs=list(all_docs or docs.values()), supporting=list(supporting))
    c.pi = build_invoice(pi) if pi else None
    c.si = build_invoice(si) if si else None
    c.pc = pc[1] if pc else None
    c.sc = sc[1] if sc else None
    return c


def full(**kw):
    """A complete, consistent transaction: buy 15 MT at 6,500, sell 15 MT at 7,200, BL 15 MT net."""
    pi = kw.get("pi") or invoice(PURCHASE_INVOICE, price="6,500.00", amount="97,500.00")
    si = kw.get("si") or invoice(SALES_INVOICE)
    pc = kw.get("pc") or contract(PURCHASE_CONTRACT, price=6500.0)
    sc = kw.get("sc") or contract(SALES_CONTRACT)
    bl = kw.get("bl") or make_doc("BILL OF LADING", "bl.pdf", BILL_OF_LADING)
    if "net_weight" not in bl.fields:
        bl.fields["net_weight"] = fld("15.0000", "net_weight", "bl.pdf", label=True)
        bl.fields["net_weight_unit"] = fld("MT")
    return ctx(pi=pi, si=si, pc=pc, sc=sc, bl=bl)


def mandatory_docs(missing=(), unreadable=()):
    docs = []
    for t in (PURCHASE_CONTRACT, PURCHASE_INVOICE, SALES_CONTRACT, SALES_INVOICE, BILL_OF_LADING):
        if t in missing:
            continue
        d = Document(f"{t}.pdf", f"{t}.pdf", [Page(1, "text", "pdf_text")], doc_type=t, classification_status="IDENTIFIED")
        if t in unreadable:
            d.classification_status = "AMBIGUOUS"
        docs.append(d)
    return Ctx({}, [], FakeModel(), date(2026, 10, 10), all_docs=docs)


def overall(results):
    return max(results, key=SEVERITY.get)


# ------------------------------------------------------------------ 1-7 mandatory documents

def test_01_all_five_documents_proceed():
    assert check_mandatory_docs(mandatory_docs(), "X").result == PASS


@pytest.mark.parametrize("missing", [PURCHASE_CONTRACT, PURCHASE_INVOICE, SALES_CONTRACT, SALES_INVOICE, BILL_OF_LADING])
def test_02_06_missing_mandatory_document_fails(missing):
    r = check_mandatory_docs(mandatory_docs(missing=(missing,)), "X")
    assert r.result == FAIL and "MISSING MANDATORY DOCUMENT" in r.reason


def test_07_attached_but_unreadable_is_review_not_missing():
    r = check_mandatory_docs(mandatory_docs(unreadable=(BILL_OF_LADING,)), "X")
    assert r.result == REVIEW and "UNREADABLE / UNIDENTIFIED" in r.reason


def test_07b_missing_but_unidentified_attachment_present_is_review():
    c = mandatory_docs(missing=(BILL_OF_LADING,))
    c.all_docs.append(Document("scan.pdf", "scan.pdf", [Page(1, "???", "ocr")], classification_status="UNKNOWN"))
    assert check_mandatory_docs(c, "X").result == REVIEW


def test_packing_list_is_not_a_bill_of_lading():
    d = classify(make_doc("PACKING LIST\nBill of Lading No: SCL1\nShipper: A\nConsignee: B\nPort of Loading: C", "x.pdf"))
    assert d.doc_type == "PACKING_LIST"


# ------------------------------------------------------------------ 8-11 optional documents

def test_08_09_debit_and_credit_note_absent_is_not_a_failure():
    r = check_adjustment_notes(full(), "X")
    assert r.result == PASS_NOTE and "No debit note supplied" in r.reason


def test_10_optional_certificate_absent_is_pass_with_note():
    assert check_supporting_docs(full(), "X").result == PASS_NOTE


def test_11_contract_required_documents_are_reported_as_contractual():
    c = full()
    c.docs[PURCHASE_CONTRACT].pages[0].text = ("D. PAYMENT\nDocuments to be presented:\n1. Commercial Invoice\n"
                                               "2. Certificate of Origin\n3. Certificate of Analysis\nOriginal documents to be couriered")
    assert [t for _, t in contract_presentation_docs(c.docs[PURCHASE_CONTRACT])] == \
        ["INVOICE", "CERTIFICATE_OF_ORIGIN", "QUALITY_CERTIFICATE"]
    r = check_supporting_docs(c, "X")
    assert "Certificate of Origin" in r.reason and "contractual presentation documents" in r.reason


def test_supplied_debit_note_matched_to_invoice():
    pi = invoice(PURCHASE_INVOICE, price="6,500.00", amount="97,500.00",
                 text="COMMERCIAL INVOICE\nTOTAL 97,500.00\nLESS: DEBIT NOTE DN-7 5,000.00\nNET 92,500.00")
    note = make_doc("DEBIT NOTE", "dn.pdf", "DEBIT_NOTE")
    note.fields.update(amount=fld("5,000.00", norm=5000.0), issuer_name=fld("VRV GLOBAL PTE LTD"),
                       recipient_name=fld("SUPPLIER LTD"), note_number=fld("DN-7"))
    c = full(pi=pi)
    c.docs[PURCHASE_CONTRACT].fields["seller_name"] = fld("SUPPLIER LTD")
    c.supporting = [note]
    r = check_adjustment_notes(c, "X")
    assert r.result == PASS and "reduces" in r.reason


# ------------------------------------------------------------------ 12-18 purchase / sales independence

def test_12_different_purchase_and_sales_prices_are_normal():
    c = full()                                  # buy 6,500 / sell 7,200
    assert check_contract_price(c, "X").result == PASS
    assert check_expected_amount(c, "X").result == PASS


def _with_advances(pc_clause, pi_adv, sc_clause, si_adv):
    pi = invoice(PURCHASE_INVOICE, price="6,500.00", amount="97,500.00", advance=pi_adv,
                 gross="97,500.00" if pi_adv else None,
                 net=f"{97500 - float(pi_adv.replace(',', '')):,.2f}" if pi_adv else None)
    si = invoice(SALES_INVOICE, advance=si_adv, gross="108,000.00" if si_adv else None,
                 net=f"{108000 - float(si_adv.replace(',', '')):,.2f}" if si_adv else None)
    return full(pi=pi, si=si, pc=contract(PURCHASE_CONTRACT, price=6500.0, clause=pc_clause),
                sc=contract(SALES_CONTRACT, clause=sc_clause))


ADV20 = "Advance payment of twenty percent (20%) of the contract value; balance against DBL"
ADV30 = "Advance payment: 30% of the invoice value; balance D/P"
NONE = "100% TT against scan copy of DBL"


@pytest.mark.parametrize("pc,pi,sc,si", [
    (ADV20, "19,500.00", ADV30, "32,400.00"),     # 13 both sides, different amounts
    (ADV20, "19,500.00", NONE, None),             # 14 purchase advance only
    (NONE, None, ADV30, "32,400.00"),             # 15 sales advance only
    (NONE, None, NONE, None),                     # 16 neither side
])
def test_13_16_advances_are_independent_per_side(pc, pi, sc, si):
    r = check_advances(_with_advances(pc, pi, sc, si), "X")
    assert r.result in (PASS, PASS_NOTE)


def test_17_18_different_formulas_and_periods_per_side_are_accepted():
    pc = contract(PURCHASE_CONTRACT, formula="LME Copper CSP x 85%, QP: month of shipment")
    sc = contract(SALES_CONTRACT, formula="LME Copper CSP x 90%, QP: month after arrival")
    r = check_contract_price(full(pc=pc, sc=sc), "X")
    assert r.result == PASS_NOTE


# ------------------------------------------------------------------ 19-25 contract vs invoice

def test_19_20_fixed_prices_match():
    assert check_contract_price(full(), "X").result == PASS


def test_21_purchase_price_mismatch():
    pi = invoice(PURCHASE_INVOICE, price="6,600.00", amount="99,000.00")
    r = check_contract_price(full(pi=pi), "X")
    assert r.result == MISMATCH and "PI" in r.reason


def test_22_sales_price_mismatch_and_wrong_total():
    si = invoice(SALES_INVOICE, price="7,200.00", amount="110,000.00")     # 15 x 7,200 = 108,000
    c = full(si=si)
    assert check_expected_amount(c, "X").result == MISMATCH


def test_23_24_advance_arithmetic():
    ok = invoice(SALES_INVOICE, advance="30,000.00", gross="108,000.00", net="78,000.00")
    bad = invoice(SALES_INVOICE, advance="30,000.00", gross="108,000.00", net="80,000.00")
    assert check_invoice_arithmetic(full(si=ok), "X").result == PASS
    r = check_invoice_arithmetic(full(si=bad), "X")
    assert r.result == MISMATCH and "incorrect advance arithmetic" in r.reason


def test_25_advance_deducted_without_debit_note_is_accepted():
    c = _with_advances(NONE, None, ADV30, "32,400.00")
    assert check_advances(c, "X").result in (PASS, PASS_NOTE)
    assert check_adjustment_notes(c, "X").result == PASS_NOTE


# ------------------------------------------------------------------ 26-32 commodity and shipment

def test_26_identical_quantities_pass():
    c = full()
    assert check_shipped_qty(c, "X").result == PASS
    assert check_contract_qty(c, "X").result == PASS


def test_27_within_tolerance_passes():
    pi = invoice(PURCHASE_INVOICE, qty="16.000", price="6,500.00", amount="104,000.00")
    si = invoice(SALES_INVOICE, qty="16.000", amount="115,200.00")
    bl = make_doc("BILL OF LADING", "bl.pdf", BILL_OF_LADING)
    bl.fields["net_weight"] = fld("16.0000", "net_weight", "bl.pdf", label=True)
    bl.fields["net_weight_unit"] = fld("MT")
    assert check_contract_qty(full(pi=pi, si=si, bl=bl), "X").result == PASS      # 15 MT ±10%


def test_28_invoice_and_bl_quantity_conflict():
    """Contract 15 MT ±10%, invoice 16 MT (within tolerance), BL 15 MT: not automatically PASS."""
    si = invoice(SALES_INVOICE, qty="16.000", amount="115,200.00")
    bl = make_doc("BILL OF LADING", "bl.pdf", BILL_OF_LADING, method="pdf_text")
    bl.fields["net_weight"] = fld("15.0000", "net_weight", "bl.pdf", label=True)
    bl.fields["net_weight_unit"] = fld("MT")
    c = full(si=si, bl=bl)
    assert check_contract_qty(c, "X").result == PASS          # within contractual tolerance...
    assert check_shipped_qty(c, "X").result == MISMATCH        # ...but invoice and shipment differ


def test_29_equivalent_wording_passes():
    pi = invoice(PURCHASE_INVOICE, price="6,500.00", amount="97,500.00", desc="Insulated Copper Scrap Wire")
    pc = contract(PURCHASE_CONTRACT, price=6500.0, desc="Insulated Copper Wire Scrap")
    c = full(pi=pi, pc=pc)
    c.si.lines[0].description = "Insulated Copper Wire Scrap"
    c.sc.materials[0].description = "Insulated Copper Wire Scrap"
    assert check_goods(c, "X").result == PASS


def test_30_incompatible_isri_grade_mismatch():
    si = invoice(SALES_INVOICE, desc="Copper Scrap Berry")
    sc = contract(SALES_CONTRACT, desc="Copper Scrap Candy as per ISRI")
    r = check_goods(full(si=si, sc=sc), "X")
    assert r.result == MISMATCH and "ISRI" in r.reason


def test_31_commercial_recovery_factors_are_not_quality():
    pc = contract(PURCHASE_CONTRACT, formula="LME Copper CSP x 80% payable")
    sc = contract(SALES_CONTRACT, formula="LME Copper CSP x 85% payable")
    c = full(pc=pc, sc=sc)
    assert check_contract_price(c, "X").result == PASS_NOTE
    assert check_goods(c, "X").result == PASS


def test_32_bl_shipper_in_a_shipping_role_is_not_a_mismatch():
    c = full()
    for dt, k, v in ((PURCHASE_CONTRACT, "seller_name", "SUPPLIER LTD"), (PURCHASE_INVOICE, "seller_name", "SUPPLIER LTD"),
                     (PURCHASE_INVOICE, "buyer_name", "VRV GLOBAL PTE LTD"), (PURCHASE_CONTRACT, "buyer_name", "VRV GLOBAL PTE LTD"),
                     (SALES_CONTRACT, "seller_name", "VRV GLOBAL PTE LTD"), (SALES_CONTRACT, "buyer_name", "BUYER LTD"),
                     (SALES_INVOICE, "buyer_name", "BUYER LTD"), (BILL_OF_LADING, "shipper_name", "FAST FORWARDING CO"),
                     (BILL_OF_LADING, "notify_party_name", "BUYER LTD")):
        c.docs[dt].fields[k] = fld(v)
    r = check_parties(c, "X")
    assert r.result == PASS_NOTE and "shipping role" in r.reason


# ------------------------------------------------------------------ 33-40 exchange-based pricing

@pytest.mark.parametrize("formula", ["LME Copper CSP x 90% x 80% recovery", "COMEX Copper Settlement + Premium USD 50"])
def test_33_34_37_exchange_formula_accepted_without_benchmark(formula):
    c = full(pc=contract(PURCHASE_CONTRACT, formula=formula))
    r = check_contract_price(c, "X")
    assert r.result == PASS_NOTE and "not independently verified" in r.reason
    assert check_expected_amount(c, "X").result in (PASS, PASS_NOTE)


def test_35_invoice_formula_matches_contract():
    pi = invoice(PURCHASE_INVOICE, price="6,500.00", amount="97,500.00",
                 text="COMMERCIAL INVOICE\nPrice basis: LME Copper CSP x 90% x 80% recovery")
    c = full(pi=pi, pc=contract(PURCHASE_CONTRACT, formula="LME Copper CSP x 90% x 80% recovery"))
    assert check_contract_price(c, "X").result == PASS_NOTE


def test_36_invoice_formula_factor_conflicts():
    si = invoice(SALES_INVOICE, text="COMMERCIAL INVOICE\nPrice basis: LME Copper CSP x 85% x 80% recovery")
    c = full(si=si, sc=contract(SALES_CONTRACT, formula="LME Copper CSP x 90% x 80% recovery"))
    r = check_contract_price(c, "X")
    assert r.result == MISMATCH and "pricing factor" in r.reason


def test_38_final_price_without_formula_breakdown_accepted():
    c = full(sc=contract(SALES_CONTRACT, formula="LME Copper CSP x 90%"))
    r = check_contract_price(c, "X")
    comp = next(x for x in r.comparisons if x["comparison"].startswith("SI"))
    assert comp["result"] == PASS_NOTE and "SI declares 7,200.00 USD/MT" in comp["calculation"]


def test_39_different_exchange_formulas_per_side():
    c = full(pc=contract(PURCHASE_CONTRACT, formula="SHFE Copper settlement x 95%"),
             sc=contract(SALES_CONTRACT, formula="LME Copper CSP x 90%"))
    assert check_contract_price(c, "X").result == PASS_NOTE


def test_40_no_network_access_during_price_validation(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    c = full(pc=contract(PURCHASE_CONTRACT, formula="LME Copper CSP x 90%"))
    check_contract_price(c, "X")
    check_expected_amount(c, "X")


# ------------------------------------------------------------------ 41-46 OCR and layout

def test_41_o_zero_in_numeric_value_corrected():
    assert fix_numeric_ocr("USD 1O,OOO", field_is_numeric=True)[0] == "USD 10,000"
    assert fix_numeric_ocr("15.OO MT", field_is_numeric=True)[0] == "15.00 MT"


def test_42_identifier_preserved():
    assert fix_numeric_ocr("ABC1O52", field_is_numeric=False)[0] == "ABC1O52"


def test_43_vertical_label_above_value():
    # "Gross Weight" column header two lines above its value
    assert label_confirmed("gross_weight", ["Description of Goods  Gross Weight", "(KGS)", "26061.5"])
    assert label_confirmed("shipped_on_board_date", ["Shipped On Board", "Korea Marine Transport", "Date Aug. 26, 2026"])


def test_44_multi_page_contract_clause_spanning_pages():
    doc = make_doc("A. TERMS\nD. PAYMENT\nThe Buyer shall pay 10% advance\fand the balance against copy of DBL\n"
                   "Documents to be presented:\n1. Commercial Invoice", "pc.pdf", PURCHASE_CONTRACT, method="ocr")
    clause, page = payment_clause(doc)
    assert page == 1 and "10% advance" in clause and "balance against copy of DBL" in clause


def _pdf(tmp_path, pages):
    d = pymupdf.open()
    for text in pages:
        y = 60
        p = d.new_page()
        for line in text.split("\n"):
            p.insert_text((50, y), line, fontsize=9)
            y += 12
    path = tmp_path / "combined.pdf"
    d.save(path)
    return str(path)


def test_45_combined_pdf_split_and_classified(tmp_path):
    from tradepilot.pdf_text import extract_pages
    pages = [
        "Contract No.: VRV/2026/001/ P00001\nThis agreement is made at SINGAPORE\nVRV GLOBAL PTE LTD, having its office at "
        "Singapore, hereinafter called 'Buyer'.\nAND ACME SPA, having its office at Chile, hereinafter called 'Seller'.\n"
        "A. TERMS OF CONTRACT\nMaterial : Copper Scrap Druid\nQuantity : 15 MT",
        "B. PRICING\nPrice : USD 6,500 per MT\nC. SHIPMENT\nPartial Shipment : Not allowed\nD. PAYMENT\n100% TT on DBL copy",
        "ACME SPA\nCOMMERCIAL INVOICE\nInvoice No: 77  Invoice Date: 01-07-2026\nCUSTOMER VRV GLOBAL PTE LTD\n"
        "Copper Scrap Druid 15.000 MT USD 6,500 USD 97,500\nTOTAL USD 97,500.00\nBeneficiary: ACME SPA",
        "BILL OF LADING\nShipper: ACME SPA\nConsignee: TO ORDER\nNotify Party: BUYER LTD\nPort of Loading: SAN ANTONIO\n"
        "Port of Discharge: NHAVA SHEVA\nTOTAL NET WEIGHT: 15.000 MT",
        "Contract No.: VRV/2026/001/ S00001\nThis agreement is made at SINGAPORE\nVRV GLOBAL PTE LTD, having its office at "
        "Singapore, hereinafter called 'Seller'.\nAND BUYER LTD, having its office at India, hereinafter called 'Buyer'.\n"
        "A. TERMS OF CONTRACT",
        "VRV GLOBAL PTE LTD\nCOMMERCIAL INVOICE\nInvoice No: VRV/INV/1  Invoice Date: 02-07-2026\nTO BUYER LTD\n"
        "TOTAL USD 108,000.00\nBeneficiary: VRV GLOBAL PTE LTD",
    ]
    path = _pdf(tmp_path, pages)
    parts = split_combined(path, "combined.pdf", extract_pages(path))
    types = [classify(p, use_filename=False).doc_type for p in parts]
    assert types == [PURCHASE_CONTRACT, PURCHASE_INVOICE, BILL_OF_LADING, SALES_CONTRACT, SALES_INVOICE]
    assert parts[0].source_pages == [1, 2] and parts[0].filename == "combined.pdf [pages 1-2]"


def test_46_ambiguous_ocr_critical_amount_is_review():
    si = invoice(SALES_INVOICE, qty="99.199", price="2,070.00", amount="205,962.93", method="ocr")
    r = check_invoice_arithmetic(full(si=si), "X")
    assert r.result == REVIEW


# ------------------------------------------------------------------ 47-50 aggregation

def test_47_missing_document_overrides_passing_checks():
    assert overall([PASS, PASS_NOTE, check_mandatory_docs(mandatory_docs(missing=(SALES_INVOICE,)), "X").result]) == FAIL


def test_48_mismatch_with_optional_note():
    assert overall([PASS, check_adjustment_notes(full(), "X").result, MISMATCH]) == MISMATCH


def test_49_exchange_note_gives_pass_with_note():
    c = full(pc=contract(PURCHASE_CONTRACT, formula="LME Copper CSP x 90%"))
    assert overall([PASS, check_contract_price(c, "X").result]) == PASS_NOTE


def test_50_all_clean_is_pass():
    c = full()
    assert overall([check_contract_price(c, "X").result, check_expected_amount(c, "X").result,
                    check_shipped_qty(c, "X").result, check_contract_qty(c, "X").result]) == PASS


def test_fixture_documents_are_independent():
    a, b = full(), full()
    assert a.docs[PURCHASE_INVOICE] is not b.docs[PURCHASE_INVOICE]
    assert copy.deepcopy(a.pi).qty_mt == pytest.approx(15.0)


def test_gross_header_above_container_rows_and_bulk_cargo_used_as_net():
    ctx_lines = ["MARKS & NUMBERS DESCRIPTION OF GOODS GROSS WEIGHT GROSS MEASUREMENT",
                 "TCLU3184131 / NGAA53385 1 BULK", "/FCL / FCL/20GP/25250.000KGS/50.000M3",
                 "TCLU3184131 2 2 BULKS IN TOTAL 50450.000KGS"]
    assert label_confirmed("gross_weight", ctx_lines)          # header above container rows
    assert not label_confirmed("net_weight", ctx_lines)        # never a net weight
    assert not label_confirmed("gross_weight", ["Gross Weight", "100.000CBM"])   # a volume on the value line
