"""Canonical trade view built from extracted fields.

Turns per-document raw fields into comparable quantities:
  invoice lines  -> qty in MT, price in USD/MT, amount (arithmetic-reconciled)
  contract terms -> per-material qty/tolerance/price, per-shipment lot, partial shipment
Nothing here decides PASS/FAIL; it only normalises and records issues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from decimal import ROUND_HALF_UP, Decimal

from . import normalize as N
from .models import AMBIGUOUS, FOUND, Document, Field
from .pricing import Formula, parse_formula, split_formulas

CENT = Decimal("0.01")


def dec(x: float | int | str | None) -> Decimal | None:
    """Decimal from a parsed number, without binary-float artefacts (all money maths uses this)."""
    return None if x is None else Decimal(str(x))


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _num_readings(f: Field | None, locale: str | None) -> list[float]:
    """All plausible numeric readings of a field (one, or two when ambiguous)."""
    if not f or f.raw_value is None or f.status not in (FOUND, AMBIGUOUS):
        return []
    p = N.parse_number(f.value, locale)
    return [v for v in (p.value, p.alt) if v is not None]


def _close(a: float, b: float, rel: float = 2e-4, abs_: float = 0.02) -> bool:
    return abs(a - b) <= max(abs_, rel * max(abs(a), abs(b)))


COUNT_UNITS = r"\b(PCS|PIECES?|BALES?|BAGS?|PACKAGES?|PKGS?|BUNDLES?|UNITS?|CONTAINERS?|NOS|COILS?|DRUMS?)\b"


def decimals(raw: str | None, locale: str | None) -> int:
    """Number of decimals actually printed (26.0015 -> 4, '75740 KGS' -> 0, '26.001,5' EU -> 1)."""
    m = re.search(r"\d(?:\d|[.,]\s?(?=\d))*", raw or "")
    if not m:
        return 0
    tok = re.sub(r"\s", "", m.group(0))
    p = N.parse_number(tok, locale)
    tail = re.split(r"[.,]", tok)[-1] if re.search(r"[.,]", tok) else ""
    digits = re.sub(r"\D", "", tok)
    if tail and p.value is not None and abs(int(digits) / 10 ** len(tail) - p.value) < 1e-9:
        return len(tail)
    return 0


def resolution_mt(raw: str | None, unit: str | None, locale: str | None) -> float:
    """Smallest step the printed value can express, in MT (used as rounding tolerance)."""
    step = 10 ** -decimals(raw, locale)
    return N.to_mt(step, unit) or step


def one_digit_apart(raw: str | None, implied: float) -> bool:
    """Does `implied`, printed with raw's decimals, differ from raw in exactly one digit?"""
    digits = re.sub(r"[^\d]", "", raw or "")
    m = re.search(r"[.,](\d+)\D*$", raw or "")
    dec = len(m.group(1)) if m else 0
    alt = re.sub(r"[^\d]", "", f"{implied:.{dec}f}")
    return len(digits) == len(alt) and sum(a != b for a, b in zip(digits, alt)) == 1


@dataclass
class Line:
    description: str | None
    hs: str | None
    qty_mt: float | None
    price_per_mt: float | None
    amount: float | None
    reconciled: bool | None          # qty x price == amount ? (None = not checkable)
    notes: list[str] = field(default_factory=list)
    ocr_doubt: bool = False          # mismatch explainable by one misread digit
    fields: dict[str, Field] = field(default_factory=dict)
    qty_unit: str | None = None      # MT | KG | COUNT | None
    qty_res_mt: float = 0.001        # printed precision, in MT
    basis: str = "WEIGHT"            # NET | GROSS | WEIGHT (net/gross not stated) | COUNT
    basis_evidence: str = ""
    price_basis: str | None = None   # "per MT" | "per KG" | "derived (amount / quantity)"
    unit_inferred: str | None = None # why the unit was inferred when the document prints none


