"""Outlook entry point: poll the inbox, turn each qualifying email into a trade, reply in the thread.

Email is only an adapter. For each new inbox message:

    1. sender check   the From (and Sender) address must be exactly @<ALLOWED_SENDER_DOMAIN>; otherwise the
                      message is recorded IGNORED_SENDER and nothing else happens (no download, no reply)
    2. PDF filter     decided from attachment metadata only; non-PDFs are never downloaded
    3. download       PDFs only; content must start like a PDF (%PDF-) or it is discarded
    4. trade          allocate TRD-YYYY-NNNNNN, save PDFs to source/, then processor.process_trade(), the
                      same function local mode uses, then record_trade() (metadata + register row)
    5. reply          the deterministic email_response.txt text, as a reply in the same thread

The email body is untrusted and never read: instructions in it cannot change any rule.
"""
from __future__ import annotations

import logging
import os
import time
from email.utils import parseaddr

from . import state as st
from .config import allowed_sender_domain, max_pdf_bytes, poll_interval_seconds, state_db_path
from .processor import _now, process_trade, record_trade
from .report import compose_email
from .trade_store import TradeStore

log = logging.getLogger("tradepilot")

FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"
PDF_TYPES = {"application/pdf", "application/x-pdf"}
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}


# ------------------------------------------------------------------ sender

def _address(field: dict | None) -> str | None:
    return ((field or {}).get("emailAddress") or {}).get("address")


def sender_address(message: dict) -> str | None:
    """The From address, normalised (lowercase, no display name), for logs, state and the register."""
    raw = _address(message.get("from")) or _address(message.get("sender"))
    return (parseaddr(raw)[1] or raw).strip().lower() if raw else None


def domain_allowed(address: str | None, domain: str | None = None) -> bool:
    """True only if the address's domain is exactly the allowed one (no substring/suffix matching)."""
    domain = (domain or allowed_sender_domain()).strip().lower()
    _, addr = parseaddr((address or "").strip())
    addr = addr.strip().lower()
    if not domain or addr.count("@") != 1 or any(ch.isspace() for ch in addr):
        return False
    local, dom = addr.split("@")
    return bool(local) and dom == domain


def sender_allowed(message: dict, domain: str | None = None) -> bool:
    """The From address must be allowed; a differing Sender (sent on behalf of) must be allowed too."""
    addresses = [a for a in (_address(message.get("from")), _address(message.get("sender"))) if a]
    return bool(addresses) and all(domain_allowed(a, domain) for a in addresses)


# ------------------------------------------------------------------ attachments

def is_pdf_attachment(att: dict) -> bool:
    """Decided from Graph metadata, before any download."""
    if att.get("@odata.type") != FILE_ATTACHMENT or att.get("isInline"):
        return False
    ctype = (att.get("contentType") or "").split(";")[0].strip().lower()
    name = (att.get("name") or "").strip().lower()
    has_ext = "." in name.rsplit("/", 1)[-1]
    if ctype in PDF_TYPES:
        return name.endswith(".pdf") or not has_ext
    return ctype in GENERIC_TYPES and name.endswith(".pdf")


def looks_like_pdf(content: bytes) -> bool:
    return b"%PDF-" in content[:1024]


FAILURE_BODY = """TradePilot assessment could not be completed.

Trade Ref: {ref}
Status: PROCESSING FAILED

The documents were received and saved under the trade reference above, but the automated check did not complete. No assessment result has been issued; please review this trade manually.

TradePilot
"""


# ------------------------------------------------------------------ one message

