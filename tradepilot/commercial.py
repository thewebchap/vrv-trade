"""Quantity and price verification: the commercial core of every case.

Each comparison is recorded as a self-contained, auditable entry:

    {"comparison": "...", "left": <value>, "right": <value>,
     "tolerance": "...", "calculation": "...", "result": PASS | PASS_WITH_NOTE | REVIEW | MISMATCH}

where every <value> carries document, page, raw wording, raw value,
normalised value, unit, basis (NET / GROSS / ...) and currency.

Measures are never silently interchanged: a gross weight is not used as a
net weight, and piece/package counts are never compared with weights.
Quantities without an explicit net/gross label ("Quantity: 26 MT") are
treated as the commercial (invoiced) weight, which for metals is the net
weight; that assumption is stated in the evidence.
"""
from __future__ import annotations

import math
import re

from . import normalize as N
from decimal import Decimal

from .canonical import (CENT, ContractView, InvoiceView, Line, Material, dec, match_material, money,
                        weight_mt)
from .checks import Ctx, _result, worst
from .models import BILL_OF_LADING, MISMATCH, PASS, PASS_NOTE, REVIEW, CheckResult, Field

BASIS_TEXT = {"NET": "net weight", "GROSS": "gross weight", "COUNT": "count (not a weight)",
              "WEIGHT": "weight, not labelled net/gross (treated as commercial net weight, the metals convention)",
              "MIXED": "mixed bases"}


# --------------------------------------------------------------------------- evidence values

def value(doc_short: str, role: str, f: Field | None, normalized=None, unit: str | None = None,
          basis: str | None = None, currency: str | None = None, **extra) -> dict:
    v = {"doc": doc_short, "role": role, "normalized_value": normalized, "unit": unit}
    if f is not None:
        v.update(document=f.document, page=f.page, raw_wording=f.source_text, raw_value=f.raw_value,
                 extraction_method=f.extraction_method, status=f.status,
                 ocr_corrected=f.ocr_corrected, ocr_corrections=f.ocr_corrections)
        if f.note:
            v["note"] = f.note
    if basis:
        v["basis"] = BASIS_TEXT.get(basis, basis)
    if currency:
        v["currency"] = currency
    v.update(extra)
    return {k: x for k, x in v.items() if x is not None}


def computed_value(doc_short: str, role: str, normalized, unit: str, how: str, parts: list[dict] | None = None,
                   **extra) -> dict:
    v = {"doc": doc_short, "role": role, "normalized_value": normalized, "unit": unit,
         "extraction_method": "computed", "computation": how, **extra}
    if parts:
        v["from"] = parts
    return v


_LEG_OF_SHORT = {"PC": "PURCHASE", "PI": "PURCHASE", "SC": "SALES", "SI": "SALES"}


def cmp(comparison: str, left: dict, right: dict, tolerance: str, calculation: str, result: str, detail: str = "") -> dict:
    """One auditable comparison. Its scope is the commercial leg when both sides belong to the same leg,
    otherwise CROSS_TRADE (physical facts such as quantity against the BL)."""
    la, lb = _LEG_OF_SHORT.get(left.get("doc")), _LEG_OF_SHORT.get(right.get("doc"))
    scope = la if la and la == lb else "CROSS_TRADE"
    return {"comparison": comparison, "scope": scope, "left": left, "right": right, "tolerance": tolerance,
            "calculation": calculation, "result": result, "detail": detail}


def _invoice_qty_value(iv: InvoiceView) -> dict:
    lab = iv.doc.short
    parts = [value(lab, f"line {i + 1} quantity", l.fields.get("quantity"),
                   round(l.qty_mt, 6) if l.qty_mt is not None else None, "MT",
                   l.basis, quantity_unit=(l.fields["quantity_unit"].raw_value if l.fields.get("quantity_unit") else None),
                   basis_label=l.basis_evidence or None, unit_inferred=l.unit_inferred)
             for i, l in enumerate(iv.lines)]
    if len(iv.lines) == 1:
        return parts[0] | {"basis": BASIS_TEXT.get(iv.basis, iv.basis)}
    return computed_value(lab, "invoiced quantity", round(iv.qty_mt, 6) if iv.qty_mt is not None else None, "MT",
                          f"sum of {len(iv.lines)} line quantities", parts, basis=BASIS_TEXT.get(iv.basis, iv.basis))


def _bl_weights(ctx: Ctx) -> dict:
    """BL net and gross weights (label-confirmed only), plus package count."""
    bl = ctx.docs.get(BILL_OF_LADING)
    out = {"net": None, "gross": None, "net_value": None, "gross_value": None, "count": None, "net_res": 0.001,
           "gross_figures": 0}
    if not bl:
        return out
    # How many separate gross-weight figures does the BL print (one per container is common)?
    out["gross_figures"] = len(re.findall(r"(?im)(GROSS\s*(WEIGHT|WT)\.?|G\.\s*W\.?)\s*[:/]\s*\d",
                                          "\n".join(p.text for p in bl.pages)))
    for kind in ("net", "gross"):
        f = bl.fields.get(f"{kind}_weight")
        mt, res = weight_mt(bl, f"{kind}_weight")
        if mt is not None and f.status in ("FOUND", "AMBIGUOUS"):
            # "17,450kg" without a document locale: choose the reading that agrees with the invoices,
            # and say so; only possible when the ambiguity is numeric, not about the label.
            p = N.parse_number(f.value, bl.number_locale)
            note = None
            if p.ambiguous and p.alt is not None:
                unit = N.normalize_weight_unit(f.raw_value) or N.normalize_weight_unit(
                    (bl.fields.get(f"{kind}_weight_unit") or Field("", None)).raw_value)
                cands = [N.to_mt(v, unit) for v in (p.value, p.alt)]
                others = [x.qty_mt for x in (ctx.pi, ctx.si) if x and x.qty_mt]
                pick = next((c for c in cands if any(abs(c - o) <= 0.001 for o in others)), None)
                if pick is None:
                    continue
                mt, note = pick, f"'{f.raw_value}' is ambiguous; reading {pick} MT chosen because it agrees with the invoices"
            out[kind], out[f"{kind}_value"] = mt, value("BL", f"BL {kind} weight", f, round(mt, 6), "MT",
                                                         kind.upper(), resolution_note=note)
            if kind == "net":
                out["net_res"] = res
    pk = bl.fields.get("packages")
    if pk and pk.raw_value:
        out["count"] = value("BL", "BL packages", pk, None, "count", "COUNT")
    if out["gross"] is None:
        _gross_from_mislabelled_net(bl, out)
    _bl_total_gross(bl, out)
    out["net_is_gross"] = None
    if out["net"] is None and out["gross"] is not None:
        why = _gross_equals_net_evidence(ctx, bl)
        if why:
            # Documented: no packaging weight, so the BL gross weight is the shipped net weight.
            out["net"], out["net_res"], out["net_is_gross"] = out["gross"], 0.001, why
            out["net_value"] = dict(out["gross_value"], role="BL gross weight, used as net weight", note=why)
    return out


