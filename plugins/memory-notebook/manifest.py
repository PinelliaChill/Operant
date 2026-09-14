"""Manifest factory for the checked-in memory-notebook package."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


def _runtime() -> Any:
    path = Path(__file__).with_name("memory_plugin_sdk.py")
    spec = importlib.util.spec_from_file_location("_operant_memory_notebook_manifest_sdk", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("package runtime is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_manifest() -> dict[str, Any]:
    return _runtime().build_manifest(
        Path(__file__).parent,
        plugin_id="memory-notebook",
        config_schema_ref="memory-notebook-config.v1",
        state_schema_version="memory-notebook-state.v1",
    )


def plugin_manifest() -> Any:
    value = build_manifest()
    try:
        from operant.contracts.b2_1 import PluginManifest
    except ImportError:
        return value
    return PluginManifest.model_validate(value)


manifest = build_manifest
