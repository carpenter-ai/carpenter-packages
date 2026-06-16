"""Tests for the carpenter-imap-email TRUSTED capability handlers.

Two layers of coverage, neither requiring a live mailbox:

1. **Handler unit tests** (mocked ``imaplib`` / ``smtplib``): prove each
   handler reads host + credentials ONLY from ``ctx`` (never from
   ``params``), validates/bounds its params, and returns a
   JSON-serialisable dict.  A malicious ``params`` carrying ``host`` /
   ``password`` must be ignored.

2. **Capability-stack test** (real handlers, real
   :class:`CapabilityRegistry`): register the package's actual handlers
   against their declared verbs, then ``registry.dispatch(verb, params)``
   and assert the handler received a :class:`CapabilityContext` bound to
   the operator-confirmed grant (host/port/protocol from the grant,
   ``ctx.secret`` resolving the package's credential platform-side).
   This proves the **ctx wiring** the dispatch gate relies on.

   The per-package dispatch GATE itself (an owner-stamped EXECUTOR arc
   CAN invoke the verb; a non-package arc is DENIED) is exercised
   end-to-end against the SQLite-backed arc/template machinery in
   carpenter-core's ``tests/packages/test_arc_grant_stamping.py``
   (``TestDispatchThroughStampedArc``).  That DB-driven path needs
   carpenter-core's pytest fixtures, which aren't available in the
   carpenter-packages repo, so here we instead assert the registry-level
   scoping primitives the gate is built on (``package_for_verb`` /
   ``verbs_for_package`` / ``capability_grant_for_package``) and document
   the reasoning below.

Gate reasoning (how a non-package arc is denied)
------------------------------------------------
The platform's ``validate_and_dispatch`` (carpenter-core
``executor/dispatch_bridge.py``) looks up the verb in the
``CapabilityRegistry``; if it is a capability verb it requires the
calling arc to carry ``capability_grant_for_package(owner)`` (i.e.
``pkg.carpenter-imap-email``) in its ``_capabilities`` arc_state, else it
raises ``DispatchError(... own arcs ...)``.  That grant is stamped onto
an arc ONLY when the arc is instantiated from a template whose
``owner_package`` is this package (loader sets
``owner_package=manifest.name``; ``template_manager.instantiate_template``
stamps every step arc).  Therefore: this package's EXECUTOR arcs (from
its ``email_*`` templates) carry the grant and CAN dispatch
``imap.fetch``; any other arc (platform template, or another package's
template) does not carry ``pkg.carpenter-imap-email`` and is denied.
We assert the registry knows the owning package per verb, which is the
exact input the gate uses.

Run via ``~/bin/run-tests`` (NEVER bare pytest).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[2] / "packages" / "carpenter-imap-email"


def _load_handlers():
    """Import the package's handler module directly (no platform loader
    needed — it's pure stdlib + the ctx duck-type)."""
    path = PKG_DIR / "handlers" / "imap_smtp.py"
    spec = importlib.util.spec_from_file_location(
        "_imap_email_handlers_under_test", path,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


handlers = _load_handlers()


class FakeCtx:
    """Stand-in for ``CapabilityContext``: host/port/protocol from the
    confirmed grant, ``secret`` resolving the package credential.

    Mirrors the real ctx's contract exactly (frozen scope, secret()
    suffix resolution) so the handlers can't tell the difference.
    """

    def __init__(self, *, host, port, protocol, secrets):
        self.package_name = "carpenter-imap-email"
        self.verb = "test.verb"
        self.kind = "egress"
        self.host = host
        self.port = port
        self.protocol = protocol
        self.credential_ref = "IMAP_EMAIL"
        self._secrets = secrets

    def secret(self, ref: str) -> str:
        if ref not in self._secrets:
            raise AssertionError(
                f"handler asked for an unexpected secret {ref!r}",
            )
        return self._secrets[ref]


def _imap_ctx():
    return FakeCtx(
        host="imap.confirmed-host.example",
        port=993,
        protocol="imaps",
        secrets={"IMAP_USERNAME": "me@example.com", "IMAP_PASSWORD": "app-pw"},
    )


def _smtp_ctx():
    return FakeCtx(
        host="smtp.confirmed-host.example",
        port=465,
        protocol="smtps",
        secrets={"SMTP_USERNAME": "me@example.com", "SMTP_PASSWORD": "app-pw"},
    )


# ── Fake imaplib / smtplib ──────────────────────────────────────────


class FakeIMAP:
    """Records the host/port/credentials it was constructed/logged-in
    with so the test can prove they came from ctx, not params."""

    instances: list["FakeIMAP"] = []

    def __init__(self, host=None, port=None, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in_as = None
        self.selected = None
        self.appended = None  # (mailbox, flags, message_bytes)
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.logged_in_as = (user, password)
        return ("OK", [b"logged in"])

    def append(self, mailbox, flags, date_time, message):
        self.appended = (mailbox, flags, message)
        return ("OK", [b"APPEND completed"])

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        command = command.upper()
        if command == "FETCH":
            uid, item = args[0], args[1]
            if "FLAGS" in item:
                return ("OK", [(b"1 (FLAGS (\\Seen))", b"")])
            return ("OK", [(b"1 (BODY[] {5}", b"hello"), b")"])
        if command == "SEARCH":
            return ("OK", [b"1 2 3"])
        if command == "STORE":
            return ("OK", [b"1 (FLAGS (\\Seen))"])
        return ("OK", [b""])

    def close(self):
        pass

    def logout(self):
        pass


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host=None, port=None, timeout=None):
        self.host = host
        self.port = port
        self.logged_in_as = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.logged_in_as = (user, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent = (from_addr, list(to_addrs), msg)
        return {}  # no refusals


@pytest.fixture(autouse=True)
def _patch_net(monkeypatch):
    FakeIMAP.instances.clear()
    FakeSMTP.instances.clear()
    monkeypatch.setattr(handlers.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(handlers.smtplib, "SMTP_SSL", FakeSMTP)
    yield


# ── Handler unit tests ──────────────────────────────────────────────


def test_imap_fetch_uses_ctx_host_and_creds_not_params():
    ctx = _imap_ctx()
    # params carries a HOSTILE host + password that MUST be ignored.
    out = handlers.handle_imap_fetch(
        {"uid": "42", "host": "evil.example", "password": "stolen"}, ctx,
    )
    assert out["ok"] is True
    assert out["uid"] == "42"
    assert out["host"] == ctx.host == "imap.confirmed-host.example"
    assert out["port"] == 993
    inst = FakeIMAP.instances[-1]
    # Connection host/port came from ctx, not params.
    assert inst.host == "imap.confirmed-host.example"
    assert inst.port == 993
    # Login creds came from ctx.secret, not params.
    assert inst.logged_in_as == ("me@example.com", "app-pw")


def test_imap_fetch_rejects_bad_uid():
    out = handlers.handle_imap_fetch({"uid": "1; DROP TABLE"}, _imap_ctx())
    assert out["ok"] is False
    assert "uid" in out["error"]
    # No connection attempted on a validation failure.
    assert FakeIMAP.instances == []


def test_imap_search_returns_capped_uids():
    out = handlers.handle_imap_search(
        {"criteria": [["TEXT", "invoice"]], "max_results": 2}, _imap_ctx(),
    )
    assert out["ok"] is True
    assert out["count"] == 2
    assert all(u.isdigit() for u in out["uids"])
    assert out["host"] == "imap.confirmed-host.example"


def test_imap_search_rejects_unknown_key():
    out = handlers.handle_imap_search(
        {"criteria": [["EVIL", "x"]]}, _imap_ctx(),
    )
    assert out["ok"] is False
    assert "allowlist" in out["error"]


def test_imap_store_reports_prior_flags():
    out = handlers.handle_imap_store(
        {"uid": "7", "flags": ["\\Seen"], "op": "add"}, _imap_ctx(),
    )
    assert out["ok"] is True
    assert out["op"] == "add"
    assert out["flags"] == ["\\Seen"]
    assert "\\Seen" in out["prior_flags"]


def test_imap_store_rejects_bad_flag():
    out = handlers.handle_imap_store(
        {"uid": "7", "flags": ["bad flag with spaces"]}, _imap_ctx(),
    )
    assert out["ok"] is False
    assert "flag" in out["error"]


def test_smtp_send_envelope_sender_is_ctx_account_not_params():
    ctx = _smtp_ctx()
    out = handlers.handle_smtp_send(
        {
            "to": ["alice@example.com"],
            "subject": "hi",
            "body": "hello",
            # Hostile From / host / password — all must be ignored.
            "from": "spoofed@evil.example",
            "host": "evil.example",
            "password": "stolen",
        },
        ctx,
    )
    assert out["ok"] is True
    assert out["accepted_recipients"] == ["alice@example.com"]
    inst = FakeSMTP.instances[-1]
    assert inst.host == "smtp.confirmed-host.example"
    assert inst.port == 465
    assert inst.logged_in_as == ("me@example.com", "app-pw")
    env_from, env_to, _msg = inst.sent
    # Envelope sender is the authenticated account from ctx, NOT params.
    assert env_from == "me@example.com"
    assert env_to == ["alice@example.com"]


def test_smtp_send_rejects_too_many_recipients():
    out = handlers.handle_smtp_send(
        {"to": [f"a{i}@x.com" for i in range(51)], "subject": "s", "body": "b"},
        _smtp_ctx(),
    )
    assert out["ok"] is False
    assert "recipients" in out["error"]


# ── imap.append handler (Sent-folder copy) ──────────────────────────


def test_imap_append_files_into_named_folder_using_ctx_only():
    ctx = _imap_ctx()
    raw = "From: me@example.com\r\nTo: alice@example.com\r\nSubject: hi\r\n\r\nbody"
    # Hostile host/password in params MUST be ignored.
    out = handlers.handle_imap_append(
        {
            "raw_message": raw,
            "mailbox": "Sent",
            "host": "evil.example",
            "password": "stolen",
        },
        ctx,
    )
    assert out["ok"] is True
    assert out["mailbox"] == "Sent"
    assert out["flags"] == ["\\Seen"]
    assert out["host"] == "imap.confirmed-host.example"
    assert out["port"] == 993
    inst = FakeIMAP.instances[-1]
    # Connection host/port + creds came from ctx, not params.
    assert inst.host == "imap.confirmed-host.example"
    assert inst.port == 993
    assert inst.logged_in_as == ("me@example.com", "app-pw")
    # The message was APPENDed to the named folder with the \Seen flag.
    mailbox, flag_str, message = inst.appended
    assert mailbox == "Sent"
    assert flag_str == "(\\Seen)"
    assert message == raw.encode("utf-8")


def test_imap_append_defaults_to_sent_folder():
    out = handlers.handle_imap_append({"raw_message": "x"}, _imap_ctx())
    assert out["ok"] is True
    assert out["mailbox"] == "Sent"


def test_imap_append_rejects_empty_message():
    out = handlers.handle_imap_append({"raw_message": ""}, _imap_ctx())
    assert out["ok"] is False
    assert "raw_message" in out["error"]
    # No connection on a validation failure.
    assert FakeIMAP.instances == []


def test_imap_append_rejects_bad_flag():
    out = handlers.handle_imap_append(
        {"raw_message": "x", "flags": ["bad flag"]}, _imap_ctx(),
    )
    assert out["ok"] is False
    assert "flag" in out["error"]


def test_send_flow_leaves_a_sent_copy():
    """Mirror what SMTP_SEND_SCRIPT does in the executor: dispatch
    smtp.send THEN imap.append(folder=Sent).  Prove the second step files
    a server-side Sent copy of the just-sent message — the whole point of
    Finding #1 (mailbox.org does not auto-populate Sent)."""
    raw = "From: me@example.com\r\nTo: alice@example.com\r\nSubject: hi\r\n\r\nbody"

    send_out = handlers.handle_smtp_send(
        {"raw_message": raw, "to": ["alice@example.com"]}, _smtp_ctx(),
    )
    assert send_out["ok"] is True

    append_out = handlers.handle_imap_append(
        {"raw_message": raw, "mailbox": "Sent", "flags": ["\\Seen"]}, _imap_ctx(),
    )
    assert append_out["ok"] is True
    assert append_out["mailbox"] == "Sent"
    # A Sent copy of the exact outgoing message now exists server-side.
    inst = FakeIMAP.instances[-1]
    mailbox, _flags, message = inst.appended
    assert mailbox == "Sent"
    assert message == raw.encode("utf-8")
    # The append egressed to the IMAP host (imaps grant), distinct from
    # the SMTP host the send used — the Sent copy stays in the IMAP grant
    # class rather than widening smtp.send's egress.
    assert inst.host == "imap.confirmed-host.example"
    assert FakeSMTP.instances[-1].host == "smtp.confirmed-host.example"


# ── Capability-stack test (real handlers + real CapabilityRegistry) ─


def test_capability_stack_dispatch_binds_grant_and_scopes_to_package():
    """Register the package's REAL handlers in a CapabilityRegistry and
    dispatch through it.  Proves the ctx the gate hands a handler binds
    the confirmed grant (host/port/protocol) + resolves the package
    credential, and that the registry scopes every verb to this package
    (the input the per-package dispatch gate uses to deny other arcs)."""
    from carpenter.packages.capabilities import (
        CapabilityRegistry,
        capability_grant_for_package,
    )
    from carpenter.packages.manifest import EgressGrant, load_manifest

    # Resolve the package's credential PLATFORM-SIDE via the real
    # ctx.secret path by seeding the process env (mirrors the daemon
    # mirroring .env into os.environ).
    # The credential_ref / env_key_prefix is EMAIL (see manifest), so
    # ctx.secret("IMAP_USERNAME") resolves the env key EMAIL_IMAP_USERNAME.
    import os
    os.environ["EMAIL_IMAP_USERNAME"] = "me@example.com"
    os.environ["EMAIL_IMAP_PASSWORD"] = "app-pw"
    try:
        manifest = load_manifest(PKG_DIR / "manifest.yaml")
        pkg = manifest.name
        registry = CapabilityRegistry()

        # Register every declared capability verb with its REAL handler.
        for cap in manifest.platform_capabilities:
            handler = getattr(handlers, cap.handler)
            # Host is what the installer would resolve from
            # IMAP_EMAIL_<host_from>; use the confirmed value.
            host = (
                "imap.confirmed-host.example"
                if cap.grant.host_from == "IMAP_HOST"
                else "smtp.confirmed-host.example"
            )
            registry.register(
                package_name=pkg,
                verb=cap.verb,
                kind=cap.kind,
                handler=handler,
                grant=EgressGrant(
                    protocol=cap.grant.protocol,
                    host_from=cap.grant.host_from,
                    port=cap.grant.port,
                    credential_ref=cap.grant.credential_ref,
                ),
                host=host,
            )

        # Scoping: every verb is owned by THIS package — the exact fact
        # the dispatch gate checks (an arc must carry pkg.<owner>).
        assert capability_grant_for_package(pkg) == "pkg.carpenter-imap-email"
        for verb in ("imap.fetch", "imap.search", "imap.store", "imap.append", "smtp.send"):
            assert registry.is_capability_verb(verb)
            assert registry.package_for_verb(verb) == pkg
        assert registry.verbs_for_package(pkg) == frozenset(
            {"imap.fetch", "imap.search", "imap.store", "imap.append", "smtp.send"}
        )
        # A different package owns none of these verbs → its arcs would be
        # denied by the gate (verbs_for_package is empty for it).
        assert registry.verbs_for_package("some-other-pkg") == frozenset()

        # ctx wiring: dispatch imap.fetch through the registry exactly as
        # the gate does.  The handler must receive host/port/protocol
        # from the grant and resolve the credential via ctx.secret —
        # never from params.  Patch the network for this real dispatch.
        FakeIMAP.instances.clear()
        orig = handlers.imaplib.IMAP4_SSL
        handlers.imaplib.IMAP4_SSL = FakeIMAP
        try:
            out = registry.dispatch(
                "imap.fetch",
                {"uid": "5", "host": "attacker.example"},  # hostile param
            )
        finally:
            handlers.imaplib.IMAP4_SSL = orig

        assert out["ok"] is True
        # Host bound from the confirmed grant, NOT the hostile param.
        assert out["host"] == "imap.confirmed-host.example"
        assert out["port"] == 993
        inst = FakeIMAP.instances[-1]
        assert inst.host == "imap.confirmed-host.example"
        # Credential resolved platform-side via ctx.secret.
        assert inst.logged_in_as == ("me@example.com", "app-pw")
    finally:
        os.environ.pop("EMAIL_IMAP_USERNAME", None)
        os.environ.pop("EMAIL_IMAP_PASSWORD", None)