@dataclass
class InvoiceView:
    doc: Document
    lines: list[Line]
    qty_mt: float | None
    gross: float | None
    advance: float | None
    net: float | None
    words_amount: float | None
    issues: list[str] = field(default_factory=list)
    currency: str | None = None
    net_weight_mt: float | None = None
    gross_weight_mt: float | None = None
    qty_res_mt: float = 0.001
    basis: str = "WEIGHT"

    @property
    def goods_value(self) -> float | None:
        """Invoiced value of goods before deductions: printed gross amount, else sum of lines."""
        if self.gross is not None:
            return self.gross
        amts = [l.amount for l in self.lines]
        return sum(amts) if amts and None not in amts else None


def _unit_for(item: dict[str, Field]) -> str | None:
    """MT / KG for weights, COUNT for piece counts, None if not stated."""
    for key in ("quantity_unit", "quantity"):
        f = item.get(key)
        if f and f.raw_value:
            u = N.normalize_weight_unit(f.raw_value)
            if u:
                return u
            if re.search(COUNT_UNITS, f.raw_value.upper()):
                return "COUNT"
    return None


def _basis_from_context(doc: Document, f: Field | None) -> tuple[str | None, str]:
    """NET / GROSS from the label on the quantity's line or the column header above it."""
    if not f or not f.page or not f.source_text:
        return None, ""
    lines = doc.pages[f.page - 1].text.splitlines()
    idx = next((i for i, l in enumerate(lines) if N.norm_space(l) == f.source_text), None)
    if idx is None:
        return None, ""
    for i in range(idx, max(-1, idx - 4), -1):
        u = lines[i].upper()
        net, gross = re.search(r"\bNET\b|N\.\s*W|NETT", u), re.search(r"GROSS|G\.\s*W", u)
        if net and not gross:
            return "NET", N.norm_space(lines[i])
        if gross and not net:
            return "GROSS", N.norm_space(lines[i])
    return None, ""


def build_line(item: dict[str, Field], doc: Document) -> Line:
    loc = doc.number_locale
    qf, pf, af = item.get("quantity"), item.get("unit_price"), item.get("amount")
    desc = item.get("description")
    hs = item.get("hs_code")
    unit = _unit_for(item)
    line = Line(desc.normalized_value if desc and desc.raw_value else None,
                N.norm_hs(hs.raw_value) if hs and hs.raw_value else None,
                None, None, None, None, fields={k: v for k, v in item.items() if v.raw_value})
    line.qty_unit = unit
    if qf and qf.raw_value:
        line.qty_res_mt = resolution_mt(qf.value, unit if unit in ("MT", "KG") else None, loc)
    if unit == "COUNT":
        line.basis = "COUNT"
        line.notes.append(f"quantity '{qf.raw_value if qf else ''}' is a count of pieces/packages, not a weight; "
                          f"it is not compared with weights")
        unit = None
    else:
        b, why = _basis_from_context(doc, qf)
        if b:
            line.basis, line.basis_evidence = b, why
    qs, ps, as_ = _num_readings(qf, loc), _num_readings(pf, loc), _num_readings(af, loc)
    if not qs:
        line.amount = as_[0] if as_ else None
        line.notes.append("quantity not found")
        return line
    if unit is None and line.basis != "COUNT":
        line.notes.append("quantity unit not stated; cannot convert to MT")

    if qs and ps and as_:
        for q in qs:
            for p in ps:
                for a in as_:
                    if _close(q * p, a):
                        line.reconciled, line.amount = True, a
                        if unit:
                            line.qty_mt = N.to_mt(q, unit)
                            line.price_per_mt = p / N.to_mt(1, unit)
                            line.price_basis = f"per {unit} (quantity in {unit} x price = amount)"
                        if (q, p, a) != (qs[0], ps[0], as_[0]):
                            line.notes.append(f"ambiguous number resolved by arithmetic: {q} x {p} = {a}")
                        return line
        line.reconciled, line.amount = False, as_[0]
        line.notes.append(f"quantity x unit price does not equal amount ({qs[0]} x {ps[0]} = "
                          f"{qs[0] * ps[0]:,.2f} vs {as_[0]:,.2f})")
        implied = as_[0] / ps[0] if ps[0] else None
        if implied and one_digit_apart(qf.value, implied):
            line.ocr_doubt = True
            line.notes.append(f"amount / price implies quantity {implied:.4f}, one digit away from the "
                              f"printed '{qf.raw_value}': likely a single misread digit")
        if unit:
            line.qty_mt, line.price_per_mt = N.to_mt(qs[0], unit), ps[0] / N.to_mt(1, unit)
            line.price_basis = f"per {unit} (assumed from quantity unit; arithmetic does not reconcile)"
        return line
    q = qs[0]
    line.qty_mt = N.to_mt(q, unit) if unit else None
    if as_:
        line.amount = as_[0]
        if not ps and unit:
            line.price_per_mt = as_[0] / line.qty_mt if line.qty_mt else None
            line.price_basis = "derived (amount / quantity)"
            line.notes.append("unit price not printed; derived as amount / quantity")
    elif ps and unit:
        line.price_per_mt = ps[0] / N.to_mt(1, unit)
        line.price_basis = f"per {unit} (assumed from quantity unit)"
    if any(f and f.status == AMBIGUOUS for f in (qf, pf, af)):
        line.notes.append("a number on this line is ambiguous and could not be confirmed arithmetically")
        line.reconciled = False
    return line


