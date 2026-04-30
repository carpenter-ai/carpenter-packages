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
- shipping JUDGE code (I3),
- pre-populating policy allowlists (I9),
- bundling `.env` files (credentials are user input at install time),
- seeding KB articles outside the package's declared namespace.

See `carpenter-core/docs/trust-invariants.md` and
`carpenter-core/docs/2026-04-30_d8-capability-package-phase-a-plan.md`.
