"""Unit tests for the carpenter-imap-email inbound poll trigger.

Covers the trigger's UID-watermark logic with a MOCKED ``imaplib`` (no
live mailbox):

* **First run** with no watermark: records the folder's current max UID
  (+ UIDVALIDITY) and emits NOTHING.
* **New higher UIDs**: each new UID above the watermark emits one
  ``email.received`` event and the watermark advances to the highest UID.
* **No new UIDs**: emits nothing, watermark unchanged.
* **UIDVALIDITY change**: the server renumbered the mailbox → the trigger
  RESETS the watermark to the current max UID and emits nothing (does NOT
  re-fan the whole mailbox under stale UIDs).
* **Credential resolution** goes through the platform-side
  ``resolve_package_secret`` (the same resolver ``ctx.secret`` uses), not
  from any executor-controlled input.

Run via ``~/bin/run-tests`` (NEVER bare pytest).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[2] / "packages" / "carpenter-imap-email"


def _load_trigger_module():
    """Import the package's poll-trigger module directly.

    It imports ``carpenter.core.engine.triggers.base`` +
    ``carpenter.packages.capabilities``; skip the whole module if the
    platform isn't importable in this environment.
    """
    try:
        import carpenter  # noqa: F401
        from carpenter.core.engine.triggers.base import PollableTrigger  # noqa: F401
    except Exception:  # pragma: no cover - platform not installed
        pytest.skip("carpenter platform not importable in this environment")
    path = PKG_DIR / "triggers" / "imap_poll.py"
    spec = importlib.util.spec_from_file_location(
        "_imap_poll_trigger_under_test", path,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


imap_poll = _load_trigger_module()


# ── Test doubles ────────────────────────────────────────────────────


class FakeState:
    """In-memory stand-in for PackageStateHandle.

    Implements the get/get_with_version/set/cas/delete subset the trigger
    uses, including the CAS version semantics: ``cas(key, 0, v)`` inserts
    if absent; ``cas(key, n, v)`` updates only when the stored version
    equals ``n``; each successful write bumps the version by 1.
    """

    def __init__(self):
        self.package_name = "carpenter-imap-email"
        # key -> (value, version)
        self._store: dict[str, tuple[object, int]] = {}

    def get(self, key, default=None):
        if key in self._store:
            return self._store[key][0]
        return default

    def get_with_version(self, key):
        if key in self._store:
            return self._store[key]
        return None

    def set(self, key, value):
        prev_ver = self._store[key][1] if key in self._store else 0
        new_ver = prev_ver + 1
        self._store[key] = (value, new_ver)
        return new_ver

    def cas(self, key, expected_version, new_value):
        cur = self._store.get(key)
        cur_ver = cur[1] if cur is not None else 0
        if cur_ver != int(expected_version):
            return False
        self._store[key] = (new_value, cur_ver + 1)
        return True

    def delete(self, key):
        return self._store.pop(key, None) is not None


class FakeImap:
    """Minimal mocked ``imaplib.IMAP4_SSL``.

    Configured with a ``uidvalidity`` and a list of message ``uids``.
    Implements the methods the trigger calls: ``login``, ``select``,
    ``response('UIDVALIDITY')``, ``uid('SEARCH', ...)`` for both
    ``UID <lo>:*`` and ``ALL`` forms, plus ``close``/``logout``.
    """

    def __init__(self, *, uidvalidity, uids):
        self.uidvalidity = uidvalidity
        self.uids = sorted(uids)
        self.logged_in = False

    def login(self, user, pw):
        self.logged_in = True
        return "OK", [b"LOGIN OK"]

    def select(self, folder, readonly=False):
        return "OK", [str(len(self.uids)).encode()]

    def response(self, key):
        if key == "UIDVALIDITY":
            return "OK", [str(self.uidvalidity).encode()]
        return "OK", [None]

    def uid(self, cmd, *args):
        assert cmd == "SEARCH"
        # args = (None, "UID", "<lo>:*")  or  (None, "ALL")
        rest = [a for a in args if a is not None]
        if rest and rest[0] == "ALL":
            matched = self.uids
        elif len(rest) >= 2 and rest[0] == "UID":
            spec = rest[1]  # "<lo>:*"
            lo = int(spec.split(":", 1)[0])
            matched = [u for u in self.uids if u >= lo]
        else:  # pragma: no cover - defensive
            matched = self.uids
        payload = " ".join(str(u) for u in matched).encode("ascii")
        return "OK", [payload]

    def close(self):
        return "OK", [b"CLOSED"]

    def logout(self):
        return "BYE", [b"LOGOUT"]


def _make_trigger(monkeypatch, state, fake_imap, *, folders=("INBOX",)):
    """Build an ImapPollTrigger wired to fakes.

    * Credentials resolve via a patched ``resolve_package_secret``.
    * The IMAP connection is the supplied FakeImap (patch ``_connect``).
    * Emits are captured in a list.
    """
    secrets = {
        "EMAIL_IMAP_HOST": "imap.example.test",
        "EMAIL_IMAP_PORT": "993",
        "EMAIL_IMAP_USERNAME": "me@example.test",
        "EMAIL_IMAP_PASSWORD": "app-pw",
    }

    def fake_resolve(package_name, key):
        assert package_name == "carpenter-imap-email"
        return secrets.get(key)

    monkeypatch.setattr(imap_poll, "resolve_package_secret", fake_resolve)

    trig = imap_poll.ImapPollTrigger(
        "imap-inbound-poll",
        {"cadence_seconds": 900, "event_type": "email.received",
         "folders": list(folders)},
        source_package="carpenter-imap-email",
        package_state=state,
    )

    # Route all connections to the fake.
    monkeypatch.setattr(trig, "_connect", lambda conn_info: fake_imap)

    emitted = []

    def fake_emit(event_type, payload=None, idempotency_key=None, priority=0):
        emitted.append({
            "event_type": event_type,
            "payload": payload,
            "idempotency_key": idempotency_key,
        })
        return len(emitted)

    monkeypatch.setattr(trig, "emit", fake_emit)
    return trig, emitted


_WM_KEY = imap_poll._watermark_key("INBOX")


# ── Tests ───────────────────────────────────────────────────────────


def test_config_defaults_and_floor(monkeypatch):
    state = FakeState()
    monkeypatch.setattr(imap_poll, "resolve_package_secret", lambda p, k: None)
    # Below-floor cadence is clamped to 60.
    trig = imap_poll.ImapPollTrigger(
        "t", {"cadence_seconds": 5},
        source_package="carpenter-imap-email", package_state=state,
    )
    assert trig.cadence_seconds == 60
    assert trig.folders == ("INBOX",)
    assert trig.event_type == "email.received"


def test_junk_never_watched_by_default(monkeypatch):
    state = FakeState()
    monkeypatch.setattr(imap_poll, "resolve_package_secret", lambda p, k: None)
    trig = imap_poll.ImapPollTrigger(
        "t", {}, source_package="carpenter-imap-email", package_state=state,
    )
    assert "Junk" not in trig.folders
    assert trig.folders == ("INBOX",)
    # But the operator MAY add it explicitly.
    trig2 = imap_poll.ImapPollTrigger(
        "t2", {"folders": ["INBOX", "Junk"]},
        source_package="carpenter-imap-email", package_state=state,
    )
    assert trig2.folders == ("INBOX", "Junk")


def test_first_run_records_max_uid_emits_nothing(monkeypatch):
    state = FakeState()
    fake = FakeImap(uidvalidity=111, uids=[10, 11, 12])
    trig, emitted = _make_trigger(monkeypatch, state, fake)

    trig._do_poll()

    assert emitted == []
    wm = state.get(_WM_KEY)
    assert wm == {"uid": 12, "uidvalidity": 111}


def test_new_higher_uids_emit_one_each_and_advance(monkeypatch):
    state = FakeState()
    # Pre-seed the watermark at uid=12 / uidvalidity=111.
    state.set(_WM_KEY, {"uid": 12, "uidvalidity": 111})
    fake = FakeImap(uidvalidity=111, uids=[10, 11, 12, 13, 14, 15])
    trig, emitted = _make_trigger(monkeypatch, state, fake)

    trig._do_poll()

    # Exactly the three new UIDs (13, 14, 15) each emit one event.
    assert [e["payload"]["provider_message_id"] for e in emitted] == ["13", "14", "15"]
    for e in emitted:
        assert e["event_type"] == "email.received"
        assert e["payload"]["folder"] == "INBOX"
        assert e["payload"]["account"] == "me@example.test"
        # Idempotency key namespaced by folder + uidvalidity + uid.
        assert e["idempotency_key"] == (
            f"imap-poll-INBOX-111-{e['payload']['provider_message_id']}"
        )
    # Watermark advanced to the highest emitted UID.
    assert state.get(_WM_KEY) == {"uid": 15, "uidvalidity": 111}


def test_no_new_uids_emits_nothing_watermark_unchanged(monkeypatch):
    state = FakeState()
    state.set(_WM_KEY, {"uid": 15, "uidvalidity": 111})
    fake = FakeImap(uidvalidity=111, uids=[10, 11, 12, 13, 14, 15])
    trig, emitted = _make_trigger(monkeypatch, state, fake)

    trig._do_poll()

    assert emitted == []
    assert state.get(_WM_KEY) == {"uid": 15, "uidvalidity": 111}


def test_uidvalidity_change_resets_and_emits_nothing(monkeypatch):
    state = FakeState()
    # Old watermark under UIDVALIDITY 111.
    state.set(_WM_KEY, {"uid": 15, "uidvalidity": 111})
    # Server renumbered: new UIDVALIDITY 222, fresh (smaller) UID space.
    fake = FakeImap(uidvalidity=222, uids=[1, 2, 3])
    trig, emitted = _make_trigger(monkeypatch, state, fake)

    trig._do_poll()

    # No events despite UIDs "below" the old watermark — we re-baseline.
    assert emitted == []
    assert state.get(_WM_KEY) == {"uid": 3, "uidvalidity": 222}


def test_emit_cap_per_poll(monkeypatch):
    state = FakeState()
    state.set(_WM_KEY, {"uid": 0, "uidvalidity": 111})
    # 40 new UIDs; the trigger caps emits at _MAX_EMITS_PER_POLL (25).
    fake = FakeImap(uidvalidity=111, uids=list(range(1, 41)))
    trig, emitted = _make_trigger(monkeypatch, state, fake)

    trig._do_poll()

    assert len(emitted) == imap_poll._MAX_EMITS_PER_POLL
    # Watermark advanced to the highest UID we actually emitted (25), so
    # the remaining UIDs are picked up on the next poll.
    assert state.get(_WM_KEY) == {
        "uid": imap_poll._MAX_EMITS_PER_POLL, "uidvalidity": 111,
    }


def test_missing_credentials_skips_poll(monkeypatch):
    state = FakeState()
    state.set(_WM_KEY, {"uid": 5, "uidvalidity": 111})
    monkeypatch.setattr(imap_poll, "resolve_package_secret", lambda p, k: None)
    trig = imap_poll.ImapPollTrigger(
        "t", {}, source_package="carpenter-imap-email", package_state=state,
    )
    emitted = []
    monkeypatch.setattr(
        trig, "emit",
        lambda *a, **k: emitted.append(a) or 1,
    )
    # No connection should be attempted; _do_poll returns cleanly.
    trig._do_poll()
    assert emitted == []
    assert state.get(_WM_KEY) == {"uid": 5, "uidvalidity": 111}