def build_invoice(doc: Document) -> InvoiceView:
    lines = [build_line(i, doc) for i in doc.line_items]
    loc = doc.number_locale

    def val(name):
        r = _num_readings(doc.fields.get(name), loc)
        f = doc.fields.get(name)
        return r[0] if r and f and f.status == FOUND else None

    # No quantity column, but an explicitly labelled "Net Weight" total (e.g. "Net Weight: 99.111 MT"):
    # that labelled net weight is the invoiced quantity of a single-line invoice.
    nw_mt, nw_res = weight_mt(doc, "net_weight")
    if len(lines) == 1 and lines[0].qty_mt is None and lines[0].basis != "COUNT" and nw_mt is not None:
        l = lines[0]
        l.qty_mt, l.qty_unit, l.qty_res_mt, l.basis = nw_mt, "MT", nw_res, "NET"
        l.fields["quantity"] = doc.fields["net_weight"]
        l.basis_evidence = doc.fields["net_weight"].source_text or ""
        l.notes = [n for n in l.notes if n != "quantity not found"]
        l.notes.append("no quantity column; invoiced quantity taken from the labelled net weight")
        if l.price_per_mt is None and l.amount is not None:
            l.price_per_mt, l.price_basis = l.amount / nw_mt, "derived (amount / net weight)"
            l.notes.append("unit price not printed; derived as amount / net weight")
    qty = sum(l.qty_mt for l in lines) if lines and all(l.qty_mt is not None for l in lines) else None
    words = doc.fields.get("amount_in_words")
    iv = InvoiceView(doc, lines, qty, val("gross_amount"), val("advance_deducted"), val("net_amount_payable"),
                     N.words_to_amount(words.raw_value) if words and words.raw_value else None)
    cur = doc.fields.get("currency")
    iv.currency = N.norm_currency(cur.raw_value) if cur and cur.status == FOUND else None
    iv.net_weight_mt, _ = weight_mt(doc, "net_weight")
    iv.gross_weight_mt, _ = weight_mt(doc, "gross_weight")
    iv.qty_res_mt = max((l.qty_res_mt for l in lines), default=0.001)
    bases = {l.basis for l in lines}
    iv.basis = bases.pop() if len(bases) == 1 else "MIXED" if bases else "WEIGHT"
    # An explicitly labelled net weight equal to the invoiced quantity settles the basis.
    if qty is not None and iv.net_weight_mt is not None and abs(qty - iv.net_weight_mt) <= iv.qty_res_mt:
        iv.basis = "NET"
    if not lines:
        iv.issues.append("no line items extracted")
    return iv


