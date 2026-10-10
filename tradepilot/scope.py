"""Which commercial leg a field belongs to, and which documents may be compared for it.

PURCHASE leg: PURCHASE_CONTRACT <-> PURCHASE_INVOICE
SALES leg:    SALES_CONTRACT <-> SALES_INVOICE
CROSS_TRADE:  physical-trade facts shared by the whole trade (goods, quantity, shipment, BL),
              or a checklist rule that explicitly spans both legs (e.g. GP% margin).

Commercial terms are independent by leg: a purchase CFR / sales CIF trade, different prices,
advances, payment terms or currencies on the two legs are normal and never compared.
"""
from __future__ import annotations

from .models import (CROSS_TRADE, PURCHASE_CONTRACT, PURCHASE_INVOICE, PURCHASE_LEG,
                     SALES_CONTRACT, SALES_INVOICE, SALES_LEG)

LEG_OF_DOC = {PURCHASE_CONTRACT: PURCHASE_LEG, PURCHASE_INVOICE: PURCHASE_LEG,
              SALES_CONTRACT: SALES_LEG, SALES_INVOICE: SALES_LEG}
LEG_DOCS = {PURCHASE_LEG: (PURCHASE_CONTRACT, PURCHASE_INVOICE), SALES_LEG: (SALES_CONTRACT, SALES_INVOICE)}
LEG_NAMES = {PURCHASE_LEG: "Purchase", SALES_LEG: "Sales"}

# Field -> scope kind. "LEG": compared only within one leg. "CROSS_TRADE": may be compared across the trade.
FIELD_SCOPE = {
    # commercial, side-specific
    "unit_price": "LEG", "price": "LEG", "amount": "LEG", "gross_amount": "LEG", "net_amount_payable": "LEG",
    "delivery_terms": "LEG", "incoterm": "LEG", "payment_terms": "LEG", "payment_clause": "LEG",
    "advance_deducted": "LEG", "discount": "LEG", "commission": "LEG", "pricing_formula": "LEG",
    "currency": "LEG", "bank_beneficiary": "LEG", "bank_name": "LEG", "bank_account": "LEG", "bank_swift": "LEG",
    "contract_reference": "LEG", "seller_name": "LEG", "buyer_name": "LEG",
    # physical trade
    "description": "CROSS_TRADE", "goods_description": "CROSS_TRADE", "hs_code": "CROSS_TRADE",
    "quantity": "CROSS_TRADE", "net_weight": "CROSS_TRADE", "gross_weight": "CROSS_TRADE",
    "vessel": "CROSS_TRADE", "voyage": "CROSS_TRADE", "port_of_loading": "CROSS_TRADE",
    "port_of_discharge": "CROSS_TRADE", "bl_number": "CROSS_TRADE", "shipped_on_board_date": "CROSS_TRADE",
}


class CrossLegComparison(ValueError):
    """Raised when code tries to compare a leg-specific commercial field across legs."""


def leg_of(doc_type: str | None) -> str | None:
    return LEG_OF_DOC.get(doc_type or "")


def scope_of(doc_type_a: str | None, doc_type_b: str | None) -> str:
    la, lb = leg_of(doc_type_a), leg_of(doc_type_b)
    if la and la == lb:
        return la
    if la and doc_type_b in (None, "", "configuration"):
        return la
    if lb and doc_type_a in (None, "", "configuration"):
        return lb
    return CROSS_TRADE


def ensure_comparable(field: str, doc_type_a: str | None, doc_type_b: str | None) -> str:
    """Return the comparison scope; refuse a cross-leg comparison of a leg-specific field."""
    base = field.split(".")[-1]
    scope = scope_of(doc_type_a, doc_type_b)
    la, lb = leg_of(doc_type_a), leg_of(doc_type_b)
    if FIELD_SCOPE.get(base) == "LEG" and la and lb and la != lb:
        raise CrossLegComparison(f"'{base}' is a {la.lower()}/{lb.lower()} leg-specific term and is not compared across legs")
    return scope
