"""Rule evaluation: PASS / MISMATCH / REVIEW_REQUIRED behaviour."""
from datetime import date

import pytest

from conftest import FakeModel, make_doc
from tradepilot import pipeline
from tradepilot.canonical import ContractView, Material, build_invoice
from tradepilot.checklist import load_checklist, match_payment_term, payment_features
from tradepilot.checks import (Ctx, ai_compare, check_gp, check_invoice_arithmetic,
                               compare_pairs, eq_id)
from tradepilot.models import (AMBIGUOUS, MISMATCH, FOUND, PASS, PURCHASE_INVOICE, REVIEW, SALES_INVOICE,
                               Field)

CHECKLIST = "Documents/Basic Checklist.xlsx"


def fld(raw, status=FOUND, norm=None, name="x"):
    return Field(name, raw, norm if norm is not None else raw, status=status, page=1, document="d.pdf")


# --------------------------------------------------------------- deterministic equality

def test_clear_match_passes():
    assert compare_pairs([("PI", fld("VRV/2026/001/ P00001"), "PC", fld("VRV/2026/001/P00001"), True)], eq_id)[0] == PASS


def test_clear_mismatch_fails():
    assert compare_pairs([("SI", fld("SCL600289500"), "BL", fld("KMTC123"), True)], eq_id)[0] == MISMATCH


def test_missing_required_field_needs_review():
    res, comps, reasons = compare_pairs([("SI", None, "BL", fld("SCL1"), True)], eq_id)
    assert res == REVIEW and "NOT_FOUND" in reasons[0]


def test_missing_optional_field_is_not_a_failure():
    res, _, _ = compare_pairs([("SI", fld("A1"), "BL", fld("A1"), True), ("PI", None, "BL", fld("A1"), False)], eq_id)
    assert res == PASS


def test_ambiguous_extraction_needs_review():
    res, _, _ = compare_pairs([("PI", fld("P00006", status=AMBIGUOUS), "PC", fld("P00006"), True)], eq_id)
    assert res == REVIEW


def test_conflicting_values_with_one_clear_mismatch_fail():
    res, _, _ = compare_pairs([("SI", fld("A1"), "BL", fld("A1"), True), ("PI", fld("B2"), "BL", fld("A1"), True)], eq_id)
    assert res == MISMATCH


# --------------------------------------------------------------- tolerance rule

def _cv(qty=26, tol=5, per_bl=None, partial=False):
    return ContractView(make_doc("x", "pc.pdf", "PURCHASE_CONTRACT"), [Material("Copper", "74040012", qty, tol, 7266, "FIXED")], qty, tol, per_bl, partial)


def _tol(qty, cv):
    from tradepilot.commercial import _tolerance_eval
    return _tolerance_eval(qty, 0.0001, cv, cv.materials[0], "PI", 1)[0]


@pytest.mark.parametrize("shipped,expected", [(26.0015, PASS), (27.3, PASS), (27.31, MISMATCH), (24.0, MISMATCH)])
def test_quantity_tolerance(shipped, expected):
    assert _tol(shipped, _cv()) == expected


def test_partial_shipment_allows_lower_quantity():
    assert _tol(50.45, _cv(qty=200, partial=True)) == PASS


def test_per_bl_lot_is_the_basis_when_stated():
    assert _tol(108.85, _cv(qty=200, tol=5, per_bl=100, partial=True)) == MISMATCH   # max 105


def test_unclear_tolerance_needs_review():
    cv = _cv(tol=None)
    cv.total_tol_pct = None
    assert _tol(26, cv) == REVIEW


# --------------------------------------------------------------- invoice arithmetic & GP

def _invoice(doc_type, qty, price, amount, unit="MT", words=None, method="pdf_text", locale="US"):
    d = make_doc("invoice", f"{doc_type}.pdf", doc_type, method=method)
    d.number_locale = locale
    d.line_items = [{"description": fld("Copper"), "quantity": fld(qty), "quantity_unit": fld(unit),
                     "unit_price": fld(price), "amount": fld(amount)}]
    if words:
        d.fields["amount_in_words"] = fld(words)
    return d


