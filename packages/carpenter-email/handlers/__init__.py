"""carpenter-email package subscription handlers.

Each handler in this package is referenced from the manifest's
``trigger_subscriptions:`` block.  The platform's
:mod:`carpenter.packages.subscription_handler` invokes the handler
when an event matches.

Phase 3a (PR-C) ships :func:`triage_inbound.handle_email_received`,
the dispatch target for ``email.received`` events emitted by
:class:`carpenter_email.triggers.gmail_poll.GmailPollTrigger`.
"""
