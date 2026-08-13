"""Test-wide isolation from the machine.

Two things a run touches outside its own tmp_path: the global run registry
(`~/.local/state/claude-batch`) and the `claude --version` subprocess. Both are
redirected/stubbed for every test, so the suite stays hermetic and offline.
"""

import pytest

from claude_batch import manifest


@pytest.fixture(autouse=True)
def isolate_run_state(tmp_path, monkeypatch):
    monkeypatch.setenv(manifest.REGISTRY_ENV, str(tmp_path / "state"))
    monkeypatch.setattr(manifest, "_claude_version", "claude-test")