def _gross_from_mislabelled_net(bl, out: dict) -> None:
    """A weight the model offered as 'net' but that sits under a GROSS WEIGHT heading is a gross weight."""
    from .extract import ground, label_confirmed
    f = bl.fields.get("net_weight")
    if not f or f.label_confirmed is not False or not f.raw_value:
        return
    page, line, context = ground(f.value, bl, "gross_weight")
    unit = N.normalize_weight_unit(f.raw_value) or N.normalize_weight_unit(
        (bl.fields.get("net_weight_unit") or Field("", None)).raw_value)
    v = N.parse_number(f.value, bl.number_locale).value
    if page and unit and v and label_confirmed("gross_weight", context):
        g = Field("gross_weight", f.raw_value, N.to_mt(v, unit), document=bl.filename, document_type=bl.doc_type,
                  page=page, source_text=line, extraction_method=f.extraction_method, status="FOUND", label_confirmed=True,
                  note="offered as net weight by the model, but printed under a GROSS WEIGHT heading")
        out["gross"] = N.to_mt(v, unit)
        out["gross_value"] = value("BL", "BL gross weight", g, round(out["gross"], 6), "MT", "GROSS")


def _bl_total_gross(bl, out: dict) -> None:
    """A BL that prints one gross weight per container: their sum is the total gross weight when the BL
    also prints that total somewhere (two independent readings agree). Replaces a single-container value."""
    figures = []
    for p in bl.pages:
        for m in re.finditer(r"(?im)(?:GROSS\s*(?:WEIGHT|WT)\.?|G\.\s*W\.?)\s*[:/]\s*(\d(?:\d|[.,]\s?(?=\d))*)\s*(KGS?|MTS?)",
                             p.text):
            v = N.parse_number(m.group(1).replace(" ", ""), bl.number_locale).value
            if v:
                figures.append(N.to_mt(v, m.group(2)))
    if len(figures) < 2:
        return
    total = sum(figures)
    if out["gross"] is not None and abs(out["gross"] - total) <= 0.001:
        return
    numbers = {round(N.to_mt(v, "KG"), 3) for p in bl.pages
               for t in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{4,}(?:\.\d+)?", p.text)
               for v in [N.parse_number(t, bl.number_locale).value] if v}
    if round(total, 3) in numbers:
        out["gross"] = total
        out["gross_value"] = computed_value("BL", "BL total gross weight", round(total, 6), "MT",
                                            f"sum of {len(figures)} per-container gross weights; the BL also prints this total",
                                            basis=BASIS_TEXT["GROSS"])


def _gross_equals_net_evidence(ctx: Ctx, bl) -> str | None:
    """Documentary reason to treat the BL gross weight as net: bulk/loose cargo (no packaging), or an
    invoice stating gross weight = net weight."""
    desc = " ".join(x.raw_value or "" for x in (bl.fields.get("goods_description"), bl.fields.get("packages")) if x).upper()
    page1 = bl.pages[0].text.upper() if bl.pages else ""
    if re.search(r"\bBULKS?\b|\bIN BULK\b|\bLOOSE\b", desc) or re.search(r"\d\s+BULKS?\b|BULKS? IN TOTAL", page1):
        return "the BL describes bulk/loose cargo (no packaging), so its gross weight is the net weight"
    for iv in (ctx.pi, ctx.si):
        if iv and iv.net_weight_mt is not None and iv.gross_weight_mt is not None and \
                abs(iv.net_weight_mt - iv.gross_weight_mt) <= 0.0005:
            return f"{iv.doc.short} states gross weight = net weight ({iv.net_weight_mt:.3f} MT): no packaging weight"
    return None


def _one_digit_mt(a: float, b: float, res: float) -> bool:
    """Do a and b, printed at the given precision, differ in exactly one digit (an OCR slip)?"""
    dec = max(0, round(-math.log10(res)))
    x, y = f"{a:.{dec}f}", f"{b:.{dec}f}"
    return len(x) == len(y) and sum(c1 != c2 for c1, c2 in zip(x, y)) == 1


def _qty_equal(a: float, b: float, res: float) -> bool:
    """Equal at the precision both documents print (half the coarser step), plus float noise."""
    return abs(a - b) <= res / 2 + 1e-9


