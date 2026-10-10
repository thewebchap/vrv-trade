"""Verification checks. Each returns a CheckResult with evidence.

Result policy (most severe first)
  FAIL            a mandatory document (PC, PI, SC, SI, BL) is genuinely missing
  MISMATCH        evidence is sufficient and a rule is clearly violated
                  (only from deterministic comparisons on grounded values;
                   the model can never produce a MISMATCH on its own)
  REVIEW          a required condition cannot be determined: missing / ambiguous /
                  OCR-doubtful values, model errors, AI-judged differences
  PASS_WITH_NOTE  accepted with a stated, non-blocking limitation (exchange price not
                  market-verified, optional document absent, data held outside the documents)
  PASS            evidence is sufficient and the rule is satisfied
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher

from . import normalize as N
from .canonical import CENT, ContractView, InvoiceView, dec, match_material, money
from .checklist import ChecklistItem
from .config import CONFIG
from .local_model import LocalModel
from .models import (BILL_OF_LADING, FAIL, MISMATCH, FOUND, PASS, PASS_NOTE, SEVERITY, PURCHASE_CONTRACT,
                     PURCHASE_INVOICE, REVIEW, SALES_CONTRACT, SALES_INVOICE, CheckResult,
                     Document, Field)


# filename -> full text of that document; set by run_checks. Used to guard
# against extraction errors before declaring a mismatch (see compare_pairs).
_DOC_TEXTS: dict[str, str] = {}
_DOCS: dict[str, Document] = {}          # filename -> document, set by run_checks


def _field_is_ocr(f: Field) -> bool:
    d = _DOCS.get(f.document)
    if not d or not d.pages:
        return False
    page = d.pages[f.page - 1] if f.page and f.page <= len(d.pages) else d.pages[0]
    return page.method == "ocr"


def _ocr_readings(f: Field) -> set[str]:
    """Every way the OCR passes of f's page read this identifier (same modulo O/0, I/1, S/5, B/8, Z/2)."""
    d = _DOCS.get(f.document)
    target = N.norm_id(f.raw_value)
    out = {target}
    if not d or not f.page or f.page > len(d.pages):
        return out
    page = d.pages[f.page - 1]
    for text in [page.text] + list(page.alt_texts):
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9/\-. ]*[A-Za-z0-9]", text):
            n = N.norm_id(tok)
            for i in range(0, max(1, len(n) - len(target) + 1)):
                piece = n[i:i + len(target)]
                if len(piece) == len(target) and N.ids_equal_modulo_ocr(piece, target):
                    out.add(piece)
    return out


def find_identifier(doc: Document | None, target: str | None, role: str) -> Field | None:
    """Look for a known identifier (e.g. the sales contract number) in another document's text when
    the model returned no usable labelled value there. OCR'd pages also match O/0, I/1, S/5, B/8, Z/2."""
    if not doc or not target:
        return None
    norm = N.norm_id(target)
    if len(norm) < 6:
        return None
    alt = {"0": "0OQ", "O": "0OQ", "1": "1IL|", "I": "1IL|", "L": "1IL|", "5": "5S", "S": "5S",
           "8": "8B", "B": "8B", "2": "2Z", "Z": "2Z"}
    for page in doc.pages:
        ocr = page.method == "ocr"
        parts = [f"[{re.escape(alt[c])}]" if ocr and c in alt else re.escape(c) for c in norm]
        rx = re.compile(r"[\s/\-.:]*".join(parts), re.I)
        for text in [page.text] + list(page.alt_texts):
            for line in text.splitlines():
                m = rx.search(line)
                if m:
                    found = m.group(0)
                    f = Field(role, found, N.norm_space(found), document_type=doc.doc_type, document=doc.filename,
                              page=page.number, source_text=N.norm_space(line), status=FOUND,
                              extraction_method="cross-document identifier search",
                              note=f"no usable labelled value was extracted; the identifier '{target}' from the "
                                   f"related document was found printed here")
                    if N.norm_id(found) != norm:
                        f.ocr_corrections = [{"from": found, "to": target, "reason": "OCR-confusable characters; "
                                              "matches the identifier printed on the related document"}]
                    return f
    return None


def _appears_in(value: str | None, filename: str) -> bool:
    text = _DOC_TEXTS.get(filename)
    v = re.sub(r"[^A-Z0-9]", "", (value or "").upper())
    return bool(text and len(v) >= 4 and v in re.sub(r"[^A-Z0-9]", "", text.upper()))


def worst(results: list[str]) -> str:
    return max(results, key=SEVERITY.get) if results else REVIEW


@dataclass
class Ctx:
    docs: dict[str, Document]
    items: list[ChecklistItem]
    model: LocalModel
    today: date
    payment: dict = field(default_factory=dict)        # payment-term match result
    signatures: dict = field(default_factory=dict)     # doc_type -> detect_signatures() output
    pc: ContractView | None = None
    sc: ContractView | None = None
    pi: InvoiceView | None = None
    si: InvoiceView | None = None
    all_docs: list[Document] = field(default_factory=list)      # every document found, any type/status
    supporting: list[Document] = field(default_factory=list)    # identified optional/supporting documents

    def f(self, doc_type: str, name: str) -> Field | None:
        d = self.docs.get(doc_type)
        return d.fields.get(name) if d else None

    def is_ocr(self, doc_type: str) -> bool:
        d = self.docs.get(doc_type)
        return bool(d and any(p.method == "ocr" for p in d.pages))


def ev(f: Field | None, label: str | None = None) -> dict:
    if f is None:
        return {"field": label, "status": "NOT_FOUND"}
    e = f.evidence()
    if label:
        e["role"] = label
    return e


def computed(label: str, value, how: str) -> dict:
    return {"role": label, "computed_value": value, "computation": how, "extraction_method": "computed"}


# --------------------------------------------------------------------------- comparison helpers

def eq_id(a: Field, b: Field) -> tuple[str, str]:
    na, nb = N.norm_id(a.raw_value), N.norm_id(b.raw_value)
    if na == nb:
        return "EQ", "identical after removing spaces/punctuation"
    if not N.ids_equal_modulo_ocr(a.raw_value, b.raw_value):
        return "NEQ", "different identifiers"
    oa, ob = _field_is_ocr(a), _field_is_ocr(b)
    if oa != ob:
        ocr_f, nat_f = (a, b) if oa else (b, a)
        return "EQ", (f"OCR correction: '{ocr_f.raw_value}' ({ocr_f.document}, OCR) differs from '{nat_f.raw_value}' "
                      f"({nat_f.document}, native text) only in OCR-confusable characters; the native text is authoritative")
    if oa and ob:
        if nb in _ocr_readings(a) or na in _ocr_readings(b):
            return "EQ", (f"OCR correction: '{a.raw_value}' vs '{b.raw_value}' differ only in OCR-confusable characters, "
                          f"and the OCR passes of the same page themselves disagree on those characters")
        return "UNSURE", "differ only in OCR-confusable characters (O/0, I/1, S/5, B/8, Z/2), and every OCR pass agrees with itself"
    return "NEQ", "different identifiers (both native text, so not an OCR effect)"


def eq_company(a: Field, b: Field) -> tuple[str, str]:
    m = N.company_match(a.raw_value, b.raw_value)
    if m != "DIFFERENT":
        return "EQ", f"company names match ({m.lower()}) after normalising legal suffixes"
    r = SequenceMatcher(None, N.norm_company(a.raw_value), N.norm_company(b.raw_value)).ratio()
    if r >= 0.85:
        return "UNSURE", f"names are very similar ({r:.0%}); possible OCR or spelling variation"
    return "NEQ", "different company names"


def eq_text(a: Field, b: Field) -> tuple[str, str]:
    x, y = N.norm_place(a.raw_value), N.norm_place(b.raw_value)
    if x == y:
        return "EQ", "identical after normalisation"
    # vessel printed together with its voyage ("MAERSK SALINA 631W")
    ta, tb = re.findall(r"[A-Z0-9]+", (a.raw_value or "").upper()), re.findall(r"[A-Z0-9]+", (b.raw_value or "").upper())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if short and long_[:len(short)] == short and all(any(ch.isdigit() for ch in t) for t in long_[len(short):]):
        return "EQ", "same name; trailing voyage number ignored"
    r = SequenceMatcher(None, x, y).ratio()
    if r >= 0.8:
        return "UNSURE", f"very similar ({r:.0%}); possible OCR variation"
    return "NEQ", "different values"


def compare_pairs(pairs: list[tuple[str, Field | None, str, Field | None, bool]], eq, mismatch=MISMATCH,
                  ocr_docs: set[str] = frozenset()) -> tuple[str, list[dict], list[str]]:
    """pairs: (label_a, field_a, label_b, field_b, required). Returns (result, comparisons, reasons)."""
    results, comps, reasons = [], [], []
    for la, fa, lb, fb, required in pairs:
        okA, okB = fa is not None and fa.status == FOUND, fb is not None and fb.status == FOUND
        if not (okA and okB):
            detail = "; ".join(f"{l}: {(f.status + (' - ' + f.note if f.note else '')) if f else 'NOT_FOUND'}"
                               for l, f, ok in ((la, fa, okA), (lb, fb, okB)) if not ok)
            if required:
                results.append(REVIEW)
                reasons.append(f"{la} vs {lb}: cannot compare ({detail})")
            comps.append({"a": la, "b": lb, "outcome": "NOT_COMPARED", "detail": detail,
                          "required": required, "evidence": [ev(fa, la), ev(fb, lb)]})
            continue
        from .scope import ensure_comparable
        scope = ensure_comparable(fa.canonical_field, fa.document_type, fb.document_type)
        verdict, why = eq(fa, fb)
        if verdict == "NEQ" and mismatch == MISMATCH and (_appears_in(fa.raw_value, fb.document) or
                                                      _appears_in(fb.raw_value, fa.document)):
            # The other document's value is printed in this one too: the mismatch
            # is more likely an extraction error than a real discrepancy.
            verdict, why = "UNSURE", why + "; but each value also appears in the other document's text (extraction conflict)"
        if verdict == "EQ":
            r = PASS
        elif verdict == "UNSURE":
            r = REVIEW
        else:
            r = mismatch
        results.append(r)
        comps.append({"a": la, "b": lb, "a_value": fa.raw_value, "b_value": fb.raw_value, "scope": scope,
                      "outcome": verdict, "detail": why, "evidence": [ev(fa, la), ev(fb, lb)]})
        if r != PASS:
            reasons.append(f"{la} '{fa.raw_value}' vs {lb} '{fb.raw_value}': {why}")
    if not results:
        return REVIEW, comps, ["nothing could be compared"]
    return worst(results), comps, reasons