def _ctx(docs):
    ctx = Ctx({d.doc_type: d for d in docs}, [], FakeModel(), date(2026, 10, 10))
    ctx.pi = build_invoice(ctx.docs[PURCHASE_INVOICE]) if PURCHASE_INVOICE in ctx.docs else None
    ctx.si = build_invoice(ctx.docs[SALES_INVOICE]) if SALES_INVOICE in ctx.docs else None
    return ctx


def test_eu_invoice_reconciles_per_kg_to_per_mt():
    ctx = _ctx([_invoice(PURCHASE_INVOICE, "26.001,5", "USD 7,26600", "USD 188.926,90", unit="KG", locale="EU")])
    line = ctx.pi.lines[0]
    assert line.reconciled and line.qty_mt == pytest.approx(26.0015) and line.price_per_mt == pytest.approx(7266)


def test_ambiguous_quantity_resolved_by_arithmetic():
    ctx = _ctx([_invoice(PURCHASE_INVOICE, "99.499.000 MT", "USD 2,050", "USD 203,972.95", locale=None)])
    assert ctx.pi.lines[0].reconciled and ctx.pi.lines[0].qty_mt == pytest.approx(99.499)


def test_amount_in_words_mismatch_fails():
    si = _invoice(SALES_INVOICE, "99.1110", "2,070.00", "205,159.77",
                  words="ONE HUNDRED SEVENTY THREE THOUSAND FIVE HUNDRED SIXTY NINE AND SIXTY FOUR CENTS ONLY")
    assert check_invoice_arithmetic(_ctx([si]), "METALS-9").result == MISMATCH


def test_single_digit_slip_on_ocr_page_needs_review():
    si = _invoice(SALES_INVOICE, "19.527", "2,860.00", "141,647.22", method="ocr")   # really 49.527
    si.fields["gross_amount"] = fld("141,647.22")
    assert check_invoice_arithmetic(_ctx([si]), "METALS-1").result in (MISMATCH, REVIEW)


def test_gp_threshold():
    ok = _ctx([_invoice(PURCHASE_INVOICE, "26.0015", "7,266.00", "188,926.90"),
               _invoice(SALES_INVOICE, "26.0015", "7,508.72", "195,237.98")])
    low = _ctx([_invoice(PURCHASE_INVOICE, "99.111", "2,050.00", "203,177.55"),
                _invoice(SALES_INVOICE, "99.111", "2,070.00", "205,159.77")])
    assert check_gp(ok, "COMMON-8", 2.0).result == PASS
    r = check_gp(low, "COMMON-8", 2.0)
    assert r.result == MISMATCH and "0.97%" in r.reason


# --------------------------------------------------------------- AI-assisted checks

def test_ai_invalid_or_missing_answer_is_an_error():
    assert ai_compare(FakeModel([{"answer": "MAYBE"}]), "goods", "a", "b", "?")[0] == "ERROR"
    assert ai_compare(FakeModel([None]), "goods", "a", "b", "?")[0] == "ERROR"
    assert ai_compare(FakeModel([{"answer": "YES", "reason": "same ISRI grade"}]), "goods", "a", "b", "?")[0] == "YES"


# --------------------------------------------------------------- checklist

def test_checklist_loads_and_pins_interpretations():
    items, sheet = load_checklist(CHECKLIST)
    ids = {i.rule_id: i for i in items}
    assert sheet == "Sheet4" and len([i for i in items if i.section == "METALS"]) == 9
    assert all(ids[f"COMMON-{n}"].interpretation_status == "CURRENT" for n in range(1, 9))
    assert "GP%" in ids["COMMON-8"].original_text      # original wording preserved