def infer_missing_units(ctx: Ctx) -> None:
    """When an invoice prints quantities without any unit, read them as MT or KG only if
    exactly one reading makes the invoiced total equal the BL net weight. The inference is
    recorded on the lines, and the invoice-vs-BL comparison it relies on is then REVIEW (circular)."""
    bl = _bl_weights(ctx)
    if bl["net"] is None:
        return
    for iv in (ctx.pi, ctx.si):
        if not iv or not iv.lines or any(l.qty_unit is not None for l in iv.lines):
            continue
        loc = iv.doc.number_locale
        raws = [N.parse_number(l.fields["quantity"].value, loc).value if l.fields.get("quantity") else None
                for l in iv.lines]
        if None in raws:
            continue
        fits = [u for u in ("MT", "KG") if _qty_equal(N.to_mt(sum(raws), u), bl["net"], max(bl["net_res"], 0.001))]
        if len(fits) != 1:
            continue
        unit = fits[0]
        for l, q in zip(iv.lines, raws):
            l.qty_unit, l.qty_mt = unit, N.to_mt(q, unit)
            p = l.fields.get("unit_price")
            if p and p.raw_value:
                l.price_per_mt = N.parse_number(p.value, loc).value / N.to_mt(1, unit)
                l.price_basis = f"per {unit} (unit inferred)"
            l.unit_inferred = (f"quantity unit not printed on {iv.doc.short}; read as {unit} because only then does the "
                               f"invoiced quantity ({sum(raws):g}) equal the BL net weight {bl['net']:.4f} MT")
            l.notes = [n for n in l.notes if "unit not stated" not in n] + [l.unit_inferred]
        iv.qty_mt = sum(l.qty_mt for l in iv.lines)


# =========================================================================== checks

def check_quantity_measures(ctx: Ctx, ref: str) -> CheckResult:
    title = "Quantity measures identified per document (net / gross / count kept separate)"
    results, reasons, comps = [], [], []
    bl = _bl_weights(ctx)
    for iv in (ctx.pi, ctx.si):
        if not iv:
            continue
        lab = iv.doc.short
        if iv.basis == "COUNT" or any(l.basis == "COUNT" for l in iv.lines):
            results.append(REVIEW); reasons.append(f"{lab} states quantity as a count; no weight to compare")
        elif iv.basis == "GROSS":
            results.append(REVIEW); reasons.append(f"{lab} invoices on gross weight; net weight not stated")
        elif iv.qty_mt is None:
            results.append(REVIEW); reasons.append(f"{lab} quantity not established")
        elif any(l.unit_inferred for l in iv.lines):
            results.append(REVIEW); reasons.append(next(l.unit_inferred for l in iv.lines if l.unit_inferred))
        else:
            results.append(PASS)
        if iv.net_weight_mt is not None and iv.gross_weight_mt is not None:
            ok = iv.net_weight_mt <= iv.gross_weight_mt + 1e-9
            results.append(PASS if ok else MISMATCH)
            comps.append(cmp(f"{lab} net weight <= gross weight",
                             value(lab, "net weight", iv.doc.fields.get("net_weight"), iv.net_weight_mt, "MT", "NET"),
                             value(lab, "gross weight", iv.doc.fields.get("gross_weight"), iv.gross_weight_mt, "MT", "GROSS"),
                             "none", f"{iv.net_weight_mt:.4f} <= {iv.gross_weight_mt:.4f}", PASS if ok else MISMATCH))
            if not ok:
                reasons.append(f"{lab} net weight exceeds gross weight")
    if bl["net_is_gross"]:
        results.append(PASS_NOTE)
        reasons.append(f"BL states only gross weight {bl['gross']:.4f} MT; used as net because {bl['net_is_gross']}")
    elif bl["net"] is None:
        results.append(REVIEW)
        reasons.append("BL does not state a net weight" + (f" (it states gross weight {bl['gross']:.4f} MT, which is a "
                                                           f"different measure)" if bl["gross"] is not None else ""))
    if bl["net"] is not None and bl["gross"] is not None and not bl["net_is_gross"]:
        ok = bl["net"] <= bl["gross"] + 1e-9
        r, detail = (PASS, "") if ok else (MISMATCH, "net weight exceeds gross weight")
        if not ok and bl["gross_figures"] > 1:
            r, detail = REVIEW, (f"the BL prints {bl['gross_figures']} separate gross-weight figures; the one extracted "
                                 f"is probably a single container's, not the BL total")
        results.append(r)
        comps.append(cmp("BL net weight <= gross weight", bl["net_value"], bl["gross_value"], "none",
                         f"{bl['net']:.4f} <= {bl['gross']:.4f}", r, detail))
        if r != PASS:
            reasons.append(f"BL: {detail}")
    table = [_invoice_qty_value(iv) for iv in (ctx.pi, ctx.si) if iv] + \
            [v for v in (bl["net_value"], bl["gross_value"], bl["count"]) if v]
    res = worst(results)
    reason = ("Each invoice states a weight quantity and the BL states a net weight; measures are consistent."
              if res == PASS else " | ".join(reasons))
    return _result("QTY.MEASURES", title, ref, "NORMALIZED", res, reason, evidence=table, comps=comps)


