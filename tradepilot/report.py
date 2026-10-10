"""Write result.json, report.md and report.xlsx for a case, plus a batch summary."""
from __future__ import annotations

import json
import os
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

STATUSES = ["PASS", "PASS_WITH_NOTE", "REVIEW", "MISMATCH", "FAIL"]
RANK = {s: i for i, s in enumerate(STATUSES)}
FILL = {"PASS": "C6EFCE", "PASS_WITH_NOTE": "DDEBF7", "REVIEW": "FFEB9C", "MISMATCH": "F8CBAD", "FAIL": "FFC7CE"}
ICON = {"PASS": "PASS", "PASS_WITH_NOTE": "PASS WITH NOTE", "REVIEW": "_REVIEW_", "MISMATCH": "**MISMATCH**",
        "FAIL": "**FAIL**"}
EXCHANGE_NOTE = "Exchange-based pricing accepted. External market benchmark not independently verified."


def _counts(c: dict) -> str:
    return " / ".join(f"{ICON[k].replace('*', '').replace('_', '')} {c.get(k, 0)}" for k in STATUSES)


def _ev_line(e: dict) -> str:
    if "computed_value" in e:
        return f"- {e.get('role')}: `{json.dumps(e['computed_value'])}` ({e.get('computation')})"
    if "error" in e:
        return f"- {e.get('document')} p.{e.get('page')}: model error: {e['error']}"
    if "block" in e and "signature_present" in e:
        return (f"- {e.get('document')} p.{e.get('page')} **{e['block'].lower()} block** → {e['result']}: "
                f"signature {'present' if e['signature_present'] else 'NOT found'}"
                f"{' (' + e['signature_basis'] + ')' if e.get('signature_basis') else ''}; "
                f"stamp {'present' if e['stamp_present'] else 'NOT found'}"
                f"{' (' + e['stamp_basis'] + ')' if e.get('stamp_basis') else ''}. "
                f"Region {e.get('region')} ({e.get('located_by')}). Method: {e.get('detection_method')}. "
                f"Seen: {(e.get('visual_description') or '-')[:220]}")
    if "seller" in e and "buyer" in e:
        return (f"- {e.get('document')} p.{e.get('page')} [{e.get('extraction_method')}]: seller {e['seller']}, "
                f"buyer {e['buyer']}. {e.get('observations') or ''}")
    if e.get("status") == "NOT_FOUND" and "raw_value" not in e:
        return f"- {e.get('role') or e.get('field')}: not found"
    loc = f"{e.get('document', '?')} p.{e.get('page', '?')}"
    norm = e.get("normalized_value")
    norm_s = f" -> `{norm}`" if norm is not None and str(norm) != str(e.get("raw_value")) else ""
    src = f' - "{e["source_text"][:140]}"' if e.get("source_text") else ""
    status = f" [{e.get("status")}{': ' + e['note'] if e.get('note') else ''}]" if e.get("status") != "FOUND" or e.get("note") else ""
    return (f"- {e.get('role') or e.get('canonical_field')}: `{e.get('raw_value')}`{norm_s} ({loc}, "
            f"{e.get('extraction_method')}){src}{status}{_corr_text(e)}")


def _corr_text(e: dict) -> str:
    """'OCR read `1O0 MT`, corrected to `100 MT` (O->0: numeric field)'."""
    cs = e.get("ocr_corrections") or []
    if not cs:
        return ""
    how = "; ".join(f"{c['from']}→{c['to']}: {c['reason']}" for c in cs)
    shown = f"`{e['ocr_corrected']}`" if e.get("ocr_corrected") else "the value shown"
    return f" — **OCR correction** to {shown} ({how})"


def _val_line(v: dict, indent: str = "  ") -> list[str]:
    """One comparison operand: value, unit, basis, currency, where it came from."""
    norm = v.get("normalized_value")
    norm_s = f"{norm:,.6g}" if isinstance(norm, float) else (str(norm) if norm is not None else "?")
    head = f"{indent}- **{v.get('doc')}** {v.get('role')}: "
    head += f"`{v['raw_value']}` → " if v.get("raw_value") is not None else ""
    head += f"**{norm_s} {v.get('unit') or ''}**".rstrip()
    extras = [x for x in (v.get("basis"), v.get("price_basis") and f"price basis {v['price_basis']}",
                          v.get("currency") and f"currency {v['currency']}", v.get("tolerance_wording") and
                          f"tolerance wording '{v['tolerance_wording']}'", v.get("resolution_note"),
                          v.get("unit_inferred")) if x]
    if extras:
        head += " (" + "; ".join(extras) + ")"
    if v.get("computation"):
        head += f" = {v['computation']}"
    if v.get("page"):
        head += f" — {v.get('document')} p.{v['page']}"
        if v.get("raw_wording"):
            head += f' "{v["raw_wording"][:120]}"'
    if v.get("extraction_method"):
        head += f" [{v['extraction_method']}{', ' + v['status'] if v.get('status') and v['status'] != 'FOUND' else ''}]"
    head += _corr_text(v)
    out = [head]
    for part in v.get("from", []):
        out += _val_line(part, indent + "  ")
    return out


