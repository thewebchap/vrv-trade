"""Field extraction: the model reads, code verifies.

1. The local model is asked for raw values *exactly as printed*, per a small
   schema per document family (derived from what the checklist needs).
2. Every returned value is grounded: we look for it in the page text. The
   matching line becomes the evidence (page + source_text). A value that
   cannot be found in the document is kept but marked AMBIGUOUS.
3. Values are normalised (numbers, dates, units) by normalize.py.

A few facts are taken deterministically instead (payment clause section,
BL original/draft marking, signature page location).
"""
from __future__ import annotations

import json
import logging
import re

from . import normalize as N
from .local_model import LocalModel
from .models import (AMBIGUOUS, BILL_OF_LADING, FOUND, NOT_FOUND, PURCHASE_CONTRACT,
                     PURCHASE_INVOICE, SALES_CONTRACT, SALES_INVOICE, Document, Field)
from .pdf_text import render_page_png

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

COMMON_RULES = """Rules:
- Copy every value EXACTLY as printed in the document text: same digits, separators (dots/commas), spelling and units. Never convert, compute, round or reformat.
- Use null when a value is not present. Do not guess.
- Output a single JSON object with exactly the keys requested."""

CONTRACT_SCHEMA = """{
 "contract_number": str, "contract_date": str,
 "seller_name": str, "buyer_name": str,
 "materials": [ {"description": str, "hs_code": str, "quantity": str, "quantity_unit": str,
                 "tolerance": str, "unit_price": str, "price_unit": str,
                 "price_type": "FIXED" | "FORMULA"} ],
 "total_quantity": str, "total_quantity_unit": str, "total_tolerance": str,
 "quantity_per_shipment": str (e.g. quantity per BL / lot size, if stated),
 "partial_shipment": str, "shipment_period": str,
 "delivery_terms": str, "port_of_loading": str, "port_of_destination": str,
 "currency": str, "price_tolerance": str (any stated tolerance on PRICE, e.g. "+/- 1%"; null if none),
 "bank_beneficiary": str, "bank_name": str, "bank_account": str, "bank_swift": str
}
Notes: one entry in "materials" per contracted material/grade. "quantity" is the number only (e.g. "75"), "tolerance" the +/- percentage text (e.g. "+/- 10%"). For formula pricing (LME etc.) set price_type "FORMULA" and unit_price null. Bank fields are the SELLER's bank details as printed in the contract."""

INVOICE_SCHEMA = """{
 "document_title": str, "invoice_number": str, "invoice_date": str,
 "seller_name": str, "buyer_name": str,
 "contract_reference": str, "bl_number": str,
 "line_items": [ {"description": str, "hs_code": str, "quantity": str, "quantity_unit": str,
                  "unit_price": str, "amount": str} ],
 "net_weight": str, "net_weight_unit": str, "gross_weight": str, "gross_weight_unit": str,
 "gross_amount": str (total value of goods before any advance/deposit deduction),
 "advance_deducted": str (advance or deposit deducted, if any),
 "net_amount_payable": str (amount payable after deductions),
 "amount_in_words": str,
 "currency": str, "delivery_terms": str,
 "vessel": str, "voyage": str, "port_of_loading": str, "port_of_discharge": str,
 "shipped_on_board_date": str, "payment_terms": str,
 "bank_beneficiary": str, "bank_name": str, "bank_account": str, "bank_swift": str
}
Notes: "quantity_unit" is the unit of the quantity column (e.g. KG, MT, PCS) as shown in the column header or next to the number. If the invoice shows only a total weight and total amount, return one line item with those. net_weight / gross_weight are totals printed with an explicit NET or GROSS label; use null when the document has no such label (do not copy a plain "Quantity" into them)."""

BL_SCHEMA = """{
 "bl_number": str, "shipper_name": str, "consignee": str, "notify_party_name": str,
 "vessel": str, "voyage": str, "port_of_loading": str, "port_of_discharge": str,
 "place_of_delivery": str, "shipped_on_board_date": str, "issue_date": str,
 "goods_description": str, "hs_code": str,
 "net_weight": str, "net_weight_unit": str, "gross_weight": str, "gross_weight_unit": str,
 "packages": str
}
Notes: names are company names only (first line), not addresses. notify_party_name is the first (main) notify party. voyage is the voyage number, never part of the vessel name. net_weight is the TOTAL NET weight for the whole BL and only if the BL labels it as net (NET WEIGHT, N.W., NETT); gross_weight is the TOTAL GROSS weight. Never put a gross weight, a volume (CBM) or a package count into a weight field; use null instead. "packages" is a count (bales, bundles, pieces...)."""

