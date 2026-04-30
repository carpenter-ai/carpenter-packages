# hello — reference capability package

Phase A reference fixture for the Carpenter capability-package
framework.  Provides one read-only chat tool, `hello_world`, that
returns a static greeting.

## Purpose

`hello` exists to:

1. Validate the cross-repo loader path end-to-end.  When this
   repository is cloned next to `carpenter-core` (or in any of the
   conventional search paths), `PackageRegistry` discovers and
   registers `hello_world` with no user configuration — proving
   leadership decision **D22** holds.
2. Act as the minimum-shape template for real packages.  Future
   packages (email, calendar, ...) should follow the same layout
   (`manifest.yaml` + `tools.py`).

## What it ships

- `manifest.yaml` — the package descriptor.
- `tools.py` — one `@chat_tool`-decorated function, `hello_world`.

It deliberately ships **no** KB articles, **no** triggers, **no**
arc templates, and **no** credentials.  Phase A doesn't yet load
those artifact types from packages.

## Using it

After `carpenter-packages` is cloned alongside `carpenter-core` (or
the path is added to `config["capability_packages"]["search_paths"]`),
restart the Carpenter daemon.  The `hello_world` tool then appears
to chat agents and the read-only `list_packages` chat tool reports
the package as loaded.

Removing the package directory and restarting the daemon
de-registers the tool — there is no install-state metadata in
Phase A.

## Trust boundary

`hello_world` is `trust_boundary='chat'` with `capabilities=['pure']`
— it has no side effects, takes no untrusted input, and could be
removed without affecting any other capability.
