"""The interpreter a TartCI process starts its Python children with.

Under launcher v2 a lane's Python runs through the generation's interpreter
shim (`bin/python3` = the pinned interpreter with -B -E -s -S), and the
launcher sets TARTCI_PYTHON to that shim. The shim execs the pinned
interpreter, so inside a lane `sys.executable` is the real pin: a child started
with it would run site-packages start-up code (.pth files, sitecustomize) that
nothing hashed. Every Python child therefore starts with child_python().

Under v1 TARTCI_PYTHON is the pinned interpreter this process was started
from, or unset, so child_python() resolves the same interpreter as
`sys.executable` did before.
"""

from __future__ import annotations

import os
import sys


def child_python() -> str:
    """$TARTCI_PYTHON when it is an absolute path; else this interpreter.

    An absolute TARTCI_PYTHON that is not an executable file is refused rather
    than silently replaced: it names the interpreter every child must use.
    Unset, or a bare command name (tartci_toml_python resolved that name to
    start this very process), keeps this interpreter, exactly as before.
    """
    configured = os.environ.get("TARTCI_PYTHON", "")
    if not configured.startswith("/"):
        return sys.executable
    if not (os.path.isfile(configured) and os.access(configured, os.X_OK)):
        raise RuntimeError(f"TARTCI_PYTHON is not an executable file: {configured}")
    return configured
