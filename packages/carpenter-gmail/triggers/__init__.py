"""carpenter-email package-shipped trigger classes.

Each module here registers a :class:`carpenter.core.engine.triggers.base.Trigger`
subclass via the manifest's ``triggers:`` block.  The platform's
installer threads ``source_package`` + a ``PackageStateHandle`` into
the constructor; see :mod:`carpenter.packages.installer._install_triggers`.
"""