def weight_mt(doc: Document, name: str) -> tuple[float | None, float]:
    """A label-confirmed net/gross weight field in MT, with its printed precision."""
    f = doc.fields.get(name)
    if not f or not f.raw_value or f.label_confirmed is False or f.page is None:
        return None, 0.001
    uf = doc.fields.get(f"{name}_unit")
    unit = N.normalize_weight_unit(f.value) or N.normalize_weight_unit(uf.raw_value if uf else None)
    p = N.parse_number(f.value, doc.number_locale)
    if unit is None or p.value is None:
        return None, 0.001
    return N.to_mt(p.value, unit), resolution_mt(f.value, unit, doc.number_locale)


# --------------------------------------------------------------------------- contracts

@dataclass
class Material:
    description: str | None
    hs: str | None
    qty_mt: float | None
    tol_pct: float | None
    price_per_mt: float | None
    price_type: str | None
    notes: list[str] = field(default_factory=list)
    fields: dict[str, Field] = field(default_factory=dict)
    price_basis: str | None = None
    formulas: list[Formula] = field(default_factory=list)   # exchange-linked pricing (one per lot when several)

    @property
    def formula(self) -> Formula | None:
        return self.formulas[0] if self.formulas else None

    @property
    def exchange_priced(self) -> bool:
        return bool(self.formulas) or bool(self.price_type and "FORMULA" in self.price_type)

    def describe_pricing(self) -> str:
        return " | ".join(f.describe() for f in self.formulas)


@dataclass
class ContractView:
    doc: Document
    materials: list[Material]
    total_qty_mt: float | None
    total_tol_pct: float | None
    per_shipment_mt: float | None
    partial_allowed: bool | None
    issues: list[str] = field(default_factory=list)
    currency: str | None = None
    price_tol_pct: float | None = None


