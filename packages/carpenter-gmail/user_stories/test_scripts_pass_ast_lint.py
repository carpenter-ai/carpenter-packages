"""
Package-internal: carpenter-gmail EXECUTOR scripts conform to the
``dispatch(Label(...))`` allowlist.

Every script string defined in ``scripts.py`` is the body of an
EXECUTOR arc.  Because EXECUTORs run under the restricted-Python
sandbox, the only way they reach the outside world is via
``dispatch(Label("<verb>"), ...)`` calls.  The package's trust
contract is that the verbs are drawn from a small, audit-readable
allowlist:

* ``state.get`` / ``state.set`` — arc-state I/O
* ``web.get`` / ``web.post`` — outbound HTTP (the platform's egress
  allowlist gates the actual target)
* ``files.write`` — write a Resource blob
* ``resource.finalize`` — close out a Resource

If a script string drifts and adds a new dispatch verb without that
verb being added here, this story fails loud at the package boundary
— before the script ships into a release that the platform would
reject at execute time anyway.

The platform's deeper AST-lint (in carpenter-core) is the actual
trust gate; this story just keeps the package's own scripts honest.
"""

from __future__ import annotations

import ast

from ._framework import PackageStory, StoryResult, load_package_module


# Verbs the package is allowed to dispatch.  Keep tight; if a new verb
# needs adding, that's a deliberate, reviewed surface change.
_ALLOWED_DISPATCH_VERBS = frozenset({
    "state.get",
    "state.set",
    "web.get",
    "web.post",
    "files.write",
    "resource.finalize",
})


class ScriptsPassAstLint(PackageStory):
    name = "carpenter-gmail::scripts_pass_ast_lint"
    description = (
        "Every EXECUTOR script in scripts.py uses only the documented "
        "dispatch verbs (state.get, state.set, web.get, web.post, "
        "files.write, resource.finalize)."
    )

    def run(self, client=None, db=None) -> StoryResult:
        scripts = load_package_module("scripts")

        # Discover every top-level string constant whose name ends in
        # ``_SCRIPT``.  That's the package's documented convention.
        script_attrs = [
            name for name in dir(scripts)
            if name.endswith("_SCRIPT")
            and isinstance(getattr(scripts, name), str)
        ]
        self.assert_that(
            len(script_attrs) > 0,
            "scripts.py exposes no *_SCRIPT constants — did the "
            "module change shape?",
        )

        verbs_by_script: dict[str, set[str]] = {}
        for attr in script_attrs:
            src = getattr(scripts, attr)
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                self.assert_that(
                    False,
                    f"{attr}: not parseable Python — {exc}",
                )
            verbs = _collect_dispatch_verbs(tree)
            self.assert_that(
                len(verbs) > 0,
                f"{attr}: no dispatch(Label(...)) call found — script "
                f"reached the outside world some other way?",
            )
            bad = verbs - _ALLOWED_DISPATCH_VERBS
            self.assert_that(
                not bad,
                f"{attr}: uses disallowed dispatch verb(s) "
                f"{sorted(bad)}; allowed: "
                f"{sorted(_ALLOWED_DISPATCH_VERBS)}",
            )
            verbs_by_script[attr] = verbs

        # Sanity: at least one script uses web.get or web.post (gmail
        # fetch / send).  Catches the case where the AST walk failed
        # to see anything.
        any_web = any(
            verbs & {"web.get", "web.post"}
            for verbs in verbs_by_script.values()
        )
        self.assert_that(
            any_web,
            "no script uses web.get or web.post — AST walk likely "
            "broken; verbs seen: "
            + repr({k: sorted(v) for k, v in verbs_by_script.items()}),
        )

        return self.result(
            f"{len(script_attrs)} EXECUTOR scripts pass the dispatch-"
            f"verb allowlist (verbs seen across all scripts: "
            f"{sorted({v for vs in verbs_by_script.values() for v in vs})})."
        )


def _collect_dispatch_verbs(tree: ast.AST) -> set[str]:
    """Return the set of string literals passed as the first arg of
    ``Label(...)`` whenever that Label is the first arg of a
    ``dispatch(...)`` call.
    """
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