NOTE_SCHEMA = """{
 "note_number": str, "note_date": str, "issuer_name": str, "recipient_name": str,
 "invoice_reference": str, "contract_reference": str, "amount": str, "currency": str, "reason": str
}
Notes: issuer_name is the company that issued the note (letterhead); recipient_name the company it is addressed to.
"amount" is the total amount of the note."""

SCHEMAS = {"DEBIT_NOTE": NOTE_SCHEMA, "CREDIT_NOTE": NOTE_SCHEMA, PURCHASE_CONTRACT: CONTRACT_SCHEMA, SALES_CONTRACT: CONTRACT_SCHEMA,
           PURCHASE_INVOICE: INVOICE_SCHEMA, SALES_INVOICE: INVOICE_SCHEMA, BILL_OF_LADING: BL_SCHEMA}

NUMBER_FIELDS = {"quantity", "unit_price", "amount", "gross_amount", "advance_deducted",
                 "net_amount_payable", "total_quantity", "net_weight", "gross_weight"}
DATE_FIELDS = {"contract_date", "invoice_date", "shipped_on_board_date", "issue_date"}
PERCENT_FIELDS = {"tolerance", "total_tolerance", "price_tolerance"}
# Fields whose values are numbers: OCR letter/digit confusions in them are corrected.
NUMERIC_CONTEXT = NUMBER_FIELDS | DATE_FIELDS | PERCENT_FIELDS | {"quantity_per_shipment"}


# --------------------------------------------------------------------------- text selection

def _contract_text(doc: Document) -> str:
    """Sections A-D (terms, price, shipment, payment) plus bank details; skips boilerplate."""
    text = "\n".join(p.text for p in doc.pages)
    end = re.search(r"\n\s*E\.\s*TITLE|\n\s*E\.\s*[A-Z ]{4,}\n", text)
    head = text[: end.start()] if end else text[:7000]
    return head[:9000]


def _doc_text(doc: Document) -> str:
    if doc.doc_type in (PURCHASE_CONTRACT, SALES_CONTRACT):
        return _contract_text(doc)
    if doc.doc_type == BILL_OF_LADING:
        # BL page 1 carries the particulars; from later pages keep only lines that
        # can carry the shipped-on-board stamp (attached sheets are otherwise noise)
        extra = [l for p in doc.pages[1:] for l in p.text.splitlines()
                 if re.search(r"(?i)board|shipped|total\s+(net|gross)|\bnet\s+w|n\.\s*w\.?\s*[:/]", l)]
        return doc.pages[0].text[:7000] + ("\n[LINES FROM LATER PAGES]\n" + "\n".join(extra[:10]) if extra else "")
    return "\n".join(p.text for p in doc.pages)[:7000]


# --------------------------------------------------------------------------- grounding

