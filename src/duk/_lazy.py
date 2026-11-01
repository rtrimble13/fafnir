"""Import a module on first use, not when it is named.

duk is run as a process per question -- portable alone runs one per ticker and
one per held stretch of it -- and every process used to pay for pandas, numpy
and the vendor HTTP clients at start, about 0.4s, even for `duk th`, which needs
none of them. :func:`lazy_import` defers that cost to the first attribute access,
so a command pays only for what it touches.

Built on :class:`importlib.util.LazyLoader`, the standard library's mechanism.
A module already imported is returned as it is, so nothing that imported it
first sees a different object. What the deferral changes is *when* an import
error surfaces (at first use rather than at start), never what the module does.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType


def lazy_import(name: str) -> ModuleType:
    """``name``, registered in ``sys.modules`` and executed on first use."""
    found = sys.modules.get(name)
    if found is not None:
        return found
    spec = importlib.util.find_spec(name)
    if spec is None or spec.loader is None:  # pragma: no cover -- a broken install
        raise ImportError(f"No module named {name!r}")
    loader = importlib.util.LazyLoader(spec.loader)
    spec.loader = loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    parent, _, child = name.rpartition(".")
    if parent:
        # What a plain import does: the package carries its submodule.
        setattr(sys.modules[parent], child, module)
    return module