def check_shipped_qty(ctx: Ctx, ref: str) -> CheckResult:
    title = "Shipped quantity consistent: purchase invoice = BL net weight = sales invoice"
    bl = _bl_weights(ctx)
    comps, results, reasons = [], [], []
    inv = {iv.doc.short: iv for iv in (ctx.pi, ctx.si) if iv}

    def compare(la: str, va: float | None, ra: float, vala: dict, ocr_a: bool,
                lb: str, vb: float | None, rb: float, valb: dict, ocr_b: bool):
        if va is None or vb is None:
            results.append(REVIEW); reasons.append(f"{la} vs {lb}: quantity not established")
            comps.append(cmp(f"{la} quantity vs {lb} quantity", vala, valb, "-", "not computable", REVIEW))
            return
        res = max(ra, rb)
        diff = va - vb
        tol = f"rounding only: ±{res / 2 * 1000:g} kg (half the coarser printed precision)"
        calc = f"{va:.4f} MT - {vb:.4f} MT = {diff * 1000:+.1f} kg"
        inferred = [l.unit_inferred for iv in inv.values() if iv.doc.short in (la, lb) for l in iv.lines if l.unit_inferred]
        if inferred and "BL" in (la + lb) and _qty_equal(va, vb, res):
            r, why = REVIEW, "agreement is circular: " + inferred[0]
        elif _qty_equal(va, vb, res):
            r, why = PASS, ""
        elif (ocr_a or ocr_b) and (_one_digit_mt(va, vb, min(ra, rb)) or
                                   any(l.ocr_doubt for iv in inv.values() for l in iv.lines)):
            r, why = REVIEW, f"differs by {abs(diff) * 1000:.1f} kg, explainable by a single misread digit on an OCR'd page"
        else:
            r, why = MISMATCH, f"differs by {abs(diff) * 1000:.1f} kg"
        results.append(r)
        comps.append(cmp(f"{la} quantity vs {lb} quantity", vala, valb, tol, calc, r, why))
        if r != PASS:
            reasons.append(f"{la} {va:.4f} MT vs {lb} {vb:.4f} MT: {why}")

    for lab, iv in inv.items():
        if iv.basis == "GROSS":
            results.append(REVIEW); reasons.append(f"{lab} quantity is a gross weight; not compared with net weights")
            continue
        if bl["net"] is not None:
            compare(lab, iv.qty_mt, iv.qty_res_mt, _invoice_qty_value(iv), ctx.is_ocr(iv.doc.doc_type),
                    "BL net", bl["net"], bl["net_res"], bl["net_value"], ctx.is_ocr(BILL_OF_LADING))
    if "PI" in inv and "SI" in inv and "GROSS" not in (inv["PI"].basis, inv["SI"].basis):
        compare("PI", inv["PI"].qty_mt, inv["PI"].qty_res_mt, _invoice_qty_value(inv["PI"]), ctx.is_ocr(inv["PI"].doc.doc_type),
                "SI", inv["SI"].qty_mt, inv["SI"].qty_res_mt, _invoice_qty_value(inv["SI"]), ctx.is_ocr(inv["SI"].doc.doc_type))
    if bl["net"] is None:
        results.append(REVIEW)
        msg = "BL states no net weight, so the shipped net quantity cannot be confirmed"
        if bl["gross"] is not None:
            same = [lab for lab, iv in inv.items() if iv.qty_mt is not None and _qty_equal(iv.qty_mt, bl["gross"], 0.001)]
            biggest = max((iv.qty_mt for iv in inv.values() if iv.qty_mt is not None), default=None)
            if same:
                tail = f"equals the {'/'.join(same)} quantity, but gross is not net"
            elif biggest is not None and bl["gross"] < biggest - 0.001:
                tail = (f"is smaller than the invoiced net weight {biggest:.4f} MT, so it cannot be the BL's total gross "
                        f"weight (likely a per-container figure)")
            else:
                tail = "differs from the invoices"
            msg += f"; the BL gross weight {bl['gross']:.4f} MT {tail}"
            for lab, iv in inv.items():
                comps.append(cmp(f"{lab} quantity vs BL gross weight (information only: different measures)",
                                 _invoice_qty_value(iv), bl["gross_value"], "not applicable",
                                 f"{(iv.qty_mt or 0):.4f} vs {bl['gross']:.4f} MT", REVIEW, "gross weight is not net weight"))
        reasons.append(msg)
    if not results:
        return _result("QTY.SHIPPED", title, ref, "NORMALIZED", REVIEW, "No quantities to compare.", comps=comps)
    if bl["net_is_gross"]:
        results = [PASS_NOTE if r == PASS else r for r in results]
        reasons.append(f"BL gross weight used as net: {bl['net_is_gross']}")
    res = worst(results)
    reason = (f"Invoices and BL net weight agree at {bl['net']:.4f} MT." if res == PASS and bl["net"] is not None
              else (f"Invoices and BL agree at {bl['net']:.4f} MT. " if res == PASS_NOTE else "") + " | ".join(reasons))
    return _result("QTY.SHIPPED", title, ref, "NORMALIZED", res, reason, comps=comps)


def _contract_value_for(mat: Material, cv: ContractView) -> dict:
    lab = cv.doc.short
    return value(lab, f"contracted quantity ({mat.description})", mat.fields.get("quantity"), mat.qty_mt, "MT", "WEIGHT",
                 tolerance_wording=(mat.fields["tolerance"].raw_value if mat.fields.get("tolerance") else None))


def _tolerance_eval(qty: float, qty_res: float, cv: ContractView, mat: Material, label: str, n_materials_shipped: int):
    """Returns (result, tolerance text, calculation, detail, contract value)."""
    tol = mat.tol_pct if mat.tol_pct is not None else cv.total_tol_pct
    cval = _contract_value_for(mat, cv)
    if tol is None:
        why = "; ".join(mat.notes + cv.issues) or "tolerance not stated"
        return REVIEW, "not established", "-", f"contract tolerance not established ({why})", cval
    multi = len(cv.materials) > 1
    if cv.per_shipment_mt and not multi:
        base, basis = cv.per_shipment_mt, "quantity per BL/lot"
        f = cv.doc.fields.get("quantity_per_shipment")
        cval = value(cv.doc.short, "contracted quantity per BL/lot", f, base, "MT", "WEIGHT")
    else:
        base, basis = mat.qty_mt, "contracted quantity"
    if base is None:
        return REVIEW, "-", "-", "contracted quantity not established", cval
    lo, hi = base * (1 - tol / 100), base * (1 + tol / 100)
    toltxt = f"±{tol:g}% of {base:g} MT ({basis}) → {lo:.4f} to {hi:.4f} MT"
    calc = f"{qty:.4f} MT vs allowed {lo:.4f}-{hi:.4f} MT"
    slack = qty_res / 2 + 1e-9
    if qty > hi + slack:
        return MISMATCH, toltxt, calc, f"exceeds the maximum by {(qty - hi) * 1000:.1f} kg", cval
    if qty >= lo - slack:
        return PASS, toltxt, calc, "within tolerance", cval
    if cv.partial_allowed and basis == "contracted quantity":
        return PASS, toltxt, calc, ("below the minimum, but partial shipment is allowed; cumulative quantity across "
                                    "shipments is not verifiable from this case"), cval
    if cv.partial_allowed is None:
        return REVIEW, toltxt, calc, "below the minimum and partial-shipment terms are unclear", cval
    return MISMATCH, toltxt, calc, f"below the minimum by {(lo - qty) * 1000:.1f} kg", cval