def _squash(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# A value for these fields is only trusted when a matching label sits on the
# same line or the line above (form/table headers). Grounding proves the text
# exists; this proves it is the right *kind* of text.
LABEL_HINTS = {   # field: (label regex, forbidden-on-value-line regex, header lines above to search)
    "port_of_loading": (r"LOAD|POL|FROM", None, 1),
    "port_of_discharge": (r"DISCHARGE|POD|DESTINATION|DELIVERY|\bTO\b", None, 1),
    "vessel": (r"VESSEL|VSL|OCEAN|VOY", None, 1),
    "voyage": (r"VOY|VESSEL", None, 1),
    "shipped_on_board_date": (r"BOARD|SHIPPED|SOB|LADEN", None, 2),   # forms often put the label 2 lines up
    "bl_number": (r"B[L1I]\b|B\s*/\s*L|B[L1I]\.|LADING|BILL", None, 1),
    "contract_reference": (r"CONTRACT|SCT|\bPO\b|\bSC\b|ORDER", None, 1),
    # must be on the value's own line: "(100 MT each BL)", "Lot Size 50MT per BL"
    "quantity_per_shipment": (r"(EACH|PER)\s+(BL|B/L|BILL|SHIPMENT|LOT)|LOT\s*SIZE|\bBL\b|B/L", r"CONTAINER", 0),
    # Net and gross weight are different measures: a net value must carry an
    # explicit NET label; a gross value may sit under a "Gross Weight" column header.
    "net_weight": (r"\bNET\b|\bNETT\b|N\.\s*W|N/W|NET\s*W", r"GROSS|G\.\s*W|G/W|CBM|M3", 1),   # value-line guard
    # column headers sit above container rows; a NET label in between still blocks it
    "gross_weight": (r"GROSS|G\.\s*W|G/W", r"\bNET\b|N\.\s*W|CBM|M3", 8),
}


def _number_tokens(line: str, locale: str | None) -> set[float]:
    out = set()
    for tok in re.findall(r"\d(?:\d|[.,]\s?(?=\d))*", line):
        p = N.parse_number(tok, locale)
        out |= {round(v, 6) for v in (p.value, p.alt) if v is not None}
    return out


def ground(raw: str, doc: Document, name: str | None = None) -> tuple[int | None, str | None, list[str]]:
    """Find the page and line where `raw` appears. Tolerates whitespace and
    punctuation differences, and values split across lines. Numbers must match
    as numbers (26.0615 is not 26061.5). When the value occurs several times,
    the occurrence with the expected label wins.
    Returns (page, source line, context) where context = [lines above..., line]."""
    target = _squash(raw)
    if not target:
        return None, None, []
    probe = target[:30]
    numeric = name is not None and name.split(".")[-1] in NUMBER_FIELDS
    want = set()
    if numeric:
        p = N.parse_number(raw, doc.number_locale)
        want = {round(v, 6) for v in (p.value, p.alt) if v is not None}
    hits = []
    for page in doc.pages:
        lines = page.text.splitlines()
        for i, line in enumerate(lines):
            if probe in _squash(line) and (not want or want & _number_tokens(line, doc.number_locale)):
                hits.append((page.number, N.norm_space(line), lines[max(0, i - 8): i + 1]))
        if not hits:
            for i in range(len(lines) - 1):              # value wraps onto the next line
                joined = lines[i] + " " + lines[i + 1]
                if probe in _squash(joined) and (not want or want & _number_tokens(joined, doc.number_locale)):
                    hits.append((page.number, N.norm_space(joined), lines[max(0, i - 8): i] + [joined]))
    if not hits:
        return None, None, []
    if name:
        for h in hits:
            if label_confirmed(name, h[2]):
                return h
    return hits[0]


def label_confirmed(name: str, context: list[str]) -> bool:
    """The expected label is on the value's own line, or (for column/form
    headers) within `window` lines above with no conflicting label in between."""
    hint = LABEL_HINTS.get(name)
    if not hint:
        return True
    ok, forbid, window = hint
    value_line = (context[-1] if context else "").upper()
    above = " ".join(context[-1 - window:-1]).upper() if len(context) > 1 else ""
    if re.search(ok, value_line):
        return True
    # The lines above only have to be free of a conflicting LABEL (net vs gross); volume units there belong
    # to other rows (container lines) and are only disqualifying on the value's own line.
    above_forbid = re.sub(r"\|?(CBM|M3)", "", forbid or "").strip("|") or None
    return bool(re.search(ok, above)) and not (forbid and re.search(forbid, value_line)) and \
        not (above_forbid and re.search(above_forbid, above))


def page_corrections_for(page, line: str | None, value: str) -> list[dict]:
    """OCR-reconciliation corrections made on the line this value was grounded to."""
    if not line or not page.ocr_corrections:
        return []
    lines = page.text.splitlines()
    numbers = {n for n, l in enumerate(lines, 1) if N.norm_space(l) == line or line.startswith(N.norm_space(l))}
    squashed = _squash(value)
    return [c for c in page.ocr_corrections
            if c.get("line") in numbers and _squash(c.get("corrected") or c["to"]) in squashed]


def _field(name: str, raw, doc: Document, method: str, locale: str | None) -> Field:
    f = Field(canonical_field=name, raw_value=None, document_type=doc.doc_type,
              document=doc.filename, extraction_method=method)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return f
    raw = str(raw).strip()
    f.raw_value = raw
    base = name.split(".")[-1]
    value, corrections = raw, []
    if base in NUMERIC_CONTEXT:
        value, corrections = N.fix_numeric_ocr(raw, field_is_numeric=True)
    page, line, context = ground(value, doc, name)
    if not page and value != raw:
        page, line, context = ground(raw, doc, name)
    f.page, f.source_text = page, line
    if page:
        corrections += page_corrections_for(doc.pages[page - 1], line, value)
    if value != raw:
        f.ocr_corrected = value
    if corrections:
        f.ocr_corrections = corrections
    f.status = FOUND if page else AMBIGUOUS
    if not page:
        f.note = "value returned by the model was not found in the document text"
        return f
    if name in LABEL_HINTS:
        f.label_confirmed = label_confirmed(name, context)
    if f.label_confirmed is False:
        f.status = AMBIGUOUS
        f.note = f"value found, but no '{name}' label next to it; meaning not confirmed"
        return f
    raw = value      # parse the corrected value; raw_value keeps what was printed/read
    if base in NUMBER_FIELDS:
        p = N.parse_number(raw, locale)
        f.normalized_value = p.value
        if p.value is None:
            f.status, f.note = AMBIGUOUS, f"not a number: '{raw}'"
        elif p.ambiguous:
            f.status = AMBIGUOUS
            f.note = p.note + (f"; alternative reading {p.alt}" if p.alt is not None else "")
        elif p.note:
            f.note = p.note
    elif base in DATE_FIELDS:
        d = N.parse_date(raw)
        f.normalized_value = d.value.isoformat() if d.value else None
        if d.value is None or d.ambiguous:
            f.status, f.note = AMBIGUOUS, d.note or f"unparseable date '{raw}'"
        elif d.note:
            f.note = d.note
    else:
        f.normalized_value = N.norm_space(raw)
    return f


# --------------------------------------------------------------------------- deterministic extras

def payment_clause(doc: Document) -> tuple[str | None, int | None]:
    """The contract's payment section, cut deterministically from the text."""
    for page in doc.pages:
        m = re.search(r"(?im)^\W*D\.\s*PAYMENT.*$", page.text)
        if not m:
            continue
        rest = page.text[m.end():]
        if page.number < len(doc.pages):
            rest += "\n" + doc.pages[page.number].text
        stop = re.search(r"(?i)documents to be presented|\n\s*E\.\s*TITLE|bank details", rest)
        clause = N.norm_space(rest[: stop.start()] if stop else rest[:1500])
        return clause, page.number
    return None, None


def pricing_clause(doc: Document) -> tuple[str | None, int | None]:
    """The contract's price / pricing-formula wording (section B), cut deterministically."""
    for page in doc.pages:
        m = re.search(r"(?im)^\W*(pricing\s+formula|price)\b\s*:?", page.text)
        if not m:
            continue
        rest = page.text[m.start():]
        if page.number < len(doc.pages):
            rest += "\n" + doc.pages[page.number].text
        stop = re.search(r"(?i)quotational\s+period|\bQP\b\s*\(|unknow\s+official|\n\s*C\.\s*SHIPMENT|"
                         r"no\s+communication|no\s+material\s+shall", rest)
        clause = N.norm_space(rest[: stop.start()] if stop else rest[:600])
        return clause, page.number
    return None, None


def bl_marking(doc: Document) -> str | None:
    head = doc.pages[0].text.upper() if doc.pages else ""
    for mark in ("DRAFT", "ORIGINAL", "COPY"):
        if re.search(rf"\b{mark}\b", head):
            return mark
    return None


def signature_page(doc: Document) -> int:
    for page in reversed(doc.pages):
        if re.search(r"(?i)authori[sz]ed\s+signatory|for the seller|for the buyer", page.text):
            return page.number
    return len(doc.pages)


SIGNATURE_PROMPT = """This image is the signature page of a contract between a SELLER and a BUYER.
For each party's signature block report what is visibly present.
Return JSON: {"seller": {"signature": true|false, "stamp": true|false}, "buyer": {"signature": true|false, "stamp": true|false}, "observations": str}
"signature" = a handwritten signature or initials. "stamp" = a company stamp, chop or seal impression. Printed text such as "AUTHORIZED SIGNATORY & STAMP" is NOT a signature or stamp. If unsure, use false and say so in observations."""


def detect_signatures(doc: Document, model: LocalModel) -> dict:
    """Whole-page answer plus a per-block visual check (signatures.detect_page)."""
    from .signatures import detect_page
    out = _whole_page_signatures(doc, model)
    if doc.pages:
        try:
            out["blocks"] = detect_page(doc, out["page"], model, out if not out.get("error") else None)["blocks"]
        except Exception as e:      # rendering/OCR failure: keep the whole-page answer, record why
            out["blocks_error"] = f"{type(e).__name__}: {e}"
    return out


def _whole_page_signatures(doc: Document, model: LocalModel) -> dict:
    page = signature_page(doc)
    pdf_page = doc.source_pages[page - 1] if doc.source_pages else page
    resp = model.ask_json(SIGNATURE_PROMPT, images=[render_page_png(doc.path, pdf_page, dpi=150)], max_tokens=300)
    out = {"page": page, "document": doc.filename, "model": resp.model, "error": resp.error}
    if resp.data:
        ok = all(isinstance(resp.data.get(p), dict) and
                 all(isinstance(resp.data[p].get(k), bool) for k in ("signature", "stamp"))
                 for p in ("seller", "buyer"))
        if ok:
            out.update(seller=resp.data["seller"], buyer=resp.data["buyer"],
                       observations=str(resp.data.get("observations", ""))[:400])
        else:
            out["error"] = f"model response does not match the expected schema: {json.dumps(resp.data)[:200]}"
    return out


# --------------------------------------------------------------------------- main entry

def extract(doc: Document, model: LocalModel) -> Document:
    schema = SCHEMAS.get(doc.doc_type)
    if schema is None:
        doc.extraction_error = "document type not identified; no extraction schema"
        return doc
    doc.number_locale = N.detect_number_locale("\n".join(p.text for p in doc.pages))
    method = "llm_on_ocr_text" if any(p.method == "ocr" for p in doc.pages) else "llm_on_pdf_text"
    prompt = (f"[{PROMPT_VERSION}] You extract data from a {doc.doc_type.replace('_', ' ').lower()} "
              f"used in metals trading.\n{COMMON_RULES}\nJSON schema:\n{schema}\n\nDOCUMENT TEXT:\n{_doc_text(doc)}")
    resp = model.ask_json(prompt, max_tokens=1800)
    if resp.data is None:
        doc.extraction_error = resp.error
        return doc

    data, loc = resp.data, doc.number_locale
    item_key = "materials" if "materials" in data else "line_items"
    for key, raw in data.items():
        if key == item_key:
            continue
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw)
        doc.fields[key] = _field(key, raw, doc, method, loc)
    for item in data.get(item_key) or []:
        if isinstance(item, dict):
            doc.line_items.append({k: _field(f"{item_key}.{k}", v, doc, method, loc) for k, v in item.items()})

    if doc.doc_type in (PURCHASE_CONTRACT, SALES_CONTRACT):
        pclause, ppage = pricing_clause(doc)
        doc.fields["pricing_clause"] = Field("pricing_clause", pclause, N.norm_space(pclause or ""), None,
                                             doc.doc_type, doc.filename, ppage, (pclause or "")[:300],
                                             "deterministic_section", FOUND if pclause else NOT_FOUND)
        clause, page = payment_clause(doc)
        doc.fields["payment_clause"] = Field("payment_clause", clause, N.norm_space(clause or ""), None,
                                             doc.doc_type, doc.filename, page, (clause or "")[:300],
                                             "deterministic_section", FOUND if clause else NOT_FOUND)
    if doc.doc_type == BILL_OF_LADING:
        mark = bl_marking(doc)
        doc.fields["bl_marking"] = Field("bl_marking", mark, mark, None, doc.doc_type, doc.filename,
                                         1 if mark else None, mark, "deterministic_keyword",
                                         FOUND if mark else NOT_FOUND)
    return doc
