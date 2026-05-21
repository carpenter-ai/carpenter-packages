"""Pre-verified executor scripts for the carpenter-email templates.

These are exact strings the package's chat tools embed into the
EXECUTOR child arc's goal so the EXECUTOR submits them verbatim via
``submit_code`` (no agent-side code generation, by design).  Same
pattern as the platform's ``_FETCH_SCRIPT`` for ``fetch_web_content``.

Trust contract:

* The script runs in the platform's existing executor sandbox (D24
  I8: untrusted EXECUTOR can never read trusted Resources or KB).
* It only uses ``dispatch(Label("..."))`` calls into platform RPCs
  the EXECUTOR is allowed to call: ``state.get``, ``web.get``,
  ``web.post``, ``files.write``, ``resource.finalize``.
* The package author audits this string once at install time; the
  AST lint should treat it as an untrusted blob the chat tool sends
  through, not as code the agent generated.
"""

from __future__ import annotations


# Gmail message-fetch script.
#
# The chat tool pre-seeds the EXECUTOR's arc state with:
#   * provider_message_id : str  (Gmail message id to fetch)
#   * raw_resource_path   : str  (where to write the JSON blob)
#   * raw_resource_id     : int  (the Resource row to finalize)
#
# OAuth credentials are read from os.environ (the EXECUTOR sandbox
# already has env passthrough for the GMAIL_OAUTH_* prefix written
# by ``carpenter.api.oauth``).
GMAIL_FETCH_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json

# Inputs from arc state
mid_result = dispatch(Label("state.get"), {"key": Label("provider_message_id")})
mid = mid_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

# OAuth bearer from env (written by carpenter.api.oauth)
access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError(
        "GMAIL_OAUTH_ACCESS_TOKEN not in environment; "
        "run pkg_email_authorize first"
    )

# Hit Gmail REST API (gmail.googleapis.com is in the platform domain
# allowlist after install).
url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    + mid
    + "?format=full"
)
result = dispatch(Label("web.get"), {
    "url": url,
    "headers": {"Authorization": "Bearer " + access_token},
})
status = result[Label("status_code")]
if status != 200:
    raise RuntimeError(
        "Gmail API GET failed: status=" + str(status)
        + " body=" + result[Label("text")][:200]
    )