def _group_lines(iv: InvoiceView, cv: ContractView) -> tuple[dict, list[str]]:
    groups, unmatched = {}, []
    for line in iv.lines:
        mat, how = match_material(line, cv.materials)
        if mat is None:
            unmatched.append(f"{iv.doc.short} line '{line.description}': {how}")
        else:
            groups.setdefault(id(mat), (mat, []))[1].append(line)
    return groups, unmatched


def check_contract_qty(ctx: Ctx, ref: str) -> CheckResult:
    title = "Quantity within contract quantity and tolerance (invoices and BL)"
    results, reasons, comps = [], [], []
    bl = _bl_weights(ctx)
    for iv, cv in ((ctx.pi, ctx.pc), (ctx.si, ctx.sc)):
        if not iv or not cv:
            results.append(REVIEW); reasons.append("invoice or contract missing"); continue
        lab, clab = iv.doc.short, cv.doc.short
        groups, unmatched = _group_lines(iv, cv)
        for u in unmatched:
            results.append(REVIEW); reasons.append(u)
        for mat, lines in groups.values():
            if any(l.qty_mt is None for l in lines):
                results.append(REVIEW); reasons.append(f"{lab} quantity not established"); continue
            qty = sum(l.qty_mt for l in lines)
            r, tol, calc, detail, cval = _tolerance_eval(qty, max(l.qty_res_mt for l in lines), cv, mat, lab, len(groups))
            left = _invoice_qty_value(iv) if len(groups) == 1 else computed_value(
                lab, f"invoiced quantity for {mat.description}", round(qty, 6), "MT", f"sum of {len(lines)} line(s)")
            comps.append(cmp(f"{lab} quantity vs {clab} quantity", left, cval, tol, calc, r, detail))
            results.append(r)
            if r != PASS or "partial" in detail:
                reasons.append(f"{lab} vs {clab}: {calc}; {detail}")
        # BL net weight against the contract (only when the shipment is a single contracted material)
        if bl["net"] is not None:
            if len(groups) == 1:
                mat = next(iter(groups.values()))[0]
                r, tol, calc, detail, cval = _tolerance_eval(bl["net"], bl["net_res"], cv, mat, "BL", 1)
                if r == PASS and bl["net_is_gross"]:
                    r, detail = PASS_NOTE, f"{detail}; BL gross used as net ({bl['net_is_gross']})"
                comps.append(cmp(f"BL net weight vs {clab} quantity", bl["net_value"], cval, tol, calc, r, detail))
                results.append(r)
                if r != PASS:
                    reasons.append(f"BL vs {clab}: {calc}; {detail}")
            else:
                reasons.append(f"BL weight not split by material; BL vs {clab} compared through the invoice lines only")
        if len(cv.materials) > 1 and cv.partial_allowed is False and len(groups) < len(cv.materials):
            results.append(REVIEW)
            reasons.append(f"{lab} covers {len(groups)} of {len(cv.materials)} contracted materials while partial "
                           f"shipment is not allowed; confirm the shipment split was agreed")
    res = worst(results)
    if res in (PASS, PASS_NOTE):
        reasons.insert(0, "All quantities are within the contractual quantity and tolerance")
    return _result("QTY.CONTRACT", title, ref, "TOLERANCE", res, " | ".join(reasons), comps=comps)


def _price_value(lab: str, line: Line, currency: str | None) -> dict:
    return value(lab, "invoice unit price", line.fields.get("unit_price") or line.fields.get("amount"),
                 round(line.price_per_mt, 6) if line.price_per_mt is not None else None,
                 f"{currency or '?'}/MT", currency=currency, price_basis=line.price_basis)


def _contract_price_value(clab: str, mat: Material, currency: str | None) -> dict:
    return value(clab, f"contract price ({mat.description})", mat.fields.get("unit_price"), mat.price_per_mt,
                 f"{currency or '?'}/MT", currency=currency, price_basis=mat.price_basis,
                 price_type=mat.price_type)


EXCHANGE_NOTE = "Exchange-based pricing accepted; external market benchmark not independently verified."


def _formula_value(clab: str, mat: Material) -> dict:
    return {"doc": clab, "role": f"contract pricing formula ({mat.description})",
            "normalized_value": mat.describe_pricing() or None, "unit": "formula",
            "raw_wording": " & ".join(f.text for f in mat.formulas)[:300] or None,
            "extraction_method": "deterministic_section + formula parse"}


