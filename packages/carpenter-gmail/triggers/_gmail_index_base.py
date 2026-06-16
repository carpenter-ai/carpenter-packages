"""Gmail-specific base for the three email-index triggers.

The shared lifecycle lives in the backend-agnostic
:class:`carpenter_email_core.triggers._index_common.IndexTriggerBase`
(composed in from the ``carpenter-email-core`` layer).  That base
defers three backend-specific decisions to its subclasses:

* :attr:`account_email_state_key` — the ``package_state`` key under
  which :mod:`carpenter_gmail.triggers.gmail_poll` caches the
  authorised mailbox address (``gmail_account_email``).
* :attr:`raw_source_prefix` — the audit ``source_descriptor`` prefix
  for the untrusted raw-fetch Resource (``"gmail"``).
* :meth:`index_template_meta` — resolve a template name to its
  ``(extract_kind, executor_script)`` pair using the pre-verified
  Gmail scripts in :mod:`carpenter_gmail.tools`.

This module is a Gmail *leaf* file (it names Gmail and imports Gmail's
scripts), so it deliberately stays OUT of the shared layer.
"""

from __future__ import annotations

from ._index_common import IndexTriggerBase
from .gmail_poll import _KEY_ACCOUNT_EMAIL


class GmailIndexTriggerBase(IndexTriggerBase):
    """Common Gmail base supplying the three backend hooks the shared
    :class:`IndexTriggerBase` requires.  The three concrete phase
    triggers subclass this instead of ``IndexTriggerBase`` directly.
    """

    # Gmail caches the authorised mailbox under this package_state key
    # (set by GmailPollTrigger after users.getProfile).
    account_email_state_key = _KEY_ACCOUNT_EMAIL
    raw_source_prefix = "gmail"

    def index_template_meta(self, template_name: str) -> tuple[str, str]:
        """Resolve ``(extract_kind, script)`` via the Gmail tools
        module's ``_index_template_meta`` (which holds the pre-verified
        Gmail index scripts)."""
        from ..tools import _index_template_meta

        return _index_template_meta(template_name)
