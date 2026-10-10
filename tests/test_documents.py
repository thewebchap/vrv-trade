"""Discovery, classification, extraction grounding."""
import pymupdf

from conftest import FakeModel, make_doc
from tradepilot.classify import classify, resolve_duplicates
from tradepilot.extract import extract
from tradepilot.models import (AMBIGUOUS, BILL_OF_LADING, FOUND, PURCHASE_CONTRACT, PURCHASE_INVOICE,
                               SALES_CONTRACT, SALES_INVOICE)
from tradepilot.pipeline import discover, is_case_dir

CONTRACT = ("Contract No.: VRV/2026/001/ P00001\nThis agreement is made at SINGAPORE\n"
            "VRV GLOBAL PTE LTD , having its office at 138 Cecil Street, hereinafter called 'Buyer'.\n"
            "AND ACME SPA, having its office at Chile hereinafter called 'Seller'.\nA. TERMS OF CONTRACT\n")


def _pdf(path, text):
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), text)
    d.save(path)


def test_discovery_finds_only_pdfs(tmp_path):
    _pdf(tmp_path / "1. PC.pdf", "x")
    _pdf(tmp_path / "b.PDF", "y")
    (tmp_path / "notes.txt").write_text("ignore")
    (tmp_path / ".DS_Store").write_text("")
    assert [p.split("/")[-1] for p in discover(str(tmp_path))] == ["1. PC.pdf", "b.PDF"]
    assert is_case_dir(str(tmp_path)) and not is_case_dir(str(tmp_path / "missing"))


def test_classify_contract_side_from_content():
    assert classify(make_doc(CONTRACT, "1. PC.pdf")).doc_type == PURCHASE_CONTRACT
    sc = classify(make_doc(CONTRACT.replace("'Buyer'", "'TMP'").replace("'Seller'", "'Buyer'").replace("'TMP'", "'Seller'"), "4. SC.pdf"))
    assert (sc.doc_type, sc.classification_status) == (SALES_CONTRACT, "IDENTIFIED")


def test_classify_invoices_by_issuer():
    pi = classify(make_doc("ACME SPA\nCOMMERCIAL INVOICE\nCUSTOMER VRV GLOBAL PTE LTD\nInvoice No 1\nTOTAL USD 10", "2. PI.pdf"))
    si = classify(make_doc("VRV Global Pte Ltd\nCOMMERCIAL INVOICE\nTO BUYER LTD\nINVOICE NO 9\nBeneficlary: VRY GLOBAL PTE LTD", "5. SI.pdf"))
    assert pi.doc_type == PURCHASE_INVOICE and si.doc_type == SALES_INVOICE   # OCR slips tolerated


def test_classify_bill_of_lading():
    d = classify(make_doc("BILL OF LADING\nShipper: ACME\nConsignee: TO ORDER\nNotify Party: X\nPort of Loading: SAN ANTONIO", "3. BL.pdf"))
    assert (d.doc_type, d.classification_status) == (BILL_OF_LADING, "IDENTIFIED")


def test_filename_disagreement_is_ambiguous_and_filename_alone_never_identifies():
    d = classify(make_doc("BILL OF LADING\nShipper: A\nConsignee: B\nPort of Loading: C", "5. SI.pdf"))
    assert d.classification_status == "AMBIGUOUS"
    d = classify(make_doc("illegible scan", "2. PI.pdf"))
    assert (d.doc_type, d.classification_status) == (PURCHASE_INVOICE, "AMBIGUOUS")
    assert classify(make_doc("illegible scan", "scan001.pdf")).classification_status == "UNKNOWN"


def test_duplicate_types_become_ambiguous():
    docs = [classify(make_doc(CONTRACT, "a.pdf")), classify(make_doc(CONTRACT, "b.pdf"))]
    resolve_duplicates(docs)
    assert all(d.classification_status == "AMBIGUOUS" for d in docs)


BL_TEXT = ("BILL OF LADING\nPlace of Receipt: SANTIAGO\nPLACE & DATE OF ISSUE: SANTIAGO, 19-JUN-2026\n"
           "SHIPPED ON BOARD DATE: 19-JUN-2026\nVessel Port of Loading\nYM SUCCESS SAN ANTONIO\nTOTAL NET WEIGHT: 26.0015 MTS")


def test_extraction_grounds_values_and_flags_hallucinations():
    doc = make_doc(BL_TEXT, "3. BL.pdf", BILL_OF_LADING)
    model = FakeModel([{"bl_number": "SCL999", "shipped_on_board_date": "19-JUN-2026",
                        "port_of_loading": "SAN ANTONIO", "net_weight": "26.0015", "net_weight_unit": "MTS"}])
    extract(doc, model)
    f = doc.fields
    assert f["bl_number"].status == AMBIGUOUS                       # not in the document text
    sob = f["shipped_on_board_date"]
    assert sob.status == FOUND and "SHIPPED ON BOARD" in sob.source_text  # labelled occurrence preferred
    assert sob.normalized_value == "2026-06-19" and sob.page == 1
    assert f["port_of_loading"].status == FOUND and f["net_weight"].normalized_value == 26.0015


def test_value_without_matching_label_is_ambiguous():
    doc = make_doc("BILL OF LADING\nShipper: ACME\nDelivery: CIF Nhava Sheva\nConsignee", "3. BL.pdf", BILL_OF_LADING)
    extract(doc, FakeModel([{"port_of_loading": "Nhava Sheva"}]))
    assert doc.fields["port_of_loading"].status == AMBIGUOUS


def test_model_unavailable_is_recorded_not_raised():
    doc = make_doc(BL_TEXT, "3. BL.pdf", BILL_OF_LADING)
    extract(doc, FakeModel([None], error="Local model server not reachable"))
    assert doc.extraction_error == "Local model server not reachable" and not doc.fields