def check_contract_price(ctx: Ctx, ref: str) -> CheckResult:
    """PC vs PI and SC vs SI only. Purchase and sales prices are never compared with each other."""
    from .pricing import compare, invoice_formula
    title = "Invoiced price terms comply with the corresponding contract (fixed price or exchange formula)"
    results, reasons, comps = [], [], []
    for iv, cv in ((ctx.pi, ctx.pc), (ctx.si, ctx.sc)):
        if not iv or not cv:
            results.append(REVIEW); reasons.append("invoice or contract missing"); continue
        lab, clab = iv.doc.short, cv.doc.short
        inv_f = invoice_formula("\n".join(p.text for p in iv.doc.pages))
        for line in iv.lines:
            mat, how = match_material(line, cv.materials)
            if mat is None:
                results.append(REVIEW); reasons.append(f"{lab} line '{line.description}': {how}"); continue
            left = _price_value(lab, line, iv.currency)
            name = f"{lab} unit price vs {clab} price"
            if mat.exchange_priced:
                if mat.formula is None:
                    results.append(REVIEW)
                    reasons.append(f"{clab} prices '{mat.description}' by a formula whose terms could not be read")
                    comps.append(cmp(name, left, _contract_price_value(clab, mat, cv.currency), "-",
                                     "contract formula not readable", REVIEW)); continue
                right = _formula_value(clab, mat)
                if inv_f is not None:
                    candidates = [compare(f, inv_f) for f in mat.formulas]
                    diffs = [] if any(not d for d in candidates) else min(candidates, key=len)
                    r = MISMATCH if diffs else PASS_NOTE
                    comps.append(cmp(f"{lab} pricing formula vs {clab} pricing formula",
                                     {"doc": lab, "role": "invoice pricing formula", "normalized_value": inv_f.describe(),
                                      "unit": "formula", "raw_wording": inv_f.text[:200]}, right,
                                     "explicitly stated formula elements must agree",
                                     "; ".join(diffs) or "exchange, metal, benchmark and factors agree", r,
                                     "formula element differs from the contract" if diffs else EXCHANGE_NOTE))
                    results.append(r)
                    if diffs:
                        reasons.append(f"{lab} formula differs from {clab}: {'; '.join(diffs)}")
                else:
                    price = f"{line.price_per_mt:,.2f} {iv.currency or ''}/MT" if line.price_per_mt else "a final price"
                    comps.append(cmp(name, left, right, "not applicable (exchange-linked price)",
                                     f"{lab} declares {price}; {clab}: {mat.describe_pricing()}", PASS_NOTE,
                                     EXCHANGE_NOTE))
                    results.append(PASS_NOTE)
                continue
            right = _contract_price_value(clab, mat, cv.currency)
            if mat.price_per_mt is None or line.price_per_mt is None:
                results.append(REVIEW); reasons.append(f"{lab}: price not established")
                comps.append(cmp(name, left, right, "-", "not computable", REVIEW)); continue
            if iv.currency and cv.currency and iv.currency != cv.currency:
                results.append(MISMATCH); reasons.append(f"{lab} in {iv.currency} but {clab} in {cv.currency}")
                comps.append(cmp(name, left, right, "-", "currencies differ", MISMATCH)); continue
            ptol = cv.price_tol_pct
            cp, ip = dec(mat.price_per_mt), dec(round(line.price_per_mt, 6))
            allowed = cp * dec(ptol) / 100 if ptol else Decimal("0.005")
            toltxt = (f"±{ptol:g}% price tolerance stated in {clab} (±{allowed:,.2f}/MT)" if ptol
                      else "none stated (exact, ±0.005 for rounding)")
            diff = ip - cp
            calc = f"{ip:,.4f} - {cp:,.4f} = {diff:+,.4f} per MT"
            r = PASS if abs(diff) <= allowed else MISMATCH
            results.append(r)
            comps.append(cmp(name, left, right, toltxt, calc, r, "" if r == PASS else "price differs from the contract"))
            if r == MISMATCH:
                reasons.append(f"{lab} {ip:,.2f}/MT ≠ {clab} {cp:,.2f}/MT")
    res = worst(results)
    if res == PASS:
        reasons.insert(0, "Invoice unit prices equal the contract prices")
    elif res == PASS_NOTE:
        reasons.insert(0, EXCHANGE_NOTE)
    return _result("PRICE.CONTRACT", title, ref, "NORMALIZED", res, " | ".join(reasons), comps=comps)


