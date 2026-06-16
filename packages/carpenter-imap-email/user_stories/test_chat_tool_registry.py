"""
Package-internal: carpenter-imap-email chat-tool registration.

Verifies the ``pkg_imap_*`` chat tools shipped by tools.py register with
the expected decorator metadata (requires_user_confirm flags +
capabilities).  Mirrors carpenter-gmail's chat_tool_registry story,
adapted for the IMAP backend's tool surface (no OAuth bootstrap tool, no
reindex control surface — those are Gmail-specific / deferred).
"""

from __future__ import annotations

from ._framework import PackageStory, StoryResult, load_package_module


class ChatToolRegistry(PackageStory):
    name = "carpenter-imap-email::chat_tool_registry"
    description = (
        "All pkg_imap_* chat tools register with the expected "
        "requires_user_confirm flags and capabilities."
    )

    # tool_name -> (requires_user_confirm, required_capabilities_subset)
    _EXPECTED_TOOLS = {
        # Read-side — no user confirm.
        "pkg_imap_search_emails": (False, {"arc_create"}),
        "pkg_imap_list_inbox": (False, {"arc_create"}),
        "pkg_imap_read_email": (False, {"arc_create"}),
        # Write-side — all require user confirm + arc_create + external_effect.
        "pkg_imap_send_email": (True, {"arc_create", "external_effect"}),
        "pkg_imap_reply_email": (True, {"arc_create", "external_effect"}),
        "pkg_imap_archive_email": (True, {"arc_create", "external_effect"}),
        "pkg_imap_mark_read_email": (True, {"arc_create", "external_effect"}),
        # Trust-management — write to the policy store, so confirm.
        "pkg_imap_trust_sender": (True, set()),
        "pkg_imap_untrust_sender": (True, set()),
    }

    def run(self, client=None, db=None) -> StoryResult:
        # Loader needs data_models / scripts / arc_builders on sys.modules
        # first because tools.py uses relative-style imports.
        load_package_module("data_models")
        load_package_module("scripts")
        load_package_module("arc_builders")
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
                f"{name}: missing _chat_tool_meta — was @chat_tool applied?",
            )
            self.assert_that(
                meta.get("name") == name,
                f"{name}: _chat_tool_meta['name'] is {meta.get('name')!r}",
            )
            seen_meta[name] = meta

        for name, (expected_confirm, required_caps) in (
            self._EXPECTED_TOOLS.items()
        ):
            meta = seen_meta[name]
            actual_confirm = meta.get("requires_user_confirm")
            self.assert_that(
                actual_confirm is expected_confirm,
                f"{name}: requires_user_confirm expected {expected_confirm!r}, "
                f"got {actual_confirm!r}",
            )
            actual_caps = set(meta.get("capabilities", []))
            missing_caps = required_caps - actual_caps
            self.assert_that(
                not missing_caps,
                f"{name}: capabilities missing {sorted(missing_caps)}; "
                f"got {sorted(actual_caps)}",
            )

        return self.result(
            f"All {len(self._EXPECTED_TOOLS)} pkg_imap_* chat tools "
            f"register with the expected metadata."
        )
