"""Installable MP-1 plugin examples.

The examples intentionally use only the typed Host API.  They are small
deterministic adapters, not production memory engines.
"""

from examples.plugins.inprocess_memory import ExampleMemoryPlugin

__all__ = ["ExampleMemoryPlugin"]