def _result(check_id, title, ref, rule_type, result, reason, evidence=None, comps=None) -> CheckResult:
    return CheckResult(check_id, title, ref, rule_type, result, reason, evidence or [], comps or [])


def _pair_result(check_id, title, ref, rule_type, pairs, eq, ok_reason, mismatch=MISMATCH) -> CheckResult:
    res, comps, reasons = compare_pairs(pairs, eq, mismatch)
    return _result(check_id, title, ref, rule_type, res, ok_reason if res == PASS else " | ".join(reasons),
                   comps=comps)


# --------------------------------------------------------------------------- AI-assisted comparison

SEMANTIC_PROMPT = """You compare two {kind} taken from different documents of the same metals trade.
A: "{a}"
B: "{b}"
{question}{context}
Consider trade synonyms (e.g. ISRI scrap grade names such as "Druid", "Twang", "Rains"), abbreviations, word order and spelling mistakes.
Different metals, different grades (e.g. 304 vs 316) or different purity are NOT the same.
Return JSON: {{"answer": "YES" | "NO" | "UNSURE", "reason": "<one sentence>"}}"""


def ai_compare(model: LocalModel, kind: str, a: str, b: str, question: str,
               context: str = "") -> tuple[str, str, str | None]:
    ctx = f"\nContext from the same document (for reference only):\n{context}" if context else ""
    resp = model.ask_json(SEMANTIC_PROMPT.format(kind=kind, a=a, b=b, question=question, context=ctx), max_tokens=200)
    if resp.data is None:
        return "ERROR", "", resp.error
    ans = str(resp.data.get("answer", "")).upper().strip()
    reason = str(resp.data.get("reason", ""))[:300]
    if ans not in ("YES", "NO", "UNSURE") or not reason:
        return "ERROR", "", f"model answer did not match the required schema: {json.dumps(resp.data)[:150]}"
    return ans, reason, None


PORT_COUNTRY_PROMPT = """Identify the country of this seaport of loading taken from a bill of lading.
Port: "{port}"
Other lines from the same bill of lading: "{context}"
Return JSON: {{"country": "<country name in English, or null if you cannot tell>", "reason": "<one sentence>"}}"""


def ai_port_country(model: LocalModel, port: str, context: str) -> tuple[str | None, str, str | None]:
    resp = model.ask_json(PORT_COUNTRY_PROMPT.format(port=port, context=context.replace("\n", " / ")), max_tokens=150)
    if resp.data is None:
        return None, "", resp.error
    country = resp.data.get("country")
    if country is not None and not isinstance(country, str):
        return None, "", "model answer did not match the required schema"
    return country, str(resp.data.get("reason", ""))[:300], None


# =========================================================================== COMMON CHECKS

def _hk_or_china(doc: Document, party: str) -> bool:
    """Is the given party (SELLER/BUYER) of this contract based in China or Hong Kong?"""
    text = "\n".join(p.text for p in doc.pages[:1]).upper()
    m = re.search(rf"(.{{0,400}})HEREINAFTER CALLED\W*{party}", text, re.S)
    span = m.group(1) if m else ""
    return bool(re.search(r"HONG KONG|\bHK\b|\bCHINA\b|\bPRC\b|N\.T\.", span))


def check_signatures(ctx: Ctx, ref: str) -> CheckResult:
    """COMMON-1: contracts signed AND stamped by both parties (a China/HK party may seal only).
    Presence is decided visually per signatory block (see signatures.py); identity/authenticity is not
    checked. Missing marks are REVIEW, never MISMATCH: absence judged by vision needs a person to confirm."""
    title = "Contracts signed and stamped by both parties (visual presence check)"
    results, evidence, reasons = [], [], []
    for dt in (PURCHASE_CONTRACT, SALES_CONTRACT):
        doc = ctx.docs.get(dt)
        sig = ctx.signatures.get(dt)
        if not doc:
            results.append(REVIEW); reasons.append(f"{dt}: document missing")
            continue
        blocks = (sig or {}).get("blocks")
        if not blocks:      # per-block check unavailable: fall back to the whole-page answer
            if not sig or sig.get("error"):
                results.append(REVIEW)
                reasons.append(f"{doc.filename}: signature check unavailable ({sig.get('error') if sig else 'not run'})")
                continue
            blocks = [{"block": p.upper(), "party": p.upper(), "page": sig["page"], "signature_present": sig[p]["signature"],
                       "stamp_present": sig[p]["stamp"], "unclear_mark": False, "detection_method": "vision on whole page",
                       "visual_description": sig.get("observations"), "region": None, "text_in_region": []}
                      for p in ("seller", "buyer")]
        for b in blocks:
            seal_only = _hk_or_china(doc, b["party"])
            ok = (b["signature_present"] and b["stamp_present"]) or (seal_only and b["stamp_present"])
            r = PASS if ok else REVIEW
            results.append(r)
            evidence.append({"document": doc.filename, "page": b["page"], "block": b["block"], "region": b.get("region"),
                             "located_by": b.get("located_by"), "result": r,
                             "signature_present": b["signature_present"], "stamp_present": b["stamp_present"],
                             "signature_basis": b.get("signature_basis"), "stamp_basis": b.get("stamp_basis"),
                             "detection_method": b["detection_method"], "visual_description": b.get("visual_description"),
                             "text_evidence": b.get("text_in_region"), "seal_only_allowed": seal_only or None,
                             "extraction_method": "visual"})
            if not ok:
                if b.get("unclear_mark"):
                    why = "a mark is visible (coloured ink / image) but it could not be classified as signature or stamp"
                else:
                    why = "no " + " or ".join(k for k, v in (("signature", b["signature_present"]),
                                                             ("stamp", b["stamp_present"])) if not v) + " detected"
                reasons.append(f"{doc.filename} p.{b['page']} {b['block'].lower()} block: {why}"
                               + (" (China/HK party: seal alone is acceptable)" if seal_only else ""))
    res = worst(results)
    n = sum(1 for e in evidence if e["result"] == PASS)
    reason = (f"Signature and stamp visibly present in all {n} signatory blocks of both contracts (presence only; "
              f"signer identity and authenticity are not verified)." if res == PASS else
              " | ".join(reasons) + ". Absence judged visually is never auto-failed; a person must look at the page.")
    return _result("SIG.CONTRACTS", title, ref, "VISION_AI", res, reason, evidence)


def check_contract_refs(ctx: Ctx, ref: str) -> CheckResult:
    title = "Invoices reference their contracts (either party's contract may be used)"
    results, comps, reasons = [], [], []
    for inv, con, side in ((PURCHASE_INVOICE, PURCHASE_CONTRACT, "purchase"), (SALES_INVOICE, SALES_CONTRACT, "sales")):
        fi, fc = ctx.f(inv, "contract_reference"), ctx.f(con, "contract_number")
        if (not fi or fi.status != FOUND) and fc and fc.status == FOUND:
            fi = find_identifier(ctx.docs.get(inv), fc.raw_value, "contract_reference") or fi
        r, c, why = compare_pairs([(f"{inv} contract ref", fi, f"{con} number", fc, True)], eq_id)
        if r == MISMATCH:
            # Checklist rule 2: the counterparty's own contract may be used instead.
            r = REVIEW
            why = [f"{inv} references '{fi.raw_value}', not {con} '{fc.raw_value}'. Checklist rule 2 allows the "
                   f"counterparty's contract, but that contract is not in the case folder, so the link cannot be verified"]
        results.append(r); comps += c; reasons += why
    res = worst(results)
    return _result("REF.CONTRACT", title, ref, "DETERMINISTIC", res,
                   "Both invoices reference the contract number of the corresponding contract." if res == PASS
                   else " | ".join(reasons), comps=comps)


def _contract_value(cv: ContractView | None) -> float | None:
    if not cv:
        return None
    vals = [m.qty_mt * m.price_per_mt for m in cv.materials if m.qty_mt and m.price_per_mt]
    return sum(vals) if vals and len(vals) == len(cv.materials) else None


def _contract_advance(ctx: Ctx, con: str) -> dict:
    """What the contract says about an advance/deposit (from the deterministic clause)."""
    from .checklist import payment_features
    clause = ctx.f(con, "payment_clause")
    if not clause or not clause.raw_value:
        return {"known": False, "clause": None}
    feats = payment_features(clause.raw_value)
    has = feats.advance != 0
    return {"known": True, "has": has, "pct": feats.advance if has else 0, "text": feats.advance_text,
            "clause": clause, "notes": feats.notes}


DEFERRED_ADVANCE = re.compile(r"ADJUSTED\s+(IN|AGAINST)\s+(THE\s+)?(LAST|FINAL|BALANCE)|DEDUCTED\s+(FROM|IN)\s+(THE\s+)?(LAST|FINAL)")


