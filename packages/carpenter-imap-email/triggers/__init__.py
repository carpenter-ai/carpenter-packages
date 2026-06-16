"""carpenter-imap-email package-shipped trigger classes.

``imap_poll.py`` defines :class:`ImapPollTrigger`, a
:class:`carpenter.core.engine.triggers.base.PollableTrigger` subclass
declared in the manifest's ``triggers:`` block.  The platform's installer
threads ``source_package`` + a ``PackageStateHandle`` into the
constructor; see :mod:`carpenter.packages.installer._install_triggers`.

``_index_common.py`` is composed from the shared carpenter-email-core
layer and supports the DEFERRED semantic-index triggers (not declared in
this package's manifest yet).
"""
