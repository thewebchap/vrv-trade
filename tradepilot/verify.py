"""Builds the canonical views and runs every check, grouped by checklist item."""
from __future__ import annotations

from .canonical import build_contract, build_invoice
from .checklist import ChecklistItem
from .checks import (_DOC_TEXTS, _DOCS, Ctx, _result, check_adjustment_notes, check_advances, check_bank,
                     check_bl_refs, check_buyer_advance_first, check_contract_refs, check_currency,
                     check_goods, check_gp, check_hs_codes, check_incoterms, check_invoice_arithmetic,
                     check_mandatory_docs, check_parties, check_payment_term, check_ports, check_recovery,
                     check_required_docs, check_shipment_date, check_signatures, check_supplier_exposure,
                     check_supporting_docs, check_vessel, check_vessel_sailed, worst)
from .commercial import (check_contract_price, check_contract_qty, check_expected_amount,
                         check_quantity_measures, check_shipped_qty, infer_missing_units)
from .models import (PURCHASE_CONTRACT, PURCHASE_INVOICE, REVIEW, SALES_CONTRACT, SALES_INVOICE,
                     CheckResult)

# Quantity and price verification is a core requirement applied to every case,
# independent of which checklist rows apply.
CORE_ITEM = {"rule_id": "CORE-QTY-PRICE", "section": "CORE (TradePilot)",
             "original_text": "Quantity and price verification: quantities (net / gross / count) across PC, PI, BL, SC "
                              "and SI within contractual tolerance; unit prices against contract; quantity x unit "
                              "price = invoiced total.",
             "instruction": None}
CORE_CHECKS = (check_quantity_measures, check_shipped_qty, check_contract_qty, check_contract_price,
               check_expected_amount, check_invoice_arithmetic, check_goods, check_recovery)
DOCS_ITEM = {"rule_id": "CORE-DOCUMENTS", "section": "CORE (TradePilot)",
             "original_text": "Document completeness: Purchase Contract, Purchase Invoice, Sales Contract, Sales Invoice and "
                              "Bill of Lading are mandatory; other documents are optional unless required.",
             "instruction": None}


# Scope of each check. "PURCHASE+SALES" = evaluated separately within each leg, never across legs.
EACH_LEG = "PURCHASE+SALES (each leg separately)"
CHECK_SCOPE = {
    "PRICE.CONTRACT": EACH_LEG, "AMOUNT.EXPECTED": EACH_LEG, "ARITH.INVOICES": EACH_LEG, "ADV.DEDUCTED": EACH_LEG,
    "REF.CONTRACT": EACH_LEG, "TERMS.INCOTERM": EACH_LEG, "TERMS.CURRENCY": EACH_LEG, "ADJ.NOTES": EACH_LEG,
    "BANK.UNCHANGED": "PURCHASE", "PAYTERM.MATCH": "PURCHASE", "DOCS.REQUIRED": "PURCHASE", "EXT.EXPOSURE": "PURCHASE",
    "QTY.MEASURES": "CROSS_TRADE", "QTY.SHIPPED": "CROSS_TRADE", "QTY.CONTRACT": "CROSS_TRADE",
    "GOODS.DESCRIPTION": "CROSS_TRADE", "GOODS.HS": "CROSS_TRADE", "QUALITY.RECOVERY": "CROSS_TRADE",
    "REF.BL": "CROSS_TRADE", "SHIP.VESSEL": "CROSS_TRADE", "ROUTING.PORTS": "CROSS_TRADE", "SHIP.DATE": "CROSS_TRADE",
    "VESSEL.SAILED": "CROSS_TRADE", "PARTIES": "CROSS_TRADE (contract/invoice parties per leg; BL parties by shipping role)",
    "GP.MIN": "CROSS_TRADE (checklist COMMON-8 margin rule)", "ADV.BUYER_FIRST": "CROSS_TRADE (checklist COMMON-6 sequencing)",
    "DOCS.MANDATORY": "CASE", "DOCS.SUPPORTING": "CASE", "SIG.CONTRACTS": "CASE",
}


def build_views(ctx: Ctx) -> None:
    if PURCHASE_CONTRACT in ctx.docs: ctx.pc = build_contract(ctx.docs[PURCHASE_CONTRACT])
    if SALES_CONTRACT in ctx.docs: ctx.sc = build_contract(ctx.docs[SALES_CONTRACT])
    if PURCHASE_INVOICE in ctx.docs: ctx.pi = build_invoice(ctx.docs[PURCHASE_INVOICE])
    if SALES_INVOICE in ctx.docs: ctx.si = build_invoice(ctx.docs[SALES_INVOICE])
    infer_missing_units(ctx)


