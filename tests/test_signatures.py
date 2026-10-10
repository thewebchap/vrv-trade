"""Signature / stamp presence is a visual check, corroborated by ink, never by extracted text alone."""
from datetime import date

import pymupdf

from tradepilot.checks import Ctx, check_signatures
from tradepilot.local_model import ModelResponse
from tradepilot.models import PASS, PURCHASE_CONTRACT, REVIEW, SALES_CONTRACT, Document, Page
from tradepilot.signatures import MARK_INK_MIN, _label_boxes, _render, detect_page, mark_ink, signatory_blocks

BLUE, PURPLE = (0.15, 0.35, 0.8), (0.35, 0.15, 0.55)


def contract_page(tmp_path, name, seller=(), buyer=(), seller2=None):
    """A signature page with printed labels; marks: 'seal' and/or 'sign' drawn in ink under a label."""
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((230, 120), "SIGNATURE TO FOLLOW", fontsize=10)
    for x, label, marks in ((90, "FOR THE SELLER", seller), (360, "FOR THE BUYER", buyer)) + \
            (((90, "FOR THE SELLER 2", seller2),) if seller2 is not None else ()):
        y = 170 if "2" not in label else 400
        p.insert_text((x, y), label, fontsize=10)
        p.insert_text((x - 20, y + 75), "ACME LTD", fontsize=10)
        p.insert_text((x - 30, y + 95), "AUTHORIZED SIGNATORY & STAMP", fontsize=10)
        if "seal" in marks:
            p.draw_circle((x + 45, y + 35), 28, color=BLUE, width=3)
            p.draw_circle((x + 45, y + 35), 20, color=BLUE, width=1)
        if "sign" in marks:
            p.draw_polyline([(x + 10, y + 50), (x + 30, y + 15), (x + 45, y + 55), (x + 60, y + 18), (x + 85, y + 45)],
                            color=PURPLE, width=2)
    path = tmp_path / name
    d.save(path)
    return Document(str(path), name, [Page(1, p.get_text(), "pdf_text")], doc_type=PURCHASE_CONTRACT)


class VisionStub:
    """Answers the block question per party; the second-look question with `second`. None = model down."""
    name = "stub"

    def __init__(self, seller=None, buyer=None, second=None, down=False):
        self.answers = {"seller": seller, "buyer": buyer}
        self.second, self.down = second, down

    def ask_json(self, prompt, images=None, max_tokens=0):
        if self.down:
            return ModelResponse(None, "Local model server not reachable", self.name)
        if "HANDWRITTEN pen strokes" in prompt:
            return ModelResponse({"handwriting_present": self.second, "evidence": "strokes through the seal"}
                                 if self.second is not None else None, None, self.name)
        party = "buyer" if "(buyer side)" in prompt else "seller"
        a = self.answers[party]
        data = None if a is None else {"signature_present": a[0], "stamp_present": a[1], "evidence": "stub answer"}
        return ModelResponse(data, None, self.name)


def result(doc, model):
    """The same page stands in for both contracts, so the check sees a complete pair."""
    import copy
    sc = copy.copy(doc)
    sc.doc_type = SALES_CONTRACT
    ctx = Ctx({PURCHASE_CONTRACT: doc, SALES_CONTRACT: sc}, [], model, date(2026, 10, 10))
    blocks = detect_page(doc, 1, model)["blocks"]
    for dt in (PURCHASE_CONTRACT, SALES_CONTRACT):
        ctx.signatures[dt] = {"page": 1, "model": "stub", "blocks": blocks}
    return check_signatures(ctx, "COMMON-1")


def test_blocks_located_from_labels_including_a_second_seller(tmp_path):
    doc = contract_page(tmp_path, "a.pdf", seller2=())
    blocks = signatory_blocks(doc.path, 1, False)
    assert sorted(b["block"] for b in blocks) == ["BUYER", "SELLER", "SELLER (2)"]
    seller = next(b for b in blocks if b["block"] == "SELLER")
    buyer = next(b for b in blocks if b["block"] == "BUYER")
    assert seller["region"][0] < buyer["region"][0] and "via PDF text" in seller["located_by"]


def test_ink_measure_ignores_printed_text(tmp_path):
    signed = contract_page(tmp_path, "s.pdf", seller=("seal", "sign"))
    blank = contract_page(tmp_path, "b.pdf")
    for doc, expect_ink in ((signed, True), (blank, False)):
        img = _render(doc.path, 1, 200)
        boxes = [b for _, b in _label_boxes(doc.path, 1, False)]
        seller = next(b for b in signatory_blocks(doc.path, 1, False) if b["block"] == "SELLER")
        assert (mark_ink(img, seller["region"], boxes) >= MARK_INK_MIN) is expect_ink


def test_visible_signature_and_stamp_pass(tmp_path):
    doc = contract_page(tmp_path, "s.pdf", seller=("seal", "sign"), buyer=("seal", "sign"))
    r = result(doc, VisionStub(seller=(True, True), buyer=(True, True)))
    assert r.result == PASS
    e = r.evidence[0]
    assert e["signature_present"] and e["stamp_present"] and e["page"] == 1 and e["region"]
    assert "vision on cropped block" in e["detection_method"] and "non-text ink" in e["detection_method"]


def test_unsigned_page_is_not_passed_even_if_the_model_says_signed(tmp_path):
    doc = contract_page(tmp_path, "u.pdf")
    r = result(doc, VisionStub(seller=(True, True), buyer=(True, True)))     # hallucinating model
    assert r.result == REVIEW
    assert all(not e["signature_present"] and not e["stamp_present"] for e in r.evidence)
    assert "NOT accepted" in r.evidence[0]["detection_method"]


def test_ink_without_a_recognised_mark_is_review_not_pass(tmp_path):
    doc = contract_page(tmp_path, "m.pdf", seller=("seal", "sign"), buyer=("seal", "sign"))
    r = result(doc, VisionStub(seller=(False, False), buyer=(True, True)))
    assert r.result == REVIEW and "could not be classified" in r.reason


def test_signature_drawn_through_a_seal_found_on_second_look(tmp_path):
    doc = contract_page(tmp_path, "t.pdf", seller=("seal", "sign"), buyer=("seal", "sign"))
    r = result(doc, VisionStub(seller=(False, True), buyer=(True, True), second=True))
    assert r.result == PASS
    assert "second look" in r.evidence[0]["detection_method"]


def test_stamp_without_signature_stays_review(tmp_path):
    doc = contract_page(tmp_path, "st.pdf", seller=("seal",), buyer=("seal", "sign"))
    r = result(doc, VisionStub(seller=(False, True), buyer=(True, True), second=False))
    assert r.result == REVIEW and "no signature detected" in r.reason


def test_model_unavailable_is_review_not_a_crash(tmp_path):
    doc = contract_page(tmp_path, "d.pdf", seller=("seal", "sign"), buyer=("seal", "sign"))
    assert result(doc, VisionStub(down=True)).result == REVIEW


def test_presence_does_not_need_readable_text(tmp_path):
    """No OCR/text layer content inside the marks is required: they are pure drawings here."""
    doc = contract_page(tmp_path, "n.pdf", seller=("seal", "sign"), buyer=("seal", "sign"))
    words = " ".join(t for t, _ in _label_boxes(doc.path, 1, False))
    assert "SIGNED" not in words.upper() and "SEAL" not in words.upper()     # nothing textual about the marks
    assert result(doc, VisionStub(seller=(True, True), buyer=(True, True))).result == PASS
