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
#   * raw_message_b64 : str  (RFC-822 raw message, base64url-encoded)
#   * expected_account_email : str (mailbox we expect to send from)
#
# The script first verifies the access token's account email matches
# expected_account_email (defence against a swapped-in refresh token
# attack at the chat-boundary trust check), then POSTs the message.
GMAIL_SEND_SCRIPT = '''\
from carpenter_tools.declarations import Label
import os

raw_result = dispatch(Label("state.get"), {"key": Label("raw_message_b64")})
raw_b64 = raw_result[Label("value")]
exp_result = dispatch(Label("state.get"), {"key": Label("expected_account_email")})
expected = exp_result[Label("value")]

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
import json as _json
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
dispatch(Label("state.set"), {
    "key": Label("_agent_response"),
    "value": "Email sent (status=" + str(send_status) + ").",
})
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
