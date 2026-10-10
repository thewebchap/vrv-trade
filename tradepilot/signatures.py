"""Signature / stamp PRESENCE on contract signature pages: a visual check.

Handwriting and seals carry little or no machine-readable text, so presence is never decided
from extracted text. Layers, per party signature block:

  1. locate the block from its label ("FOR THE SELLER" / "FOR THE BUYER", "Authorised Signatory"...)
     using native PDF text or OCR word positions; fall back to the bottom of the page
  2. deterministic visual evidence inside the block:
       - coloured (non-black) ink coverage: stamps and signatures are typically blue/purple/red ink,
         while the printed contract is black
       - embedded image objects (signature/stamp images pasted into digital PDFs)
       - any text OCR can read in the block (e.g. an inked "For <Company> / Authorised Signatory" stamp)
  3. the local vision model, asked a constrained question about the CROPPED block (and the whole page)

Only presence is checked; signer identity and authenticity are not (the checklist does not ask).
"""
from __future__ import annotations

import io
import re

import pymupdf


LABELS = {"SELLER": r"FOR\s+THE\s+SELLER|\bSELLER\b", "BUYER": r"FOR\s+THE\s+BUYER|\bBUYER\b"}


def _render(path: str, page_no: int, dpi: int):
    from PIL import Image
    with pymupdf.open(path) as doc:
        return Image.open(io.BytesIO(doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png"))).convert("RGB")


def _label_boxes(path: str, page_no: int, is_ocr: bool) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(text, (x0, y0, x1, y1)) in page fractions, top-down, from the text layer or OCR."""
    if not is_ocr:
        with pymupdf.open(path) as doc:
            p = doc[page_no - 1]
            w, h = p.rect.width, p.rect.height
            out = []
            for phrase in ("FOR THE SELLER", "FOR THE BUYER", "SELLER", "BUYER"):
                out += [(phrase, (r.x0 / w, r.y0 / h, r.x1 / w, r.y1 / h)) for r in p.search_for(phrase)]
            out += [(t, (x0 / w, y0 / h, x1 / w, y1 / h)) for x0, y0, x1, y1, t, *_ in p.get_text("words")]
            return out
    from ocrmac import ocrmac
    obs = ocrmac.OCR(_render(path, page_no, 200), recognition_level="accurate", language_preference=["en-US"]).recognize()
    return [(t, (b[0], 1 - b[1] - b[3], b[0] + b[2], 1 - b[1])) for t, _c, b in obs]


BLOCK_LABEL = re.compile(r"FOR\s+THE\s+(SELLER|BUYER)(\s*\d)?|FOR\s+AND\s+ON\s+BEHALF\s+OF\s+(THE\s+)?(SELLER|BUYER)")


def signatory_blocks(path: str, page_no: int, is_ocr: bool, boxes=None) -> list[dict]:
    """Every labelled signatory block on the page ("FOR THE SELLER", "FOR THE SELLER 2", "FOR THE BUYER").
    Each block's region is its column, from just above the label to below the typed signatory line."""
    boxes = boxes if boxes is not None else _label_boxes(path, page_no, is_ocr)
    found = []
    for t, b in boxes:
        m = BLOCK_LABEL.search(t.upper())
        if m and (b[2] - b[0]) < 0.45:
            party = m.group(1) or m.group(4)
            name = f"{party}{(' ' + m.group(2).strip()) if m.group(2) else ''}"
            if not any(abs(f["label_box"][1] - b[1]) < 0.02 and abs(f["label_box"][0] - b[0]) < 0.1 for f in found):
                found.append({"block": name, "party": party, "label_box": b})
    if not found:     # no "FOR THE ..." labels: bare SELLER / BUYER words, lowest occurrence
        for party, rx in LABELS.items():
            hits = [b for t, b in boxes if re.search(r"\b" + party + r"\b", t.upper()) and (b[2] - b[0]) < 0.45]
            if hits:
                found.append({"block": party, "party": party, "label_box": max(hits, key=lambda b: b[1])})
    seen: dict[str, int] = {}
    for f in sorted(found, key=lambda f: f["label_box"][1]):
        seen[f["block"]] = seen.get(f["block"], 0) + 1
        if seen[f["block"]] > 1:
            f["block"] = f"{f['block']} ({seen[f['block']]})"
    out = []
    for f in found:
        x0, y0, x1, y1 = f["label_box"]
        cx = (x0 + x1) / 2
        region = (max(0.0, cx - 0.25), max(0.0, y0 - 0.06), min(1.0, cx + 0.25), min(1.0, y1 + 0.15))
        out.append(f | {"region": region, "located_by": f"label '{f['block'].title()}' via {'OCR' if is_ocr else 'PDF text'}",
                        "text_in_region": [t for t, b in boxes if _inside(b, region)][:12]})
    if not any(o["party"] == "SELLER" for o in out) or not any(o["party"] == "BUYER" for o in out):
        for party, (a, b) in (("SELLER", (0.0, 0.5)), ("BUYER", (0.5, 1.0))):
            if not any(o["party"] == party for o in out):
                out.append({"block": party, "party": party, "region": (a, 0.62, b, 1.0), "label_box": None,
                            "located_by": "fallback: bottom 38% of the page (seller assumed left, buyer right)",
                            "text_in_region": []})
    return out


def party_regions(path: str, page_no: int, is_ocr: bool) -> dict:
    """Lowest labelled block per party (kept for callers that need one region per party)."""
    out = {}
    for b in signatory_blocks(path, page_no, is_ocr):
        if b["party"] not in out or (b["region"][1] > out[b["party"]]["region"][1] and b["block"] == b["party"]):
            out[b["party"]] = b
    return out


def _inside(b, r) -> bool:
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    return r[0] <= cx <= r[2] and r[1] <= cy <= r[3]


def ink_evidence(img, region) -> dict:
    """Coloured-ink coverage in the region (fraction of pixels that are saturated and not near-white)."""
    import numpy as np
    w, h = img.size
    crop = img.crop((int(region[0] * w), int(region[1] * h), int(region[2] * w), int(region[3] * h)))
    hsv = np.asarray(crop.convert("HSV"), dtype=np.int16)
    s, v = hsv[..., 1], hsv[..., 2]
    colored = (s > 70) & (v < 235) & (v > 40)
    ratio = float(colored.mean()) if colored.size else 0.0
    dark = float(((v < 110) & (s <= 70)).mean()) if colored.size else 0.0
    return {"colored_ink_ratio": round(ratio, 4), "dark_ink_ratio": round(dark, 4)}


def mark_ink(img, region, text_boxes) -> float:
    """Fraction of the block covered by ink that is NOT part of any recognised text line: coloured pixels or
    dark pixels outside the text boxes. Signatures and stamps leave such ink; printed labels do not."""
    import numpy as np
    from PIL import ImageOps
    w, h = img.size
    x0, y0, x1, y1 = int(region[0] * w), int(region[1] * h), int(region[2] * w), int(region[3] * h)
    crop = img.crop((x0, y0, x1, y1))
    hsv = np.asarray(crop.convert("HSV"), dtype=np.int16)
    s = hsv[..., 1]
    # contrast-normalised lightness, so faint grey scans of seals/signatures still register as ink
    v = np.asarray(ImageOps.autocontrast(crop.convert("L"), cutoff=0.5), dtype=np.int16)
    ink = ((s > 70) & (hsv[..., 2] < 235) & (hsv[..., 2] > 40)) | (v < 150)
    for b in text_boxes:
        bx0, by0, bx1, by1 = (int(b[0] * w) - x0 - 3, int(b[1] * h) - y0 - 3, int(b[2] * w) - x0 + 3, int(b[3] * h) - y0 + 3)
        if bx1 > 0 and by1 > 0 and bx0 < ink.shape[1] and by0 < ink.shape[0]:
            ink[max(0, by0):max(0, by1), max(0, bx0):max(0, bx1)] = False
    return round(float(ink.mean()), 4) if ink.size else 0.0


def embedded_images(path: str, page_no: int, region) -> int:
    """Image objects on a digital page overlapping the region (pasted signature/stamp images)."""
    with pymupdf.open(path) as doc:
        p = doc[page_no - 1]
        w, h = p.rect.width, p.rect.height
        n = 0
        for info in p.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            box = (x0 / w, y0 / h, x1 / w, y1 / h)
            if (box[2] - box[0]) > 0.6 and (box[3] - box[1]) > 0.6:
                continue                                    # a full-page scan is not a pasted mark
            if box[0] < region[2] and box[2] > region[0] and box[1] < region[3] and box[3] > region[1]:
                n += 1
        return n


def crop_png(img, region, pad: float = 0.0) -> bytes:
    w, h = img.size
    box = (int(max(0, region[0] - pad) * w), int(max(0, region[1] - pad) * h),
           int(min(1, region[2] + pad) * w), int(min(1, region[3] + pad) * h))
    buf = io.BytesIO()
    img.crop(box).save(buf, format="PNG")
    return buf.getvalue()


BLOCK_PROMPT = """This image is one party's signature block cut from the last page of a commodity contract ({party} side).
Report what is VISIBLY present. Definitions:
- "signature_present": handwritten strokes, a cursive signature, initials or a pasted signature image. A signature drawn over or through a stamp counts.
- "stamp_present": any company stamp, seal or chop: round/oval seals, rectangular rubber stamps, or inked company-name blocks such as "For <Company> ... Authorised Signatory" stamped in coloured ink.
Printed black text such as "FOR THE SELLER", a typed company name or "AUTHORIZED SIGNATORY & STAMP" is NOT a signature or stamp.
Return JSON: {{"signature_present": true|false, "stamp_present": true|false, "evidence": "<what you see and where, one sentence>"}}"""


FOCUS_PROMPT = """This image shows one party's signature block on a contract, which contains a company stamp/seal.
Look closely at the stamp area. Besides the printed ring text and border of the stamp, are there HANDWRITTEN pen strokes
(a signature, initials or a scribble) drawn across, inside or next to the stamp?
Return JSON: {"handwriting_present": true|false, "evidence": "<one sentence>"}"""


def _second_look(model, doc_path: str, pdf_page: int, block: dict) -> dict | None:
    """A stamp was seen but no signature: signatures drawn through a seal are easy to miss at page scale,
    so look again at 300 dpi with a single, specific question."""
    img = _render(doc_path, pdf_page, 300)
    resp = model.ask_json(FOCUS_PROMPT, images=[crop_png(img, block["region"])], max_tokens=150)
    d = resp.data or {}
    if not isinstance(d.get("handwriting_present"), bool):
        return None
    return {"handwriting_present": d["handwriting_present"], "evidence": str(d.get("evidence", ""))[:300]}


def _ask_block(model, img, block: dict) -> dict:
    """Constrained visual question on the cropped block. Never raises."""
    resp = model.ask_json(BLOCK_PROMPT.format(party=block["block"].lower()),
                          images=[crop_png(img, block["region"], pad=0.02)], max_tokens=200)
    d = resp.data or {}
    ok = isinstance(d.get("signature_present"), bool) and isinstance(d.get("stamp_present"), bool)
    if not ok:
        return {"error": resp.error or f"model answer did not match the schema: {str(d)[:120]}"}
    return {"signature_present": d["signature_present"], "stamp_present": d["stamp_present"],
            "evidence": str(d.get("evidence", ""))[:300]}


# Non-text ink needed to corroborate a "present" answer. Calibrated on the dataset: real signature blocks
# 0.18%-3.1% of the block, blocks with the marks removed 0.00%-0.01%.
MARK_INK_MIN = 0.0008


def detect_page(doc, page_no: int, model, whole_page: dict | None = None) -> dict:
    """Presence of signature and stamp for every signatory block on a page, with layered evidence."""
    is_ocr = doc.pages[page_no - 1].method == "ocr" if doc.pages else True
    pdf_page = doc.source_pages[page_no - 1] if doc.source_pages else page_no
    img = _render(doc.path, pdf_page, 200)
    boxes = _label_boxes(doc.path, pdf_page, is_ocr)
    text_boxes = [b for _, b in boxes]
    blocks = []
    for b in signatory_blocks(doc.path, pdf_page, is_ocr, boxes):
        ink = ink_evidence(img, b["region"])
        ink["non_text_ink_ratio"] = mark_ink(img, b["region"], text_boxes)
        has_ink = ink["non_text_ink_ratio"] >= MARK_INK_MIN
        imgs = embedded_images(doc.path, pdf_page, b["region"]) if not is_ocr else 0
        vis = _ask_block(model, img, b)
        whole = (whole_page or {}).get(b["party"].lower()) if b["block"] == b["party"] else None
        methods = []
        sig = bool(vis.get("signature_present")) or bool(whole and whole.get("signature"))
        stamp = bool(vis.get("stamp_present")) or bool(whole and whole.get("stamp"))
        sig_basis = ("vision (cropped block)" if vis.get("signature_present") else
                     "vision (whole page)" if sig else None)
        stamp_basis = ("vision (cropped block)" if vis.get("stamp_present") else
                       "vision (whole page)" if stamp else None)
        if vis.get("signature_present") or vis.get("stamp_present"):
            methods.append("vision on cropped block")
        if whole and (whole.get("signature") or whole.get("stamp")):
            methods.append("vision on whole page")
        typed = {"FOR THE SELLER", "FOR THE BUYER", "SIGNATURE TO FOLLOW"}
        stamp_text = [t for t in b["text_in_region"] if re.search(r"^\s*FOR\s+\S", t.upper()) and t.upper().strip() not in typed
                      and not BLOCK_LABEL.search(t.upper())]
        if stamp_text:
            methods.append(f"OCR read stamp wording: '{stamp_text[0][:60]}'")
            if not stamp and ink["colored_ink_ratio"] >= 0.003:
                # wording printed in coloured ink inside the block ("For <Company> ... Authorised Signatory"):
                # a rubber stamp, even when it is not a round seal
                stamp = True
                stamp_basis = (f"inked company stamp: '{stamp_text[0][:50]}' read by OCR in coloured ink "
                               f"({ink['colored_ink_ratio']:.2%} of block)")
        second = None
        if stamp and not sig and has_ink:
            second = _second_look(model, doc.path, pdf_page, b)
            if second and second["handwriting_present"]:
                sig = True
                sig_basis = "vision second look at the stamp (300 dpi): handwriting drawn through the seal"
                methods.append("vision second look at the stamp (300 dpi): handwriting found")
        uncorroborated = None
        if (sig or stamp) and not has_ink:
            # the model reported a mark but there is no ink outside the printed text: do not accept it
            uncorroborated = {"signature": sig, "stamp": stamp}
            sig = stamp = False
            sig_basis = stamp_basis = None
            methods.append("vision answer NOT accepted: no non-text ink in the block")
        if has_ink:
            methods.append(f"non-text ink {ink['non_text_ink_ratio']:.2%} of block (coloured {ink['colored_ink_ratio']:.2%})"
                           + (f", {imgs} embedded image(s)" if imgs else ""))
        unclear = has_ink and not (sig or stamp)
        blocks.append({"block": b["block"], "party": b["party"], "page": page_no, "pdf_page": pdf_page,
                       "region": [round(x, 3) for x in b["region"]], "located_by": b["located_by"],
                       "signature_present": sig, "stamp_present": stamp, "unclear_mark": unclear,
                       "signature_basis": sig_basis, "stamp_basis": stamp_basis,
                       "uncorroborated_model_answer": uncorroborated,
                       "detection_method": "; ".join(methods) or "none",
                       "visual_description": " ".join(x for x in (vis.get("evidence"), second and second["evidence"]) if x),
                       "vision_error": vis.get("error"), "ink": ink, "embedded_images": imgs,
                       "text_in_region": b["text_in_region"]})
    return {"document": doc.filename, "page": page_no, "model": getattr(model, "name", ""), "blocks": blocks}
