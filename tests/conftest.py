"""Shared pytest fixtures for the unit-test suite."""

import textwrap
from typing import Callable

import pytest


@pytest.fixture
def write_cfg(tmp_path) -> Callable[[str], str]:
    """Return a helper that writes cfg text to a temp file and returns its path.

    Leading indentation is stripped (via textwrap.dedent) so tests can use
    triple-quoted, indented blocks without tripping ConfigParser.
    """
    counter = {"n": 0}

    def _write(text: str) -> str:
        counter["n"] += 1
        path = tmp_path / f"cfg_{counter['n']}.cfg"
        path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")
        return str(path)

    return _write