def process_message(message: dict, state: st.State, store: TradeStore, *, outlook=None, model=None,
                    today=None, engine=process_trade) -> str | None:
    """Handle one inbox message. Returns the state status recorded, or None if it was already handled."""
    outlook = outlook or _outlook()
    mid = message["id"]
    if not state.should_process(mid):
        return None
    sender, subject = sender_address(message), message.get("subject")
    log.info("[EMAIL] %r from %s", subject, sender)

    if not sender_allowed(message):
        log.info("[EMAIL] Ignored: sender domain not %s", allowed_sender_domain())
        state.save(mid, st.IGNORED_SENDER, sender=sender, subject=subject, received_at=message.get("receivedDateTime"))
        return st.IGNORED_SENDER

    previous = state.get(mid) or {}
    try:
        attachments = outlook.get_attachments(mid)
        pdfs = []
        for att in attachments:
            if not is_pdf_attachment(att):
                log.info("[ATTACHMENT] Ignored non-PDF: %s (%s)", att.get("name"), att.get("contentType"))
            elif (att.get("size") or 0) > max_pdf_bytes():
                log.info("[ATTACHMENT] Ignored PDF over %d MB: %s", max_pdf_bytes() // 2**20, att.get("name"))
            else:
                pdfs.append(att)
        downloaded = []
        for att in pdfs:
            content = outlook.download_attachment(mid, att["id"])
            if looks_like_pdf(content):
                downloaded.append((att, content))
                log.info("[ATTACHMENT] PDF %s (%d KB)", att.get("name"), len(content) // 1024)
            else:
                log.info("[ATTACHMENT] Discarded %s: content is not a PDF", att.get("name"))
    except outlook.OutlookError as e:
        log.error("[OUTLOOK] %s; will retry on a later poll", e)
        state.save(mid, st.FAILED, sender=sender, subject=subject, error=str(e)[:500])
        return st.FAILED

    if not downloaded:
        # Nothing to verify (e.g. a plain reply in the thread). Replying could start a reply loop.
        log.info("[EMAIL] Ignored: no PDF attachments")
        state.save(mid, st.IGNORED_NO_PDF, sender=sender, subject=subject, received_at=message.get("receivedDateTime"))
        return st.IGNORED_NO_PDF

    # -------- the trade (a retry after a crash resumes the same trade_ref)
    ref = previous.get("trade_ref") or store.allocate()
    state.save(mid, st.PROCESSING, trade_ref=ref, sender=sender, subject=subject,
               received_at=message.get("receivedDateTime"), error=None)
    log.info("[TRADE] %s for message from %s", ref, sender)
    metadata = store.read_metadata(ref) if previous.get("trade_ref") else {}
    saved = {a["attachment_id"]: a for a in metadata.get("attachments", [])}
    for att, content in downloaded:
        if att["id"] not in saved:
            path = store.save_source_pdf(ref, att.get("name"), content)
            saved[att["id"]] = {"attachment_id": att["id"], "original_name": att.get("name"),
                                "stored_as": os.path.basename(path), "size": len(content)}
    metadata = {"source": "email", "graph_message_id": mid, "internet_message_id": message.get("internetMessageId"),
                "conversation_id": message.get("conversationId"), "sender": sender, "subject": subject,
                "received_at": message.get("receivedDateTime"), "attachments": list(saved.values()),
                "pdf_count": len(saved), "processing_status": "PROCESSING"}
    store.write_metadata(ref, dict(metadata, trade_ref=ref))

    try:
        res = engine(store.source_folder(ref), store.folder(ref), case_id=ref, model=model, today=today)
        body, outcome = compose_email(res, ref), "COMPLETED"
    except Exception as e:
        log.exception("[VERIFY] %s processing failed", ref)
        res, body, outcome = None, FAILURE_BODY.format(ref=ref), "FAILED"
        metadata.update(overall_status="FAILED", notes=f"processing failed: {type(e).__name__}: {e}"[:500])
    metadata.update(processing_status=outcome, processed_at=_now(), reply_status="pending")
    record_trade(store, ref, res, metadata)

    # -------- reply (never retried: a message is never answered twice)
    state.save(mid, st.REPLYING, trade_ref=ref)
    final = st.COMPLETED if outcome == "COMPLETED" else st.FAILED
    try:
        outlook.reply_to_message(mid, body)
        replied_at = st.now_utc()
        metadata.update(reply_status="sent", replied_at=replied_at)
        state.save(mid, final, trade_ref=ref, replied_at=replied_at, reply_status="sent",
                   error=metadata.get("notes") if final == st.FAILED else None)
        log.info("[EMAIL] Reply sent for %s", ref)
    except Exception as e:
        metadata.update(reply_status=f"failed: {e}"[:300])
        state.save(mid, final, trade_ref=ref, reply_status="failed", error=f"reply failed: {e}"[:500])
        log.error("[EMAIL] Reply failed for %s: %s (not retried)", ref, e)
    record_trade(store, ref, res, metadata)
    try:
        outlook.mark_as_read(mid)
    except Exception as e:                          # cosmetic; the message is already recorded
        log.warning("[OUTLOOK] Could not mark as read: %s", e)
    return final


# ------------------------------------------------------------------ polling

def poll_once(state: st.State, store: TradeStore, *, outlook=None, model=None, today=None,
              engine=process_trade) -> list[str | None]:
    outlook = outlook or _outlook()
    messages = [m for m in outlook.list_recent_messages(state.start_time()) if state.should_process(m["id"])]
    log.info("[OUTLOOK] %d new message(s)", len(messages))
    return [process_message(m, state, store, outlook=outlook, model=model, today=today, engine=engine)
            for m in messages]


def run(*, once: bool = False, model=None) -> int:
    """Poll the inbox every POLL_INTERVAL_SECONDS (or once). Stops with Ctrl+C."""
    outlook = _outlook()
    try:
        outlook.get_access_token()                 # interactive device-code login only if the cache has none
    except outlook.OutlookError as e:
        log.error("[OUTLOOK] Cannot sign in: %s", e)
        return 2
    state, store = st.State(state_db_path()), TradeStore()
    log.info("[OUTLOOK] Mailbox %s; accepting senders @%s; trades in %s; processing mail received after %s",
             outlook.signed_in_user(), allowed_sender_domain(), store.root, state.start_time())
    log.info("[OUTLOOK] Polling inbox every %ss (Ctrl+C to stop)", poll_interval_seconds())
    try:
        while True:
            try:
                poll_once(state, store, outlook=outlook, model=model)
            except outlook.OutlookError as e:
                log.error("[OUTLOOK] Poll failed: %s", e)
            if once:
                return 0
            time.sleep(poll_interval_seconds())
    except KeyboardInterrupt:
        # A message interrupted mid-processing stays PROCESSING and is resumed (same trade ref) next start.
        log.info("[OUTLOOK] Stopped")
        return 0


def _outlook():
    """Imported on first use: Graph/MSAL are needed only in Outlook mode (and read config at import)."""
    from . import outlook
    return outlook