def run_checks(ctx: Ctx) -> tuple[list[CheckResult], list[dict]]:
    """Returns (checks, checklist item roll-ups). A check shared by several items is evaluated once."""
    checks: dict[str, CheckResult] = {}
    rollup: list[dict] = []
    _DOC_TEXTS.clear()
    _DOC_TEXTS.update({d.filename: "\n".join(p.text for p in d.pages) for d in ctx.docs.values()})
    _DOCS.clear()
    _DOCS.update({d.filename: d for d in ctx.docs.values()})

    def add(c: CheckResult) -> str:
        c.scope = c.scope or CHECK_SCOPE.get(c.check_id, "CASE")
        checks.setdefault(c.check_id, c)
        return c.check_id

    doc_ids = [add(check_mandatory_docs(ctx, DOCS_ITEM["rule_id"])), add(check_supporting_docs(ctx, DOCS_ITEM["rule_id"]))]
    rollup.append({**DOCS_ITEM, "interpretation_status": "CORE",
                   "result": worst([checks[i].result for i in doc_ids]), "check_ids": doc_ids})
    core_ids = [add(fn(ctx, CORE_ITEM["rule_id"])) for fn in CORE_CHECKS]
    rollup.append({**CORE_ITEM, "interpretation_status": "CORE",
                   "result": worst([checks[i].result for i in core_ids]), "check_ids": core_ids})

    common = {i.number: i for i in ctx.items if i.section.startswith("COMMON")}
    for num, item in sorted(common.items()):
        ref = item.rule_id
        if item.interpretation_status != "CURRENT":
            ids = [add(_result(f"INTERP.{ref}", "Checklist wording changed", ref, "DETERMINISTIC", REVIEW,
                               "The workbook text no longer matches the reviewed interpretation; "
                               "rule not applied automatically."))]
        elif num == 1: ids = [add(check_signatures(ctx, ref))]
        elif num == 2: ids = [add(check_contract_refs(ctx, ref))]
        elif num == 3: ids = [add(check_advances(ctx, ref))]
        elif num == 4: ids = [add(check_adjustment_notes(ctx, ref))]
        elif num == 5: ids = [add(check_bank(ctx, ref))]
        elif num == 6: ids = [add(check_buyer_advance_first(ctx, ref))]
        elif num == 7: ids = [add(check_supplier_exposure(ctx, ref))]
        elif num == 8: ids = [add(check_gp(ctx, ref, item.interpretation["threshold_pct"]))]
        else: ids = []
        rollup.append(_roll(item, ids, checks))

    row = next((i for i in ctx.items if i.rule_id == ctx.payment.get("rule_id")), None)
    if row:
        ref = row.rule_id
        ids = [add(check_payment_term(ctx, ref)), add(check_required_docs(ctx, ref, row)), "SIG.CONTRACTS"]
        if row.interpretation["features"]["vessel_sailed"]:
            ids.append(add(check_vessel_sailed(ctx, ref)))
        ids += ["REF.CONTRACT"] + core_ids   # "ensure all documents match" includes the commercial core
        for fn in (check_bl_refs, check_hs_codes, check_parties, check_ports, check_vessel,
                   check_shipment_date, check_incoterms, check_currency):
            ids.append(add(fn(ctx, ref)))
        rollup.append(_roll(row, ids, checks))
    else:
        c = _result("PAYTERM.MATCH", "Purchase payment term matches a METALS checklist row", "METALS-?", "DETERMINISTIC",
                    REVIEW, "Could not determine the purchase payment term; document-match checks not run.")
        add(c)
        rollup.append({"rule_id": "METALS-?", "section": "METALS", "original_text": None, "instruction": None,
                       "result": REVIEW, "check_ids": [c.check_id]})
    return list(checks.values()), rollup


def _roll(item: ChecklistItem, ids: list[str], checks: dict[str, CheckResult]) -> dict:
    return {"rule_id": item.rule_id, "section": item.section, "excel_row": item.excel_row,
            "original_text": item.original_text, "instruction": item.instruction,
            "interpretation": {k: v for k, v in (item.interpretation or {}).items() if k != "pin"},
            "interpretation_status": item.interpretation_status,
            "result": worst([checks[i].result for i in ids if i in checks]), "check_ids": ids}