def _cmp_block(c: dict) -> list[str]:
    out = [f"- **{c['comparison']}** [scope: {c.get('scope', '-')}] → {ICON.get(c['result'], c['result'])}"
           + (f": {c['detail']}" if c.get("detail") else "")]
    out += _val_line(c["left"]) + _val_line(c["right"])
    out += [f"  - tolerance: {c['tolerance']}", f"  - calculation: {c['calculation']}"]
    return out


def _commercial_table(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    f = lambda x, d=4: "" if x is None else (f"{x:,.{d}f}" if isinstance(x, (int, float)) else str(x))
    out = ["", "## Quantities and prices", "",
           "| Doc | Item | Measure | Printed quantity | MT | Tolerance | Printed price | Price/MT | Basis | Cur | Amount | Page |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append("| " + " | ".join([r["doc"], (r["item"] or "")[:40], r["measure"] or "", f(r["raw_quantity"]),
                                      f(r["quantity_mt"]), f(r["tolerance"]), f(r["raw_price"]), f(r["price_per_mt"], 2),
                                      (r["price_basis"] or "").split(" (")[0], r["currency"] or "", f(r["amount"], 2),
                                      str(r["page"] or "")]) + " |")
    return out


def _side_table(title: str, sd: dict) -> list[str]:
    out = ["", f"## {title}", ""]
    if not sd.get("available"):
        return out + ["Not evaluated: contract or invoice missing."]
    rows = [("Commodity", sd["commodity"]), ("ISRI grade", sd["grade"]), ("Contract quantity", sd["contract_quantity"]),
            ("Invoice quantity", sd["invoice_quantity"]), ("Contract price terms", sd["price_terms"]),
            ("Invoice unit price", sd["invoice_price"]), ("Currency", sd["currency"]),
            ("Advance deducted", sd["advance_deducted"]), ("Gross amount", sd["gross_amount"]),
            ("Net payable", sd["net_payable"]), ("Payment terms (contract)", sd["payment_terms"]),
            ("Result", ICON[sd["result"]])]
    return out + ["| | |", "|---|---|"] + [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows]