def check_advances(ctx: Ctx, ref: str) -> CheckResult:
    """Advances are validated per side, each against its own contract. Purchase and sales advances are
    independent: their amounts are never compared and one side never requires an advance on the other."""
    title = "Advance deductions consistent with the corresponding contract (each side independently)"
    results, evidence, reasons = [], [], []
    for con, inv, iv, cv in ((PURCHASE_CONTRACT, PURCHASE_INVOICE, ctx.pi, ctx.pc),
                             (SALES_CONTRACT, SALES_INVOICE, ctx.si, ctx.sc)):
        side = "purchase" if con == PURCHASE_CONTRACT else "sales"
        adv = _contract_advance(ctx, con)
        f_adv = ctx.f(inv, "advance_deducted")
        if not adv["known"] or iv is None:
            results.append(REVIEW); reasons.append(f"{side}: payment clause or invoice missing")
            continue
        evidence.append(ev(adv["clause"], f"{con} payment clause"))
        deducted = dec(iv.advance) if iv.advance else None
        if f_adv:
            evidence.append(ev(f_adv, f"{inv} advance deducted"))
        if not adv["has"]:
            if deducted:
                results.append(REVIEW)
                reasons.append(f"{side}: {inv} deducts {deducted:,.2f} but {con} provides for no advance")
            else:
                results.append(PASS); reasons.append(f"{side}: no advance in {con} and none deducted")
            continue
        if not deducted:
            if DEFERRED_ADVANCE.search(adv["clause"].raw_value.upper()):
                results.append(PASS_NOTE)
                reasons.append(f"{side}: {con} adjusts the advance/deposit on a later (last/final) shipment invoice, "
                               f"so no deduction is expected on this invoice")
            else:
                results.append(REVIEW)
                reasons.append(f"{side}: {con} provides for an advance/deposit ('{adv['text'][:70]}') but {inv} shows no "
                               f"deduction; confirm whether it is adjusted on another invoice")
            continue
        gross = dec(iv.goods_value)
        if gross is not None and deducted > gross:
            results.append(MISMATCH); reasons.append(f"{side}: advance {deducted:,.2f} exceeds the invoice value {gross:,.2f}")
            continue
        expected = []
        if adv["pct"]:
            cval = _contract_value(cv)
            if cval:
                expected.append(("contract value", money(dec(adv["pct"]) / 100 * dec(cval))))
            if gross is not None:
                expected.append(("invoice value", money(dec(adv["pct"]) / 100 * gross)))
        for label, v in expected:
            evidence.append(computed(f"{adv['pct']}% of {label}", float(v), f"{adv['pct']}% x {label}"))
        exact = [label for label, v in expected if abs(v - deducted) <= CENT * max(1, adv["pct"])]
        if exact:
            results.append(PASS_NOTE)
            reasons.append(f"{side}: deduction {deducted:,.2f} = {adv['pct']}% of the {exact[0]}")
        elif expected and deducted > max(v for _, v in expected) + CENT:
            results.append(REVIEW)
            reasons.append(f"{side}: deduction {deducted:,.2f} exceeds {adv['pct']}% of the contract/invoice value "
                           f"({', '.join(f'{v:,.2f}' for _, v in expected)}) without an explanation in the documents")
        else:
            results.append(PASS_NOTE)
            reasons.append(f"{side}: deduction {deducted:,.2f} is within the contractual advance"
                           + (f" ({', '.join(f'{adv['pct']}% of {l} = {v:,.2f}' for l, v in expected)})" if expected else
                              " (contract value not fixed, e.g. exchange-priced)"))
    res = worst(results)
    if res == PASS_NOTE:
        reasons.append("Advance amounts are taken as stated on the invoices; receipt/payment was not independently "
                       "verified (no bank confirmation in the documents)")
    return _result("ADV.DEDUCTED", title, ref, "CONDITIONAL", res, " | ".join(reasons), evidence)


def check_external(check_id: str, title: str, ref: str, needed: str) -> CheckResult:
    return _result(check_id, title, ref, "EXTERNAL_DATA", PASS_NOTE,
                   f"Not verified: requires {needed}, which is not part of the trade document package and is not "
                   f"connected to TradePilot. Confirm in the treasury/exposure records before releasing payment.")


NOTE_REF = re.compile(r"\b(DEBIT|CREDIT)\s*NOTE\b|\b[DC]/N\s*(NO|#|:)", re.I)


def check_adjustment_notes(ctx: Ctx, ref: str) -> CheckResult:
    """Debit/credit notes are supporting documents. A contract clause saying a debit note 'must be
    deducted' is conditional and does not mean one exists. Notes are only validated when supplied."""
    title = "Debit / credit note adjustments"
    notes = [d for d in ctx.supporting if d.doc_type in ("DEBIT_NOTE", "CREDIT_NOTE")]
    referenced = [(d.short, N.norm_space(l)) for d in (ctx.docs.get(PURCHASE_INVOICE), ctx.docs.get(SALES_INVOICE)) if d
                  for p in d.pages for l in p.text.splitlines() if NOTE_REF.search(l)]
    if not notes:
        if referenced:
            return _result("ADJ.NOTES", title, ref, "DETERMINISTIC", REVIEW,
                           f"An invoice refers to a debit/credit note that is not in the package: {referenced[0][1][:120]}",
                           [{"role": f"{lab} reference", "source_text": t} for lab, t in referenced])
        return _result("ADJ.NOTES", title, ref, "DETERMINISTIC", PASS_NOTE,
                       "No debit note supplied; no debit adjustment identified. No credit note supplied.")
    results, reasons, evidence = [], [], []
    numbers = [N.norm_id(_raw(d.fields.get("note_number"))) for d in notes]
    for d, num in zip(notes, numbers):
        r, why, evs = _validate_note(ctx, d)
        if num and numbers.count(num) > 1:
            r, why = MISMATCH, why + f"; note {num} appears more than once (duplicate adjustment)"
        results.append(r); reasons.append(f"{d.filename}: {why}"); evidence += evs
    return _result("ADJ.NOTES", title, ref, "DETERMINISTIC", worst(results), " | ".join(reasons), evidence)


def _validate_note(ctx: Ctx, note: Document) -> tuple[str, str, list[dict]]:
    """Match a debit/credit note to its invoice and check the invoice shows the adjustment in the
    direction implied by who issued the note."""
    kind = "debit" if note.doc_type == "DEBIT_NOTE" else "credit"
    f = note.fields
    amount_f, issuer, recipient = f.get("amount"), _raw(f.get("issuer_name")), _raw(f.get("recipient_name"))
    evidence = [ev(x, f"{kind} note {k}") for k, x in f.items() if x and x.raw_value]
    if not amount_f or amount_f.status != FOUND or amount_f.normalized_value is None:
        return REVIEW, f"{kind} note amount not readable", evidence
    amount = money(dec(amount_f.normalized_value))
    own = CONFIG.own_company
    own_issued = bool(issuer) and N.company_match(issuer, own) != "DIFFERENT"
    # which side: the counterparty named on the note
    other = recipient if own_issued else issuer
    side = None
    for inv, con in ((PURCHASE_INVOICE, PURCHASE_CONTRACT), (SALES_INVOICE, SALES_CONTRACT)):
        party = ctx.f(con, "seller_name" if inv == PURCHASE_INVOICE else "buyer_name")
        if other and party and N.company_match(other, party.raw_value) != "DIFFERENT":
            side = inv
    ref_no = N.norm_id(_raw(f.get("invoice_reference")))
    for inv in (PURCHASE_INVOICE, SALES_INVOICE):
        num = ctx.f(inv, "invoice_number")
        if ref_no and num and N.norm_id(num.raw_value) and N.norm_id(num.raw_value) in ref_no:
            side = inv
    if side is None:
        return REVIEW, f"{kind} note could not be matched to the purchase or sales invoice", evidence
    # Purchase side: a debit note WE issue (or a credit note the supplier issues) reduces what we pay;
    # a debit note the supplier issues increases it. Sales side mirrors this for what we receive.
    if side == PURCHASE_INVOICE:
        reduces = (kind == "debit" and own_issued) or (kind == "credit" and not own_issued)
    else:
        reduces = (kind == "credit" and own_issued) or (kind == "debit" and not own_issued)
    direction = "reduce" if reduces else "increase"
    inv_doc = ctx.docs.get(side)
    hits = []
    for p in (inv_doc.pages if inv_doc else []):
        for line in p.text.splitlines():
            vals = {money(dec(N.parse_number(t, inv_doc.number_locale).value)) for t in
                    re.findall(r"\d(?:\d|[.,](?=\d))*", line) if N.parse_number(t, inv_doc.number_locale).value is not None}
            if amount in vals:
                hits.append((p.number, N.norm_space(line)))
    evidence += [{"role": f"{side} line showing the amount", "page": pg, "source_text": l} for pg, l in hits]
    if not hits:
        return REVIEW, (f"{kind} note for {amount:,.2f} should {direction} the {side.split('_')[0].lower()} amount, but the "
                        f"invoice does not show this adjustment; confirm it is settled separately"), evidence
    if len(hits) > 1:
        return REVIEW, f"{kind} note amount {amount:,.2f} appears {len(hits)} times on the invoice (possible duplicate)", evidence
    line = hits[0][1].upper()
    shown_reduce = bool(re.search(r"LESS|DEDUCT|MINUS|\(|-\s*\d|DEBIT NOTE|CREDIT NOTE", line))
    shown_increase = bool(re.search(r"\bADD\b|PLUS|\+", line))
    if (reduces and shown_increase and not shown_reduce) or (not reduces and shown_reduce and not shown_increase):
        return MISMATCH, f"{kind} note should {direction} the amount, but the invoice applies it the other way", evidence
    return PASS, f"{kind} note {amount:,.2f} applied on {side} ({direction}s the amount, as expected)", evidence


def check_bank(ctx: Ctx, ref: str) -> CheckResult:
    title = "Supplier bank details on the invoice unchanged from the contract"

    def eq_acct(a: Field, b: Field) -> tuple[str, str]:
        x, y = N.norm_id(a.raw_value), N.norm_id(b.raw_value)
        if x == y or (min(len(x), len(y)) >= 8 and (x in y or y in x)):
            return "EQ", "same account (IBAN contains the account number)" if x != y else "identical"
        return eq_id(a, b)

    pairs = [("PI " + k, ctx.f(PURCHASE_INVOICE, k), "PC " + k, ctx.f(PURCHASE_CONTRACT, k), False)
             for k in ("bank_beneficiary", "bank_name", "bank_account", "bank_swift")]
    results, comps, reasons = [], [], []
    eqs = {"bank_beneficiary": eq_company, "bank_name": eq_company, "bank_account": eq_acct, "bank_swift": eq_id}
    for (la, fa, lb, fb, req), key in zip(pairs, eqs):
        r, c, why = compare_pairs([(la, fa, lb, fb, req)], eqs[key], mismatch=REVIEW)
        comps += c
        if c and c[0]["outcome"] != "NOT_COMPARED":
            results.append(r); reasons += why
    if not results:
        return _result("BANK.UNCHANGED", title, ref, "DETERMINISTIC", REVIEW,
                       "Bank details could not be compared (missing in contract or invoice).", comps=comps)
    res = worst(results)
    reason = (f"{len(results)} bank attribute(s) on the purchase invoice match the purchase contract."
              if res == PASS else "Bank details differ from the contract: " + " | ".join(reasons) +
              ". Checklist rule 5: verify the change with Trade Operations before payment.")
    return _result("BANK.UNCHANGED", title, ref, "DETERMINISTIC", res, reason, comps=comps)


