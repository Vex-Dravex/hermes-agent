"""
Active Memory v2 — agent-side tests.

Verifies:
  1. _build_system_prompt omits built-in memory when live_refresh=True (default).
  2. _build_system_prompt includes frozen snapshot when live_refresh=False.
  3. _build_live_memory_block() returns "" when live_refresh=False.
  4. _build_live_memory_block() returns live entries when live_refresh=True.
  5. After a mid-session add, _build_live_memory_block() includes the new entry
     while _cached_system_prompt stays unchanged.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tools.memory_tool import MemoryStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(**extra):
    """Return a minimal AIAgent with memory tool registered and no real I/O."""
    defaults = dict(
        api_key="test-key-1234567890",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,   # we attach a MemoryStore manually below
    )
    defaults.update(extra)
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("memory")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(**defaults)
    a.client = MagicMock()
    return a


def _attach_store(agent, store: MemoryStore, *, live_refresh: bool = True):
    """Wire a MemoryStore into an agent, bypassing config loading."""
    agent._memory_store = store
    agent._memory_enabled = True
    agent._user_profile_enabled = True
    agent._memory_live_refresh = live_refresh


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.load_from_disk()
    return s


# ---------------------------------------------------------------------------
# _build_system_prompt — memory gate
# ---------------------------------------------------------------------------

class TestBuildSystemPromptMemoryGate:
    def test_omits_memory_when_live_refresh_true(self, store):
        """With live_refresh=True, built-in blocks must NOT appear in cached prompt."""
        store.add("memory", "should NOT appear in cached prompt")
        store.load_from_disk()   # capture frozen snapshot that includes the entry

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        prompt = agent._build_system_prompt()

        assert "should NOT appear in cached prompt" not in prompt

    def test_includes_memory_when_live_refresh_false(self, store):
        """With live_refresh=False, frozen snapshot must be baked into the cached prompt."""
        store.add("memory", "should appear in cached prompt")
        store.load_from_disk()   # frozen snapshot captures the entry

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=False)

        prompt = agent._build_system_prompt()

        assert "should appear in cached prompt" in prompt

    def test_both_targets_omitted_when_live_refresh_true(self, store):
        """Both memory and user blocks are skipped from the cached prompt."""
        store.add("memory", "memory-note")
        store.add("user", "user-note")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        prompt = agent._build_system_prompt()

        assert "memory-note" not in prompt
        assert "user-note" not in prompt

    def test_both_targets_present_when_live_refresh_false(self, store):
        """Both memory and user blocks are baked in when live_refresh=False."""
        store.add("memory", "memory-note")
        store.add("user", "user-note")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=False)

        prompt = agent._build_system_prompt()

        assert "memory-note" in prompt
        assert "user-note" in prompt


# ---------------------------------------------------------------------------
# _build_live_memory_block
# ---------------------------------------------------------------------------

class TestBuildLiveMemoryBlock:
    def test_returns_empty_when_no_store(self):
        agent = _make_agent()
        # _memory_store is None (skip_memory=True, nothing attached)
        assert agent._build_live_memory_block() == ""

    def test_returns_empty_when_live_refresh_false(self, store):
        store.add("memory", "some entry")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=False)

        assert agent._build_live_memory_block() == ""

    def test_returns_empty_when_no_entries(self, store):
        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)   # empty store

        assert agent._build_live_memory_block() == ""

    def test_returns_live_memory_entry(self, store):
        store.add("memory", "live memory entry")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        block = agent._build_live_memory_block()
        assert "live memory entry" in block

    def test_returns_live_user_entry(self, store):
        store.add("user", "Name: Charlie")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        block = agent._build_live_memory_block()
        assert "Name: Charlie" in block

    def test_returns_both_targets(self, store):
        store.add("memory", "mem-fact")
        store.add("user", "user-pref")
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        block = agent._build_live_memory_block()
        assert "mem-fact" in block
        assert "user-pref" in block


# ---------------------------------------------------------------------------
# Mid-session write: live injection picks up new entries without touching cache
# ---------------------------------------------------------------------------

class TestMidSessionLiveInjection:
    def test_mid_session_add_visible_in_live_block(self, store):
        """A mid-session add is immediately reflected in _build_live_memory_block."""
        store.load_from_disk()   # empty frozen snapshot

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        # Simulate what run_agent does: build & cache the system prompt first
        agent._cached_system_prompt = agent._build_system_prompt()
        cached_before = agent._cached_system_prompt

        # Mid-session write (simulates memory tool call result)
        store.add("memory", "fact added mid-session")

        # The live block must include the new entry…
        live_block = agent._build_live_memory_block()
        assert "fact added mid-session" in live_block

        # …but the cached prompt must be unchanged (no rebuild triggered)
        assert agent._cached_system_prompt == cached_before
        assert "fact added mid-session" not in agent._cached_system_prompt

    def test_frozen_snapshot_unchanged_after_live_refresh_write(self, store):
        """live_snapshot() must not alter the frozen _system_prompt_snapshot."""
        store.add("memory", "pre-session entry")
        store.load_from_disk()
        snapshot_before = dict(store._system_prompt_snapshot)

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=True)

        store.add("memory", "new mid-session entry")
        _ = agent._build_live_memory_block()   # side-effect-free read

        assert store._system_prompt_snapshot == snapshot_before

    def test_live_refresh_false_live_block_empty_after_write(self, store):
        """With live_refresh=False, _build_live_memory_block stays empty after writes."""
        store.load_from_disk()

        agent = _make_agent()
        _attach_store(agent, store, live_refresh=False)

        store.add("memory", "new entry")

        assert agent._build_live_memory_block() == ""
