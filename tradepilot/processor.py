"""The one trade processor shared by every entry point (local CLI, Outlook worker, tests).

    process_trade(source_folder, output_folder)   existing verification engine + existing report writer
    record_trade(store, ref, result, metadata)     trade_metadata.json + one row in the master register
    save_local_trade(store, case_dir)              local mode with --save-trade: copy PDFs into a new trade

Email is only an input/output adapter around these functions (see mailbox.py).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime

from .pipeline import discover, process_case
from .report import write_case
from .trade_store import TradeStore, register_row

log = logging.getLogger("tradepilot")


def process_trade(source_folder: str, output_folder: str, *, case_id: str | None = None, model=None,
                  today: date | None = None) -> dict:
    """Verify the PDFs in source_folder and write result.json, report.md, report.xlsx and
    email_response.txt into output_folder. Identical for email and local mode."""
    res = process_case(source_folder, model, today, case_id=case_id)
    write_case(res, output_folder)
    s = res["summary"]["checks"]
    log.info("[VERIFY] PASS=%s NOTE=%s REVIEW=%s MISMATCH=%s FAIL=%s -> %s", s.get("PASS"), s.get("PASS_WITH_NOTE"),
             s.get("REVIEW"), s.get("MISMATCH"), s.get("FAIL"), res["summary"]["overall_status"])
    log.info("[REPORT] %s", os.path.join(output_folder, "report.xlsx"))
    return res


def record_trade(store: TradeStore, ref: str, res: dict | None, metadata: dict) -> dict:
    """Write trade_metadata.json and insert/update the trade's single row in the master register."""
    metadata = dict(metadata, trade_ref=ref)
    if res is not None:
        metadata["overall_status"] = res["summary"]["overall_status"]
        metadata["documents"] = {d["filename"]: d["doc_type"] for d in res.get("documents", [])}
    store.write_metadata(ref, metadata)
    store.upsert_register(register_row(ref, res, metadata, store.folder(ref)))
    log.info("[REGISTER] %s updated (%s)", os.path.basename(store.register_path), ref)
    return metadata


def save_local_trade(store: TradeStore, case_dir: str, *, model=None, today: date | None = None) -> tuple[str, dict]:
    """Local mode with --save-trade: register a local folder as a trade, exactly like an email trade."""
    ref = store.allocate()
    log.info("[TRADE] Allocated %s for %s", ref, case_dir)
    pdfs = discover(case_dir)
    for path in pdfs:
        with open(path, "rb") as f:
            store.save_source_pdf(ref, os.path.basename(path), f.read())
    metadata = {"source": "local", "local_path": os.path.abspath(case_dir), "graph_message_id": None,
                "internet_message_id": None, "sender": None, "subject": None, "received_at": None,
                "pdf_count": len(pdfs), "processing_status": "PROCESSING"}
    store.write_metadata(ref, dict(metadata, trade_ref=ref))
    try:
        res = process_trade(store.source_folder(ref), store.folder(ref), case_id=ref, model=model, today=today)
    except Exception as e:
        record_trade(store, ref, None, dict(metadata, processing_status="FAILED", overall_status="FAILED",
                                            processed_at=_now(), notes=f"processing failed: {type(e).__name__}: {e}"))
        raise
    record_trade(store, ref, res, dict(metadata, processing_status="COMPLETED", processed_at=_now(),
                                       reply_status="not applicable (local)"))
    log.info("[TRADE] %s completed", ref)
    return ref, res


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
