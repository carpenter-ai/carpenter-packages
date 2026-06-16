"""
Package-internal: carpenter-imap-email EXECUTOR scripts conform to the
``dispatch(Label(...))`` allowlist — and are CRED-FREE / HOST-FREE.

Every script string in ``scripts.py`` is the body of an EXECUTOR arc.
The package's trust contract is that the only verbs an EXECUTOR may
dispatch are drawn from a small, audit-readable allowlist.  Crucially,
for this backend that allowlist includes the package's own TRUSTED
capability verbs (``imap.fetch`` / ``imap.search`` / ``imap.store`` /
``smtp.send``) — and EXCLUDES ``web.get`` / ``web.post``, because reaching
the network is the trusted parent-side handler's job, not the executor's.

This story ALSO enforces the security-critical invariant that the
executor scripts are cred-free and host-free: they must NOT read
``os.environ`` (where Gmail's scripts find an OAuth token) and must NOT
contain a hardcoded mailbox host.  Host + credentials come exclusively
from the operator-confirmed grant via the trusted handler.
"""

from __future__ import annotations

import ast

from ._framework import PackageStory, StoryResult, load_package_module


# Verbs the IMAP/SMTP package is allowed to dispatch.  Note: NO web.get /
# web.post — egress goes through the trusted capability handlers, not the
# untrusted executor.
_ALLOWED_DISPATCH_VERBS = frozenset({
    "state.get",
    "state.set",
    "imap.fetch",
    "imap.search",
    "imap.store",
    "smtp.send",
    "files.write",
    "resource.finalize",
})

# The package's own trusted capability verbs — at least one script must
# use one of these (otherwise the AST walk is broken or a script reached
# the network some other way).
_CAPABILITY_VERBS = frozenset({
    "imap.fetch", "imap.search", "imap.store", "smtp.send",
})

# Substrings that would indicate a cred/host leak into the untrusted
# executor — banned.
_BANNED_SUBSTRINGS = (
    "os.environ",
    "getenv",
    "IMAP_EMAIL_",     # never reference the credential env keys in-script
    "imap.mailbox.org",
    "smtp.mailbox.org",
    "PASSWORD",
)


class ScriptsPassAstLint(PackageStory):
    name = "carpenter-imap-email::scripts_pass_ast_lint"
    description = (
        "Every EXECUTOR script in scripts.py uses only the documented "
        "dispatch verbs (state.get/set, imap.fetch/search/store, "
        "smtp.send, files.write, resource.finalize) and is cred-free / "
        "host-free (no os.environ, no hardcoded host)."
    )

    def run(self, client=None, db=None) -> StoryResult:
        scripts = load_package_module("scripts")

        script_attrs = [
            name for name in dir(scripts)
            if name.endswith("_SCRIPT")
            and isinstance(getattr(scripts, name), str)
        ]
        self.assert_that(
            len(script_attrs) > 0,
            "scripts.py exposes no *_SCRIPT constants",
        )

        verbs_by_script: dict[str, set[str]] = {}
        for attr in script_attrs:
            src = getattr(scripts, attr)
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                self.assert_that(False, f"{attr}: not parseable Python — {exc}")
            verbs = _collect_dispatch_verbs(tree)
            self.assert_that(
                len(verbs) > 0,
                f"{attr}: no dispatch(Label(...)) call found",
            )
            bad = verbs - _ALLOWED_DISPATCH_VERBS
            self.assert_that(
                not bad,
                f"{attr}: uses disallowed dispatch verb(s) {sorted(bad)}; "
                f"allowed: {sorted(_ALLOWED_DISPATCH_VERBS)}",
            )
            verbs_by_script[attr] = verbs

            # Cred-free / host-free invariant.
            for banned in _BANNED_SUBSTRINGS:
                self.assert_that(
                    banned not in src,
                    f"{attr}: script must be cred-free / host-free but "
                    f"contains {banned!r}; egress + credentials are the "
                    f"trusted handler's job, never the executor's",
                )

        # At least one script must use a TRUSTED capability verb (proves
        # the AST walk saw the egress path).
        any_cap = any(
            verbs & _CAPABILITY_VERBS for verbs in verbs_by_script.values()
        )
        self.assert_that(
            any_cap,
            "no script dispatches a capability verb (imap.*/smtp.send); "
            "AST walk likely broken; verbs seen: "
            + repr({k: sorted(v) for k, v in verbs_by_script.items()}),
        )

        # And no script may use web.get / web.post (that would mean the
        # executor is reaching the network directly).
        for attr, verbs in verbs_by_script.items():
            self.assert_that(
                not (verbs & {"web.get", "web.post"}),
                f"{attr}: must NOT use web.get/web.post — egress is the "
                f"trusted capability handler's job",
            )

        return self.result(
            f"{len(script_attrs)} EXECUTOR scripts pass the dispatch-verb "
            f"allowlist, are cred-free / host-free, and route egress "
            f"through the trusted capability verbs (verbs seen: "
            f"{sorted({v for vs in verbs_by_script.values() for v in vs})})."
        )


def _collect_dispatch_verbs(tree: ast.AST) -> set[str]:
    """Return the set of string literals passed as the first arg of
    ``Label(...)`` when that Label is the first arg of a ``dispatch(...)``
    call."""
    verbs: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dispatch"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
            and node.args[0].func.id == "Label"
            and node.args[0].args
            and isinstance(node.args[0].args[0], ast.Constant)
            and isinstance(node.args[0].args[0].value, str)
        ):
            verbs.add(node.args[0].args[0].value)
    return verbs