def _md(res: dict) -> str:
    s = res["summary"]
    checks = {c["check_id"]: c for c in res["checks"]}
    tx = res.get("transaction") or {}
    issues = sorted((c for c in res["checks"] if c["result"] in ("FAIL", "MISMATCH", "REVIEW")),
                    key=lambda c: -RANK[c["result"]])
    out = [f"# TradePilot validation: case {res['case_id']}", "",
           f"## Final outcome: {ICON[s['overall_status']].replace('*', '').replace('_', '')}", ""]
    out += [f"- {ICON[c['result']]} `{c['check_id']}` [{c.get('scope') or '-'}]: {c['reason'][:300]}" for c in issues[:8]] or \
           ["No material issues. Notes, where any, are listed in the sections below."]
    out += ["", f"Checklist items: {_counts(s['checklist_items'])} · checks: {_counts(s['checks'])}",
            f"Folder `{res['case_path']}` · as of {res['as_of_date']} · model `{res['model']['name']}` · "
            f"checklist `{res['checklist']['path']}` ({res['checklist']['sheet']})", "", f"_{s['overall_rule']}_"]

    # A. Document completeness
    out += ["", "## A. Document completeness", ""]
    md = checks.get("DOCS.MANDATORY")
    if md:
        out += ["| Mandatory document | File | Status |", "|---|---|---|"]
        out += [f"| {e['role']} | {e.get('document') or '-'} | {ICON[e['status']]}{' — ' + e['note'] if e['status'] != 'PASS' else ''} |"
                for e in md["evidence"]]
    out += ["", "| File | Type | Classification | Text source |", "|---|---|---|---|"]
    for d in res["documents"]:
        src = ",".join(sorted({p["text_source"] for p in d["pages"]})) or "-"
        out.append(f"| {d['filename']} | {d['doc_type']} | {d['classification_status']} | {src} |")
        if d.get("extraction_error"):
            out.append(f"| ↳ extraction error: {d['extraction_error']} | | | |")

    out += _side_table("B. Purchase validation (Purchase Contract ↔ Purchase Invoice)", tx.get("purchase", {}))
    out += _side_table("C. Sales validation (Sales Contract ↔ Sales Invoice)", tx.get("sales", {}))
    out += ["", "_Purchase and sales prices, advances, formulas and payment terms are validated independently and never "
                "compared with each other. The GP% rule (checklist COMMON-8) is a margin threshold, not an equality test._"]

    out += ["", "## D. Physical commodity reconciliation", "", "| Aspect | Result | Detail |", "|---|---|---|"]
    out += [f"| {p['aspect']} | {ICON[p['result']]} | {p['reason'][:220].replace('|', '/')} |" for p in tx.get("physical", [])]

    out += ["", "## E. Price validation", ""]
    for cid in ("PRICE.CONTRACT", "AMOUNT.EXPECTED"):
        c = checks.get(cid)
        if not c:
            continue
        out.append(f"**{c['title']}: {ICON[c['result']]}**")
        for cp in c["comparisons"]:
            if "comparison" in cp:
                note = "" if cp.get("detail") in (EXCHANGE_NOTE.replace(". ", "; ").replace("External", "external"),) else cp.get("detail", "")
                note = "" if "benchmark not independently verified" in (note or "") else note
                out.append(f"- {cp['comparison']}: {cp['calculation']} → {ICON[cp['result']]}" + (f" ({note})" if note else ""))
        out.append("")
    if (tx.get("pricing") or {}).get("exchange_priced"):
        out.append(f"> {EXCHANGE_NOTE}")

    out += ["", "## F. Additional supporting documents", ""]
    for cid in ("DOCS.SUPPORTING", "ADJ.NOTES"):
        if cid in checks:
            out.append(f"- {checks[cid]['title']}: {ICON[checks[cid]['result']]} — {checks[cid]['reason']}")

    out += ["", "## G. Final outcome", "", f"**{ICON[s['overall_status']].replace('*', '').replace('_', '')}**", ""]
    if issues:
        out += [f"- {ICON[c['result']]}: {c['reason'][:300]}" for c in issues]
    out += ["", "---", "", "# Detail"]

    ocr = [(d["filename"], p["page"], c) for d in res["documents"] for p in d["pages"] for c in p.get("ocr_corrections", [])]
    if ocr:
        out += ["", "## OCR corrections", "", "Changes made to the primary OCR text before extraction (raw → corrected).", "",
                "| Document | Page | Line | OCR read | Corrected to | Reason |", "|---|---|---|---|---|---|"]
        out += [f"| {fn} | {pg} | {c.get('line', '')} | `{c.get('token') or c['from'] or '(line missing)'}` | `{c.get('corrected') or c['to']}` | {c['reason']} |"
                for fn, pg, c in ocr]
    out += _commercial_table(res.get("commercial_summary", []))
    p = res["payment_term"]
    out += ["", "## Purchase payment term", "",
            f"Matched checklist row: **{p.get('rule_id')}** ({p.get('status')}). Features read from the purchase "
            f"contract clause: `{json.dumps(p.get('features'))}`."]
    if p.get("differences"):
        out.append(f"Differs from the row on: {p['differences']}. {'; '.join(p.get('feature_notes') or [])}")

    out += ["", "## Checklist results", "", "| Item | Checklist text (original) | Result |", "|---|---|---|"]
    for it in res["checklist_items"]:
        txt = (it["original_text"] or "").replace("|", "/")
        out.append(f"| {it['rule_id']} | {txt[:110]}{'…' if len(txt) > 110 else ''} | {ICON[it['result']]} |")

    attention = [c for c in res["checks"] if c["result"] not in ("PASS", "PASS_WITH_NOTE")]
    if attention:
        out += ["", "## Needs attention", ""]
        for c in sorted(attention, key=lambda c: -RANK[c["result"]]):
            out.append(f"- {ICON[c['result']]} `{c['check_id']}` ({c['checklist_ref']}): {c['reason']}")

    out += ["", "## Evidence by checklist item"]
    for it in res["checklist_items"]:
        out += ["", f"### {it['rule_id']}: {ICON[it['result']]}", "", f"> {it['original_text']}"]
        if it.get("instruction"):
            out.append(f">\n> _{it['instruction']}_")
        for cid in it["check_ids"]:
            c = checks.get(cid)
            if not c:
                continue
            if c["checklist_ref"] != it["rule_id"]:
                out += ["", f"- `{cid}` {ICON[c['result']]}, see {c['checklist_ref']}"]
                continue
            out += ["", f"#### `{cid}` {c['title']}: {ICON[c['result']]}",
                    f"*{c['rule_type']}* · scope: **{c.get('scope') or '-'}** · {c['reason']}", ""]
            out += [_ev_line(e) if "doc" not in e else "\n".join(_val_line(e, "")) for e in c["evidence"]]
            for cmp in c["comparisons"]:
                if "comparison" in cmp:
                    out += _cmp_block(cmp)
                    continue
                out.append(f"- {cmp.get('a')} `{cmp.get('a_value', '')}` vs {cmp.get('b')} `{cmp.get('b_value', '')}` "
                           f"[scope: {cmp.get('scope', '-')}] → {cmp.get('outcome')}"
                           f"{' (' + cmp['method'] + ')' if cmp.get('method') else ''}: {cmp.get('detail', '')}")
                for e in cmp.get("evidence", []):
                    if e.get("page"):
                        out.append(f"  - {e.get('document')} p.{e.get('page')}: \"{(e.get('source_text') or '')[:120]}\""
                                   f" [{e.get('extraction_method')}]{_corr_text(e)}")
    return "\n".join(out) + "\n"


