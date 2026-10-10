"""Load the Excel checklist and attach an explicit interpretation to each row.

The workbook is the business source of truth, but its rows are prose. Each
row's machine interpretation is authored below (reviewable by the business),
pinned to the row's original wording. If the wording in the workbook changes,
the pin no longer matches and the item is reported as REVIEW instead
of silently applying a stale interpretation.

We deliberately do not ask the model to re-interpret business rules on
every run: rule meaning should be stable, versioned and auditable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import openpyxl

from .models import BILL_OF_LADING, PURCHASE_CONTRACT, PURCHASE_INVOICE, SALES_CONTRACT, SALES_INVOICE

# Documents named by the checklist that this engine does not classify
# (no examples in the dataset). If required, they are reported missing.
PROFORMA = "PROFORMA_INVOICE"
PACKING_LIST = "PACKING_LIST"
AIFTA = "AIFTA_CERTIFICATE"
CERTIFICATES = "OTHER_REQUIRED_CERTIFICATES"
QUALITY_CHECK = "END_BUYER_QUALITY_CHECK"
FINAL_INVOICE = "FINAL_INVOICE"


@dataclass
class ChecklistItem:
    rule_id: str              # COMMON-1, METALS-3, ...
    section: str              # COMMON CHECKS | METALS | RUBBER
    number: int
    original_text: str        # column B, verbatim
    instruction: str | None   # column C, verbatim (payment-term rows)
    excel_row: int
    interpretation: dict | None = None
    interpretation_status: str = "MISSING"   # CURRENT | STALE | MISSING


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", (s or "").lower()).strip()


# --------------------------------------------------------------------------- interpretations
# Common checks. "pin" is a distinctive phrase of the original wording.

COMMON = {
    1: {"pin": "all contracts must be signed and stamped by both parties",
        "type": "VISION_AI", "summary": "PC and SC each show signature and stamp for both parties; "
        "a China/Hong Kong party may show a seal only."},
    2: {"pin": "the contract of either party may be used",
        "type": "DETERMINISTIC", "summary": "Each invoice must reference its contract. A reference to the "
        "counterparty's own contract is allowed, but that contract must be available to verify."},
    3: {"pin": "any advance payment already made must be deducted",
        "type": "CONDITIONAL", "summary": "Where a contract provides for an advance/deposit, the corresponding "
        "invoice must show it deducted (amount consistent with the contract)."},
    4: {"pin": "any debit note payable by the party must be deducted",
        "type": "EXTERNAL_DATA", "summary": "Needs the debit-note register; not derivable from the case documents."},
    5: {"pin": "any change in the bank account details mentioned in the invoice",
        "type": "DETERMINISTIC", "summary": "Bank details on the purchase invoice must equal those in the purchase "
        "contract; any difference needs Trade Operations verification."},
    6: {"pin": "where advances are due from both the buyer and supplier",
        "type": "CONDITIONAL", "summary": "If both SC and PC require advances, the buyer's advance must be shown "
        "as received before the supplier advance is paid."},
    7: {"pin": "supplier advances must remain within the approved exposure limit",
        "type": "EXTERNAL_DATA", "summary": "Needs exposure limits and the advance ledger when a supplier advance "
        "exists; not derivable from the case documents."},
    8: {"pin": "the gp% must not be below 2%",
        "type": "NORMALIZED", "summary": "GP% = (sales value - purchase value) / sales value, per MT, from the "
        "invoices; must be >= 2%.", "threshold_pct": 2.0},
}

# Metals payment-term rows. Features are what we look for in the purchase
# contract's payment clause; stages list the documents the row says to check.
_SHIP = [PURCHASE_INVOICE, PACKING_LIST, BILL_OF_LADING]
METALS = {
    1: {"pin": "100% tt on scan copy of dbl, after vessel sailing",
        "features": {"advance": 0, "trigger": "DBL", "vessel_sailed": True, "aifta": False, "quality_check": False},
        "stages": {"FULL": [PURCHASE_CONTRACT, SALES_CONTRACT, PURCHASE_INVOICE, BILL_OF_LADING]}},
    2: {"pin": "100% tt on scan copy of dbl",
        "features": {"advance": 0, "trigger": "DBL", "vessel_sailed": False, "aifta": False, "quality_check": False},
        "stages": {"FULL": [PURCHASE_CONTRACT, SALES_CONTRACT, PURCHASE_INVOICE, BILL_OF_LADING]}},
    3: {"pin": "20% deposit and balance against dbl",
        "features": {"advance": 20, "trigger": "DBL", "vessel_sailed": False, "aifta": False, "quality_check": False},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA], "BALANCE": [BILL_OF_LADING]}},
    4: {"pin": "10% advance, 70% against shipping documents and balance after quality check",
        "features": {"advance": 10, "trigger": "DOCS", "vessel_sailed": False, "aifta": False, "quality_check": True},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA],
                   "BALANCE": _SHIP + [CERTIFICATES], "FINAL": [QUALITY_CHECK, FINAL_INVOICE]}},
    5: {"pin": "10% deposit and balance against dbl",
        "features": {"advance": 10, "trigger": "DBL", "vessel_sailed": False, "aifta": False, "quality_check": False},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA], "BALANCE": [BILL_OF_LADING]}},
    6: {"pin": "10% against purchase order & balance against copy documents",
        "features": {"advance": 10, "trigger": "DOCS", "vessel_sailed": False, "aifta": False, "quality_check": False},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA], "BALANCE": _SHIP + [CERTIFICATES]}},
    7: {"pin": "10% advance and balance 90% on scan copy of obl, after vessel sailing",
        "features": {"advance": 10, "trigger": "OBL", "vessel_sailed": True, "aifta": False, "quality_check": False},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA], "BALANCE": _SHIP + [CERTIFICATES]}},
    8: {"pin": "100% tt on scan obl copy",
        "features": {"advance": 0, "trigger": "OBL", "vessel_sailed": False, "aifta": False, "quality_check": False},
        "stages": {"FULL": [PURCHASE_CONTRACT, SALES_CONTRACT, PURCHASE_INVOICE, SALES_INVOICE, BILL_OF_LADING]}},
    9: {"pin": "10% advance and balance on obl copy & aifta copy",
        "features": {"advance": 10, "trigger": "OBL", "vessel_sailed": False, "aifta": True, "quality_check": False},
        "stages": {"ADVANCE": [PURCHASE_CONTRACT, SALES_CONTRACT, PROFORMA], "BALANCE": _SHIP + [AIFTA]}},
}

INTERPRETATIONS = {"COMMON CHECKS": COMMON, "METALS": METALS}


# --------------------------------------------------------------------------- loading

def load_checklist(path: str) -> tuple[list[ChecklistItem], str]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    items: list[ChecklistItem] = []
    section = None
    for row in ws.iter_rows(min_row=1):
        a, b = row[0].value, row[1].value if len(row) > 1 else None
        c = row[2].value if len(row) > 2 else None
        if a is None and isinstance(b, str) and b.strip():
            section = b.strip().upper()           # section header rows have no number
            continue
        if section and isinstance(a, (int, float)) and b:
            num = int(a)
            prefix = "COMMON" if section.startswith("COMMON") else section
            item = ChecklistItem(f"{prefix}-{num}", section, num, str(b).strip(),
                                 str(c).strip() if c else None, row[0].row)
            interp = INTERPRETATIONS.get(section, {}).get(num)
            if interp:
                item.interpretation = interp
                item.interpretation_status = "CURRENT" if _norm(interp["pin"]) in _norm(item.original_text) else "STALE"
            items.append(item)
    return items, ws.title


# --------------------------------------------------------------------------- payment-term features

WORD_PCT = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50}


@dataclass
class PaymentFeatures:
    advance: int | None                      # % advance/deposit; 0 = none; None = present but not a %
    advance_text: str | None
    trigger: str | None                      # DBL | OBL | DOCS
    vessel_sailed: bool
    aifta: bool
    quality_check: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"advance": self.advance, "trigger": self.trigger, "vessel_sailed": self.vessel_sailed,
                "aifta": self.aifta, "quality_check": self.quality_check}


def payment_features(clause: str) -> PaymentFeatures:
    u = re.sub(r"\s+", " ", clause.upper())
    notes = []
    adv_pct, adv_text = 0, None
    m = re.search(r"(ADVANCE|DEPOSIT|UPFRONT)", u)
    if m:
        window = u[max(0, m.start() - 40): m.end() + 80]
        pct = re.search(r"(\d{1,2})\s*%", window)
        word = re.search(r"\b(" + "|".join(WORD_PCT).upper() + r")\s+PERCENT", window)
        amt = re.search(r"(USD|US\$|\$)\s*[\d.,]+", window)
        adv_text = window.strip()
        if pct and int(pct.group(1)) < 100:
            adv_pct = int(pct.group(1))
        elif word:
            adv_pct = WORD_PCT[word.group(1).lower()]
        elif amt:
            adv_pct = None
            notes.append(f"advance/deposit stated as a fixed amount ('{amt.group(0).strip()}'), not a percentage")
        else:
            adv_pct = None
            notes.append("advance/deposit mentioned without a percentage or amount")
    if re.search(r"DRAFT BILLS? OF LADING|DRAFT COPY|\bDBL\b|BL DRAFT|DRAFT B/?L", u):
        trigger = "DBL"
    elif re.search(r"ORIGINAL BILLS? OF LADING|\bOBL\b|ORIGINAL B/?L", u):
        trigger = "OBL"
    elif re.search(r"SHIPPING DOCUMENTS|COPY DOCUMENTS", u):
        trigger = "DOCS"
    else:
        trigger = None
        notes.append("payment trigger document not recognised")
    return PaymentFeatures(adv_pct, adv_text, trigger,
                           bool(re.search(r"VESSEL SAILING|VESSEL HAS SAILED|AFTER SAILING", u)),
                           "AIFTA" in u, "QUALITY CHECK" in u, notes)


def match_payment_term(clause: str, items: list[ChecklistItem]) -> dict:
    """Deterministically match a purchase payment clause to one METALS row."""
    feats = payment_features(clause)
    rows = [i for i in items if i.section == "METALS" and i.interpretation]
    scored = []
    for it in rows:
        want = it.interpretation["features"]
        diffs = [k for k, v in want.items() if feats.as_dict()[k] != v]
        scored.append((len(diffs), it.rule_id, diffs))
    scored.sort()
    best = scored[0] if scored else None
    exact = [s for s in scored if s[0] == 0]
    if len(exact) == 1:
        status, rule_id, diffs = "IDENTIFIED", exact[0][1], []
    elif best:
        status, rule_id, diffs = "AMBIGUOUS", best[1], best[2]
    else:
        status, rule_id, diffs = "UNKNOWN", None, []
    runners = [s[1] for s in scored if best and s[0] == best[0] and s[1] != rule_id]
    return {"status": status, "rule_id": rule_id, "differences": diffs, "features": feats.as_dict(),
            "feature_notes": feats.notes, "advance_text": feats.advance_text, "tied_with": runners}