def check_buyer_advance_first(ctx: Ctx, ref: str) -> CheckResult:
    title = "Buyer's advance collected before paying the supplier's advance"
    pa, sa = _contract_advance(ctx, PURCHASE_CONTRACT), _contract_advance(ctx, SALES_CONTRACT)
    evidence = [ev(x["clause"], f"{d} payment clause") for x, d in ((pa, "PC"), (sa, "SC")) if x.get("clause")]
    if not (pa["known"] and sa["known"]):
        return _result("ADV.BUYER_FIRST", title, ref, "CONDITIONAL", REVIEW, "Payment clause missing.", evidence)
    if not (pa["has"] and sa["has"]):
        who = "supplier" if not pa["has"] else "buyer"
        return _result("ADV.BUYER_FIRST", title, ref, "CONDITIONAL", PASS,
                       f"Condition not triggered: no {who} advance in the contracts.", evidence)
    f = ctx.f(SALES_INVOICE, "advance_deducted")
    if f and f.status == FOUND and ctx.si and ctx.si.advance:
        evidence.append(ev(f, "SI advance received"))
        return _result("ADV.BUYER_FIRST", title, ref, "CONDITIONAL", PASS,
                       f"Both contracts require advances; the sales invoice records the buyer's advance as received "
                       f"({ctx.si.advance:,.2f}).", evidence)
    return _result("ADV.BUYER_FIRST", title, ref, "CONDITIONAL", PASS_NOTE,
                   "Both contracts provide for advances; the sales invoice does not show the buyer's advance as received. "
                   "Checklist guidance: try to collect the buyer's advance before paying the supplier (payment status "
                   "is not in the documents).", evidence)


def check_supplier_exposure(ctx: Ctx, ref: str) -> CheckResult:
    title = "Supplier advance within exposure limit; previous advances shipped"
    pa = _contract_advance(ctx, PURCHASE_CONTRACT)
    if pa["known"] and not pa["has"]:
        return _result("EXT.EXPOSURE", title, ref, "CONDITIONAL", PASS,
                       "Condition not triggered: the purchase contract has no supplier advance.",
                       [ev(pa["clause"], "PC payment clause")])
    return check_external("EXT.EXPOSURE", title, ref,
                          "the approved exposure limit, the supplier advance ledger and the shipment status of "
                          "previous advances")


def check_gp(ctx: Ctx, ref: str, threshold: float) -> CheckResult:
    title = f"Gross profit at least {threshold:g}% (metals)"
    pi, si = ctx.pi, ctx.si
    evidence = []
    for iv, lab in ((pi, "PI"), (si, "SI")):
        if iv:
            evidence += [ev(l.fields.get("amount"), f"{lab} line amount") for l in iv.lines if l.fields.get("amount")]
    if not (pi and si) or not (pi.qty_mt and si.qty_mt):
        return _result("GP.MIN", title, ref, "NORMALIZED", REVIEW, "Purchase or sales invoice quantity/value missing.", evidence)
    if any(l.amount is None for l in pi.lines + si.lines):
        return _result("GP.MIN", title, ref, "NORMALIZED", REVIEW, "An invoice line amount is missing.", evidence)
    if any(l.reconciled is False for l in pi.lines + si.lines):
        return _result("GP.MIN", title, ref, "NORMALIZED", REVIEW,
                       "Invoice arithmetic does not reconcile, so values cannot be trusted for GP.", evidence)
    # GP is a margin threshold from the checklist, not an equality test between purchase and sales prices.
    cost = sum(dec(l.amount) for l in pi.lines) / dec(round(pi.qty_mt, 6))
    sale = sum(dec(l.amount) for l in si.lines) / dec(round(si.qty_mt, 6))
    gp = (sale - cost) / sale * 100
    evidence += [computed("purchase value per MT", float(round(cost, 4)), "sum(PI amounts) / PI quantity MT"),
                 computed("sales value per MT", float(round(sale, 4)), "sum(SI amounts) / SI quantity MT"),
                 computed("GP %", float(round(gp, 3)), "(sales - purchase) / sales x 100")]
    notes = ""
    if abs(pi.qty_mt - si.qty_mt) > 0.0005:
        notes += f" Quantities differ (PI {pi.qty_mt:.4f} MT, SI {si.qty_mt:.4f} MT); GP computed per MT."
    pt, st = N.norm_incoterm(_raw(ctx.f(PURCHASE_CONTRACT, "delivery_terms"))), N.norm_incoterm(_raw(ctx.f(SALES_CONTRACT, "delivery_terms")))
    if pt and st and pt != st:
        notes += (f" Context: the legs have different delivery terms (purchase {pt}, sales {st}), which is normal; "
                  f"GP uses invoice values and does not deduct leg-specific costs such as insurance.")
    res = PASS if gp >= dec(threshold) else MISMATCH
    return _result("GP.MIN", title, ref, "NORMALIZED", res,
                   f"GP = ({sale:,.2f} - {cost:,.2f}) / {sale:,.2f} = {gp:.2f}% "
                   f"({'meets' if res == PASS else 'is below'} the {threshold:g}% minimum).{notes}", evidence)


def _raw(f: Field | None) -> str | None:
    return f.raw_value if f and f.status == FOUND else None


# =========================================================================== PAYMENT-TERM ROW CHECKS

def check_payment_term(ctx: Ctx, ref: str) -> CheckResult:
    p = ctx.payment
    clause = ctx.f(PURCHASE_CONTRACT, "payment_clause")
    evidence = [ev(clause, "PC payment clause"), computed("clause features", p.get("features"), "deterministic keyword/percentage parse")]
    if p.get("status") == "IDENTIFIED":
        return _result("PAYTERM.MATCH", "Purchase payment term matches a METALS checklist row", ref, "DETERMINISTIC", PASS,
                       f"Purchase contract payment clause matches {p['rule_id']} on all features {p['features']}.", evidence)
    detail = f"closest row {p.get('rule_id')} differs on {p.get('differences')}" if p.get("rule_id") else "no row matched"
    notes = "; ".join(p.get("feature_notes") or [])
    return _result("PAYTERM.MATCH", "Purchase payment term matches a METALS checklist row", ref, "DETERMINISTIC", REVIEW,
                   f"Payment term not uniquely identified: {detail}. {notes}. Checks below use {p.get('rule_id')}'s "
                   f"document list; confirm the applicable payment term.", evidence)


def payment_stage(ctx: Ctx, item: ChecklistItem) -> tuple[str, str]:
    stages = item.interpretation["stages"]
    title = (_raw(ctx.f(PURCHASE_INVOICE, "document_title")) or "").upper()
    if "FULL" in stages:
        return "FULL", "single payment against documents"
    if BILL_OF_LADING in ctx.docs and "PROFORMA" not in title:
        return "BALANCE", "bill of lading and a commercial/final purchase invoice are present"
    if "PROFORMA" in title:
        return "ADVANCE", "purchase invoice is a proforma"
    return "BALANCE", "assumed balance stage"


MANDATORY_NAMES = {PURCHASE_CONTRACT: "Purchase Contract", PURCHASE_INVOICE: "Purchase Invoice",
                   SALES_CONTRACT: "Sales Contract", SALES_INVOICE: "Sales Invoice", BILL_OF_LADING: "Bill of Lading"}


def check_mandatory_docs(ctx: Ctx, ref: str) -> CheckResult:
    """All five documents are mandatory. Genuinely absent -> FAIL. Attached but unreadable or not
    confidently identified -> REVIEW (never declared absent when it may be there)."""
    title = "Mandatory documents present: PC, PI, SC, SI, BL"
    unresolved = [d for d in ctx.all_docs if d.classification_status != "IDENTIFIED" or not d.pages or d.extraction_error]
    results, reasons, evidence = [], [], []
    for t, name in MANDATORY_NAMES.items():
        mine = [d for d in ctx.all_docs if d.doc_type == t]
        ok = [d for d in mine if d.classification_status == "IDENTIFIED" and d.pages and not d.extraction_error]
        if len(ok) == 1 and len(mine) == 1:
            status, why = PASS, f"present: {ok[0].filename}"
        elif mine:
            status = REVIEW
            why = (f"MANDATORY DOCUMENT UNREADABLE / UNIDENTIFIED: {', '.join(d.filename for d in mine)} "
                   f"({'; '.join((d.extraction_error or d.classification_reason)[:90] for d in mine)})")
        elif unresolved:
            status = REVIEW
            why = (f"not identified among the attachments; {len(unresolved)} attachment(s) could not be read or classified "
                   f"({', '.join(d.filename for d in unresolved)}), so it cannot be declared missing")
        else:
            status, why = FAIL, "MISSING MANDATORY DOCUMENT"
        results.append(status)
        evidence.append({"role": name, "document": ", ".join(d.filename for d in mine) or None, "status": status, "note": why})
        if status != PASS:
            reasons.append(f"{name}: {why}")
    res = worst(results)
    return _result("DOCS.MANDATORY", title, ref, "DETERMINISTIC", res,
                   "All five mandatory documents are present and identified." if res == PASS else " | ".join(reasons),
                   evidence)


PRESENTATION_TYPES = [(r"COMMERCIAL\s+INVOICE", "INVOICE"), (r"BILLS?\s+OF\s+LADING|TRANSPORT\s+BILL", "BILL_OF_LADING"),
                      (r"CERTIFICATE\s+OF\s+ORIGIN|AIFTA", "CERTIFICATE_OF_ORIGIN"),
                      (r"QUALITY|ANALYSIS|ASSAY", "QUALITY_CERTIFICATE"),
                      (r"PACKING\s+LIST|WEIGHT", "PACKING_LIST"), (r"INSURANCE", "INSURANCE_CERTIFICATE"),
                      (r"INSPECTION|PSIC", "INSPECTION_REPORT")]


def contract_presentation_docs(doc: Document | None) -> list[tuple[str, str]]:
    """(wording, type) for each document a contract lists under 'Documents to be presented'."""
    if not doc:
        return []
    text = "\n".join(p.text for p in doc.pages)
    m = re.search(r"(?is)documents?\s+to\s+be\s+presented\s*:?(.*?)(?:original\s+documents|(?-i:\n\s*[A-Z]\.\s+[A-Z]{2})|"
                  r"beneficiary|bank\s+details|$)", text)
    if not m:
        return []
    items = re.split(r"(?:^|\s)\d{1,2}[.:)]\s+", m.group(1))
    out = []
    for it in items:
        it = N.norm_space(it)
        if len(it) < 4 or re.match(r"^[a-z]\.", it):
            continue
        t = next((t for rx, t in PRESENTATION_TYPES if re.search(rx, it.upper())), "OTHER")
        out.append((it[:80], t))
    return out[:15]


