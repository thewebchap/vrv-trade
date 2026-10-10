"""Test doubles for the trade/Outlook workflow: a fake Graph mailbox and a fake verification engine.

The fake engine returns a structured result in the engine's own format and writes it with the real
report writer, so storage, register and email formatting are exercised without OCR or a model.
"""
from __future__ import annotations

import os

from tradepilot.report import write_case

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
MANDATORY = [("PC.pdf", "PURCHASE_CONTRACT"), ("PI.pdf", "PURCHASE_INVOICE"), ("BL.pdf", "BILL_OF_LADING"),
             ("SC.pdf", "SALES_CONTRACT"), ("SI.pdf", "SALES_INVOICE")]


def check(check_id, result, reason="ok", comparisons=None, scope="CASE_SCOPE"):
    return {"check_id": check_id, "title": check_id, "checklist_ref": None, "rule_type": "DETERMINISTIC",
            "result": result, "reason": reason, "evidence": [], "comparisons": comparisons or [], "scope": scope}


def make_result(case_id, checks, filenames=None):
    filenames = filenames or [f for f, _ in MANDATORY]
    types = dict(MANDATORY)
    docs = [{"filename": f, "doc_type": types.get(f, "OTHER_SUPPORTING"), "classification_status": "CLASSIFIED",
             "classification_reason": "test", "pages": [{"page": 1, "text_source": "pdf_text"}],
             "number_locale": None, "extraction_error": None, "fields": {}, "line_items": []} for f in filenames]
    statuses = ("PASS", "PASS_WITH_NOTE", "REVIEW", "MISMATCH", "FAIL")
    counts = {s: sum(c["result"] == s for c in checks) for s in statuses}
    worst = next((s for s in reversed(statuses) if counts[s]), "PASS")
    return {"case_id": case_id, "case_path": "test", "commodity": "METALS", "generated_at": "2026-10-10T00:00:00",
            "as_of_date": "2026-10-10", "model": {"provider": "fake", "name": "fake"},
            "checklist": {"path": "checklist.xlsx", "sheet": "Sheet4"}, "documents": docs,
            "payment_term": {"status": "IDENTIFIED", "rule_id": "METALS-2", "differences": [], "features": {},
                             "feature_notes": [], "advance_text": None, "tied_with": []},
            "commercial_summary": [], "transaction": {}, "checklist_items": [], "checks": checks,
            "summary": {"checklist_items": {"total": 0}, "checks": dict(counts, total=len(checks)),
                        "overall_status": worst, "overall_rule": ""}}


GOOD = [check("QTY.CONTRACT", "PASS"), check("PRICE.CONTRACT", "PASS"),
        check("DOCS.SUPPORTING", "PASS_WITH_NOTE", "no supporting documents")]
MISMATCH = GOOD + [check("QTY.CONTRACT", "MISMATCH", comparisons=[{
    "comparison": "PI quantity vs PC quantity", "calculation": "108.8500 MT vs allowed 95.0000-105.0000 MT",
    "result": "MISMATCH", "detail": "exceeds the maximum by 3850.0 kg", "scope": "PURCHASE",
    "left": {"doc": "PI", "normalized_value": 108.85, "unit": "MT"},
    "right": {"doc": "PC", "normalized_value": 100.0, "unit": "MT"}}])]
REVIEW = GOOD + [check("SHIP.VESSEL", "REVIEW", comparisons=[{
    "a": "PI vessel", "a_value": "KMIC DELHI", "b": "BL vessel", "b_value": "KMTC DELHI", "outcome": "UNSURE",
    "result": "REVIEW", "detail": "very similar (82%); possible OCR variation"}])]


class FakeEngine:
    """Stands in for processor.process_trade; records what it was given."""

    def __init__(self, checks=GOOD, error=None):
        self.checks, self.error, self.calls = checks, error, []

    def __call__(self, source_folder, output_folder, *, case_id=None, model=None, today=None):
        files = sorted(os.listdir(source_folder))
        self.calls.append({"source": source_folder, "output": output_folder, "case_id": case_id, "files": files})
        if self.error:
            raise self.error
        res = make_result(case_id, list(self.checks), files)
        write_case(res, output_folder)
        return res


class OutlookError(Exception):
    pass


class FakeOutlook:
    """The functions of tradepilot.outlook used by the mailbox worker, backed by dicts."""

    OutlookError = OutlookError

    def __init__(self, messages=(), attachments=None, contents=None):
        self.messages = list(messages)
        self.attachments = attachments or {}           # message id -> attachment metadata list
        self.contents = contents or {}                 # attachment id -> bytes
        self.downloads, self.replies, self.read = [], [], []
        self.fail_reply = self.fail_download = None

    def list_recent_messages(self, since, limit=50):
        return list(self.messages)

    def get_attachments(self, message_id):
        return self.attachments.get(message_id, [])

    def download_attachment(self, message_id, attachment_id):
        if self.fail_download:
            raise self.fail_download
        self.downloads.append(attachment_id)
        return self.contents[attachment_id]

    def reply_to_message(self, message_id, body):
        if self.fail_reply:
            raise self.fail_reply
        self.replies.append((message_id, body))

    def mark_as_read(self, message_id):
        self.read.append(message_id)


def message(mid="m1", sender="ops@vrvglobal.com", subject="Trade docs", body="please check", on_behalf=None):
    m = {"id": mid, "subject": subject, "receivedDateTime": "2026-10-10T08:00:00Z", "hasAttachments": True,
         "internetMessageId": f"<{mid}@mail>", "conversationId": f"conv-{mid}",
         "from": {"emailAddress": {"address": sender}}, "bodyPreview": body}
    if on_behalf:
        m["sender"] = {"emailAddress": {"address": on_behalf}}
    return m


def attachment(aid, name, content_type="application/pdf", size=1000, inline=False,
               kind="#microsoft.graph.fileAttachment"):
    return {"id": aid, "name": name, "contentType": content_type, "size": size, "isInline": inline,
            "@odata.type": kind}


def mailbox_with_pdfs(mid="m1", names=tuple(f for f, _ in MANDATORY), sender="ops@vrvglobal.com", extra=()):
    atts = [attachment(f"a{i}", n) for i, n in enumerate(names)] + list(extra)
    contents = {a["id"]: PDF for a in atts}
    return FakeOutlook([message(mid, sender)], {mid: atts}, contents)
