"""Gmail history-list polling trigger (carpenter-email, Phase 3a PR-C).

Periodically polls the Gmail ``users.history.list`` API for new
inbound messages and emits one ``email.received`` event per newly-seen
message id.  Subscribers (the package's manifest declares an
``email.received`` subscription that fans out to a triage arc tree)
do the full untrusted fetch + REVIEWER+JUDGE pipeline.

Design notes (cross-reference :mod:`carpenter.notes.phase-3a-plan`):

* **In-process PollableTrigger**, checked each heartbeat.  Self-rate-
  limits via a config-overridable cadence (default 15 minutes).
* **Watermark** persists in :mod:`carpenter.packages.state` under key
  ``history_id``.  CAS-updates on every emit so two overlapping polls
  cannot regress the watermark.
* **Concurrent-poll guard** uses a separate ``poll_in_progress`` flag
  set via CAS at poll entry.  A second heartbeat that arrives while a
  prior poll is still working returns immediately.
* **HTTP cap 10 s** — synchronous urllib call.  If the call times out
  the poll cycle simply skips this heartbeat; we re-try on the next
  cadence.
* **Backpressure**: cap of 25 emits per poll cycle.  When the response
  carries more newly-added messages we still bump the watermark to the
  last-seen ``historyId`` (avoids re-fanning on every cycle), and the
  remaining ids will be picked up on subsequent polls if Gmail still
  returns them in the next history page.
* **First-run init**: if no watermark exists, call ``users.getProfile``
  to read the current ``historyId``, store it, emit nothing.  Backfill
  of historical messages is explicitly out of scope (Phase 4).
* **HTTP 401**: emit one ``email.auth_revoked`` event, then disable
  the trigger in-process for the rest of the daemon's lifetime (until
  the operator re-authorises and restarts).  A persistent flag would
  be wrong here — the operator typically restarts the daemon after
  re-running ``pkg_email_authorize``.
* **HTTP 429 / 5xx**: store a ``gmail_poll_backoff_until`` ISO timestamp
  via ``package_state``; skip the poll until elapsed.

Event payload is **minimal** (provider_message_id, received_history_id,
account).  No subject/from/snippet — the triage arc does the full fetch
inside the trust pipeline.  See the manifest's ``trigger_subscriptions``
for the wiring into ``email.received``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from carpenter.core.engine.triggers.base import PollableTrigger


logger = logging.getLogger(__name__)


# Package state keys.  Held under the carpenter-email PackageStateHandle.
_KEY_HISTORY_ID = "history_id"
_KEY_BACKOFF_UNTIL = "gmail_poll_backoff_until"
_KEY_POLL_IN_PROGRESS = "gmail_poll_in_progress"
_KEY_ACCOUNT_EMAIL = "gmail_account_email"

# Tunables.  Cadence is config-overridable; the rest are constants by
# design — exposing them widens the attack surface for little gain.
_DEFAULT_CADENCE_SECONDS = 15 * 60
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_EMITS_PER_POLL = 25
_BACKOFF_SECONDS_429 = 60 * 60  # 1 hour on rate-limit / server error
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class _AuthRevoked(RuntimeError):
    """Internal sentinel raised on HTTP 401 from Gmail."""


class _RateLimited(RuntimeError):
    """Internal sentinel raised on HTTP 429 / 5xx from Gmail."""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _gmail_request(
    url: str,
    *,
    access_token: str,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One Gmail GET call.  Raises :class:`_AuthRevoked` on 401,
    :class:`_RateLimited` on 429 / 5xx, :class:`RuntimeError` otherwise.

    Returns the parsed JSON body.  Hard 10-second timeout.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise _AuthRevoked(
                f"Gmail returned 401 for {url!r}",
            ) from exc
        if exc.code == 429 or 500 <= exc.code < 600:
            raise _RateLimited(
                f"Gmail returned {exc.code} for {url!r}",
            ) from exc
        raise RuntimeError(
            f"Gmail GET {url!r} failed: HTTP {exc.code}",
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError(
            f"Gmail GET {url!r} failed: {exc}",
        ) from exc


def _normalize_history_id(raw: Any) -> str | None:
    """Gmail returns historyIds as numeric strings.  Coerce to str."""
    if raw is None:
        return None
    if isinstance(raw, (int,)):
        return str(int(raw))
    if isinstance(raw, str):
        raw = raw.strip()
        return raw or None
    return None


class GmailPollTrigger(PollableTrigger):
    """Polls Gmail ``users.history.list`` and emits ``email.received`` events.

    Config keys (all optional, all set via manifest's ``triggers[].config``):

    * ``cadence_seconds``: int seconds between polls.  Default 900 (15 min).
    * ``event_type``: the event_type string to emit.  Default
      ``"email.received"``.

    The trigger reads its OAuth bearer from ``GMAIL_OAUTH_ACCESS_TOKEN``
    in process environment (mirroring the package's EXECUTOR scripts).
    The auth path is the platform's generic OAuth callback flow
    (:mod:`carpenter.api.oauth`); the trigger does NOT refresh tokens
    itself — token refresh is a platform responsibility.
    """

    @classmethod
    def trigger_type(cls) -> str:
        return "gmail_poll"

    def __init__(
        self,
        name: str,
        config: dict,
        *,
        source_package: str | None = None,
        package_state: Any = None,
    ) -> None:
        super().__init__(
            name,
            config,
            source_package=source_package,
            package_state=package_state,
        )
        cadence_raw = config.get("cadence_seconds", _DEFAULT_CADENCE_SECONDS)
        try:
            cadence = int(cadence_raw)
        except (TypeError, ValueError):
            logger.warning(
                "GmailPollTrigger %r: cadence_seconds=%r is not an int; "
                "falling back to %d",
                name, cadence_raw, _DEFAULT_CADENCE_SECONDS,
            )
            cadence = _DEFAULT_CADENCE_SECONDS
        if cadence < 60:
            logger.warning(
                "GmailPollTrigger %r: cadence_seconds=%d is below the "
                "60s floor; clamping to 60",
                name, cadence,
            )
            cadence = 60
        self.cadence_seconds = cadence
        self.event_type = str(config.get("event_type", "email.received"))
        # State that does NOT persist (intentional): set on auth revoke
        # so we stop polling until restart.  Operator workflow is:
        # re-run pkg_email_authorize, restart the daemon.  A persistent
        # flag would risk skipping polls after a successful re-auth.
        self._disabled_in_process = False
        # Heartbeat cadence guard — the heartbeat loop calls check()
        # every few seconds, but we only do a real poll every
        # cadence_seconds.
        self._last_poll_at: datetime | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """First-run init.  Stores the current ``historyId`` if none yet."""
        if self.package_state is None:
            logger.warning(
                "GmailPollTrigger %r: no package_state handle; trigger "
                "cannot persist watermark or backoff state.  Polling "
                "disabled in-process.",
                self.name,
            )
            self._disabled_in_process = True
            return
        # If we already have a watermark, nothing to do here.  Even when
        # the token is currently absent / invalid, we leave the watermark
        # alone and let the first real check() handle the error path.
        try:
            existing = self.package_state.get(_KEY_HISTORY_ID)
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: failed to read history_id from "
                "package_state",
                self.name,
            )
            existing = None
        if existing:
            logger.info(
                "GmailPollTrigger %r: resuming from history_id=%s",
                self.name, existing,
            )
            return
        # First-run init.  Don't fail loudly if we have no token yet —
        # the operator may install before authorising.  Just log and
        # leave the watermark unset; the next check() will retry.
        token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
        if not token:
            logger.info(
                "GmailPollTrigger %r: no GMAIL_OAUTH_ACCESS_TOKEN in env "
                "at start; first-run init deferred until token is set",
                self.name,
            )
            return
        try:
            self._initialise_watermark(token)
        except Exception:
            # First-run init failures are non-fatal — we'll retry each
            # poll cycle until init succeeds.
            logger.exception(
                "GmailPollTrigger %r: first-run init failed; will retry "
                "on next poll",
                self.name,
            )

    def stop(self) -> None:
        """Best-effort: clear the poll-in-progress flag on shutdown.

        If the daemon crashes mid-poll the flag may be left set; the
        backoff window provides a self-clearing guard but we still try
        to be tidy on clean shutdown.
        """
        if self.package_state is None:
            return
        try:
            self.package_state.delete(_KEY_POLL_IN_PROGRESS)
        except Exception:
            logger.debug(
                "GmailPollTrigger %r: stop() failed to clear "
                "poll_in_progress",
                self.name,
                exc_info=True,
            )

    # ── Heartbeat entry point ────────────────────────────────────────

    def check(self) -> None:
        """Called each heartbeat.  Decides whether to do a real poll."""
        if self._disabled_in_process:
            return
        if self.package_state is None:
            return
        now = _now_utc()
        # Cadence guard.
        if self._last_poll_at is not None:
            delta = (now - self._last_poll_at).total_seconds()
            if delta < self.cadence_seconds:
                return
        # Backoff guard.
        backoff_until = self._backoff_until()
        if backoff_until is not None and now < backoff_until:
            logger.debug(
                "GmailPollTrigger %r: in backoff until %s; skipping",
                self.name, backoff_until.isoformat(),
            )
            return
        # Concurrent-poll guard.  CAS on the flag — if a concurrent
        # heartbeat (different thread, future) has already set it, we
        # back out without changing anything.
        if not self._claim_poll_slot():
            logger.debug(
                "GmailPollTrigger %r: another poll in progress; skipping",
                self.name,
            )
            return
        try:
            self._do_poll()
        finally:
            self._release_poll_slot()
            self._last_poll_at = _now_utc()

    # ── Internals ────────────────────────────────────────────────────

    def _claim_poll_slot(self) -> bool:
        """CAS-set ``poll_in_progress`` from absent → True.

        Returns True if we own the slot, False if another caller owns it.
        """
        state = self.package_state
        if state is None:
            return False
        # ``cas(expected_version=0, ...)`` is the "insert-if-absent" form
        # per :mod:`carpenter.packages.state`.  If the row exists at any
        # version we lose the CAS and return False.
        try:
            return bool(state.cas(_KEY_POLL_IN_PROGRESS, 0, True))
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: CAS on poll_in_progress failed",
                self.name,
            )
            return False

    def _release_poll_slot(self) -> None:
        state = self.package_state
        if state is None:
            return
        try:
            state.delete(_KEY_POLL_IN_PROGRESS)
        except Exception:
            logger.debug(
                "GmailPollTrigger %r: failed to clear poll_in_progress",
                self.name,
                exc_info=True,
            )

    def _backoff_until(self) -> datetime | None:
        state = self.package_state
        if state is None:
            return None
        try:
            raw = state.get(_KEY_BACKOFF_UNTIL)
        except Exception:
            return None
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            logger.warning(
                "GmailPollTrigger %r: malformed backoff value %r; clearing",
                self.name, raw,
            )
            try:
                state.delete(_KEY_BACKOFF_UNTIL)
            except Exception:
                pass
            return None

    def _set_backoff(self, seconds: int) -> None:
        state = self.package_state
        if state is None:
            return
        until = _now_utc() + timedelta(seconds=int(seconds))
        try:
            state.set(_KEY_BACKOFF_UNTIL, _iso(until))
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: failed to store backoff",
                self.name,
            )

    def _clear_backoff(self) -> None:
        state = self.package_state
        if state is None:
            return
        try:
            state.delete(_KEY_BACKOFF_UNTIL)
        except Exception:
            pass

    def _get_watermark(self) -> tuple[str | None, int]:
        state = self.package_state
        if state is None:
            return None, 0
        try:
            res = state.get_with_version(_KEY_HISTORY_ID)
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: failed to read history_id",
                self.name,
            )
            return None, 0
        if res is None:
            return None, 0
        value, version = res
        return _normalize_history_id(value), int(version)

    def _set_watermark(
        self, history_id: str, expected_version: int,
    ) -> bool:
        state = self.package_state
        if state is None:
            return False
        try:
            return bool(state.cas(
                _KEY_HISTORY_ID, expected_version, history_id,
            ))
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: CAS on history_id failed",
                self.name,
            )
            return False

    def _initialise_watermark(self, access_token: str) -> None:
        """Call ``users.getProfile`` to discover the current historyId.

        Also caches the authenticated account email for later use by
        the triage arc (and so the chat agent has an account label).
        """
        data = _gmail_request(
            f"{_GMAIL_BASE}/profile", access_token=access_token,
        )
        hid = _normalize_history_id(data.get("historyId"))
        if not hid:
            raise RuntimeError(
                f"Gmail getProfile returned no historyId: {data!r}",
            )
        account = data.get("emailAddress") or ""
        state = self.package_state
        if state is None:
            return
        # Use ``set`` (last-write-wins) for first-run; CAS isn't needed
        # because we already hold the poll slot.
        state.set(_KEY_HISTORY_ID, hid)
        if account:
            state.set(_KEY_ACCOUNT_EMAIL, str(account))
        logger.info(
            "GmailPollTrigger %r: first-run init complete (history_id=%s, "
            "account=%s)",
            self.name, hid, account or "<unknown>",
        )

    def _do_poll(self) -> None:
        token = os.environ.get("GMAIL_OAUTH_ACCESS_TOKEN", "")
        if not token:
            logger.debug(
                "GmailPollTrigger %r: no GMAIL_OAUTH_ACCESS_TOKEN; "
                "skipping",
                self.name,
            )
            return
        watermark, watermark_version = self._get_watermark()
        if watermark is None:
            # First-run init that didn't happen at start() (no token at
            # the time).  Do it now.
            try:
                self._initialise_watermark(token)
            except _AuthRevoked:
                self._on_auth_revoked()
            except _RateLimited:
                self._set_backoff(_BACKOFF_SECONDS_429)
                logger.warning(
                    "GmailPollTrigger %r: rate-limited during init; "
                    "backing off",
                    self.name,
                )
            except Exception:
                logger.exception(
                    "GmailPollTrigger %r: first-run init failed",
                    self.name,
                )
            return
        # Fetch new history pages from the watermark.
        try:
            messages, new_history_id = self._collect_new_messages(
                token, start_history_id=watermark,
            )
        except _AuthRevoked:
            self._on_auth_revoked()
            return
        except _RateLimited:
            self._set_backoff(_BACKOFF_SECONDS_429)
            logger.warning(
                "GmailPollTrigger %r: rate-limited; backing off",
                self.name,
            )
            return
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: history.list failed",
                self.name,
            )
            return
        # Successful poll — clear any prior backoff window.
        self._clear_backoff()
        if not messages and (
            new_history_id is None or new_history_id == watermark
        ):
            logger.debug(
                "GmailPollTrigger %r: no new history since %s",
                self.name, watermark,
            )
            return
        account = ""
        try:
            account = self.package_state.get(_KEY_ACCOUNT_EMAIL, "") or ""
        except Exception:
            account = ""
        # Cap emits at 25 per poll for backpressure (see plan).  The
        # ``messages`` list is already truncated by _collect_new_messages
        # to MAX_EMITS_PER_POLL, but we re-check here as defence in depth.
        emitted = 0
        for mid in messages[:_MAX_EMITS_PER_POLL]:
            payload = {
                "provider_message_id": mid,
                "received_history_id": new_history_id or watermark,
                "account": account,
            }
            idem = f"gmail-poll-{mid}"
            self.emit(
                self.event_type,
                payload=payload,
                idempotency_key=idem,
            )
            emitted += 1
        # Advance the watermark via CAS so an overlapping poll cannot
        # regress it.  If CAS fails another poll already advanced it;
        # we drop our update silently — the messages we just emitted are
        # idempotent on the work-queue side.
        if new_history_id and new_history_id != watermark:
            ok = self._set_watermark(new_history_id, watermark_version)
            if not ok:
                logger.debug(
                    "GmailPollTrigger %r: watermark CAS lost race "
                    "(expected_version=%d); leaving as-is",
                    self.name, watermark_version,
                )
        if emitted:
            logger.info(
                "GmailPollTrigger %r: emitted %d email.received event(s) "
                "(new history_id=%s)",
                self.name, emitted, new_history_id or watermark,
            )

    def _collect_new_messages(
        self,
        access_token: str,
        *,
        start_history_id: str,
    ) -> tuple[list[str], str | None]:
        """Walk ``users.history.list`` pages from the watermark.

        Returns a tuple ``(message_ids, new_history_id)``.  The message
        id list is deduplicated and capped at :data:`_MAX_EMITS_PER_POLL`.
        ``new_history_id`` is the largest ``historyId`` value seen in
        the response (Gmail's ``historyId`` field on the top-level
        envelope, or the last entry's id if the envelope is missing).
        """
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
        }
        url = f"{_GMAIL_BASE}/history?{urllib.parse.urlencode(params)}"
        ids: list[str] = []
        seen: set[str] = set()
        new_history_id: str | None = None
        page_count = 0
        while True:
            page_count += 1
            if page_count > 5:
                # Defence in depth — Gmail typically returns at most a
                # handful of pages before nextPageToken stops being
                # set.  Cap at 5 to bound worst-case HTTP work.
                logger.warning(
                    "GmailPollTrigger %r: history.list returned >5 pages; "
                    "stopping early",
                    self.name,
                )
                break
            data = _gmail_request(url, access_token=access_token)
            envelope_hid = _normalize_history_id(data.get("historyId"))
            if envelope_hid is not None:
                new_history_id = envelope_hid
            for entry in data.get("history", []) or []:
                entry_hid = _normalize_history_id(entry.get("id"))
                if entry_hid is not None:
                    # Pick the largest id we see — historyIds are monotonic.
                    if new_history_id is None or self._gt(entry_hid, new_history_id):
                        new_history_id = entry_hid
                for added in entry.get("messagesAdded", []) or []:
                    msg = added.get("message") or {}
                    mid = msg.get("id")
                    if not isinstance(mid, str) or not mid:
                        continue
                    if mid in seen:
                        continue
                    seen.add(mid)
                    ids.append(mid)
                    if len(ids) >= _MAX_EMITS_PER_POLL:
                        return ids, new_history_id
            next_token = data.get("nextPageToken")
            if not next_token:
                break
            # Re-encode with the page token.
            params["pageToken"] = next_token
            url = f"{_GMAIL_BASE}/history?{urllib.parse.urlencode(params)}"
        return ids, new_history_id

    @staticmethod
    def _gt(a: str, b: str) -> bool:
        """Compare two Gmail historyId strings as integers, defensively."""
        try:
            return int(a) > int(b)
        except (TypeError, ValueError):
            return a > b

    def _on_auth_revoked(self) -> None:
        """Emit ``email.auth_revoked`` once and disable in-process."""
        logger.warning(
            "GmailPollTrigger %r: Gmail returned 401; emitting "
            "email.auth_revoked and disabling in-process until restart",
            self.name,
        )
        try:
            self.emit(
                "email.auth_revoked",
                payload={"account": self._account_or_empty()},
                idempotency_key=f"gmail-auth-revoked-{self.name}",
            )
        except Exception:
            logger.exception(
                "GmailPollTrigger %r: emit(email.auth_revoked) failed",
                self.name,
            )
        self._disabled_in_process = True

    def _account_or_empty(self) -> str:
        try:
            return self.package_state.get(_KEY_ACCOUNT_EMAIL, "") or ""
        except Exception:
            return ""