def check_supporting_docs(ctx: Ctx, ref: str) -> CheckResult:
    """Supporting documents are optional by default. Documents the contracts list for presentation are
    reported as contractual obligations, separately from ordinary optional documents."""
    title = "Additional supporting documents"
    have = {d.doc_type for d in ctx.supporting}
    evidence = [{"role": "supplied supporting document", "document": d.filename, "note": d.doc_type} for d in ctx.supporting]
    lines = []
    if ctx.supporting:
        lines.append("Supplied: " + ", ".join(f"{d.doc_type.replace('_', ' ').lower()} ({d.filename})" for d in ctx.supporting))
    else:
        lines.append("Additional supporting document not provided (optional)")
    for con in (PURCHASE_CONTRACT, SALES_CONTRACT):
        listed = contract_presentation_docs(ctx.docs.get(con))
        missing = [w for w, t in listed if t not in ("INVOICE", "BILL_OF_LADING") and t not in have]
        if missing:
            evidence.append({"role": f"{MANDATORY_NAMES[con]} presentation documents not in this set", "note": "; ".join(missing)})
            lines.append(f"{MANDATORY_NAMES[con]} lists for presentation, not in this set: {'; '.join(m[:40] for m in missing[:6])}"
                         f"{' ...' if len(missing) > 6 else ''} (contractual presentation documents; not part of the five "
                         f"mandatory documents and not required by the checklist row for this payment)")
    return _result("DOCS.SUPPORTING", title, ref, "DETERMINISTIC", PASS_NOTE, " | ".join(lines), evidence)


SUPPORTING_FOR = {"PACKING_LIST": {"PACKING_LIST", "WEIGHT_CERTIFICATE"}, "AIFTA_CERTIFICATE": {"CERTIFICATE_OF_ORIGIN"},
                  "OTHER_REQUIRED_CERTIFICATES": {"CERTIFICATE_OF_ORIGIN", "QUALITY_CERTIFICATE", "WEIGHT_CERTIFICATE",
                                                  "INSURANCE_CERTIFICATE", "INSPECTION_REPORT"},
                  "END_BUYER_QUALITY_CHECK": {"INSPECTION_REPORT", "QUALITY_CERTIFICATE"}}


def check_required_docs(ctx: Ctx, ref: str, item: ChecklistItem) -> CheckResult:
    stage, why = payment_stage(ctx, item)
    required = item.interpretation["stages"][stage]
    present, missing, ambiguous = [], [], []
    for dt in required:
        d = ctx.docs.get(dt)
        if d is None and dt in SUPPORTING_FOR:
            d = next((x for x in ctx.supporting if x.doc_type in SUPPORTING_FOR[dt]), None)
        if d is None:
            missing.append(dt)
        elif d.classification_status != "IDENTIFIED":
            ambiguous.append(f"{dt} ({d.filename}: {d.classification_status})")
        else:
            present.append(f"{dt} = {d.filename}")
    evidence = [computed("payment stage", stage, why), computed("required documents", required, f"{item.rule_id} instruction")]
    evidence += [{"role": "present", "document": p} for p in present]
    if missing or ambiguous:
        return _result("DOCS.REQUIRED", f"Documents required by {item.rule_id} ({stage.lower()} stage) are present", ref,
                       "DETERMINISTIC", REVIEW,
                       f"Required by the checklist row for this payment, not supplied: {', '.join(missing) or '-'}"
                       + (f"; not confidently identified: {', '.join(ambiguous)}" if ambiguous else "")
                       + f". The checklist row requires them for the {stage.lower()} payment.", evidence)
    return _result("DOCS.REQUIRED", f"Documents required by {item.rule_id} ({stage.lower()} stage) are present", ref,
                   "DETERMINISTIC", PASS, f"All {len(required)} required documents are present and identified.", evidence)


def check_vessel_sailed(ctx: Ctx, ref: str) -> CheckResult:
    f = ctx.f(BILL_OF_LADING, "shipped_on_board_date")
    title = "Vessel has sailed (shipped-on-board date on the BL)"
    if not f or f.status != FOUND or not f.normalized_value:
        return _result("VESSEL.SAILED", title, ref, "DETERMINISTIC", REVIEW, "No usable shipped-on-board date on the BL.", [ev(f, "BL on-board date")])
    d = date.fromisoformat(f.normalized_value)
    if d <= ctx.today:
        return _result("VESSEL.SAILED", title, ref, "DETERMINISTIC", PASS,
                       f"BL shows goods shipped on board on {d.isoformat()}, on or before {ctx.today.isoformat()}. "
                       f"(An on-board date evidences loading; live vessel tracking is not checked.)", [ev(f, "BL on-board date")])
    return _result("VESSEL.SAILED", title, ref, "DETERMINISTIC", MISMATCH,
                   f"BL on-board date {d.isoformat()} is after today ({ctx.today.isoformat()}).", [ev(f, "BL on-board date")])


# =========================================================================== "ALL DOCUMENTS MATCH"

def check_bl_refs(ctx: Ctx, ref: str) -> CheckResult:
    bl = ctx.f(BILL_OF_LADING, "bl_number")
    si, pi = ctx.f(SALES_INVOICE, "bl_number"), ctx.f(PURCHASE_INVOICE, "bl_number")
    # identifier pattern: a BL number carries digits ("MAEU" is the carrier's SCAC code, not a BL number)
    ok = lambda f: f is not None and f.status == FOUND and sum(ch.isdigit() for ch in N.norm_id(f.raw_value)) >= 4
    if not ok(bl):   # BL label garbled by OCR ("ALM." for "B/L No."): look for the invoices' BL number on the BL
        bl = next((x for x in (find_identifier(ctx.docs.get(BILL_OF_LADING), f.raw_value, "bl_number")
                               for f in (si, pi) if ok(f)) if x), bl)
    if ok(bl):
        if not ok(si):
            si = find_identifier(ctx.docs.get(SALES_INVOICE), bl.raw_value, "bl_number") or si
        if not ok(pi):
            pi = find_identifier(ctx.docs.get(PURCHASE_INVOICE), bl.raw_value, "bl_number") or pi
    return _pair_result("REF.BL", "BL number consistent across invoices and BL", ref, "DETERMINISTIC", [
        ("SI BL ref", si, "BL number", bl, True),
        ("PI BL ref", pi, "BL number", bl, False),
    ], eq_id, "Invoices cite the same BL number as the bill of lading.")


def _digits_differ_by_one(a: float, b: float) -> bool:
    x, y = f"{a:.2f}", f"{b:.2f}"
    return len(x) == len(y) and sum(c1 != c2 for c1, c2 in zip(x, y)) == 1


def check_invoice_arithmetic(ctx: Ctx, ref: str) -> CheckResult:
    title = "Invoice arithmetic: qty x price = amount; totals; amount in words"
    results, reasons, evidence = [], [], []
    for iv, dt in ((ctx.pi, PURCHASE_INVOICE), (ctx.si, SALES_INVOICE)):
        if not iv:
            continue
        lab = iv.doc.short
        ocr = ctx.is_ocr(dt)

        def bad(msg, a=None, b=None):
            # On OCR'd pages a single misread digit is plausible: review instead of fail.
            if ocr and a is not None and b is not None and _digits_differ_by_one(a, b):
                results.append(REVIEW); reasons.append(f"{lab}: {msg} (single-digit difference on an OCR'd page)")
            else:
                results.append(MISMATCH); reasons.append(f"{lab}: {msg}")

        for l in iv.lines:
            evidence += [ev(l.fields.get(k), f"{lab} line {k}") for k in ("quantity", "unit_price", "amount") if l.fields.get(k)]
            if l.reconciled is True:
                results.append(PASS)
                if l.notes:
                    reasons.append(f"{lab}: {'; '.join(l.notes)}")
            elif l.reconciled is False:
                if any("ambiguous" in n for n in l.notes) or (l.ocr_doubt and ocr):
                    results.append(REVIEW); reasons.append(f"{lab}: {'; '.join(l.notes)}")
                else:
                    bad("; ".join(l.notes))
        amounts = [dec(l.amount) for l in iv.lines]
        lines_total = sum(amounts) if amounts and None not in amounts else None
        gross, adv, net = dec(iv.gross), dec(iv.advance), dec(iv.net)
        if gross is not None and lines_total is not None:
            evidence.append(ev(iv.doc.fields.get("gross_amount"), f"{lab} gross amount"))
            if abs(lines_total - gross) <= CENT * 2 * max(1, len(iv.lines)):
                results.append(PASS)
            else:
                bad(f"line amounts total {lines_total:,.2f} but gross amount shows {gross:,.2f}", float(lines_total), float(gross))
        payable = net if net is not None else gross if gross is not None else lines_total
        if adv and gross is not None and net is not None:
            evidence += [ev(iv.doc.fields.get("advance_deducted"), f"{lab} advance"), ev(iv.doc.fields.get("net_amount_payable"), f"{lab} net payable")]
            calc = gross - adv
            evidence.append(computed(f"{lab} net payable (calculated)", float(calc), "gross - advance - other deductions (none shown)"))
            if abs(calc - net) <= CENT * 2:
                results.append(PASS)
                reasons.append(f"{lab}: net payable {net:,.2f} = gross {gross:,.2f} - advance {adv:,.2f}")
            else:
                bad(f"gross {gross:,.2f} - advance {adv:,.2f} = {calc:,.2f}, but net payable shows {net:,.2f} (incorrect advance arithmetic)",
                    float(calc), float(net))
        words = iv.doc.fields.get("amount_in_words")
        if words and words.raw_value:
            evidence.append(ev(words, f"{lab} amount in words"))
            if iv.words_amount is None:
                results.append(REVIEW); reasons.append(f"{lab}: amount in words could not be parsed")
            elif payable is not None:
                targets = {v for v in (net, gross, payable) if v is not None}
                words_amt = money(dec(iv.words_amount))
                evidence.append(computed(f"{lab} amount in words (parsed)", iv.words_amount, "words -> number"))
                if any(abs(words_amt - t) <= CENT for t in targets):
                    results.append(PASS)
                else:
                    results.append(MISMATCH)   # words are not subject to digit-level OCR slips
                    reasons.append(f"{lab}: amount in words = {iv.words_amount:,.2f} but the invoice total is {payable:,.2f}")
    if not results:
        return _result("ARITH.INVOICES", title, ref, "ARITHMETIC", REVIEW, "No invoice values available to check.", evidence)
    res = worst(results)
    if res == PASS:
        reasons.insert(0, "All invoice line extensions, totals and amounts in words reconcile")
    return _result("ARITH.INVOICES", title, ref, "ARITHMETIC", res, " | ".join(reasons), evidence)


