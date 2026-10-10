"""Core data structures shared across the pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Result statuses, in increasing severity.
PASS = "PASS"                        # check completed and matched
PASS_NOTE = "PASS_WITH_NOTE"         # accepted, with a stated non-blocking limitation
REVIEW = "REVIEW"                    # a required condition cannot be determined reliably
MISMATCH = "MISMATCH"                # a definite, material discrepancy is established
FAIL = "FAIL"                        # a mandatory document is missing (critical)
SEVERITY = {PASS: 0, PASS_NOTE: 1, REVIEW: 2, MISMATCH: 3, FAIL: 4}

# Commercial legs. Purchase and sales are separate commercial transactions: their commercial terms
# are compared only within the leg. CROSS_TRADE is for physical-trade facts (goods, quantity,
# shipment) and for checklist rules that explicitly span the whole trade.
PURCHASE_LEG, SALES_LEG, CROSS_TRADE, CASE_SCOPE = "PURCHASE", "SALES", "CROSS_TRADE", "CASE"

# Field statuses
FOUND, NOT_FOUND, AMBIGUOUS, CONFLICT = "FOUND", "NOT_FOUND", "AMBIGUOUS", "CONFLICT"

# Document types (derived from the dataset)
PURCHASE_CONTRACT = "PURCHASE_CONTRACT"
PURCHASE_INVOICE = "PURCHASE_INVOICE"
BILL_OF_LADING = "BILL_OF_LADING"
SALES_CONTRACT = "SALES_CONTRACT"
SALES_INVOICE = "SALES_INVOICE"
UNKNOWN_DOC = "UNKNOWN"
DOC_TYPES = [PURCHASE_CONTRACT, PURCHASE_INVOICE, BILL_OF_LADING, SALES_CONTRACT, SALES_INVOICE]
MANDATORY_DOCS = DOC_TYPES      # all five are mandatory for every transaction

# Supporting documents: optional unless a contract or checklist row requires them.
DEBIT_NOTE, CREDIT_NOTE = "DEBIT_NOTE", "CREDIT_NOTE"
OPTIONAL_DOC_TYPES = [DEBIT_NOTE, CREDIT_NOTE, "PACKING_LIST", "DELIVERY_ORDER", "SHIPPING_INSTRUCTION",
                      "CERTIFICATE_OF_ORIGIN", "QUALITY_CERTIFICATE", "WEIGHT_CERTIFICATE", "INSURANCE_CERTIFICATE",
                      "INSPECTION_REPORT", "PAYMENT_ADVICE"]
SHORT = {PURCHASE_CONTRACT: "PC", PURCHASE_INVOICE: "PI", BILL_OF_LADING: "BL",
         SALES_CONTRACT: "SC", SALES_INVOICE: "SI"}


@dataclass
class Page:
    number: int          # 1-based
    text: str            # native text, or the reconciled OCR text the model reads
    method: str          # "pdf_text" | "ocr"
    alt_texts: list[str] = field(default_factory=list)       # other OCR passes, kept for reconciliation
    ocr_corrections: list[dict] = field(default_factory=list)  # every change made to the primary OCR text
    notes: list[str] = field(default_factory=list)            # preprocessing / reliability notes


@dataclass
class Document:
    path: str
    filename: str
    pages: list[Page]
    doc_type: str = UNKNOWN_DOC
    classification_status: str = "UNKNOWN"   # IDENTIFIED | AMBIGUOUS | UNKNOWN
    classification_reason: str = ""
    number_locale: str | None = None          # "EU" | "US" | None (unknown)
    fields: dict[str, "Field"] = field(default_factory=dict)
    line_items: list[dict[str, "Field"]] = field(default_factory=list)
    extraction_error: str | None = None
    source_pages: list[int] | None = None     # original PDF page numbers when split from a combined PDF

    @property
    def short(self) -> str:
        return SHORT.get(self.doc_type, "?")

    def get(self, name: str) -> "Field | None":
        f = self.fields.get(name)
        return f if f and f.status == FOUND else None


@dataclass
class Field:
    canonical_field: str
    raw_value: str | None
    normalized_value: Any = None
    unit: str | None = None
    document_type: str = ""
    document: str = ""
    page: int | None = None
    source_text: str | None = None
    extraction_method: str = ""
    status: str = NOT_FOUND
    note: str | None = None
    label_confirmed: bool | None = None   # None = field has no label requirement
    ocr_corrected: str | None = None      # value after OCR correction, when it differs from raw_value
    ocr_corrections: list[dict] | None = None

    @property
    def value(self) -> str | None:
        """The value to interpret: OCR-corrected when a correction was made, else as read."""
        return self.ocr_corrected or self.raw_value

    def evidence(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class CheckResult:
    check_id: str
    title: str
    checklist_ref: str                     # e.g. "COMMON-8" or "METALS-1"
    rule_type: str                         # DETERMINISTIC | NORMALIZED | TOLERANCE | CONDITIONAL | ARITHMETIC | SEMANTIC_AI | VISION_AI | EXTERNAL_DATA
    result: str
    reason: str
    evidence: list[dict] = field(default_factory=list)
    comparisons: list[dict] = field(default_factory=list)
    scope: str = ""          # PURCHASE | SALES | PURCHASE+SALES (each leg separately) | CROSS_TRADE | CASE

    def to_dict(self) -> dict:
        return asdict(self)
