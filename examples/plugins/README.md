# MP-1 plugin examples

`ExampleMemoryPlugin` is an in-process implementation.  It receives a
`RestrictedHostApi` and returns the same typed result models used by the stdio
example.  The stdio example communicates with the Host using one JSON object
per line; Host API calls are sent back over the same pipe and are handled by
the Host before the engine response is accepted.

An installation package must contain `dependencies.json` and
`permissions.json`.  MP-1 currently accepts only a dependency declaration with
`{"stdlib_only": true}`; package managers and arbitrary environment
dependencies are unsupported.  Their raw SHA-256 values are bound to the
manifest and to any certification record.  Use
`operant.plugins.compute_package_digest` to build the final package digest
after writing those files.  Uncertified packages can run only through a
successful actual platform sandbox admission.
