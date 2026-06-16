"""carpenter-imap-email package handlers.

Two handler families live here:

* :mod:`handlers.imap_smtp` — the TRUSTED platform-side capability
  handlers (``imap.fetch`` / ``imap.search`` / ``imap.store`` /
  ``smtp.send``) referenced from the manifest's ``platform_capabilities``
  section.  These run parent-side with egress + credentials.
* :mod:`handlers.triage_inbound` — the (DEFERRED) ``email.received``
  subscription shim, composed verbatim from the carpenter-email-core
  layer.  The MVP manifest does not declare the triage subscription, so
  this module is dormant until the inbound-poll feature ships in v0.2.0.
"""