@pytest.mark.parametrize("clause,row,status", [
    ("The Buyer shall pay 100% of the Invoice amount within 3 Business Days following presentation of the Scan "
     "Copy of Draft Bills of Lading to the Buyer for Payment, after vessel sailing.", "METALS-1", "IDENTIFIED"),
    ("Advance Payment: Ten percent (10%) of the total invoice value against the Proforma Invoice. Balance Payment: "
     "90% against a scanned copy of the Original Bill of Lading (OBL) & AIFTA copy.", "METALS-9", "IDENTIFIED"),
    ("10% Deposit and Balance 100% Via telegraphic transfer within 3 days after receiving the draft copy.", "METALS-5", "IDENTIFIED"),
    ("USD 50,0000 Deposit against the signed contract and balance within 3 days after receiving the Scan OBL "
     "copy and AIFTA copy.", "METALS-9", "AMBIGUOUS"),
])
def test_payment_term_matching(clause, row, status):
    items, _ = load_checklist(CHECKLIST)
    m = match_payment_term(clause, items)
    assert (m["rule_id"], m["status"]) == (row, status)


def test_fixed_deposit_is_noted():
    assert "fixed amount" in payment_features("USD 50,0000 Deposit against the signed contract").notes[0]


# --------------------------------------------------------------- batch

def test_batch_continues_after_a_case_crashes(tmp_path, monkeypatch):
    for name in ("1", "2", "3"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.pdf").write_bytes(b"%PDF-1.4")

    def fake_case(path, model, today):
        if path.endswith("2"):
            raise RuntimeError("corrupt PDF")
        return {"case_id": path[-1], "summary": {"overall_status": PASS}}
    monkeypatch.setattr(pipeline, "process_case", fake_case)
    results = pipeline.process_batch(str(tmp_path), model=FakeModel())
    assert [r["case_id"] for r in results] == ["1", "2", "3"]
    assert results[1]["summary"]["overall_status"] == REVIEW and "corrupt PDF" in results[1]["error"]


def test_lines_of_same_material_are_summed_before_tolerance():
    from tradepilot.commercial import check_contract_qty
    pi = _invoice(PURCHASE_INVOICE, "16.1765", "9,616.68", "155,564.22")
    pi.line_items.append({"description": fld("Copper"), "quantity": fld("3.855"), "quantity_unit": fld("MT"),
                          "unit_price": fld("7,510.63"), "amount": fld("28,953.48")})
    ctx = _ctx([pi])
    ctx.pc = _cv(qty=20, tol=5)
    r = check_contract_qty(ctx, "METALS-1")
    comp = [c for c in r.comparisons if c["comparison"] == "PI quantity vs PC quantity"][0]
    assert comp["result"] == PASS and comp["left"]["normalized_value"] == pytest.approx(20.0315)
    assert "sum of 2 line" in comp["left"]["computation"]


def test_mismatch_is_review_when_value_appears_in_other_document():
    from tradepilot import checks
    checks._DOC_TEXTS.clear()
    checks._DOC_TEXTS.update({"bl.pdf": "VOYAGE NUMBER MA628R\nVESSEL MSC NEW HAVEN VIII", "si.pdf": "VOYAGE NO MA628R"})
    a = Field("voyage", "MA628R", status=FOUND, document="si.pdf")
    b = Field("voyage", "VIII", status=FOUND, document="bl.pdf")
    assert compare_pairs([("SI", a, "BL", b, True)], eq_id)[0] == REVIEW
    checks._DOC_TEXTS.clear()


def test_ocr_single_digit_quantity_doubt_is_review():
    from tradepilot.canonical import one_digit_apart
    assert one_digit_apart("99.199", 99.499) and not one_digit_apart("99.199", 98.4)
    si = _invoice(SALES_INVOICE, "99.199", "2,070.00", "205,962.93", method="ocr")
    ctx = _ctx([si])
    assert ctx.si.lines[0].ocr_doubt
    assert check_invoice_arithmetic(ctx, "METALS-9").result == REVIEW
