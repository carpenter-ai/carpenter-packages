# carpenter-packages

Capability packages for the [Carpenter](https://carpenter-ai.org/) AI
agent platform.

Capability packages are reusable bundles of chat tools (and, in later
phases, KB articles, triggers, arc templates, and Pydantic data
models) that extend Carpenter without modifying the platform itself.
Each package lives at `packages/<name>/` with a `manifest.yaml`
descriptor and a `tools.py` module containing `@chat_tool`-decorated
functions.

## Layout

```
carpenter-packages/
  packages/
    hello/                    # reference package — Phase A only
      manifest.yaml
      tools.py
      README.md
    <future-packages>/...
  README.md
```

## Discovery

The platform's `PackageRegistry` walks a small set of conventional
search paths at startup:

1. `$CARPENTER_PACKAGES_PATH` (env var, colon-separated).
2. `${base_dir}/packages/`.
3. The sibling-repo location, i.e. wherever `carpenter-packages` is
   checked out next to `carpenter-core` (typically `~/repos/`).
4. `~/repos/carpenter-packages/`.

Per leadership decision **D22**, cloning `carpenter-packages` next
to `carpenter-core` is sufficient — no config edit required.

## Phase A scope

Phase A (cut B) ships only the framework and the `hello` reference
package, validating the cross-repo loader path end-to-end.  Real
capability packages (email, calendar, etc.) are Phase B and beyond.

## Trust model

Capability packages are **opinionated configuration**, not security
mechanism.  The framework forbids packages from:

- declaring chat tools at the `platform` trust boundary (I10),
- shipping JUDGE code (I3) — JUDGE handlers run platform-controlled
  deterministic Python, intercepted at arc dispatch,
- pre-populating policy allowlists (I9),
- bundling `.env` files (credentials are user input at install time),
- seeding KB articles outside the package's declared namespace.

The `I1`–`I10` invariant identifiers refer to Carpenter's trust
boundary system: untrusted data (e.g. an email body) can only become
trusted after a deterministic JUDGE arc approves a structured extract
of it (I3), and chat tools have enforced trust boundaries and
capability lists (I10). Per-package design docs (e.g.
[`packages/carpenter-gmail/docs/design.md`](packages/carpenter-gmail/docs/design.md))
explain how a given package threads these invariants for its
particular ingress.