def check_hs_codes(ctx: Ctx, ref: str) -> CheckResult:
    title = "HS codes consistent across contracts, invoices and BL"
    sets: dict[str, set[str]] = {}
    evidence = []
    for dt in (PURCHASE_CONTRACT, PURCHASE_INVOICE, BILL_OF_LADING, SALES_INVOICE, SALES_CONTRACT):
        d = ctx.docs.get(dt)
        if not d:
            continue
        fields = [i.get("hs_code") for i in d.line_items] + [d.fields.get("hs_code")]
        codes = {N.norm_hs(f.raw_value) for f in fields if f and f.status == FOUND and len(N.norm_hs(f.raw_value)) >= 6}
        evidence += [ev(f, f"{d.short} HS code") for f in fields if f and f.raw_value]
        if codes:
            sets[d.short] = codes
    if len(sets) < 2:
        return _result("GOODS.HS", title, ref, "DETERMINISTIC", REVIEW, "HS codes found on fewer than two documents.", evidence)
    # Compare on the 6-digit international HS heading; national 8-digit suffixes vary.
    six = {k: {c[:6] for c in v} for k, v in sets.items()}
    reasons, results = [], []
    ref_set = six.get("BL") or six.get("PI") or six.get("SI")
    for k, v in six.items():
        if k == "BL" or ref_set is None:
            continue
        ok = ref_set <= v if k in ("PC", "SC") else v == ref_set   # a contract may list more materials
        results.append(PASS if ok else MISMATCH)
        if not ok:
            reasons.append(f"{k} {sorted(sets[k])} vs BL {sorted(sets.get('BL', ref_set))}")
    evidence.append(computed("HS codes by document", {k: sorted(v) for k, v in sets.items()}, "digits only"))
    if "BL" not in sets:
        results.append(REVIEW); reasons.append("no HS code on the BL")
    res = worst(results)
    return _result("GOODS.HS", title, ref, "DETERMINISTIC", res,
                   "HS codes agree: " + ", ".join(f"{k} {sorted(v)}" for k, v in sets.items()) if res == PASS
                   else "HS code mismatch: " + " | ".join(reasons), evidence)


def _grade_conflict(a: str, b: str) -> str | None:
    ga = set(re.findall(r"GRADE\s*(\d{3})", a.upper()))
    gb = set(re.findall(r"GRADE\s*(\d{3})", b.upper()))
    if ga and gb and not (ga & gb):
        return f"grade {sorted(ga)} vs {sorted(gb)}"
    return None


def _same_material_wording(a: str, b: str) -> bool:
    """Same metal and form, and one description's words are contained in the other's
    ('Insulated Copper Wire Scrap' / 'Insulated Copper Scrap Wire'; 'Refined Lead' / 'Refined Lead 99.985% Min')."""
    from .commodity import describe
    ca, cb = describe(a), describe(b)
    if not ca.metal or ca.metal != cb.metal or ca.forms != cb.forms:
        return False
    stop = {"AS", "PER", "ISRI", "THE", "OF", "AND", "WITH", "SCRAP", "SCARP"}
    wa = {w for w in re.findall(r"[A-Z]{3,}", a.upper()) if w not in stop}
    wb = {w for w in re.findall(r"[A-Z]{3,}", b.upper()) if w not in stop}
    return bool(wa and wb) and (wa <= wb or wb <= wa)


def check_goods(ctx: Ctx, ref: str) -> CheckResult:
    """Commodity identity across PC, PI, BL, SI and SC. Grades and metals are decided deterministically;
    the model is only asked when wording differs and nothing deterministic applies, and its 'no' is REVIEW."""
    from .commodity import compatible, describe
    title = "Commodity identity and grade consistent (ISRI grade, metal, form)"
    pairs: list[tuple[str, str, str, str]] = []
    bl_desc = _raw(ctx.f(BILL_OF_LADING, "goods_description"))
    for iv, cv, lab, clab in ((ctx.pi, ctx.pc, "PI", "PC"), (ctx.si, ctx.sc, "SI", "SC")):
        for l in (iv.lines if iv else []):
            if not l.description:
                continue
            if cv:
                m, _ = match_material(l, cv.materials)
                if m and m.description:
                    pairs.append((lab, l.description, clab, m.description))
            if bl_desc:
                pairs.append((lab, l.description, "BL", bl_desc))
    if ctx.pc and ctx.sc:                       # physical goods bought and sold must be the same material
        from .canonical import Line
        for m in ctx.pc.materials:
            if not m.description:
                continue
            # pair with the SC material for the same goods (HS code, grade numbers, wording), as for invoice lines
            sm, _ = match_material(Line(m.description, m.hs, None, None, None, None), ctx.sc.materials)
            if sm and sm.description:
                pairs.append(("PC", m.description, "SC", sm.description))
    results, reasons, comps = [], [], []
    for la, a, lb, b in pairs:
        comp = {"a": la, "a_value": a, "b": lb, "b_value": b}
        verdict, why = compatible(describe(a), describe(b))
        g = _grade_conflict(a, b)
        if g:
            verdict, why = "NEQ", g
        if verdict == "EQ" or (verdict == "UNKNOWN" and (N.norm_place(a) == N.norm_place(b) or _same_material_wording(a, b))):
            results.append(PASS)
            comps.append(comp | {"outcome": "EQ", "method": "ISRI grade / metal / wording (deterministic)",
                                 "detail": why if verdict == "EQ" else "same material, wording differs only in order or extra specification"})
            continue
        if verdict == "NEQ":
            results.append(MISMATCH); reasons.append(f"{la} '{a}' vs {lb} '{b}': {why}")
            comps.append(comp | {"outcome": "NEQ", "method": "ISRI grade / metal (deterministic)", "detail": why})
            continue
        ans, mwhy, err = ai_compare(ctx.model, "goods descriptions", a, b,
                                    "Do A and B describe the same commodity (same metal, same grade/type)?")
        comp["method"] = f"semantic_ai ({ctx.model.name}); used only because nothing deterministic applied"
        if err:
            results.append(REVIEW); reasons.append(f"{la} vs {lb}: model unavailable ({err})")
            comp.update(outcome="NOT_COMPARED", detail=err)
        elif ans == "YES":
            results.append(PASS); comp.update(outcome="EQ", detail=mwhy)
        else:
            results.append(REVIEW); reasons.append(f"{la} '{a}' vs {lb} '{b}': model says {ans} - {mwhy}")
            comp.update(outcome="UNSURE" if ans == "UNSURE" else "NEQ", detail=mwhy)
        comps.append(comp)
    if not results:
        return _result("GOODS.DESCRIPTION", title, ref, "SEMANTIC_AI", REVIEW, "No goods descriptions to compare.", comps=comps)
    res = worst(results)
    return _result("GOODS.DESCRIPTION", title, ref, "DETERMINISTIC", res,
                   f"{len(pairs)} description pair(s) refer to the same goods." if res == PASS else " | ".join(reasons), comps=comps)


def _contract_recoveries(doc: Document | None) -> list[tuple[float, str]]:
    """Physical recovery stated in the contract's material specification (section A), with its wording."""
    from .commodity import physical_recoveries
    if not doc or not doc.pages:
        return []
    text = doc.pages[0].text
    m = re.search(r"(?is)material.*?(?=\n\W*quantity)", text)
    out = []
    for line in (m.group(0) if m else "").splitlines():
        for r in physical_recoveries(line):
            out.append((r, N.norm_space(line)))
    return out


def check_recovery(ctx: Ctx, ref: str) -> CheckResult:
    """Physical recovery is a quality characteristic: PC and SC specifications must be compatible, and
    recoveries stated on invoice lines must meet the contract (on average when the contract says so).
    Commercial payable-metal factors in pricing formulas are not quality and are not read here."""
    from .commodity import physical_recoveries, recovery_is_average
    title = "Physical recovery / quality specification compatible across contracts and invoices"
    pc_r, sc_r = _contract_recoveries(ctx.docs.get(PURCHASE_CONTRACT)), _contract_recoveries(ctx.docs.get(SALES_CONTRACT))
    results, reasons, evidence = [], [], []
    for (r, line), lab in [(x, "PC") for x in pc_r] + [(x, "SC") for x in sc_r]:
        evidence.append({"role": f"{lab} physical recovery", "raw_value": f"{r:g}%", "source_text": line,
                         "extraction_method": "deterministic_section"})
    if pc_r and sc_r:
        pcs, scs = sorted(r for r, _ in pc_r), sorted(r for r, _ in sc_r)
        if pcs == scs:
            results.append(PASS); reasons.append(f"PC and SC state the same physical recovery {pcs}")
        else:
            results.append(REVIEW)
            reasons.append(f"PC recovery {pcs} vs SC recovery {scs}: specifications differ; confirm they are compatible")
    for iv, contract_r, lab, clab in ((ctx.pi, pc_r, "PI", "PC"), (ctx.si, sc_r, "SI", "SC")):
        if not iv or len(contract_r) != 1:
            continue
        need, line = contract_r[0]
        stated = [(physical_recoveries(l.description), l.qty_mt) for l in iv.lines]
        if not all(rs and q for rs, q in stated):
            continue
        avg = sum(rs[0] * q for rs, q in stated) / sum(q for _, q in stated)
        evidence.append(computed(f"{lab} weighted recovery", round(avg, 2), "sum(recovery x qty) / sum(qty) over invoice lines"))
        rule = "average" if recovery_is_average(line) else "each lot"
        ok = avg >= need - 1e-9 if rule == "average" else all(rs[0] >= need for rs, _ in stated)
        results.append(PASS if ok else MISMATCH)
        reasons.append(f"{lab} recovery {avg:.1f}% ({rule}) {'meets' if ok else 'is below'} {clab} {need:g}%")
    if not results:
        return _result("QUALITY.RECOVERY", title, ref, "DETERMINISTIC", PASS,
                       "Not applicable: no physical recovery specification to reconcile.", evidence)
    return _result("QUALITY.RECOVERY", title, ref, "DETERMINISTIC", worst(results), " | ".join(reasons), evidence)