def _autosize(ws, widths: dict[str, int]):
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=1, max_row=1):
        for c in row:
            c.font = Font(bold=True)


def _xlsx(res: dict, path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Checklist"
    ws.append(["Item", "Original checklist text", "Result", "Checks"])
    for it in res["checklist_items"]:
        ws.append([it["rule_id"], it["original_text"], it["result"], ", ".join(it["check_ids"])])
        ws.cell(ws.max_row, 3).fill = PatternFill("solid", fgColor=FILL.get(it["result"], "FFFFFF"))
    ws.append([])
    ws.append(["OVERALL", "", res["summary"]["overall_status"]])
    ws.cell(ws.max_row, 3).fill = PatternFill("solid", fgColor=FILL.get(res["summary"]["overall_status"], "FFFFFF"))
    _autosize(ws, {"A": 12, "B": 90, "C": 18, "D": 60})

    ws = wb.create_sheet("Checks")
    ws.append(["Check", "Checklist item", "Title", "Rule type", "Result", "Reason", "Scope"])
    for c in res["checks"]:
        ws.append([c["check_id"], c["checklist_ref"], c["title"], c["rule_type"], c["result"], c["reason"], c.get("scope")])
        ws.cell(ws.max_row, 5).fill = PatternFill("solid", fgColor=FILL.get(c["result"], "FFFFFF"))
        ws.cell(ws.max_row, 6).alignment = Alignment(wrap_text=True, vertical="top")
    _autosize(ws, {"A": 18, "B": 12, "C": 45, "D": 15, "E": 18, "F": 110})

    ws = wb.create_sheet("Evidence")
    ws.append(["Check", "Role", "Document", "Page", "Raw value", "Normalised", "Source text", "Method", "Status", "Note",
               "OCR corrected value", "OCR corrections"])
    for c in res["checks"]:
        evs = list(c["evidence"]) + [e for cmp in c["comparisons"] for e in cmp.get("evidence", [])]
        for e in evs:
            ws.append([c["check_id"], e.get("role") or e.get("canonical_field"), e.get("document"), e.get("page"),
                       str(e.get("raw_value", e.get("computed_value", ""))), str(e.get("normalized_value", "")),
                       e.get("source_text") or e.get("computation"), e.get("extraction_method"), e.get("status"), e.get("note"),
                       e.get("ocr_corrected"), json.dumps(e["ocr_corrections"]) if e.get("ocr_corrections") else None])
    _autosize(ws, {"A": 18, "B": 28, "C": 14, "D": 6, "E": 30, "F": 22, "G": 70, "H": 20, "I": 12, "J": 50})

    ws = wb.create_sheet("Quantities & prices")
    cols = ["doc", "item", "measure", "raw_quantity", "quantity_mt", "tolerance", "raw_price", "price_per_mt",
            "price_basis", "currency", "amount", "page"]
    ws.append(cols)
    for r in res.get("commercial_summary", []):
        ws.append([r.get(k) for k in cols])
    _autosize(ws, {"A": 6, "B": 40, "C": 34, "D": 18, "E": 12, "F": 16, "G": 16, "H": 12, "I": 30, "J": 6, "K": 14, "L": 6})

    ws = wb.create_sheet("Comparisons")
    head = ["Check", "Comparison", "Scope", "Result", "Tolerance", "Calculation", "Detail"]
    side = ["doc", "role", "document", "page", "raw_wording", "raw_value", "normalized_value", "unit", "basis",
            "currency", "extraction_method"]
    ws.append(head + [f"left {k}" for k in side] + [f"right {k}" for k in side])
    for c in res["checks"]:
        for cmp in c["comparisons"]:
            if "comparison" not in cmp:
                continue
            ws.append([c["check_id"], cmp["comparison"], cmp.get("scope"), cmp["result"], cmp.get("tolerance"),
                       cmp.get("calculation"), cmp.get("detail")]
                      + [str(cmp["left"].get(k, "")) for k in side] + [str(cmp["right"].get(k, "")) for k in side])
            ws.cell(ws.max_row, 4).fill = PatternFill("solid", fgColor=FILL.get(cmp["result"], "FFFFFF"))
    _autosize(ws, {"A": 16, "B": 50, "C": 18, "D": 45, "E": 45, "F": 50})

    ws = wb.create_sheet("Extracted fields")
    ws.append(["Document", "Type", "Field", "Raw value", "Normalised", "Page", "Source text", "Method", "Status", "Note",
               "OCR corrected value", "OCR corrections"])
    for d in res["documents"]:
        rows = list(d["fields"].items()) + [(f"line{i + 1}.{k}", f) for i, it in enumerate(d["line_items"]) for k, f in it.items()]
        for name, f in rows:
            ws.append([d["filename"], d["doc_type"], name, f.get("raw_value"), str(f.get("normalized_value", "")),
                       f.get("page"), f.get("source_text"), f.get("extraction_method"), f.get("status"), f.get("note"),
                       f.get("ocr_corrected"), json.dumps(f["ocr_corrections"]) if f.get("ocr_corrections") else None])
    _autosize(ws, {"A": 12, "B": 20, "C": 26, "D": 40, "E": 30, "F": 6, "G": 70, "H": 18, "I": 12, "J": 50})
    wb.save(path)


# ---------------------------------------------------------------------------- email reply
# Built deterministically from the structured results: no model rewrites the assessment, so numbers and
# conclusions in the email are exactly those in result.json.

SHORT_TITLE = {
    "DOCS.MANDATORY": "Mandatory documents", "QTY.CONTRACT": "Quantity vs contract", "QTY.SHIPPED": "Shipped quantity",
    "QTY.MEASURES": "Weights", "PRICE.CONTRACT": "Price", "AMOUNT.EXPECTED": "Invoice total",
    "ARITH.INVOICES": "Invoice arithmetic", "GP.MIN": "Gross profit", "SHIP.DATE": "Shipment date",
    "TERMS.INCOTERM": "Delivery terms", "TERMS.CURRENCY": "Currency", "GOODS.HS": "HS code",
    "GOODS.DESCRIPTION": "Goods / grade", "QUALITY.RECOVERY": "Quality / recovery", "REF.BL": "BL reference",
    "REF.CONTRACT": "Contract reference", "SHIP.VESSEL": "Vessel / voyage", "ROUTING.PORTS": "Ports",
    "PARTIES": "Parties", "BANK.UNCHANGED": "Bank details", "ADV.DEDUCTED": "Advance deduction",
    "ADV.BUYER_FIRST": "Advance sequencing", "SIG.CONTRACTS": "Signature / stamp", "DOCS.REQUIRED": "Required documents",
    "PAYTERM.MATCH": "Payment term", "ADJ.NOTES": "Debit / credit notes", "VESSEL.SAILED": "Vessel sailed",
}
NOTE_TEXT = {
    "DOCS.SUPPORTING": "Optional supporting documents (certificates etc.) were not included; not required for this check.",
    "ADV.DEDUCTED": "Advance deductions accepted as stated on the invoices; the payments themselves were not independently confirmed.",
    "ADJ.NOTES": "No debit or credit note supplied; no adjustment identified.",
    "EXT.EXPOSURE": "Supplier exposure limit / previous advances not checked (treasury records are not connected).",
    "ADV.BUYER_FIRST": "Both legs have advances: collect the buyer's advance before paying the supplier's.",
}
EXCHANGE_EMAIL_NOTE = "Pricing is exchange-based (LME/formula); formula terms were checked, the market price itself was not independently verified."


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(",;:(") + " …"


def _issue_lines(c: dict) -> list[str]:
    """One concise line per discrepancy/review item of a check (from its comparisons when available)."""
    title = SHORT_TITLE.get(c["check_id"], c["title"])
    bad = c["result"]
    lines = []
    for cp in c.get("comparisons", []):
        if cp.get("result", "PASS" if cp.get("outcome") == "EQ" else bad) not in (bad,):
            continue
        if "comparison" in cp:
            detail = cp.get("detail") or ""
            lines.append(f"{title} — {cp['comparison']}: {cp['calculation']}" + (f" ({_clip(detail, 110)})" if detail else ""))
        elif cp.get("outcome") in ("NEQ", "UNSURE", "NOT_COMPARED"):
            lines.append(f"{title} — {cp.get('a')}: {cp.get('a_value') or '-'} | {cp.get('b')}: {cp.get('b_value') or '-'}"
                         + (f" ({_clip(cp['detail'], 110)})" if cp.get("detail") else ""))
    if not lines:
        parts = [p.strip() for p in c["reason"].split(" | ") if p.strip()]
        keep = [p for p in parts if not re.search(r"\s=\s|\b(within tolerance|agree|no advance in|not compared with each other)\b", p)] or parts
        lines = [f"{title} — {_clip(p, 230)}" for p in keep[:3]]
    return lines[:4]


def _notes(res: dict) -> list[str]:
    notes, checks = [], res.get("checks", [])
    if any("benchmark not independently verified" in c["reason"] for c in checks):
        notes.append(EXCHANGE_EMAIL_NOTE)
    for c in checks:
        if c["result"] != "PASS_WITH_NOTE" or "benchmark not independently verified" in c["reason"]:
            continue
        if c["check_id"] in NOTE_TEXT:
            notes.append(NOTE_TEXT[c["check_id"]])
        elif c["check_id"] in ("QTY.SHIPPED", "QTY.MEASURES") and "used as net" in c["reason"]:
            n = "BL states gross weight only; used as net weight because the documents show no packaging weight."
            if n not in notes:
                notes.append(n)
        elif c["check_id"] == "TERMS.INCOTERM":
            notes += [f"Delivery terms — {p.strip()}" for p in c["reason"].split(" | ") if "does not state" in p]
        else:
            notes.append(f"{SHORT_TITLE.get(c['check_id'], c['title'])} — {_clip(c['reason'].split(' | ')[0], 200)}")
    inc = next((c for c in checks if c["check_id"] == "TERMS.INCOTERM"), None)
    if inc:
        terms = {cp.get("scope"): cp.get("a_value") for cp in inc.get("comparisons", [])}
        if terms.get("PURCHASE") and terms.get("SALES") and terms["PURCHASE"] != terms["SALES"]:
            notes.append(f"Purchase ({terms['PURCHASE']}) and sales ({terms['SALES']}) delivery terms differ — normal for separate legs.")
    return list(dict.fromkeys(notes))


def compose_email(res: dict, trade_ref: str | None = None) -> str:
    """The reply body: status, every mismatch/FAIL, every REVIEW, meaningful notes, and the trade reference."""
    checks = res.get("checks", [])
    ref = trade_ref or res.get("trade_ref") or res["case_id"]
    fails = [c for c in checks if c["result"] == "FAIL"]
    mismatches = [c for c in checks if c["result"] == "MISMATCH"]
    reviews = [c for c in checks if c["result"] == "REVIEW"]
    if fails:
        status = "INCOMPLETE — MANDATORY DOCUMENT MISSING"
    elif mismatches and reviews:
        status = "MISMATCH / REVIEW REQUIRED"
    elif mismatches:
        status = "MISMATCH FOUND"
    elif reviews:
        status = "REVIEW REQUIRED"
    else:
        status = "GOOD TO GO"
    passed = sum(1 for c in checks if c["result"] in ("PASS", "PASS_WITH_NOTE"))
    out = ["TradePilot assessment completed.", "", f"Trade Ref: {ref}", f"Status: {status}", ""]
    if not (fails or mismatches or reviews):
        out.append("All applicable checks passed.")
    else:
        out.append(f"{passed} of {len(checks)} checks passed.")
    if fails:
        out += ["", "Missing:"] + [f"• {line}" for c in fails for line in _missing_lines(c)]
    if mismatches:
        out += ["", "Mismatches:"] + [f"• {line}" for c in mismatches for line in _issue_lines(c)]
    if reviews:
        out += ["", "Review:"] + [f"• {line}" for c in reviews for line in _issue_lines(c)]
    notes = _notes(res)
    if notes:
        out += ["", "Notes:"] + [f"• {n}" for n in notes]
    out += ["", "TradePilot"]
    return "\n".join(out) + "\n"


def _missing_lines(c: dict) -> list[str]:
    if c["check_id"] == "DOCS.MANDATORY":
        return [f"{e['role']} — {e.get('note')}" for e in c.get("evidence", []) if e.get("status") == "FAIL"]
    return _issue_lines(c)


def write_case(res: dict, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "result.json"), "w") as f:
        json.dump(res, f, indent=2, default=str)
    if res.get("error"):
        with open(os.path.join(out_dir, "report.md"), "w") as f:
            f.write(f"# Case {res['case_id']}: processing error\n\n{res['error']}\n\nOverall: REVIEW\n")
        return out_dir
    with open(os.path.join(out_dir, "report.md"), "w") as f:
        f.write(_md(res))
    with open(os.path.join(out_dir, "email_response.txt"), "w") as f:
        f.write(compose_email(res, res.get("trade_ref")))
    _xlsx(res, os.path.join(out_dir, "report.xlsx"))
    return out_dir


