# memory-standard

The default source-backed memory engine for `operant-memory-sdk.v1`.

Build a manifest with `plugins/memory-standard/manifest.py` and install the
directory through `PluginRegistry.install`.  The verified in-process entry is
`plugin.py:create_plugin`; isolated Host execution starts the same file with
`python -I -S`.  `extract` reads every source through `host.read_source` and
returns pending proposals.  `recall` delegates to `host.search`, so Core keeps
ownership, source evidence, permission, and publication/CAS authority.  A
source envelope may provide a Core-selected `record_id`, version and base
head; those values remain proposals until Core accepts them.
