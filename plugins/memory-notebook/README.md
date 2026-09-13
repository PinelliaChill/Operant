# memory-notebook

An independent key/value implementation of `operant-memory-sdk.v1`.  It
accepts explicit `key=value` or `key: value` lines, stores a Host-managed
private index when the trusted adapter exposes registration, and performs
case-sensitive exact-key recall.  Each explicit save produces one pending
proposal; a published `MemoryHead` event is the only thing that moves that
entry into the recall index.  Its proposal and index strategy is separate from
`memory-standard`; neither package can publish a head or grant evidence
status.  The verified in-process entry is `plugin.py:create_plugin`, while the
same file is runnable through the isolated `python -I -S` stdio adapter.