def parse_tolerance(raw: str | None) -> tuple[float | None, str | None]:
    if not raw:
        return None, None
    pcts = {float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", raw)}
    if len(pcts) == 1:
        return pcts.pop(), None
    if len(pcts) > 1:
        return None, f"tolerance '{raw}' states more than one percentage"
    return None, f"tolerance '{raw}' has no percentage"


def parse_partial(raw: str | None) -> tuple[bool | None, str | None]:
    if not raw:
        return None, None
    u = raw.upper().strip(" .:-")
    if re.match(r"^\W*(NOT\s+ALLOWED|NOT\s+PERMITTED|PROHIBITED)", u):
        return False, None
    if re.match(r"^\W*(ALLOWED|PERMITTED)", u):
        return True, ("partial shipment allowed with a qualification: '" + raw + "'") if len(u) > 12 else None
    return None, f"partial-shipment wording not understood: '{raw}'"


def _price_per_mt(f: Field | None, unit_f: Field | None, locale) -> tuple[float | None, str | None]:
    r = _num_readings(f, locale)
    if not r or f.status != FOUND:
        return None, None
    unit = (unit_f.raw_value if unit_f and unit_f.raw_value else "") + " " + (f.source_text or "")
    if re.search(r"(/|PER)\s*(KG|KGS)\b", unit.upper()):
        return r[0] * 1000, "per KG (converted x1000 to per MT)"
    if re.search(r"(/|PER)\s*(MT|M/T|TON|TONNE|METRIC)", unit.upper()):
        return r[0], "per MT"
    return r[0], "per MT (assumed: contract quantities are in MT)"


def build_contract(doc: Document) -> ContractView:
    loc = doc.number_locale
    cv = ContractView(doc, [], None, None, None, None)
    for item in doc.line_items:
        q = item.get("quantity")
        unit = _unit_for(item) or "MT"
        qv = _num_readings(q, loc)
        tol, tol_issue = parse_tolerance(item.get("tolerance").value if item.get("tolerance") else None)
        pt = item.get("price_type")
        price, basis = _price_per_mt(item.get("unit_price"), item.get("price_unit"), loc)
        m = Material(item["description"].normalized_value if item.get("description") and item["description"].raw_value else None,
                     N.norm_hs(item["hs_code"].raw_value) if item.get("hs_code") and item["hs_code"].raw_value else None,
                     N.to_mt(qv[0], unit) if qv and q.status == FOUND else None,
                     tol, price, (pt.raw_value or "").upper() if pt and pt.raw_value else None,
                     fields={k: v for k, v in item.items() if v.raw_value}, price_basis=basis)
        if tol_issue:
            m.notes.append(tol_issue)
        if q and q.status == AMBIGUOUS:
            m.notes.append(f"quantity ambiguous: {q.note}")
        cv.materials.append(m)

    tq = doc.fields.get("total_quantity")
    tqv = _num_readings(tq, loc)
    unit = (doc.fields.get("total_quantity_unit").raw_value if doc.fields.get("total_quantity_unit") else None) or "MT"
    cv.total_qty_mt = N.to_mt(tqv[0], unit) if tqv and tq.status == FOUND else None
    tt = doc.fields.get("total_tolerance")
    cv.total_tol_pct, issue = parse_tolerance(tt.value if tt else None)
    if issue:
        cv.issues.append(issue)
    ps = doc.fields.get("quantity_per_shipment")
    if ps and ps.status == FOUND and ps.raw_value:
        m = re.search(r"([\d.,]+)\s*(MT|MTS|KG|KGS|TONS?)?", ps.value.upper())
        if m:
            v = N.parse_number(m.group(1), loc).value
            cv.per_shipment_mt = N.to_mt(v, m.group(2) or "MT") if v else None
    pa = doc.fields.get("partial_shipment")
    cv.partial_allowed, issue = parse_partial(pa.raw_value if pa and pa.status == FOUND else None)
    if issue:
        cv.issues.append(issue)
    cur = doc.fields.get("currency")
    cv.currency = N.norm_currency(cur.raw_value) if cur and cur.status == FOUND else None
    # Exchange-linked pricing formulas from the contract's pricing clause, one per material when numbered.
    pc = doc.fields.get("pricing_clause")
    formulas = [parse_formula(t) for t in split_formulas(pc.raw_value if pc else None)]
    priced = [m for m in cv.materials if m.price_per_mt is None or (m.price_type and "FORMULA" in m.price_type)]
    if formulas and priced:
        if len(formulas) == len(cv.materials) and len(cv.materials) > 1:
            for m, f in zip(cv.materials, formulas):
                m.formulas = [f] if m in priced else []
        else:
            for m in priced:          # e.g. one material priced per recovery lot: all formulas apply to it
                m.formulas = list(formulas)
    pt = doc.fields.get("price_tolerance")
    if pt and pt.status == FOUND and pt.raw_value:
        cv.price_tol_pct, issue = parse_tolerance(pt.value)
        if issue:
            cv.issues.append("price " + issue)
    return cv


def match_material(line: Line, materials: list[Material]) -> tuple[Material | None, str]:
    """Pick the contract material an invoice line refers to: HS code first, then
    distinguishing numbers in the description (grade 304 vs 316, recovery %)."""
    if not materials:
        return None, "contract has no materials"
    if len(materials) == 1:
        return materials[0], "single contracted material"
    cands = [m for m in materials if line.hs and m.hs and m.hs[:6] == line.hs[:6]] or materials
    if len(cands) == 1:
        return cands[0], "matched by HS code"
    nums = set(re.findall(r"\b\d{2,3}\b", (line.description or "").upper()))
    scored = []
    for m in cands:
        mnums = set(re.findall(r"\b\d{2,3}\b", (m.description or "").upper()))
        words = set(re.findall(r"[A-Z]{4,}", (m.description or "").upper())) & set(re.findall(r"[A-Z]{4,}", (line.description or "").upper()))
        scored.append((len(nums & mnums), len(words), m))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    if len(scored) > 1 and scored[0][:2] == scored[1][:2]:
        return None, "invoice line matches more than one contracted material"
    return scored[0][2], "matched by description (grade / recovery numbers)"