def summary_table(results: list[dict]) -> str:
    head = f"{'Case':<6}{'PASS':>6}{'NOTE':>6}{'REVIEW':>8}{'MISM.':>7}{'FAIL':>6}  {'Overall':<16}{'Payment term':<14}"
    lines = [head, "-" * len(head)]
    for r in results:
        s = r["summary"].get("checklist_items") or {}
        term = (r.get("payment_term") or {}).get("rule_id") or "-"
        status = r["summary"]["overall_status"] + (" (error)" if r.get("error") else "")
        lines.append(f"{r['case_id']:<6}{s.get('PASS', 0):>6}{s.get('PASS_WITH_NOTE', 0):>6}{s.get('REVIEW', 0):>8}"
                     f"{s.get('MISMATCH', 0):>7}{s.get('FAIL', 0):>6}  {status:<16}{term:<14}")
    return "\n".join(lines)


def write_batch(results: list[dict], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for r in results:
        s = r["summary"].get("checklist_items") or {}
        issues = [f"{c['result']} {c['check_id']}: {c['reason']}" for c in r.get("checks", [])
                  if c["result"] in ("FAIL", "MISMATCH")]
        rows.append({"case_id": r["case_id"], "overall_status": r["summary"]["overall_status"], "error": r.get("error"),
                     "payment_term": (r.get("payment_term") or {}).get("rule_id"),
                     **{k: s.get(k, 0) for k in STATUSES}, "material_issues": issues})
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(rows, f, indent=2)
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write("# TradePilot batch summary (checklist items)\n\n```\n" + summary_table(results) + "\n```\n\n")
        for r in rows:
            if r["material_issues"] or r["error"]:
                f.write(f"## Case {r['case_id']}\n\n" + (f"- ERROR: {r['error']}\n" if r["error"] else "") +
                        "".join(f"- {x}\n" for x in r["material_issues"]) + "\n")
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Case", "Payment term", *STATUSES, "Overall", "Material issues"])
    for r in rows:
        ws.append([r["case_id"], r["payment_term"], *[r[k] for k in STATUSES], r["overall_status"],
                   "\n".join(r["material_issues"]) or r["error"]])
        ws.cell(ws.max_row, 8).fill = PatternFill("solid", fgColor=FILL.get(r["overall_status"], "FFFFFF"))
        ws.cell(ws.max_row, 9).alignment = Alignment(wrap_text=True, vertical="top")
    _autosize(ws, {"A": 8, "B": 14, "C": 7, "D": 16, "E": 9, "F": 10, "G": 7, "H": 18, "I": 140})
    wb.save(os.path.join(out_dir, "summary.xlsx"))
