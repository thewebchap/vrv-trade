"""Process one case folder, or a folder of cases."""
from __future__ import annotations

import logging
import os
import traceback
from datetime import date, datetime

from .checklist import load_checklist, match_payment_term
from .checks import Ctx
from .commercial import commercial_summary, transaction_summary
from .verify import build_views, run_checks
from .classify import classify, resolve_duplicates, split_combined
from .config import CONFIG
from .extract import detect_signatures, extract
from .local_model import LocalModel
from .models import (FAIL, MANDATORY_DOCS, MISMATCH, OPTIONAL_DOC_TYPES, PASS, PASS_NOTE, PURCHASE_CONTRACT,
                     REVIEW, SALES_CONTRACT, SEVERITY, Document)
from .pdf_text import extract_pages

log = logging.getLogger("tradepilot")


def discover(case_dir: str) -> list[str]:
    return sorted(os.path.join(case_dir, f) for f in os.listdir(case_dir)
                  if f.lower().endswith(".pdf") and not f.startswith("."))


def is_case_dir(path: str) -> bool:
    return os.path.isdir(path) and bool(discover(path))


def process_case(case_dir: str, model: LocalModel | None = None, today: date | None = None,
                 case_id: str | None = None) -> dict:
    model = model or LocalModel()
    today = today or date.today()
    case_id = case_id or os.path.basename(os.path.normpath(case_dir))
    log.info("[CASE] %s", case_dir)

    docs: list[Document] = []
    for path in discover(case_dir):
        try:
            pages = extract_pages(path)
        except Exception as e:   # unreadable PDF: keep it visible, never crash the case
            log.warning("[DOC] %s: text extraction failed: %s", os.path.basename(path), e)
            d = Document(path, os.path.basename(path), [], extraction_error=f"PDF text extraction failed: {e}")
            docs.append(d)
            continue
        parts = split_combined(path, os.path.basename(path), pages)
        if len(parts) > 1:
            log.info("[DOC] %s holds %d documents", os.path.basename(path), len(parts))
        for part in parts:
            docs.append(classify(part, use_filename=len(parts) == 1))
    resolve_duplicates(docs)
    for d in docs:
        log.info("[DOC] %s -> %s (%s)", d.filename, d.doc_type, d.classification_status)

    # One document per mandatory type is used for validation; duplicates/ambiguous ones are reported by
    # DOCS.MANDATORY rather than silently picked.
    by_type = {d.doc_type: d for d in docs if d.doc_type in MANDATORY_DOCS and d.classification_status != "UNKNOWN"}
    supporting = [d for d in docs if d.doc_type in OPTIONAL_DOC_TYPES]
    for d in list(by_type.values()) + [d for d in supporting if d.doc_type in ("DEBIT_NOTE", "CREDIT_NOTE")]:
        extract(d, model)
        found = sum(1 for f in d.fields.values() if f.status == "FOUND")
        log.info("[EXTRACT] %s: %d values found%s", d.short, found,
                 f" (error: {d.extraction_error})" if d.extraction_error else "")

    items, sheet = load_checklist(CONFIG.checklist_path)
    ctx = Ctx(by_type, items, model, today, all_docs=docs, supporting=supporting)
    clause = by_type.get(PURCHASE_CONTRACT).fields.get("payment_clause") if PURCHASE_CONTRACT in by_type else None
    ctx.payment = match_payment_term(clause.raw_value, items) if clause and clause.raw_value else {"status": "UNKNOWN"}
    log.info("[TERMS] purchase payment term -> %s (%s)", ctx.payment.get("rule_id"), ctx.payment.get("status"))
    for dt in (PURCHASE_CONTRACT, SALES_CONTRACT):
        if dt in by_type:
            ctx.signatures[dt] = detect_signatures(by_type[dt], model)
    build_views(ctx)
    checks, rollup = run_checks(ctx)
    for c in checks:
        log.info("[CHECK] %-18s %s", c.check_id, c.result)

    item_results = [r["result"] for r in rollup]
    overall = max(item_results, key=SEVERITY.get) if item_results else REVIEW
    statuses = (PASS, PASS_NOTE, REVIEW, MISMATCH, FAIL)
    count = lambda rs: {st: rs.count(st) for st in statuses}
    summary = {"checklist_items": {"total": len(rollup), **count(item_results)},
               "checks": {"total": len(checks), **count([c.result for c in checks])},
               "overall_status": overall,
               "overall_rule": "FAIL (missing mandatory document) > MISMATCH (confirmed discrepancy) > REVIEW (unresolved) "
                               "> PASS_WITH_NOTE (accepted with a stated limitation) > PASS"}
    s = summary["checklist_items"]
    log.info("[SUMMARY] %s items: PASS=%d NOTE=%d REVIEW=%d MISMATCH=%d FAIL=%d -> %s", case_id,
             s[PASS], s[PASS_NOTE], s[REVIEW], s[MISMATCH], s[FAIL], overall)

    return {
        "case_id": case_id, "case_path": case_dir, "commodity": "metals",
        "generated_at": datetime.now().isoformat(timespec="seconds"), "as_of_date": today.isoformat(),
        "model": {"provider": CONFIG.model_provider, "name": model.name},
        "checklist": {"path": CONFIG.checklist_path, "sheet": sheet},
        "documents": [_doc_dict(d) for d in docs],
        "payment_term": {k: v for k, v in ctx.payment.items()},
        "commercial_summary": commercial_summary(ctx),
        "transaction": transaction_summary(ctx, checks),
        "summary": summary,
        "checklist_items": rollup,
        "checks": [c.to_dict() for c in checks],
    }


def _doc_dict(d: Document) -> dict:
    return {"filename": d.filename, "doc_type": d.doc_type, "classification_status": d.classification_status,
            "classification_reason": d.classification_reason,
            "pages": [{"page": p.number, "text_source": p.method,
                       **({"ocr_notes": p.notes} if p.notes else {}),
                       **({"ocr_corrections": p.ocr_corrections} if p.ocr_corrections else {})} for p in d.pages],
            "number_locale": d.number_locale, "extraction_error": d.extraction_error,
            "fields": {k: f.evidence() for k, f in d.fields.items()},
            "line_items": [{k: f.evidence() for k, f in it.items()} for it in d.line_items],
            **({"source_pages": d.source_pages} if d.source_pages else {})}


def process_batch(root: str, model: LocalModel | None = None, today: date | None = None,
                  on_result=None) -> list[dict]:
    """Each case is independent: one failing case is recorded and the batch continues."""
    model = model or LocalModel()
    cases = sorted((os.path.join(root, d) for d in os.listdir(root) if is_case_dir(os.path.join(root, d))),
                   key=lambda p: (len(os.path.basename(p)), os.path.basename(p)))
    out = []
    for case in cases:
        try:
            res = process_case(case, model, today)
        except Exception as e:
            log.error("[CASE] %s crashed: %s", case, e)
            res = {"case_id": os.path.basename(case), "case_path": case, "error": f"{type(e).__name__}: {e}",
                   "traceback": traceback.format_exc(),
                   "summary": {"overall_status": REVIEW, "checklist_items": {}, "checks": {}}}
        out.append(res)
        if on_result:
            on_result(res)
    return out