def check_expected_amount(ctx: Ctx, ref: str) -> CheckResult:
    """Recompute, in Decimal: quantity x CONTRACT unit price = expected total; compare with the invoiced total.
    For exchange-priced goods the document-declared unit price is used and labelled as such."""
    title = "Invoiced total = quantity x contract unit price (independent recalculation)"
    results, reasons, comps = [], [], []
    bl = _bl_weights(ctx)
    for iv, cv in ((ctx.pi, ctx.pc), (ctx.si, ctx.sc)):
        if not iv or not cv:
            results.append(REVIEW); reasons.append("invoice or contract missing"); continue
        lab, clab = iv.doc.short, cv.doc.short
        cur = iv.currency or cv.currency
        parts, expected, problem, declared = [], Decimal(0), None, False
        for line in iv.lines:
            mat, how = match_material(line, cv.materials)
            if mat is None:
                problem = f"{lab} line '{line.description}': {how}"; break
            if line.qty_mt is None:
                problem = f"{lab}: quantity not established"; break
            if mat.exchange_priced:
                if line.price_per_mt is None:
                    problem = f"{lab}: no declared unit price for exchange-priced '{mat.description}'"; break
                price, src, declared = dec(round(line.price_per_mt, 6)), f"{lab}-declared price (exchange-linked)", True
                pval = _price_value(lab, line, iv.currency)
            elif mat.price_per_mt is None:
                problem = f"{clab}: contract price not established"; break
            else:
                price, src, pval = dec(mat.price_per_mt), f"{clab} price", _contract_price_value(clab, mat, cv.currency)
            amt = dec(round(line.qty_mt, 6)) * price
            expected += amt
            parts.append(computed_value(lab, f"line: {line.description}", float(money(amt)), cur or "?",
                                        f"{line.qty_mt:.4f} MT x {price:,.2f} {cur or ''}/MT ({src})",
                                        [_invoice_qty_value(iv) if len(iv.lines) == 1 else
                                         value(lab, "line quantity", line.fields.get("quantity"), line.qty_mt, "MT", line.basis),
                                         pval]))
        invoiced = dec(iv.goods_value)
        gross_f = iv.doc.fields.get("gross_amount")
        right = (value(lab, "invoiced goods value (before deductions)", gross_f, iv.goods_value, cur, currency=cur)
                 if iv.gross is not None else
                 computed_value(lab, "invoiced goods value (sum of line amounts)", iv.goods_value, cur or "?",
                                "sum of printed line amounts",
                                [value(lab, "line amount", l.fields.get("amount"), l.amount, cur) for l in iv.lines]))
        name = f"{lab}: quantity x {clab} price vs {lab} invoiced total"
        if problem:
            results.append(REVIEW); reasons.append(problem)
            comps.append(cmp(name, computed_value(lab, "expected total", None, cur or "?", "not computable"), right,
                             "-", problem, REVIEW))
            continue
        if invoiced is None:
            results.append(REVIEW); reasons.append(f"{lab} invoiced total not established"); continue
        expected = money(expected)
        ptol = cv.price_tol_pct
        rounding = CENT * max(1, len(iv.lines))
        allowed = money(expected * dec(ptol) / 100) + rounding if ptol and not declared else rounding
        toltxt = (f"±{ptol:g}% contract price tolerance (±{allowed:,.2f})" if ptol and not declared
                  else f"rounding only (±{rounding:.2f} {cur or ''})")
        left = computed_value(lab, "expected total", float(expected), cur or "?",
                              " + ".join(p["computation"] for p in parts), parts)
        diff = invoiced - expected
        calc = f"invoiced {invoiced:,.2f} - expected {expected:,.2f} = {diff:+,.2f} {cur or ''}"
        if abs(diff) <= allowed:
            r = PASS_NOTE if declared else PASS
            detail = ("quantity x declared exchange-linked price = invoiced total; " + EXCHANGE_NOTE if declared
                      else "invoiced total equals quantity x contract price")
        else:
            r = MISMATCH
            price_differs = (not declared) and any(
                l.price_per_mt is not None and m is not None and m.price_per_mt is not None
                and abs(l.price_per_mt - m.price_per_mt) > 0.005
                for l in iv.lines for m in [match_material(l, cv.materials)[0]])
            detail = (f"invoiced total differs from the calculated total by {diff:+,.2f} {cur or ''}"
                      + (" (the invoice unit price differs from the contract; see PRICE.CONTRACT)" if price_differs else ""))
            if any(l.ocr_doubt or l.reconciled is False for l in iv.lines) and ctx.is_ocr(iv.doc.doc_type):
                r, detail = REVIEW, detail + "; an invoice line does not reconcile on an OCR'd page, so the printed values are uncertain"
        results.append(r)
        comps.append(cmp(name, left, right, toltxt, calc, r, detail))
        if r not in (PASS, PASS_NOTE):
            reasons.append(f"{lab}: {calc}; {detail}")
        # The same calculation on the shipped (BL net) quantity: catches invoicing more than was shipped.
        if bl["net"] is not None and len(iv.lines) == 1 and parts and not declared:
            mat = match_material(iv.lines[0], cv.materials)[0]
            cp = dec(mat.price_per_mt)
            exp_bl = money(dec(round(bl["net"], 6)) * cp)
            res_amt = money(cp * dec(max(bl["net_res"], iv.qty_res_mt)) / 2) + rounding
            d2 = invoiced - exp_bl
            r2 = PASS if abs(d2) <= res_amt else MISMATCH
            if r2 == MISMATCH and (ctx.is_ocr(BILL_OF_LADING) and abs(bl["net"] - (iv.qty_mt or 0)) < 0.1):
                r2 = REVIEW
            comps.append(cmp(f"{lab}: BL net weight x {clab} price vs {lab} invoiced total",
                             computed_value("BL", "expected total on shipped net weight", float(exp_bl), cur or "?",
                                            f"{bl['net']:.4f} MT x {cp:,.2f} {cur or ''}/MT",
                                            [bl["net_value"], _contract_price_value(clab, mat, cv.currency)]),
                             right, f"rounding of the printed weights (±{res_amt:,.2f})",
                             f"invoiced {invoiced:,.2f} - expected {exp_bl:,.2f} = {d2:+,.2f}", r2,
                             "" if r2 == PASS else "invoiced value does not match the shipped net weight at the contract price"))
            results.append(r2)
            if r2 != PASS:
                reasons.append(f"{lab} vs BL-based value: {d2:+,.2f}")
    res = worst(results)
    if res == PASS:
        reasons.insert(0, "Invoiced totals equal quantity x contract price")
    elif res == PASS_NOTE:
        reasons.insert(0, "Invoiced totals reconcile with quantity x price; " + EXCHANGE_NOTE)
    return _result("AMOUNT.EXPECTED", title, ref, "ARITHMETIC", res, " | ".join(reasons), comps=comps)


