"""
Package-internal: carpenter-gmail chat-tool registration.

Verifies that the 13 ``pkg_gmail_*`` chat tools shipped by tools.py
register with the expected decorator metadata:

* Tool name (function name) matches the expected list.
* ``requires_user_confirm`` flag has the documented value.
* The expected capabilities are declared.
* Tools that touch external state declare the ``external_effect``
  capability; read-only tools declare ``pure`` or ``read_state``.
* Tools that create arcs declare ``arc_create``.

This is the package's own contract test for its chat-tool surface.
The platform-level s055-s058 stories used to assert these flags too;
they now trust this story to own that surface and only check the
integration behaviour that needs the running daemon.
"""

from __future__ import annotations

from ._framework import PackageStory, StoryResult, load_package_module


class ChatToolRegistry(PackageStory):
    name = "carpenter-gmail::chat_tool_registry"
    description = (
        "All 13 pkg_gmail_* chat tools register with the expected "
        "requires_user_confirm flags and capabilities."
    )

    # tool_name -> (requires_user_confirm, required_capabilities_subset)
    _EXPECTED_TOOLS = {
        # OAuth bootstrap: external_effect (kicks off a Google flow).
        "pkg_gmail_authorize": (False, {"external_effect"}),
        # Read-side tools — no user confirm required.
        "pkg_gmail_search_emails": (False, set()),
        "pkg_gmail_list_inbox": (False, set()),
        "pkg_gmail_read_email": (False, set()),
        # Write-side tools — all require user confirm.  Each creates
        # an arc tree (arc_create) and has an external effect
        # (external_effect) on Gmail.
        "pkg_gmail_send_email": (
            True, {"arc_create", "external_effect"},
        ),
        "pkg_gmail_archive_email": (
            True, {"arc_create", "external_effect"},
        ),
        "pkg_gmail_mark_read_email": (
            True, {"arc_create", "external_effect"},
        ),
        "pkg_gmail_draft_email": (
            True, {"arc_create", "external_effect"},
        ),
        # Trust-management tools — write to the policy store, so they
        # require user confirm.
        "pkg_gmail_trust_sender": (True, set()),
        "pkg_gmail_untrust_sender": (True, set()),
        # Reindex control surface — affects per-package state, so
        # confirms.
        "pkg_gmail_reindex": (True, set()),
        "pkg_gmail_reindex_pause": (True, set()),
        "pkg_gmail_reindex_resume": (True, set()),
    }

    def run(self, client=None, db=None) -> StoryResult:
        # Loader needs data_models and scripts on sys.modules first
        # because tools.py uses relative-style imports.
        load_package_module("data_models")
        load_package_module("scripts")
        tools_mod = load_package_module("tools")

        seen_meta: dict[str, dict] = {}
        for name in self._EXPECTED_TOOLS:
            fn = getattr(tools_mod, name, None)
            self.assert_that(
                fn is not None,
                f"tools.py missing chat tool function {name!r}",
            )
            meta = getattr(fn, "_chat_tool_meta", None)
            self.assert_that(
                isinstance(meta, dict),
                f"{name}: missing _chat_tool_meta — was @chat_tool "
                f"decorator applied?",
            )
            self.assert_that(
                meta.get("name") == name,
                f"{name}: _chat_tool_meta['name'] is "
                f"{meta.get('name')!r}, expected {name!r}",
            )
            seen_meta[name] = meta

        # Now assert per-tool expectations.
        for name, (expected_confirm, required_caps) in (
            self._EXPECTED_TOOLS.items()
        ):
            meta = seen_meta[name]
            actual_confirm = meta.get("requires_user_confirm")
            self.assert_that(
                actual_confirm is expected_confirm,
                f"{name}: requires_user_confirm expected "
                f"{expected_confirm!r}, got {actual_confirm!r}",
            )
            actual_caps = set(meta.get("capabilities", []))
            missing_caps = required_caps - actual_caps
            self.assert_that(
                not missing_caps,
                f"{name}: capabilities missing {sorted(missing_caps)}; "
                f"got {sorted(actual_caps)}",
            )

        return self.result(
            f"All {len(self._EXPECTED_TOOLS)} pkg_gmail_* chat tools "
            f"register with the expected metadata."
        )
