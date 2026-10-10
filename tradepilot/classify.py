"""Deterministic document classification from content, corroborated by filename.

Content decides the document family (contract / invoice / bill of lading)
and, using the configured own company, the side (purchase vs sales).
The filename is only a corroborating hint: a disagreement makes the result
AMBIGUOUS, and a filename alone is never enough to reach IDENTIFIED.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .config import CONFIG
from .models import (BILL_OF_LADING, OPTIONAL_DOC_TYPES, PURCHASE_CONTRACT, PURCHASE_INVOICE,
                     SALES_CONTRACT, SALES_INVOICE, UNKNOWN_DOC, Document, Page)

# Supporting-document titles. A title must START a line near the top of the page, so that a
# contract's list "3. Certificate of Origin" or an invoice footer "PAYMENT ADVICE" is not mistaken
# for a document of that type. A packing list, delivery order or shipping instruction is never a BL.
OPTIONAL_TITLES = [
    ("DEBIT_NOTE", r"DEBIT\s*NOTE"), ("CREDIT_NOTE", r"CREDIT\s*NOTE"),
    ("PACKING_LIST", r"(PACKING|WEIGHT)\s*LIST"), ("DELIVERY_ORDER", r"DELIVERY\s*ORDER"),
    ("SHIPPING_INSTRUCTION", r"SHIPPING\s*INSTRUCTIONS?"),
    ("CERTIFICATE_OF_ORIGIN", r"CERTIFICATE\s*OF\s*ORIGIN|AIFTA|FORM\s*AI\b"),
    ("QUALITY_CERTIFICATE", r"(ASSAY|QUALITY|ANALYSIS)\s*(CERTIFICATE|REPORT)|CERTIFICATE\s*OF\s*(ANALYSIS|QUALITY)"),
    ("WEIGHT_CERTIFICATE", r"(WEIGHT|WEIGHING)\s*(CERTIFICATE|NOTE)|CERTIFICATE\s*OF\s*WEIGHT"),
    ("INSURANCE_CERTIFICATE", r"INSURANCE\s*(CERTIFICATE|POLICY)"),
    ("INSPECTION_REPORT", r"INSPECTION\s*(REPORT|CERTIFICATE)"),
    ("PAYMENT_ADVICE", r"(PAYMENT|BANK|REMITTANCE)\s*(ADVICE|CONFIRMATION)|ADVANCE\s*(PAYMENT\s*)?RECEIPT"),
]
assert {t for t, _ in OPTIONAL_TITLES} <= set(OPTIONAL_DOC_TYPES)
TOP_LINES = 12

FILENAME_HINTS = {
    PURCHASE_CONTRACT: [r"\bPC\b", r"PURCHASE[ _-]?CONTRACT"],
    PURCHASE_INVOICE: [r"\bPI\b", r"PURCHASE[ _-]?INVOICE"],
    BILL_OF_LADING: [r"\bBL\b", r"\bB/?L\b", r"BILL[ _-]?OF[ _-]?LADING", r"\bOBL\b", r"\bDBL\b"],
    SALES_CONTRACT: [r"\bSC\b", r"SALES?[ _-]?CONTRACT"],
    SALES_INVOICE: [r"\bSI\b", r"SALES?[ _-]?INVOICE"],
}


def filename_hint(filename: str) -> str | None:
    stem = re.sub(r"\.pdf$", "", filename, flags=re.I).upper().replace("_", " ")
    hits = [t for t, pats in FILENAME_HINTS.items() if any(re.search(p, stem) for p in pats)]
    return hits[0] if len(hits) == 1 else None


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s.upper())


def optional_title(page_text: str) -> str | None:
    for line in page_text.splitlines()[:8]:
        u = line.strip().upper()
        if re.search(r"DOCUMENTS?\s+(TO\s+BE\s+PRESENTED|REQUIRED)", u):
            return None      # what follows is a contract's list of documents, not a document title
        if not u or u[0].isdigit() or len(u) > 60:
            continue
        for doc_type, rx in OPTIONAL_TITLES:
            if re.match(rf"^\W{{0,3}}({rx})\b", u):
                return doc_type
    return None


def _family(first_page: str, all_text: str) -> tuple[str | None, str]:
    # whitespace-insensitive: native text sometimes loses spaces ("COMMERCIALINVOICE")
    head = _squash(first_page)
    scores = {
        "BL": sum(_squash(k) in head for k in ("BILL OF LADING", "SHIPPER", "CONSIGNEE", "NOTIFY PARTY",
                                      "PORT OF LOADING", "PORT OF DISCHARGE", "VESSEL")),
        "CONTRACT": sum(_squash(k) in head for k in ("THIS AGREEMENT IS MADE", "HEREINAFTER CALLED",
                                            "TERMS OF CONTRACT", "CONTRACT NO")),
        "INVOICE": sum(_squash(k) in head for k in ("COMMERCIAL INVOICE", "TAX INVOICE", "FINAL INVOICE",
                                           "PROFORMA INVOICE", "INVOICE NO", "INVOICE NUMBER",
                                           "INVOICE DATE", "TOTAL USD", "AMOUNT (USD)")),
    }
    scores = {k: v for k, v in scores.items()}
    if "BILLOFLADING" not in head:
        scores["BL"] = 0
    if "HEREINAFTERCALLED" not in _squash(all_text):
        scores["CONTRACT"] = 0
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    if ranked[0] < 2:
        return None, f"no strong content signals ({scores})"
    if ranked[0] == ranked[1]:
        return None, f"content signals tie ({scores})"
    return best, f"content signals {scores}"


def _contract_side(text: str, own: str) -> str | None:
    """Find the role the own company is given: "<own> ... hereinafter called 'Buyer'"."""
    t = re.sub(r"\s+", " ", text.upper())
    for m in re.finditer(re.escape(own.upper()), t):
        tail = t[m.end(): m.end() + 400]
        r = re.search(r"HEREINAFTER CALLED\s*\W*\s*(BUYER|SELLER)", tail)
        if r:
            return r.group(1)
    return None


def _ocr_similar(a: str, b: str) -> bool:
    """Tolerate a single OCR character slip ("VRY GLOBAL" for "VRV GLOBAL")."""
    return len(a) >= 6 and SequenceMatcher(None, a, b).ratio() >= 0.88


def _invoice_side(text: str, own: str) -> str | None:
    """SELLER if the own company issues the invoice (is the payment beneficiary
    or heads the letterhead), BUYER if it only appears as the customer."""
    u = text.upper()
    own_u = own.upper()
    if own_u not in u:
        return None
    for line in u.splitlines():
        if re.search(r"BENEF\w*", line):           # OCR-tolerant: "Beneficlary"
            rest = line.split(":", 1)[-1].strip()
            if own_u in rest or _ocr_similar(own_u, rest[:len(own_u)]):
                return "SELLER"
            if len(rest.strip()) > 3:
                return "BUYER"
    letterhead = " ".join(u.splitlines()[:2])
    return "SELLER" if own_u in letterhead else "BUYER"


def classify(doc: Document, use_filename: bool = True) -> Document:
    own = CONFIG.own_company
    all_text = "\n".join(p.text for p in doc.pages)
    opt = optional_title(doc.pages[0].text) if doc.pages else None
    if opt:
        doc.doc_type, doc.classification_status = opt, "IDENTIFIED"
        doc.classification_reason = f"supporting-document title '{opt.replace('_', ' ').title()}' at the top of page 1"
        return doc
    family, why = _family(doc.pages[0].text if doc.pages else "", all_text)
    content_type = None
    if family == "BL":
        content_type = BILL_OF_LADING
    elif family == "CONTRACT":
        side = _contract_side(all_text, own)
        content_type = {"BUYER": PURCHASE_CONTRACT, "SELLER": SALES_CONTRACT}.get(side)
        why += f"; {own} is {side or 'not identified as buyer/seller'}"
    elif family == "INVOICE":
        side = _invoice_side(all_text, own)
        content_type = {"BUYER": PURCHASE_INVOICE, "SELLER": SALES_INVOICE}.get(side)
        why += f"; {own} is {'issuer/beneficiary' if side == 'SELLER' else 'customer' if side else 'absent'}"

    hint = filename_hint(doc.filename) if use_filename else None
    if content_type and (hint is None or hint == content_type):
        doc.doc_type, doc.classification_status = content_type, "IDENTIFIED"
        why += f"; filename hint {'agrees' if hint else 'absent'}"
    elif content_type and hint != content_type:
        doc.doc_type, doc.classification_status = content_type, "AMBIGUOUS"
        why += f"; filename suggests {hint} but content suggests {content_type}"
    elif hint:
        doc.doc_type, doc.classification_status = hint, "AMBIGUOUS"
        why += f"; only the filename suggests {hint}"
    else:
        doc.doc_type, doc.classification_status = UNKNOWN_DOC, "UNKNOWN"
    doc.classification_reason = why
    return doc


def resolve_duplicates(docs: list[Document]) -> None:
    """Two documents claiming the same type cannot both be trusted."""
    by_type: dict[str, list[Document]] = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(d)
    for t, ds in by_type.items():
        if t != UNKNOWN_DOC and len(ds) > 1:
            for d in ds:
                d.classification_status = "AMBIGUOUS"
                d.classification_reason += f"; {len(ds)} documents in this case classify as {t}"


# --------------------------------------------------------------------------- combined PDFs

def _page_start(text: str) -> str | None:
    """Family of a document that STARTS on this page, judged from its top lines; None for a
    continuation page."""
    opt = optional_title(text)
    if opt:
        return opt
    top = _squash("\n".join(text.splitlines()[:TOP_LINES]))
    page = _squash(text)
    if "BILLOFLADING" in top and sum(k in page for k in ("SHIPPER", "CONSIGNEE", "NOTIFYPARTY", "PORTOFLOADING")) >= 2:
        return "BL"
    if re.search(r"(COMMERCIAL|TAX|FINAL|PROFORMA)INVOICE|^INVOICE", top) and \
            sum(k in page for k in ("INVOICENO", "INVOICENUMBER", "INVOICEDATE", "TOTAL", "AMOUNT")) >= 2:
        return "INVOICE"
    if "THISAGREEMENTISMADE" in page or ("CONTRACTNO" in top and "HEREINAFTERCALLED" in page):
        return "CONTRACT"
    return None


def _page_side(text: str, family: str, own: str) -> str | None:
    if family == "CONTRACT":
        return _contract_side(text, own)
    if family == "INVOICE":
        return _invoice_side(text, own)
    return None


def split_combined(path: str, filename: str, pages: list[Page]) -> list[Document]:
    """Split a PDF holding several documents at pages where a new document starts: a different
    document family, or an invoice/contract for the other side of the trade. Consecutive bill of
    lading pages (attached sheets) stay together. A single-document PDF is returned unchanged."""
    own = CONFIG.own_company
    starts = []
    current = None
    for i, p in enumerate(pages):
        fam = _page_start(p.text)
        if fam is None:
            continue
        side = _page_side(p.text, fam, own)
        if current is None or fam != current[0] or (fam in ("INVOICE", "CONTRACT") and side and current[1] and side != current[1]):
            starts.append(i)
            current = (fam, side)
    if len(starts) <= 1:
        return [Document(path, filename, pages)]
    if starts[0] != 0:
        starts = [0] + starts
    docs = []
    for a, b in zip(starts, starts[1:] + [len(pages)]):
        seg = pages[a:b]
        renumbered = [Page(n + 1, pg.text, pg.method, pg.alt_texts, pg.ocr_corrections, pg.notes) for n, pg in enumerate(seg)]
        first, last = seg[0].number, seg[-1].number
        name = f"{filename} [page {first}]" if first == last else f"{filename} [pages {first}-{last}]"
        docs.append(Document(path, name, renumbered, source_pages=[pg.number for pg in seg]))
    return docs