def commercial_summary(ctx: Ctx) -> list[dict]:
    """One row per stated quantity/price, for the human-readable report."""
    rows = []
    for cv in (ctx.pc, ctx.sc):
        if not cv:
            continue
        for m in cv.materials:
            q, p = m.fields.get("quantity"), m.fields.get("unit_price")
            rows.append({"doc": cv.doc.short, "item": m.description, "measure": "contracted quantity",
                         "raw_quantity": q.raw_value if q else None, "quantity_mt": m.qty_mt,
                         "tolerance": (m.fields["tolerance"].raw_value if m.fields.get("tolerance") else
                                       f"{cv.total_tol_pct:g}%" if cv.total_tol_pct is not None else None),
                         "raw_price": p.raw_value if p else m.price_type, "price_per_mt": m.price_per_mt,
                         "price_basis": m.price_basis, "currency": cv.currency, "amount": None,
                         "page": q.page if q else None})
    for iv in (ctx.pi, ctx.si):
        if not iv:
            continue
        for l in iv.lines:
            q, p = l.fields.get("quantity"), l.fields.get("unit_price")
            rows.append({"doc": iv.doc.short, "item": l.description, "measure": BASIS_TEXT.get(l.basis, l.basis),
                         "raw_quantity": " ".join(x for x in (q.raw_value if q else None,
                                                              l.fields["quantity_unit"].raw_value if l.fields.get("quantity_unit") else None) if x),
                         "quantity_mt": l.qty_mt, "tolerance": None, "raw_price": p.raw_value if p else None,
                         "price_per_mt": l.price_per_mt, "price_basis": l.price_basis, "currency": iv.currency,
                         "amount": l.amount, "page": q.page if q else None})
    bl = _bl_weights(ctx)
    for kind in ("net", "gross"):
        v = bl[f"{kind}_value"]
        if v:
            rows.append({"doc": "BL", "item": None, "measure": f"{kind} weight", "raw_quantity": v.get("raw_value"),
                         "quantity_mt": v.get("normalized_value"), "tolerance": None, "raw_price": None,
                         "price_per_mt": None, "price_basis": None, "currency": None, "amount": None, "page": v.get("page")})
    if bl["count"]:
        rows.append({"doc": "BL", "item": None, "measure": "package count (not a weight)",
                     "raw_quantity": bl["count"].get("raw_value"), "quantity_mt": None, "tolerance": None, "raw_price": None,
                     "price_per_mt": None, "price_basis": None, "currency": None, "amount": None, "page": bl["count"].get("page")})
    return rows


def transaction_summary(ctx: Ctx, checks: list[CheckResult]) -> dict:
    """Purchase side, sales side and physical reconciliation at a glance, for report sections B-E."""
    from .commodity import describe
    by_id = {c.check_id: c for c in checks}
    status_rank = {PASS: 0, PASS_NOTE: 1, REVIEW: 2, MISMATCH: 3, "FAIL": 4}

    def side_result(prefix: str) -> str:
        rs = [cp["result"] for cid in ("QTY.CONTRACT", "PRICE.CONTRACT", "AMOUNT.EXPECTED")
              for cp in (by_id[cid].comparisons if cid in by_id else [])
              if cp.get("comparison", "").startswith(prefix)]
        return max(rs, key=status_rank.get) if rs else REVIEW

    def side(iv: InvoiceView | None, cv: ContractView | None, lab: str, clab: str) -> dict:
        if not iv or not cv:
            return {"available": False}
        mats = cv.materials
        pay = ctx.f(cv.doc.doc_type, "payment_clause")
        return {
            "available": True,
            "commodity": "; ".join(m.description or "?" for m in mats),
            "grade": ", ".join(sorted({g for m in mats for g in describe(m.description).grades})) or "-",
            "contract_quantity": "; ".join(f"{m.qty_mt:g} MT" + (f" ±{m.tol_pct:g}%" if m.tol_pct is not None else
                                                                   f" ±{cv.total_tol_pct:g}%" if cv.total_tol_pct is not None else "")
                                           for m in mats if m.qty_mt is not None) or "-",
            "invoice_quantity": f"{iv.qty_mt:.4f} MT" if iv.qty_mt is not None else "-",
            "price_terms": "; ".join((f"exchange formula: {m.describe_pricing()}" if m.formulas else
                                      "exchange formula (not readable)" if m.exchange_priced else
                                      f"fixed {m.price_per_mt:,.2f} {cv.currency or ''}/MT" if m.price_per_mt else "-")
                                     for m in mats),
            "invoice_price": "; ".join(f"{l.price_per_mt:,.2f}/MT" for l in iv.lines if l.price_per_mt) or "-",
            "currency": iv.currency or cv.currency or "-",
            "advance_deducted": f"{iv.advance:,.2f}" if iv.advance else "none",
            "gross_amount": f"{iv.goods_value:,.2f}" if iv.goods_value is not None else "-",
            "net_payable": f"{(iv.net if iv.net is not None else iv.goods_value):,.2f}"
                           if (iv.net is not None or iv.goods_value is not None) else "-",
            "payment_terms": (pay.raw_value[:160] + "...") if pay and pay.raw_value and len(pay.raw_value) > 160
                             else (pay.raw_value if pay else "-"),
            "result": side_result(lab),
        }

    physical = []
    for cid, label in (("GOODS.DESCRIPTION", "Commodity identity / ISRI grade"), ("QUALITY.RECOVERY", "Quality / physical recovery"),
                       ("QTY.SHIPPED", "Quantity: invoices vs BL"), ("QTY.CONTRACT", "Quantity within contract tolerance"),
                       ("QTY.MEASURES", "Weight measures (net / gross)"), ("GOODS.HS", "HS codes"),
                       ("REF.BL", "BL reference"), ("SHIP.VESSEL", "Vessel / voyage"), ("ROUTING.PORTS", "Ports"),
                       ("SHIP.DATE", "Shipment date"), ("PARTIES", "Parties (by contractual / shipping role)")):
        if cid in by_id:
            physical.append({"aspect": label, "check_id": cid, "result": by_id[cid].result, "reason": by_id[cid].reason})
    pricing = {"exchange_priced": any(m.exchange_priced for cv in (ctx.pc, ctx.sc) if cv for m in cv.materials),
               "price_check": by_id.get("PRICE.CONTRACT").to_dict() if "PRICE.CONTRACT" in by_id else None,
               "amount_check": by_id.get("AMOUNT.EXPECTED").to_dict() if "AMOUNT.EXPECTED" in by_id else None}
    return {"purchase": side(ctx.pi, ctx.pc, "PI", "PC"), "sales": side(ctx.si, ctx.sc, "SI", "SC"),
            "physical": physical, "pricing": pricing}
