"""Small stand-ins for modules that ChipWhisperer imports but newer environments no longer ship.

ChipWhisperer's TraceWhisperer (``cw.trace``) does ``import pkg_resources`` to locate its bitstream and Verilog defines. setuptools 81 removed ``pkg_resources`` and Python 3.12 virtual environments and the standalone bundles have no setuptools at all, so that import fails. When the real module is missing, Studio registers a minimal one that provides the functions ChipWhisperer uses, built on ``importlib.resources``.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types


def _resource_filename(package: str, resource: str) -> str:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ImportError(f"no package named {package}")
    return os.path.join(list(spec.submodule_search_locations)[0], *resource.split("/"))


def _resource_exists(package: str, resource: str) -> bool:
    return os.path.exists(_resource_filename(package, resource))


def _resource_string(package: str, resource: str) -> bytes:
    with open(_resource_filename(package, resource), "rb") as f:
        return f.read()


def install() -> None:
    """Register the ``pkg_resources`` stand-in unless the real module is importable."""
    if "pkg_resources" in sys.modules or importlib.util.find_spec("pkg_resources") is not None:
        return
    mod = types.ModuleType("pkg_resources", "Minimal pkg_resources stand-in registered by ChipWhisperer Studio (resource_filename, resource_exists, resource_string).")
    mod.resource_filename = _resource_filename
    mod.resource_exists = _resource_exists
    mod.resource_string = _resource_string
    sys.modules["pkg_resources"] = mod