def check_parties(ctx: Ctx, ref: str) -> CheckResult:
    title = "Parties consistent across documents"
    own = Field("own_company", CONFIG.own_company, CONFIG.own_company, status=FOUND, extraction_method="configuration")
    strict = [
        ("PI seller", ctx.f(PURCHASE_INVOICE, "seller_name"), "PC seller", ctx.f(PURCHASE_CONTRACT, "seller_name"), True),
        ("PI buyer", ctx.f(PURCHASE_INVOICE, "buyer_name"), "own company", own, True),
        ("SI buyer", ctx.f(SALES_INVOICE, "buyer_name"), "SC buyer", ctx.f(SALES_CONTRACT, "buyer_name"), True),
        ("PC buyer", ctx.f(PURCHASE_CONTRACT, "buyer_name"), "own company", own, True),
        ("SC seller", ctx.f(SALES_CONTRACT, "seller_name"), "own company", own, True),
    ]
    # BL parties act in shipping roles: the shipper may be the supplier, a producer, a forwarder or a
    # nominated entity; the notify party may be an agent. A different name there is a note, not a mismatch.
    soft = [
        ("BL shipper", ctx.f(BILL_OF_LADING, "shipper_name"), "PC seller", ctx.f(PURCHASE_CONTRACT, "seller_name"), False),
        ("BL notify party", ctx.f(BILL_OF_LADING, "notify_party_name"), "SC buyer", ctx.f(SALES_CONTRACT, "buyer_name"), False),
    ]
    # The contract may name a different payee (e.g. a trading arm invoicing "on behalf of" the seller):
    # an invoice from the contract's named payment beneficiary is consistent with the contract.
    pi_seller, pc_seller = ctx.f(PURCHASE_INVOICE, "seller_name"), ctx.f(PURCHASE_CONTRACT, "seller_name")
    pc_benef = ctx.f(PURCHASE_CONTRACT, "bank_beneficiary")
    if all(f and f.status == FOUND for f in (pi_seller, pc_seller, pc_benef)) \
            and eq_company(pi_seller, pc_seller)[0] == "NEQ" and eq_company(pi_seller, pc_benef)[0] == "EQ":
        strict[0] = ("PI seller", pi_seller, "PC named payment beneficiary", pc_benef, True)
    # The own company cannot be the buyer on its own sales invoice: such a value is an extraction error,
    # so look for the sales contract's buyer on the invoice instead.
    si_buyer, sc_buyer = ctx.f(SALES_INVOICE, "buyer_name"), ctx.f(SALES_CONTRACT, "buyer_name")
    if si_buyer and sc_buyer and sc_buyer.status == FOUND and N.company_match(si_buyer.raw_value, CONFIG.own_company) != "DIFFERENT":
        found = find_identifier(ctx.docs.get(SALES_INVOICE), sc_buyer.raw_value, "buyer_name")
        if found:
            found.note = (f"the model returned the own company as buyer; the sales contract buyer '{sc_buyer.raw_value}' "
                          f"is printed on the invoice")
            strict[2] = ("SI buyer", found, "SC buyer", sc_buyer, True)
    r1, c1, w1 = compare_pairs(strict, eq_company)
    r2, c2, w2 = compare_pairs(soft, eq_company, mismatch=PASS_NOTE)
    consignee = ctx.f(BILL_OF_LADING, "consignee")
    role_notes = [w + " (shipping role; not a contractual party mismatch)" for w in w2]
    if consignee and consignee.status == FOUND and re.search(r"TO\s+ORDER|BANK", consignee.raw_value.upper()):
        role_notes.append(f"BL consignee '{consignee.raw_value}' is a negotiable / bank consignee, as usual")
    res = worst([r1, r2])
    reasons = w1 + role_notes
    return _result("PARTIES", title, ref, "NORMALIZED", res,
                   "Seller and buyer names agree across contracts, invoices and BL." if res == PASS
                   else ("Contract and invoice parties agree. " if r1 == PASS else "") + " | ".join(reasons),
                   comps=c1 + c2)


def _bl_location_context(ctx: Ctx) -> str:
    """Lines of the BL that mention places (shipper address, place of receipt/issue)."""
    d = ctx.docs.get(BILL_OF_LADING)
    if not d or not d.pages:
        return ""
    keep = [l for l in d.pages[0].text.splitlines()[:40]
            if re.search(r"SHIPPER|RECEIPT|ISSUE|LOADING|ADDRESS|,\s*[A-Z]{4,}", l.upper())]
    return "\n".join(keep[:15])[:1200]


def check_ports(ctx: Ctx, ref: str) -> CheckResult:
    title = "Ports consistent with contracts and BL"
    results, reasons, comps = [], [], []
    bl_pol, bl_pod = ctx.f(BILL_OF_LADING, "port_of_loading"), ctx.f(BILL_OF_LADING, "port_of_discharge")
    bl_pdl = ctx.f(BILL_OF_LADING, "place_of_delivery")

    def place_ok(contract_f: Field | None, bl_f: Field | None, label: str):
        if not (contract_f and contract_f.status == FOUND and bl_f and bl_f.status == FOUND):
            gone = [n for n, f in (("contract", contract_f), ("BL", bl_f)) if not (f and f.status == FOUND)]
            detail = "; ".join(f"{n}: {f.status + (' - ' + f.note if f.note else '') if f else 'NOT_FOUND'}"
                               for n, f in (("contract", contract_f), ("BL", bl_f)) if n in gone)
            results.append(REVIEW); reasons.append(f"{label}: value not established ({detail})")
            comps.append({"a": label, "outcome": "NOT_COMPARED", "evidence": [ev(contract_f), ev(bl_f)]})
            return
        c, b = contract_f.raw_value, bl_f.raw_value
        ct, bt = N.place_tokens(c), N.place_tokens(b)
        if label.endswith("destination") and bl_pdl and bl_pdl.status == FOUND:
            bt |= N.place_tokens(bl_pdl.raw_value)      # final destination may be the BL place of delivery
            b = f"{b} / place of delivery {bl_pdl.raw_value}"
        comp = {"a": f"{label} (contract)", "a_value": c, "b": "BL", "b_value": b, "evidence": [ev(contract_f), ev(bl_f)]}
        any_port = re.search(r"ANY\s+PORT\s+(?:IN|OF)\s+([A-Z ]+)", c.upper())
        if ct and bt and (ct & bt):
            results.append(PASS); comp.update(outcome="EQ", method="place-name tokens", detail=f"shared {sorted(ct & bt)}")
        elif any_port:
            country = N.norm_place(any_port.group(1))
            if country and country in N.norm_place(b):
                results.append(PASS); comp.update(outcome="EQ", method="country named on BL", detail=f"BL port is in {country}")
            else:
                # The model supplies one fact (the port's country); code makes the comparison.
                found, why, err = ai_port_country(ctx.model, b, _bl_location_context(ctx))
                comp["method"] = f"semantic_ai fact + deterministic compare ({ctx.model.name})"
                if err:
                    results.append(REVIEW); reasons.append(f"{label}: model unavailable ({err})")
                    comp.update(outcome="NOT_COMPARED", detail=err)
                elif found and N.norm_place(found) == country:
                    results.append(PASS); comp.update(outcome="EQ", detail=f"model: '{b}' is in {found} - {why}")
                else:
                    results.append(REVIEW)
                    reasons.append(f"{label}: contract requires a port in {country.title()}; model places BL port '{b}' in {found or 'unknown'} ({why})")
                    comp.update(outcome="UNSURE", detail=f"model: {found} - {why}")
        else:
            ans, why, err = ai_compare(ctx.model, "shipping locations", b, c,
                                       "Is location A (from the bill of lading) the same place as, or inside, location B (from the contract)?",
                                       context=_bl_location_context(ctx))
            comp["method"] = f"semantic_ai ({ctx.model.name})"
            if err:
                results.append(REVIEW); reasons.append(f"{label}: model unavailable ({err})"); comp.update(outcome="NOT_COMPARED", detail=err)
            elif ans == "YES":
                results.append(PASS); comp.update(outcome="EQ", detail=why)
            else:
                results.append(REVIEW); reasons.append(f"{label}: BL '{b}' vs contract '{c}' - model says {ans}: {why}")
                comp.update(outcome="NEQ" if ans == "NO" else "UNSURE", detail=why)
        comps.append(comp)

    place_ok(ctx.f(PURCHASE_CONTRACT, "port_of_loading"), bl_pol, "PC port of loading")
    dest_bl = bl_pod if bl_pod and bl_pod.status == FOUND else bl_pdl
    place_ok(ctx.f(PURCHASE_CONTRACT, "port_of_destination"), dest_bl, "PC destination")
    place_ok(ctx.f(SALES_CONTRACT, "port_of_destination"), dest_bl, "SC destination")
    # sales invoice ports should restate the BL
    for k, blf in (("port_of_loading", bl_pol), ("port_of_discharge", bl_pod)):
        si_f = ctx.f(SALES_INVOICE, k)
        if si_f and si_f.status == FOUND and blf and blf.status == FOUND:
            bl_tokens = N.place_tokens(blf.raw_value)
            if k == "port_of_discharge" and bl_pdl and bl_pdl.status == FOUND:
                bl_tokens |= N.place_tokens(bl_pdl.raw_value)   # an invoice may name the final place of delivery
            ok = bool(N.place_tokens(si_f.raw_value) & bl_tokens) or \
                 N.norm_place(si_f.raw_value) in N.norm_place(blf.raw_value) or N.norm_place(blf.raw_value) in N.norm_place(si_f.raw_value)
            results.append(PASS if ok else MISMATCH)
            comps.append({"a": f"SI {k}", "a_value": si_f.raw_value, "b": f"BL {k}", "b_value": blf.raw_value,
                          "outcome": "EQ" if ok else "NEQ", "method": "place-name tokens", "evidence": [ev(si_f), ev(blf)]})
            if not ok:
                reasons.append(f"SI {k} '{si_f.raw_value}' vs BL '{blf.raw_value}'")
    res = worst(results)
    return _result("ROUTING.PORTS", title, ref, "SEMANTIC_AI", res,
                   "Loading and destination ports agree with the contracts; sales invoice restates the BL." if res == PASS
                   else " | ".join(reasons), comps=comps)


def check_vessel(ctx: Ctx, ref: str) -> CheckResult:
    pairs = [("SI vessel", ctx.f(SALES_INVOICE, "vessel"), "BL vessel", ctx.f(BILL_OF_LADING, "vessel"), False),
             ("SI voyage", ctx.f(SALES_INVOICE, "voyage"), "BL voyage", ctx.f(BILL_OF_LADING, "voyage"), False),
             ("PI vessel", ctx.f(PURCHASE_INVOICE, "vessel"), "BL vessel", ctx.f(BILL_OF_LADING, "vessel"), False)]
    res, comps, reasons = compare_pairs(pairs, lambda a, b: eq_id(a, b) if "voyage" in a.canonical_field else eq_text(a, b))
    if all(c["outcome"] == "NOT_COMPARED" for c in comps):
        res, reasons = PASS, []
        return _result("SHIP.VESSEL", "Vessel and voyage consistent with BL", ref, "DETERMINISTIC", PASS,
                       "No invoice states a vessel; nothing to reconcile.", comps=comps)
    return _result("SHIP.VESSEL", "Vessel and voyage consistent with BL", ref, "DETERMINISTIC", res,
                   "Vessel/voyage on the invoices match the BL." if res == PASS else " | ".join(reasons), comps=comps)


