"""Shared test helpers: run the suite honestly under the hosts' Python.

Fleet hosts have two interpreters. launchd agents, the interval guard and the
tartci_toml_exec_or_python3 fallbacks run /usr/bin/python3, which is 3.9 and
has no tomllib; the profile-reading paths run under tartci_toml_python
(Homebrew 3.11+). The python-39-tests CI job runs every test module under 3.9,
so a test that needs tomllib must say so instead of failing there, and a test
that does not is proven to work on the hosts' Python.

    @testing_support.requires_tomllib        # one test or class
    testing_support.skip_module_without_tomllib()   # a whole module, at import

Python 3.9-safe.
"""

from __future__ import annotations

import unittest

try:
    import tomllib  # noqa: F401
    HAVE_TOMLLIB = True
except ModuleNotFoundError:  # /usr/bin/python3 on the hosts is 3.9
    HAVE_TOMLLIB = False

REASON = "needs tomllib (Python 3.11+); the hosts' /usr/bin/python3 is 3.9"
requires_tomllib = unittest.skipUnless(HAVE_TOMLLIB, REASON)


def skip_module_without_tomllib() -> None:
    """Skip the importing test module under Python < 3.11 (call before its imports)."""
    if not HAVE_TOMLLIB:
        raise unittest.SkipTest(REASON)