# Persist response body to the raw Resource blob and finalize.
dispatch(Label("files.write"), {
    "path": output_path,
    "content": result[Label("text")],
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Gmail send script.
#
# The chat tool pre-seeds the EXECUTOR's arc state with:
#   * raw_message_b64        : str  (RFC-822 raw message, base64url-encoded)
#   * expected_account_email : str  (mailbox we expect to send from)
#   * raw_resource_path      : str  (where to write the JSON receipt)
#   * raw_resource_id        : int  (the Resource row to finalize)
#
# The script first verifies the access token's account email matches
# expected_account_email (defence against a swapped-in refresh token
# attack at the chat-boundary trust check), then POSTs the message,
# parses the Gmail-issued message id out of the response, and writes a
# structured JSON receipt to the raw Resource so the REVIEWER + JUDGE
# can graduate a typed EmailSendResult dataclass into trusted state.
GMAIL_SEND_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

raw_result = dispatch(Label("state.get"), {"key": Label("raw_message_b64")})
raw_b64 = raw_result[Label("value")]
exp_result = dispatch(Label("state.get"), {"key": Label("expected_account_email")})
expected = exp_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError(
        "GMAIL_OAUTH_ACCESS_TOKEN not in environment; "
        "run pkg_email_authorize first"
    )

# Step 1: verify token belongs to expected_account_email
who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
who_status = who[Label("status_code")]
if who_status != 200:
    raise RuntimeError(
        "userinfo lookup failed: status=" + str(who_status)
    )
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected or "").strip().lower():
    raise RuntimeError(
        "expected-account check failed: token belongs to "
        + repr(actual) + ", briefing said " + repr(expected)
    )

# Step 2: send via gmail.users.messages.send
send_result = dispatch(Label("web.post"), {
    "url": "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    "headers": {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    },
    "json_data": {"raw": raw_b64},
})
send_status = send_result[Label("status_code")]
if send_status not in (200, 202):
    raise RuntimeError(
        "Gmail send failed: status=" + str(send_status)
        + " body=" + send_result[Label("text")][:200]
    )
send_body = _json.loads(send_result[Label("text")])
provider_message_id = send_body.get("id") or ""

# Step 3: write a structured receipt for the REVIEWER + JUDGE.
receipt = {
    "operation": "send",
    "expected_account_email": actual,
    "provider_message_id": provider_message_id,
    "status_code": send_status,
}
dispatch(Label("files.write"), {
    "path": output_path,
    "content": _json.dumps(receipt),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Gmail search script.
#
# The chat tool pre-seeds:
#   * search_query : str  (Gmail search syntax, e.g. "newer_than:7d invoice")
#   * max_results  : int
#   * id_list_path : str  (output JSON file path)
#
# Writes a JSON file with [{id, threadId}, ...] for the chat tool to
# parse.  Used to seed N email_read_simple_text child arcs.
GMAIL_SEARCH_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json
from urllib.parse import quote_plus

q_result = dispatch(Label("state.get"), {"key": Label("search_query")})
q = q_result[Label("value")]
n_result = dispatch(Label("state.get"), {"key": Label("max_results")})
max_results = n_result[Label("value")]
out_result = dispatch(Label("state.get"), {"key": Label("id_list_path")})
out_path = out_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError("GMAIL_OAUTH_ACCESS_TOKEN not in environment")

# Use full URL-encoding (quote_plus): a Gmail search query may contain
# &, #, ?, and other reserved characters that ``str.replace(" ", "+")``
# would not escape, breaking the request URL.
url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    + "?maxResults=" + str(max_results)
    + "&q=" + quote_plus(q)
)
result = dispatch(Label("web.get"), {
    "url": url,
    "headers": {"Authorization": "Bearer " + access_token},
})
status = result[Label("status_code")]
if status != 200:
    raise RuntimeError(
        "Gmail search failed: status=" + str(status)
    )
dispatch(Label("files.write"), {
    "path": out_path,
    "content": result[Label("text")],
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Gmail archive script.
#
# The chat tool pre-seeds the EXECUTOR's arc state with:
#   * provider_message_id     : str  (Gmail message id to archive)
#   * expected_account_email  : str  (mailbox we expect to be acting on)
#   * raw_resource_path       : str  (where to write the JSON receipt)
#   * raw_resource_id         : int  (the Resource row to finalize)
#
# "Archive" in Gmail terms means removing the INBOX label.  The script
# first verifies the OAuth token's account email matches expected
# (defence against a swapped-in refresh token), reads the message's
# current labelIds to determine ``was_already_archived``, POSTs the
# modify request, then writes a structured receipt to the raw Resource
# for the REVIEWER + JUDGE to graduate as an EmailArchiveResult.
# Idempotent: Gmail's modify endpoint accepts ``removeLabelIds=["INBOX"]``
# even when the label is already absent.
GMAIL_ARCHIVE_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

mid_result = dispatch(Label("state.get"), {"key": Label("provider_message_id")})
mid = mid_result[Label("value")]
exp_result = dispatch(Label("state.get"), {"key": Label("expected_account_email")})
expected = exp_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError(
        "GMAIL_OAUTH_ACCESS_TOKEN not in environment; "
        "run pkg_email_authorize first"
    )

# Step 1: verify token belongs to expected_account_email
who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
who_status = who[Label("status_code")]
if who_status != 200:
    raise RuntimeError(
        "userinfo lookup failed: status=" + str(who_status)
    )
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected or "").strip().lower():
    raise RuntimeError(
        "expected-account check failed: token belongs to "
        + repr(actual) + ", briefing said " + repr(expected)
    )

# Step 2: read current labelIds to compute was_already_archived
meta_url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    + mid
    + "?format=metadata"
)
meta = dispatch(Label("web.get"), {
    "url": meta_url,
    "headers": {"Authorization": "Bearer " + access_token},
})
meta_status = meta[Label("status_code")]
if meta_status != 200:
    raise RuntimeError(
        "Gmail metadata GET failed: status=" + str(meta_status)
    )
meta_body = _json.loads(meta[Label("text")])
current_labels = meta_body.get("labelIds") or []
was_already_archived = "INBOX" not in current_labels

# Step 3: modify (idempotent — removing a label that's not present is a no-op)
modify_url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    + mid
    + "/modify"
)
modify_result = dispatch(Label("web.post"), {
    "url": modify_url,
    "headers": {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    },
    "json_data": {"removeLabelIds": ["INBOX"]},
})
modify_status = modify_result[Label("status_code")]
if modify_status not in (200, 202):
    raise RuntimeError(
        "Gmail modify failed: status=" + str(modify_status)
        + " body=" + modify_result[Label("text")][:200]
    )

# Step 4: write a structured receipt for the REVIEWER + JUDGE.
receipt = {
    "operation": "archive",
    "expected_account_email": actual,
    "provider_message_id": mid,
    "was_already_archived": was_already_archived,
    "status_code": modify_status,
}
dispatch(Label("files.write"), {
    "path": output_path,
    "content": _json.dumps(receipt),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Gmail mark-as-read script.
#
# The chat tool pre-seeds the EXECUTOR's arc state with:
#   * provider_message_id     : str
#   * expected_account_email  : str
#   * raw_resource_path       : str  (where to write the JSON receipt)
#   * raw_resource_id         : int  (the Resource row to finalize)
#
# Mark-read removes the UNREAD label.  Same shape as the archive
# script: expected-account check, read current labelIds to compute
# was_already_read, modify, then emit a structured receipt for the
# REVIEWER + JUDGE to graduate as an EmailMarkReadResult.
GMAIL_MARK_READ_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

mid_result = dispatch(Label("state.get"), {"key": Label("provider_message_id")})
mid = mid_result[Label("value")]
exp_result = dispatch(Label("state.get"), {"key": Label("expected_account_email")})
expected = exp_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError(
        "GMAIL_OAUTH_ACCESS_TOKEN not in environment; "
        "run pkg_email_authorize first"
    )

# Step 1: verify token belongs to expected_account_email
who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
who_status = who[Label("status_code")]
if who_status != 200:
    raise RuntimeError(
        "userinfo lookup failed: status=" + str(who_status)
    )
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected or "").strip().lower():
    raise RuntimeError(
        "expected-account check failed: token belongs to "
        + repr(actual) + ", briefing said " + repr(expected)
    )

# Step 2: read current labelIds to compute was_already_read
meta_url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    + mid
    + "?format=metadata"
)
meta = dispatch(Label("web.get"), {
    "url": meta_url,
    "headers": {"Authorization": "Bearer " + access_token},
})
meta_status = meta[Label("status_code")]
if meta_status != 200:
    raise RuntimeError(
        "Gmail metadata GET failed: status=" + str(meta_status)
    )
meta_body = _json.loads(meta[Label("text")])
current_labels = meta_body.get("labelIds") or []
was_already_read = "UNREAD" not in current_labels

# Step 3: modify
modify_url = (
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
    + mid
    + "/modify"
)
modify_result = dispatch(Label("web.post"), {
    "url": modify_url,
    "headers": {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    },
    "json_data": {"removeLabelIds": ["UNREAD"]},
})
modify_status = modify_result[Label("status_code")]
if modify_status not in (200, 202):
    raise RuntimeError(
        "Gmail modify failed: status=" + str(modify_status)
        + " body=" + modify_result[Label("text")][:200]
    )

# Step 4: write a structured receipt for the REVIEWER + JUDGE.
receipt = {
    "operation": "mark_read",
    "expected_account_email": actual,
    "provider_message_id": mid,
    "was_already_read": was_already_read,
    "status_code": modify_status,
}
dispatch(Label("files.write"), {
    "path": output_path,
    "content": _json.dumps(receipt),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Gmail draft-create script.
#
# The chat tool pre-seeds the EXECUTOR's arc state with:
#   * raw_message_b64        : str  (RFC-822 raw message, base64url-encoded)
#   * expected_account_email : str  (mailbox we expect to be drafting under)
#   * raw_resource_path      : str  (where to write the JSON receipt)
#   * raw_resource_id        : int  (the Resource row to finalize)
#
# Each call creates a NEW draft.  There is no update-draft tool in
# Phase 1.5 because sending a stale draft would bypass the chat-boundary
# re-confirm on body content; updates would need to round-trip back
# through pkg_email_send_email re-confirmation.  The Gmail-assigned
# draft_id and provider_message_id of the staged message are written
# to the raw Resource for the REVIEWER + JUDGE to graduate as an
# EmailDraftResult.
GMAIL_DRAFT_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

raw_result = dispatch(Label("state.get"), {"key": Label("raw_message_b64")})
raw_b64 = raw_result[Label("value")]
exp_result = dispatch(Label("state.get"), {"key": Label("expected_account_email")})
expected = exp_result[Label("value")]
path_result = dispatch(Label("state.get"), {"key": Label("raw_resource_path")})
output_path = path_result[Label("value")]
rid_result = dispatch(Label("state.get"), {"key": Label("raw_resource_id")})
raw_resource_id = rid_result[Label("value")]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError(
        "GMAIL_OAUTH_ACCESS_TOKEN not in environment; "
        "run pkg_email_authorize first"
    )

# Step 1: verify token belongs to expected_account_email
who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
who_status = who[Label("status_code")]
if who_status != 200:
    raise RuntimeError(
        "userinfo lookup failed: status=" + str(who_status)
    )
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected or "").strip().lower():
    raise RuntimeError(
        "expected-account check failed: token belongs to "
        + repr(actual) + ", briefing said " + repr(expected)
    )

# Step 2: create the draft via gmail.users.drafts.create
draft_result = dispatch(Label("web.post"), {
    "url": "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
    "headers": {
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
    },
    "json_data": {"message": {"raw": raw_b64}},
})
draft_status = draft_result[Label("status_code")]
if draft_status not in (200, 202):
    raise RuntimeError(
        "Gmail draft create failed: status=" + str(draft_status)
        + " body=" + draft_result[Label("text")][:200]
    )
draft_body = _json.loads(draft_result[Label("text")])
draft_id = draft_body.get("id") or ""
message = draft_body.get("message") or {}
provider_message_id = message.get("id") or ""

# Step 3: write a structured receipt for the REVIEWER + JUDGE.
receipt = {
    "operation": "draft",
    "expected_account_email": actual,
    "provider_message_id": provider_message_id,
    "draft_id": draft_id,
    "status_code": draft_status,
}
dispatch(Label("files.write"), {
    "path": output_path,
    "content": _json.dumps(receipt),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# ---------------------------------------------------------------------------
# Phase 4: Semantic resource index scripts.
#
# These three EXECUTOR scripts feed the email vector index.  None of
# them invoke ``state.set`` or any embedding/vector RPC: their only job
# is to call Gmail, write a JSON ``EmailIndexFetchedBatch`` document to
# disk for the REVIEWER + JUDGE, and finalize the resource.  Embedding
# and upsert happen in the trusted trigger callback *after* JUDGE
# verdict (D24 I3 closure).
#
# Pre-seeded arc state for all three scripts:
#   * raw_resource_path     : str  (where to write the JSON batch)
#   * raw_resource_id       : int  (the Resource row to finalize)
#   * expected_account_email: str  (used by Phase 1 / incremental;
#                                   Phase 2 also enforces it)
#   * model_identity        : str  (current embedding model identity;
#                                   the script does not embed but the
#                                   JUDGE compares it against the
#                                   trigger snapshot)
#   * batch_id              : str  (opaque id for the batch)
#   * phase                 : str  (one of EMAIL_INDEX_PHASES)
#
# Phase-specific state keys are listed inline.

# Phase 1: backfill by descending Gmail ``internalDate``.
#
# Pre-seeded state keys:
#   * watermark_before : str (Gmail internalDate at or above which the
#                             EXECUTOR skips; "" on first run)
#   * max_batch        : int (capped by JUDGE to 100)
#
# Watermark format is the bare Gmail ``internalDate`` numeric string
# (matches the JUDGE's ``^[a-zA-Z0-9_:.-]{0,128}$`` shape).
GMAIL_INDEX_PHASE1_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

# ----- inputs ---------------------------------------------------------
def _state(key):
    return dispatch(Label("state.get"), {"key": Label(key)})[Label("value")]

raw_resource_path     = _state("raw_resource_path")
raw_resource_id       = _state("raw_resource_id")
expected_account      = _state("expected_account_email")
model_identity        = _state("model_identity")
batch_id              = _state("batch_id")
phase                 = _state("phase")
watermark_before = _state("watermark_before")
max_batch        = int(_state("max_batch"))
if max_batch < 1:
    max_batch = 1
if max_batch > 100:
    max_batch = 100

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError("GMAIL_OAUTH_ACCESS_TOKEN not present in env")

# ----- userinfo enforcement ------------------------------------------
who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
if who[Label("status_code")] != 200:
    raise RuntimeError("userinfo lookup failed: " + str(who[Label("status_code")]))
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected_account or "").strip().lower():
    raise RuntimeError(
        "expected-account check failed: token=" + repr(actual)
        + " briefing=" + repr(expected_account)
    )

# ----- list ids -------------------------------------------------------
list_result = dispatch(Label("web.get"), {
    "url": (
        "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        "?maxResults=" + str(max_batch)
        + "&includeSpamTrash=false"
    ),
    "headers": {"Authorization": "Bearer " + access_token},
})
if list_result[Label("status_code")] != 200:
    raise RuntimeError(
        "messages.list failed: " + str(list_result[Label("status_code")])
        + " body=" + list_result[Label("text")][:200]
    )
list_body = _json.loads(list_result[Label("text")])
ids = [m.get("id") or "" for m in (list_body.get("messages") or [])]
ids = [mid for mid in ids if mid]

# ----- fetch metadata for each id ------------------------------------
entries = []
skipped = 0
new_watermark = watermark_before

for mid in ids:
    meta = dispatch(Label("web.get"), {
        "url": (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            + mid + "?format=metadata"
            + "&metadataHeaders=Subject"
            + "&metadataHeaders=From"
            + "&metadataHeaders=To"
            + "&metadataHeaders=Cc"
            + "&metadataHeaders=Date"
        ),
        "headers": {"Authorization": "Bearer " + access_token},
    })
    if meta[Label("status_code")] != 200:
        skipped += 1
        continue
    m = _json.loads(meta[Label("text")])
    headers = {h.get("name", ""): h.get("value", "") for h in (m.get("payload") or {}).get("headers", [])}
    internal_date = m.get("internalDate") or ""
    # Skip past-watermark messages (descending walk).
    if watermark_before and internal_date and internal_date >= watermark_before:
        skipped += 1
        continue
    entries.append({
        "provider_message_id": m.get("id") or mid,
        "thread_id": m.get("threadId") or "",
        "internal_date": internal_date,
        "subject": headers.get("Subject", "")[:512],
        "from_header": headers.get("From", "")[:256],
        "to_header": headers.get("To", "")[:1024],
        "cc_header": headers.get("Cc", "")[:1024],
        "date_header": headers.get("Date", "")[:128],
        "label_ids": tuple(m.get("labelIds") or ()),
        "snippet": (m.get("snippet") or "")[:512],
        "schema_version": 1,
    })
    # Track the smallest (oldest) internalDate seen this batch; that's
    # what we hand back as the new watermark for descending walk.
    if internal_date:
        if not new_watermark or internal_date < new_watermark:
            new_watermark = internal_date

batch = {
    "phase": phase,
    "batch_id": batch_id,
    "watermark_before": watermark_before,
    "watermark_after":  new_watermark,
    "entries": entries,
    "fetched_count": len(entries) + skipped,
    "skipped_count": skipped,
    "model_identity": model_identity,
    "expected_account_email": actual,
    "error_kind": "",
    "schema_version": 1,
}

dispatch(Label("files.write"), {
    "path": raw_resource_path,
    "content": _json.dumps(batch),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Phase 2: re-index bodies for a pre-seeded list of message ids.
#
# Pre-seeded state keys:
#   * message_ids_json : str (JSON array of provider message ids)
GMAIL_INDEX_PHASE2_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

def _state(key):
    return dispatch(Label("state.get"), {"key": Label(key)})[Label("value")]

raw_resource_path = _state("raw_resource_path")
raw_resource_id   = _state("raw_resource_id")
expected_account  = _state("expected_account_email")
model_identity    = _state("model_identity")
batch_id          = _state("batch_id")
phase             = _state("phase")
message_ids_json  = _state("message_ids_json")

ids = _json.loads(message_ids_json)
if not isinstance(ids, list):
    raise RuntimeError("message_ids_json must be a JSON array")
ids = [str(x) for x in ids if isinstance(x, str) and x][:100]

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError("GMAIL_OAUTH_ACCESS_TOKEN not present in env")

who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
if who[Label("status_code")] != 200:
    raise RuntimeError("userinfo lookup failed: " + str(who[Label("status_code")]))
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected_account or "").strip().lower():
    raise RuntimeError("expected-account check failed: token=" + repr(actual))

entries = []
skipped = 0
for mid in ids:
    meta = dispatch(Label("web.get"), {
        "url": (
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
            + mid + "?format=metadata"
            + "&metadataHeaders=Subject"
            + "&metadataHeaders=From"
            + "&metadataHeaders=To"
            + "&metadataHeaders=Cc"
            + "&metadataHeaders=Date"
        ),
        "headers": {"Authorization": "Bearer " + access_token},
    })
    if meta[Label("status_code")] != 200:
        skipped += 1
        continue
    m = _json.loads(meta[Label("text")])
    headers = {h.get("name", ""): h.get("value", "") for h in (m.get("payload") or {}).get("headers", [])}
    entries.append({
        "provider_message_id": m.get("id") or mid,
        "thread_id": m.get("threadId") or "",
        "internal_date": m.get("internalDate") or "",
        "subject": headers.get("Subject", "")[:512],
        "from_header": headers.get("From", "")[:256],
        "to_header": headers.get("To", "")[:1024],
        "cc_header": headers.get("Cc", "")[:1024],
        "date_header": headers.get("Date", "")[:128],
        "label_ids": tuple(m.get("labelIds") or ()),
        "snippet": (m.get("snippet") or "")[:512],
        "schema_version": 1,
    })

batch = {
    "phase": phase,
    "batch_id": batch_id,
    "watermark_before": "",
    "watermark_after":  "",
    "entries": entries,
    "fetched_count": len(entries) + skipped,
    "skipped_count": skipped,
    "model_identity": model_identity,
    "expected_account_email": actual,
    "error_kind": "",
    "schema_version": 1,
}

dispatch(Label("files.write"), {
    "path": raw_resource_path,
    "content": _json.dumps(batch),
})
dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''


# Incremental: walk Gmail history.list from a stored historyId.
#
# Pre-seeded state keys:
#   * start_history_id    : str (Gmail historyId watermark; required;
#                                "" means trigger should not have
#                                emitted)
#
# Watermark format is the bare Gmail ``historyId`` numeric string.
GMAIL_INDEX_INCREMENTAL_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os
import json as _json

def _state(key):
    return dispatch(Label("state.get"), {"key": Label(key)})[Label("value")]

raw_resource_path = _state("raw_resource_path")
raw_resource_id   = _state("raw_resource_id")
expected_account  = _state("expected_account_email")
model_identity    = _state("model_identity")
batch_id          = _state("batch_id")
phase             = _state("phase")
start_history_id  = _state("start_history_id")

if not start_history_id:
    raise RuntimeError("start_history_id is required for incremental phase")

access_token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
if not access_token:
    raise RuntimeError("GMAIL_OAUTH_ACCESS_TOKEN not present in env")

who = dispatch(Label("web.get"), {
    "url": "https://www.googleapis.com/oauth2/v3/userinfo",
    "headers": {"Authorization": "Bearer " + access_token},
})
if who[Label("status_code")] != 200:
    raise RuntimeError("userinfo lookup failed: " + str(who[Label("status_code")]))
who_body = _json.loads(who[Label("text")])
actual = (who_body.get("email") or "").strip().lower()
if actual != (expected_account or "").strip().lower():
    raise RuntimeError("expected-account check failed: token=" + repr(actual))

hist = dispatch(Label("web.get"), {
    "url": (
        "https://gmail.googleapis.com/gmail/v1/users/me/history"
        "?startHistoryId=" + start_history_id
        + "&historyTypes=messageAdded"
        + "&maxResults=100"
    ),
    "headers": {"Authorization": "Bearer " + access_token},
})
hist_status = hist[Label("status_code")]
if hist_status == 404:
    # historyId expired (>7 days).  Surface a structured error so the
    # JUDGE can route the trigger back to Phase 1.
    batch = {
        "phase": phase,
        "batch_id": batch_id,
        "watermark_before": start_history_id,
        "watermark_after":  start_history_id,
        "entries": [],
        "fetched_count": 0,
        "skipped_count": 0,
        "model_identity": model_identity,
        "expected_account_email": actual,
        "error_kind": "history_expired",
        "schema_version": 1,
    }
    dispatch(Label("files.write"), {
        "path": raw_resource_path,
        "content": _json.dumps(batch),
    })
    dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
elif hist_status != 200:
    raise RuntimeError(
        "history.list failed: " + str(hist_status)
        + " body=" + hist[Label("text")][:200]
    )
else:
    hist_body = _json.loads(hist[Label("text")])
    new_history_id = hist_body.get("historyId") or start_history_id
    added_ids = []
    for entry in (hist_body.get("history") or ()):
        for ma in (entry.get("messagesAdded") or ()):
            msg = ma.get("message") or {}
            mid = msg.get("id") or ""
            if mid:
                added_ids.append(mid)
    # Dedupe while preserving order.
    seen = set()
    unique_ids = []
    for mid in added_ids:
        if mid in seen:
            continue
        seen.add(mid)
        unique_ids.append(mid)
    unique_ids = unique_ids[:100]

    entries = []
    skipped = 0
    for mid in unique_ids:
        meta = dispatch(Label("web.get"), {
            "url": (
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/"
                + mid + "?format=metadata"
                + "&metadataHeaders=Subject"
                + "&metadataHeaders=From"
                + "&metadataHeaders=To"
                + "&metadataHeaders=Cc"
                + "&metadataHeaders=Date"
            ),
            "headers": {"Authorization": "Bearer " + access_token},
        })
        if meta[Label("status_code")] != 200:
            skipped += 1
            continue
        m = _json.loads(meta[Label("text")])
        headers = {h.get("name", ""): h.get("value", "") for h in (m.get("payload") or {}).get("headers", [])}
        entries.append({
            "provider_message_id": m.get("id") or mid,
            "thread_id": m.get("threadId") or "",
            "internal_date": m.get("internalDate") or "",
            "subject": headers.get("Subject", "")[:512],
            "from_header": headers.get("From", "")[:256],
            "to_header": headers.get("To", "")[:1024],
            "cc_header": headers.get("Cc", "")[:1024],
            "date_header": headers.get("Date", "")[:128],
            "label_ids": tuple(m.get("labelIds") or ()),
            "snippet": (m.get("snippet") or "")[:512],
            "schema_version": 1,
        })

    batch = {
        "phase": phase,
        "batch_id": batch_id,
        "watermark_before": start_history_id,
        "watermark_after":  new_history_id,
        "entries": entries,
        "fetched_count": len(entries) + skipped,
        "skipped_count": skipped,
        "model_identity": model_identity,
        "expected_account_email": actual,
        "error_kind": "",
        "schema_version": 1,
    }
    dispatch(Label("files.write"), {
        "path": raw_resource_path,
        "content": _json.dumps(batch),
    })
    dispatch(Label("resource.finalize"), {"resource_id": raw_resource_id})
'''