def _period(ctx: Ctx, con: str) -> tuple[tuple[date, date] | None, str]:
    f, cd = ctx.f(con, "shipment_period"), ctx.f(con, "contract_date")
    raw = _raw(f)
    if not raw:
        return None, "shipment period not found"
    mp = N.parse_month_period(raw)
    if mp:
        return mp, f"'{raw}'"
    m = re.search(r"(?:WITHIN\s+)?(\d+)\s+DAYS\s+FROM\s+(?:THE\s+)?(?:DATE\s+OF\s+)?CONTRACT", raw.upper())
    if m and cd and cd.status == FOUND and cd.normalized_value:
        start = date.fromisoformat(cd.normalized_value)
        return (start, start + timedelta(days=int(m.group(1)))), f"'{raw}' from contract date {start.isoformat()}"
    return None, f"shipment period '{raw}' cannot be resolved to dates from the documents"


def check_shipment_date(ctx: Ctx, ref: str) -> CheckResult:
    title = "Shipment date within contract shipment period; invoices restate BL date"
    bl = ctx.f(BILL_OF_LADING, "shipped_on_board_date")
    evidence = [ev(bl, "BL on-board date")]
    if not bl or bl.status != FOUND or not bl.normalized_value:
        return _result("SHIP.DATE", title, ref, "CONDITIONAL", REVIEW, "BL on-board date not available.", evidence)
    d = date.fromisoformat(bl.normalized_value)
    results, reasons = [], []
    for con in (PURCHASE_CONTRACT, SALES_CONTRACT):
        evidence.append(ev(ctx.f(con, "shipment_period"), f"{con} shipment period"))
        per, how = _period(ctx, con)
        short = "PC" if con == PURCHASE_CONTRACT else "SC"
        if per is None:
            results.append(REVIEW); reasons.append(f"{short}: {how}")
        elif per[0] <= d <= per[1]:
            results.append(PASS); reasons.append(f"{short}: {d} within {per[0]}..{per[1]} ({how})")
        else:
            results.append(MISMATCH); reasons.append(f"{short}: BL date {d} outside {per[0]}..{per[1]} ({how})")
    si = ctx.f(SALES_INVOICE, "shipped_on_board_date")
    if si and si.status == FOUND and si.normalized_value:
        evidence.append(ev(si, "SI shipped on board"))
        ok = si.normalized_value == bl.normalized_value
        results.append(PASS if ok else MISMATCH)
        if not ok:
            reasons.append(f"SI on-board date {si.normalized_value} ≠ BL {bl.normalized_value}")
    return _result("SHIP.DATE", title, ref, "CONDITIONAL", worst(results), " | ".join(reasons), evidence)


INCOTERM_RX = r"\b(EXW|FCA|FAS|FOB|CFR|CNF|C\s*&\s*F|CIF|CPT|CIP|DAP|DPU|DDP)\b"
# A delivery-term LABEL makes the term on the same line operative.
DELIVERY_LABEL = re.compile(r"DELIVERY\s*TERMS?|TERMS?\s*OF\s*DELIVERY|\bINCOTERMS?\b(?!\s*20\d\d)|"
                            r"SHIPMENT\s*TERMS?|PRICE\s*BASIS|\bDELIVERY\s*:")
# Wording that mentions Incoterms without stating the deal's term (explanations, examples, rule references).
NON_OPERATIVE = re.compile(r"FOR\s+EXAMPLE|E\.\s?G\.|SUCH\s+AS|RULE\s+SPECIFIED|INCOTERMS\s*20\d\d\s+RULE|"
                           r"\bOR\s+(CIF|CFR|FOB)\b|AS\s+DEFINED|RISK\s+OF\s+LOSS")


def incoterm_statements(doc: Document | None) -> dict:
    """Find the OPERATIVE delivery term of one document.

    primary    a labelled delivery-term field ("Delivery Terms : CIF Chennai", "TERMS OF DELIVERY : CIF")
    secondary  another line carrying exactly one Incoterm ("Port of Destination : CIF Nhava Sheva")
    ignored    explanatory/template text ("... Incoterms 2020 rule ... (for example, FOB, CFR or CIF)")
    The operative term comes from primary statements; secondary ones are used only when there is no
    primary statement. A CONFLICT exists only when two primary statements in the same document differ.
    Pages' alternative OCR passes are consulted when the primary text states nothing."""
    out = {"term": None, "status": "NONE", "primary": [], "secondary": [], "ignored": [], "label_without_value": []}
    if not doc:
        return out

    def scan(texts):
        for pg, text, ocr in texts:
            for line in text.splitlines():
                u = line.upper()
                found = [N.norm_incoterm(m.group(0)) for m in re.finditer(INCOTERM_RX, u)]
                found = [f for f in found if f]
                label = DELIVERY_LABEL.search(u)
                if not found:
                    if label and ocr:   # an empty field on an OCR'd page may be unreadable; in PDF text it is blank
                        out["label_without_value"].append({"page": pg, "source_text": N.norm_space(line)})
                    continue
                st = {"page": pg, "document": doc.filename, "source_text": N.norm_space(line)}
                if NON_OPERATIVE.search(u) or len(set(found)) > 1:
                    out["ignored"].append(st | {"terms": sorted(set(found)), "why": "explanatory / example / list text"})
                elif label and re.search(INCOTERM_RX, u[label.start():]):
                    out["primary"].append(st | {"term": found[0]})
                else:
                    out["secondary"].append(st | {"term": found[0]})

    scan([(p.number, p.text, p.method == "ocr") for p in doc.pages])
    if not out["primary"] and not out["secondary"]:
        scan([(p.number, alt, True) for p in doc.pages for alt in p.alt_texts])
    prim = {s["term"] for s in out["primary"]}
    sec = {s["term"] for s in out["secondary"]}
    if len(prim) == 1:
        out["term"], out["status"] = prim.pop(), "DETERMINED"
    elif len(prim) > 1:
        out["status"] = "CONFLICT"
    elif len(sec) == 1:
        out["term"], out["status"] = sec.pop(), "DETERMINED"
    elif len(sec) > 1:
        out["status"] = "CONFLICT"
    return out


def check_incoterms(ctx: Ctx, ref: str) -> CheckResult:
    """Delivery terms are a commercial term of each leg: PC <-> PI and SC <-> SI. Purchase and sales
    Incoterms are independent (buying CFR and selling CIF is normal) and are never compared."""
    from .scope import LEG_DOCS, LEG_NAMES, ensure_comparable
    title = "Delivery terms (Incoterm) consistent within each commercial leg"
    results, reasons, evidence, comps = [], [], [], []
    for leg, (con, inv) in LEG_DOCS.items():
        ensure_comparable("delivery_terms", con, inv)
        cd, idoc = ctx.docs.get(con), ctx.docs.get(inv)
        if not cd or not idoc:
            results.append(REVIEW); reasons.append(f"{leg}: contract or invoice missing"); continue
        c, i = incoterm_statements(cd), incoterm_statements(idoc)
        for st in c["primary"] + c["secondary"] + i["primary"] + i["secondary"]:
            evidence.append({"role": f"{leg} delivery term", "document": st["document"], "page": st["page"],
                             "source_text": st["source_text"], "raw_value": st["term"],
                             "note": "operative (labelled)" if st in c["primary"] + i["primary"] else "secondary mention",
                             "scope": leg})
        for st in c["ignored"] + i["ignored"]:
            evidence.append({"role": f"{leg} non-operative Incoterm wording (ignored)", "document": st["document"],
                             "page": st["page"], "source_text": st["source_text"], "note": st["why"], "scope": leg})
        name = LEG_NAMES[leg]
        if c["status"] == "CONFLICT":
            stated = "; ".join(f"'{s['source_text']}' (p.{s['page']})" for s in c["primary"] or c["secondary"])
            hint = f" The {name.lower()} invoice states {i['term']}." if i["term"] else ""
            r, why = REVIEW, (f"{cd.short} states more than one operative delivery term: {stated}. Which one governs "
                              f"cannot be determined from the document.{hint}")
        elif c["status"] == "NONE":
            r, why = REVIEW, f"{cd.short}: no operative delivery term found"
        elif i["status"] == "CONFLICT":
            r, why = REVIEW, f"{idoc.short} states more than one delivery term"
        elif i["status"] == "NONE":
            if i["label_without_value"]:
                r, why = REVIEW, (f"{idoc.short} has a delivery-term field ('{i['label_without_value'][0]['source_text']}') "
                                  f"but its value is not legible")
            else:
                r, why = PASS_NOTE, f"{cd.short} {c['term']}; {idoc.short} does not state a delivery term"
        elif c["term"] == i["term"]:
            r, why = PASS, f"{cd.short} {c['term']} = {idoc.short} {i['term']}"
        else:
            r, why = MISMATCH, f"{cd.short} {c['term']} but {idoc.short} {i['term']} (same {name.lower()} leg)"
        results.append(r)
        reasons.append(f"[{leg}] {why}")
        comps.append({"scope": leg, "a": f"{cd.short} delivery term", "a_value": c["term"],
                      "b": f"{idoc.short} delivery term", "b_value": i["term"],
                      "outcome": {PASS: "EQ", MISMATCH: "NEQ"}.get(r, "NOT_COMPARED"), "detail": why, "result": r})
    reasons.append("Purchase and sales delivery terms are independent commercial legs and are not compared with each other")
    return _result("TERMS.INCOTERM", title, ref, "NORMALIZED", worst(results), " | ".join(reasons), evidence, comps)


def check_currency(ctx: Ctx, ref: str) -> CheckResult:
    """Currency is a commercial term of each leg: PC <-> PI and SC <-> SI. Purchase and sales may use
    different currencies unless the checklist requires otherwise (it does not)."""
    from .scope import LEG_DOCS, ensure_comparable
    title = "Currency consistent within each commercial leg"
    results, reasons, evidence = [], [], []
    for leg, (con, inv) in LEG_DOCS.items():
        ensure_comparable("currency", con, inv)
        fc, fi = ctx.f(con, "currency"), ctx.f(inv, "currency")
        evidence += [ev(fc, f"{leg} contract currency") | {"scope": leg}, ev(fi, f"{leg} invoice currency") | {"scope": leg}]
        cc, ci = N.norm_currency(_raw(fc)), N.norm_currency(_raw(fi))
        if cc and ci:
            r = PASS if cc == ci else MISMATCH
            reasons.append(f"[{leg}] contract {cc} {'=' if r == PASS else '≠'} invoice {ci}")
        elif cc or ci:
            r = PASS_NOTE
            reasons.append(f"[{leg}] currency stated only on the {'contract' if cc else 'invoice'} ({cc or ci})")
        else:
            r = REVIEW
            reasons.append(f"[{leg}] currency not found on contract or invoice")
        results.append(r)
    return _result("TERMS.CURRENCY", title, ref, "NORMALIZED", worst(results), " | ".join(reasons), evidence)


