"""Microsoft Graph (Outlook) access for an organizational Microsoft 365 user.

Authentication: delegated OAuth with MSAL PublicClientApplication against the
organization's tenant (https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}).
The first run signs in with the device code flow; the MSAL token cache is saved to
MICROSOFT_TOKEN_CACHE (default .token_cache.json) so later runs (and token refreshes) need no interaction.
The mailbox is the signed-in user's own (/me). Tokens and passwords are never logged.

Ported unchanged in behaviour from the earlier TradePilot pilot (vrv-tradepilot/outlook.py); only the token
cache location became configurable and messages/attachments select the extra fields TradePilot records.
Only the Outlook worker imports this module: document processing never needs msal or httpx.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import msal

from .config import token_cache_path

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
# Delegated permissions: Mail.ReadWrite, Mail.Send and offline_access. MSAL always adds
# offline_access (refresh token), openid and profile itself and raises an error if they are
# passed explicitly, so only the Graph scopes are listed here. Never ".default".
SCOPES = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE_FILE = Path(token_cache_path())
MAX_RETRIES = 3
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Known sign-in errors -> what to do about them.
_HINTS = {
    "AADSTS7000218": "In Entra > App registration > Authentication, set 'Allow public client flows' to Yes.",
    "AADSTS700016": "MICROSOFT_CLIENT_ID is wrong, or the app is not registered in this tenant.",
    "AADSTS700038": "MICROSOFT_CLIENT_ID is wrong: use the Application (client) ID.",
    "AADSTS90002": "MICROSOFT_TENANT_ID is wrong (use the Directory (tenant) ID).",
    "AADSTS900023": "MICROSOFT_TENANT_ID is not a valid tenant ID.",
    "AADSTS50020": "You signed in with an account from a different organization than MICROSOFT_TENANT_ID.",
    "AADSTS65001": "Your organization requires an administrator to approve (consent to) this app. See README.",
    "AADSTS90094": "Your organization requires an administrator to approve (consent to) this app. See README.",
    "AADSTS53003": "Blocked by your organization's Conditional Access policy. Ask your IT administrator.",
    "authorization_declined": "The sign-in was cancelled or declined.",
    "expired_token": "The device code expired before sign-in was completed. Run again and sign in within 15 minutes.",
    "consent_required": "Consent is required. Sign in again and accept, or ask an administrator to grant consent.",
}

_app: msal.PublicClientApplication | None = None
_cache = msal.SerializableTokenCache()


class OutlookError(Exception):
    pass


# --------------------------------------------------------------------------- authentication


def _settings() -> tuple[str, str]:
    client_id = os.environ.get("MICROSOFT_CLIENT_ID", "").strip()
    tenant_id = os.environ.get("MICROSOFT_TENANT_ID", "").strip()
    if not client_id or not tenant_id:
        raise OutlookError("MICROSOFT_CLIENT_ID and MICROSOFT_TENANT_ID must be set in .env (see README)")
    if tenant_id.lower() in ("consumers", "common", "organizations"):
        raise OutlookError("MICROSOFT_TENANT_ID must be your organization's Directory (tenant) ID, not " + tenant_id)
    return client_id, tenant_id


def _load_cache() -> None:
    if not TOKEN_CACHE_FILE.exists():
        return
    try:
        _cache.deserialize(TOKEN_CACHE_FILE.read_text())
    except (OSError, ValueError) as exc:
        log.warning("Token cache %s is unreadable (%s); you will be asked to sign in again", TOKEN_CACHE_FILE, type(exc).__name__)


def _save_cache() -> None:
    if not _cache.has_state_changed:
        return
    try:
        TOKEN_CACHE_FILE.write_text(_cache.serialize())
        TOKEN_CACHE_FILE.chmod(0o600)  # readable by you only
        _cache.has_state_changed = False
    except OSError as exc:
        log.warning("Could not save %s (%s); you will need to sign in again next time", TOKEN_CACHE_FILE, exc)


def _application() -> msal.PublicClientApplication:
    global _app
    if _app is None:
        client_id, tenant_id = _settings()
        _load_cache()
        try:
            _app = msal.PublicClientApplication(
                client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", token_cache=_cache
            )
        except ValueError as exc:  # MSAL could not find the tenant
            raise OutlookError(f"MICROSOFT_TENANT_ID {tenant_id!r} was not found: use the Directory (tenant) ID") from exc
        except Exception as exc:  # e.g. no network connection to login.microsoftonline.com
            raise OutlookError(f"could not reach Microsoft sign-in ({type(exc).__name__})") from exc
    return _app


def _explain(result: dict) -> str:
    error = result.get("error", "unknown_error")
    description = (result.get("error_description") or "").splitlines()[0] if result.get("error_description") else ""
    codes = re.findall(r"AADSTS\d+", description) + [error]
    hint = next((_HINTS[c] for c in codes if c in _HINTS), "")
    return f"{error}: {description} {hint}".strip()


def _cached_account(app: msal.PublicClientApplication) -> dict | None:
    """The cached account for this tenant (ignores accounts from other tenants)."""
    _, tenant_id = _settings()
    accounts = app.get_accounts()
    if _GUID.match(tenant_id):
        matching = [a for a in accounts if a.get("home_account_id", "").lower().endswith("." + tenant_id.lower())]
        if accounts and not matching:
            log.info("Cached sign-in belongs to a different tenant; a new sign-in is needed")
        accounts = matching
    return accounts[0] if accounts else None


def _device_code_login(app: msal.PublicClientApplication) -> dict:
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise OutlookError(f"could not start sign-in: {_explain(flow)}")
    # Microsoft's message: "To sign in, use a web browser to open the page <url> and enter the code <code> ..."
    print("\n" + flow["message"] + "\n", flush=True)
    result = app.acquire_token_by_device_flow(flow)  # waits until sign-in completes, is cancelled, or expires
    if "access_token" not in result:
        raise OutlookError(f"sign-in failed: {_explain(result)}")
    return result


def get_access_token() -> str:
    """Silent token from the cache (refreshed by MSAL when needed); device code sign-in otherwise."""
    app = _application()
    account = _cached_account(app)
    result = None
    if account:
        result = app.acquire_token_silent_with_error(SCOPES, account=account)
        if result and "access_token" not in result:
            # login_required, interaction_required (e.g. AADSTS50058), invalid_grant (expired or
            # revoked refresh token), consent_required: the user has to sign in again.
            log.warning("Saved sign-in can no longer be used (%s); signing in again", result.get("error"))
            result = None
    if result is None:
        result = _device_code_login(app)
        claims = result.get("id_token_claims") or {}
        log.info("Signed in as %s", claims.get("preferred_username", "unknown user"))
    _save_cache()
    return result["access_token"]


def signed_in_user() -> str | None:
    account = _cached_account(_application())
    return account.get("username") if account else None


# --------------------------------------------------------------------------- HTTP


def _request(method: str, path: str, *, json: dict | None = None, retry_server_errors: bool = True) -> httpx.Response:
    """Call Graph. Retries on 429 (honouring Retry-After) and, unless disabled, on 5xx."""
    for attempt in range(MAX_RETRIES + 1):
        headers = {"Authorization": f"Bearer {get_access_token()}"}
        try:
            response = httpx.request(method, f"{GRAPH}/{path}", json=json, headers=headers, timeout=30)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES or not (retry_server_errors or isinstance(exc, httpx.ConnectError)):
                raise OutlookError(f"{method} {path.split('?')[0]}: {type(exc).__name__}") from exc
            delay = 2**attempt
        else:
            if response.status_code < 400:
                return response
            retry = response.status_code == 429 or (retry_server_errors and response.status_code >= 500)
            if not retry or attempt == MAX_RETRIES:
                raise OutlookError(_describe(response))
            retry_after = response.headers.get("Retry-After", "")
            delay = min(int(retry_after), 120) if retry_after.isdigit() else 2**attempt
        log.warning("Graph %s %s failed; retrying in %ss", method, path.split("?")[0], delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


def _describe(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
        return f"Graph returned {response.status_code}: {error.get('code')}: {error.get('message')}"
    except ValueError:
        return f"Graph returned {response.status_code}: {response.reason_phrase}"


def _id(message_id: str) -> str:
    return quote(message_id, safe="")


# --------------------------------------------------------------------------- mail


def list_recent_messages(since: str, limit: int = 50) -> list[dict]:
    """Inbox messages received at/after ``since`` (ISO UTC), oldest first."""
    params = (
        f"$filter=receivedDateTime ge {since}&$orderby=receivedDateTime desc&$top={limit}"
        "&$select=id,subject,from,sender,receivedDateTime,hasAttachments,internetMessageId,conversationId"
    )
    messages = _request("GET", f"me/mailFolders/inbox/messages?{params}").json().get("value", [])
    return list(reversed(messages))


def get_attachments(message_id: str) -> list[dict]:
    """Attachment metadata (id, name, contentType, size, isInline) — not the content."""
    path = f"me/messages/{_id(message_id)}/attachments?$select=id,name,contentType,size,isInline"
    return _request("GET", path).json().get("value", [])


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    data = _request("GET", f"me/messages/{_id(message_id)}/attachments/{_id(attachment_id)}").json()
    return base64.b64decode(data.get("contentBytes") or "")


def reply_to_message(message_id: str, body: str) -> None:
    """Reply in the original thread. Not retried on 5xx/timeouts, to never reply twice."""
    escaped = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = "<br>".join(escaped.splitlines())
    _request("POST", f"me/messages/{_id(message_id)}/reply", json={"comment": html}, retry_server_errors=False)


def mark_as_read(message_id: str) -> None:
    _request("PATCH", f"me/messages/{_id(message_id)}", json={"isRead": True})
