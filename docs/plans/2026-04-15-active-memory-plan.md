# Active Memory: Live In-Session Memory Injection

Date: 2026-04-15

## Goal

Hermes memory is currently frozen at session start: `MemoryStore.load_from_disk()` captures a
snapshot that never refreshes, and external provider recall (`prefetch_all`) fires once per turn
but its result is not reflected back into the system prompt. When the agent writes a memory
mid-session the written content is invisible to itself until the next cold boot.

This plan makes memory **active**: every write is immediately visible in subsequent turns,
external provider recall is surfaced as a living context block that updates each turn, and the
system prompt memory section is rebuilt incrementally rather than frozen.

Scope is the built-in `MemoryStore` (MEMORY.md / USER.md) and the `MemoryManager` prefetch
path. External provider `sync_turn` and `on_session_end` are unchanged.

---

## Architecture

```
Turn N                              Turn N+1

user message                        user message
    │                                   │
    ▼                                   ▼
[MemoryManager.prefetch_all]        [MemoryManager.prefetch_all]
    │  external recall (unchanged)      │  external recall (unchanged)
    │                                   │
[MemoryStore.snapshot()]  ◄─────────── write via memory tool (turn N)
    │  rebuilds live block              │
    ▼                                   ▼
system prompt reassembled           system prompt reassembled
    │  (memory section updated)         │  (reflects turn-N writes)
    ▼                                   ▼
API call                            API call
```

**Key insight:** `run_agent.py` already rebuilds parts of the system prompt before each API
call (model, toolsets, persona). Memory is the one block still taken from a frozen snapshot.
We extend the existing rebuild hook to call `MemoryStore.live_snapshot()` instead of the
boot snapshot, and ensure `MemoryManager.prefetch_all()` result is inserted into the
assembled messages list as a refreshed context block rather than only into the initial system
prompt.

---

## Tech Stack

- Python 3.11+  
- Existing `MemoryStore` in `tools/memory_tool.py`  
- Existing `MemoryManager` in `agent/memory_manager.py`  
- Existing `MemoryProvider` ABC in `agent/memory_provider.py`  
- System prompt assembly in `agent/prompt_builder.py`  
- Agent init / turn loop in `run_agent.py`  
- Config schema in `cli-config.yaml.example`  
- Tests in `tests/tools/`, `tests/agent/`, `tests/gateway/`

---

## Sequential Tasks

### Task 1 — Add `live_snapshot()` to `MemoryStore`

**File:** `tools/memory_tool.py`

`MemoryStore` currently exposes `format_for_system_prompt(target)` which always returns the
boot-time snapshot. Add a `live_snapshot(target)` method that reads the on-disk content at
call time, applies the same char limit and formatting, and returns the result without mutating
the stored snapshot.

Steps:
1. Locate `format_for_system_prompt()` (around line 190 in `tools/memory_tool.py`).
2. Extract the formatting logic into a private helper `_format_entries(entries, target)`.
3. Implement `live_snapshot(target: str) -> str` that calls `_read_from_disk(target)` then
   `_format_entries()`. No lock needed for reads (use the existing `_file_lock` only for
   writes).
4. Leave `format_for_system_prompt()` unchanged so the boot snapshot path is still usable
   for callers that want the frozen version (e.g., the flush agent in `gateway/run.py:701`).

**Verification:**
```python
# tests/tools/test_memory_tool.py  — add new test class
class TestLiveSnapshot:
    def test_write_then_live_snapshot_reflects_write(self, tmp_path):
        store = MemoryStore(memory_dir=tmp_path)
        store.load_from_disk()
        store.add_entry("memory", "initial fact")
        live = store.live_snapshot("memory")
        assert "initial fact" in live

    def test_live_snapshot_does_not_mutate_boot_snapshot(self, tmp_path):
        store = MemoryStore(memory_dir=tmp_path)
        store.load_from_disk()
        boot = store.format_for_system_prompt("memory")
        store.add_entry("memory", "new fact")
        assert store.format_for_system_prompt("memory") == boot
        assert "new fact" in store.live_snapshot("memory")
```

**Risk:** `_read_from_disk` may not exist as a standalone function — it may be inlined in
`load_from_disk`. If so, extract it first before writing `live_snapshot`.

---

### Task 2 — Add `live_memory_block()` to `MemoryManager`

**File:** `agent/memory_manager.py`

`MemoryManager.build_system_prompt()` (line 145) iterates `_providers` and calls
`system_prompt_block()` on each. This produces the boot-time block. Add a parallel method
`live_memory_block()` that:

- Calls `_memory_store.live_snapshot("memory")` and `live_snapshot("user")` for the
  built-in provider (detect by `isinstance(p, BuiltinMemoryProvider)` or by name).
- For external providers, calls `system_prompt_block()` unchanged (external providers
  manage their own staleness through `prefetch`).
- Returns the assembled string in the same format as `build_system_prompt()`.

Steps:
1. In `MemoryManager.__init__` (line 78), store a reference to the built-in provider
   separately: `self._builtin_provider: Optional[MemoryProvider] = None`.
2. In `add_provider()` (line 85), detect built-in via `provider.name == "builtin"` (or
   whatever the built-in declares) and assign `self._builtin_provider = provider`.
3. Implement `live_memory_block() -> str`.

**Verification:**
```python
# tests/agent/test_memory_manager.py  — new test
def test_live_memory_block_reflects_mid_session_write():
    manager = MemoryManager()
    manager.add_provider(BuiltinMemoryProvider(store=fake_store))
    fake_store.add_entry("memory", "new fact")
    block = manager.live_memory_block()
    assert "new fact" in block
```

**Risk:** The built-in provider may not use a fixed name string. Read `tools/memory_tool.py`
to find what `name` property the built-in returns before writing the detection logic.

---

### Task 3 — Inject live memory into the per-turn system prompt rebuild

**File:** `run_agent.py`

The agent rebuilds parts of the system prompt before each API call. Locate the point where
memory is inserted into the messages list (search for `format_for_system_prompt` or
`build_system_prompt` in `run_agent.py`, likely around lines 3220–3260).

Steps:
1. Find the existing memory injection call. It likely reads:
   ```python
   memory_block = self._memory_store.format_for_system_prompt("memory")
   ```
2. Replace with:
   ```python
   if self._memory_enabled and self._memory_store:
       memory_block = self._memory_manager.live_memory_block() \
           if self._memory_manager else \
           self._memory_store.live_snapshot("memory")
   ```
3. Gate the change behind a new config key `memory.live_refresh` (default `true`) so
   operators who prefer the frozen-snapshot behavior can opt out without code changes.
4. The config key is read at agent init (line ~1142) alongside existing memory config keys
   and stored in `self._memory_live_refresh: bool`.

**Verification:** No unit-testable hook here — covered by integration test in Task 6.

**Risk:** System prompt rebuild in `run_agent.py` is called in a hot loop (every turn). The
`live_snapshot()` call does a file read. Benchmark on spinning disk; if p99 latency is > 5 ms
consider caching with a 1-turn TTL (store `(mtime, content)` tuple).

---

### Task 4 — Refresh external-provider recall block each turn

**File:** `run_agent.py`, `agent/memory_manager.py`

Currently `prefetch_all()` is called before the API call but its result is injected as a
user-turn message or ephemeral system message that may be assembled only once. Confirm by
searching for `prefetch_all` in `run_agent.py`.

If the recall result is assembled once at session start, move the injection to the per-turn
system prompt rebuild path (same location as Task 3).

Steps:
1. Trace `prefetch_all()` call site in `run_agent.py`. Note whether the result is stored in
   `self._memory_context` or assembled inline.
2. If stored, ensure it is re-fetched and the stored value updated before each API call, not
   just before the first.
3. Wrap the external recall result in `build_memory_context_block()` from
   `agent/memory_manager.py` (line 48) before injection, same as current code does.
4. If `prefetch_all` is already called per-turn, this task is a no-op — verify and close.

**Verification:**
```python
# tests/agent/test_memory_manager.py
def test_prefetch_all_called_each_turn():
    manager = MemoryManager()
    mock_provider = MockExternalProvider()
    manager.add_provider(mock_provider)
    manager.prefetch_all("turn 1 query")
    manager.prefetch_all("turn 2 query")
    assert mock_provider.prefetch_call_count == 2
```

**Risk:** External providers may have per-call cost (API round-trip). If `nudge_interval` is
already throttling calls, ensure Task 4 does not bypass that throttle.

---

### Task 5 — Config key and documentation

**File:** `cli-config.yaml.example`

Add the new `memory.live_refresh` key in the `memory:` block alongside the existing keys
(currently around line 373):

```yaml
memory:
  memory_enabled: true
  live_refresh: true          # Rebuild memory section on every turn (default: true).
                              # Set false to use boot-time snapshot (lower I/O).
  ...
```

**File:** `docs/config.md` (if it exists) — add a one-line entry for `memory.live_refresh`.

**Verification:** Run `python -c "import yaml; yaml.safe_load(open('cli-config.yaml.example'))"` — must parse without error.

---

### Task 6 — Integration test: mid-session write is visible next turn

**File:** `tests/agent/test_active_memory_integration.py` (new file)

Write a test that:
1. Creates an `AIAgent` instance with memory enabled (use existing test fixtures from
   `tests/agent/` to find the right factory pattern).
2. Simulates a tool call that writes a memory entry mid-session.
3. Calls the system-prompt-assembly path for the next turn.
4. Asserts the written entry appears in the assembled system prompt.

```python
class TestActiveMemoryIntegration:
    def test_write_visible_next_turn(self, agent_fixture):
        agent = agent_fixture(memory_enabled=True, live_refresh=True)
        agent._memory_store.add_entry("memory", "project is Hermes")
        prompt = agent._build_system_prompt_for_turn()
        assert "project is Hermes" in prompt

    def test_live_refresh_false_uses_snapshot(self, agent_fixture):
        agent = agent_fixture(memory_enabled=True, live_refresh=False)
        agent._memory_store.add_entry("memory", "should not appear")
        prompt = agent._build_system_prompt_for_turn()
        assert "should not appear" not in prompt
```

**Risk:** `_build_system_prompt_for_turn` may not be a standalone method. Adapt to whatever
the actual call site is after reading the code in Task 3.

---

### Task 7 — Smoke test the gateway flush path is unaffected

**File:** `tests/gateway/test_async_memory_flush.py`

The gateway flush (`gateway/run.py:701`) creates a temporary agent with `skip_memory=True`
and reads from disk directly. Confirm the existing flush tests still pass after Tasks 1–4.
No new code needed — just run:

```
pytest tests/gateway/test_async_memory_flush.py tests/gateway/test_flush_memory_stale_guard.py -v
```

If any test breaks, it is because `live_snapshot()` or `live_memory_block()` introduced a
side effect on the global `MemoryStore` state. Fix by ensuring `live_snapshot` is stateless.

---

## Verification Checklist

- [ ] `pytest tests/tools/test_memory_tool.py` — all existing + new `TestLiveSnapshot` pass
- [ ] `pytest tests/agent/test_memory_manager.py` — live block and per-turn prefetch tests pass
- [ ] `pytest tests/agent/test_active_memory_integration.py` — integration tests pass
- [ ] `pytest tests/gateway/test_async_memory_flush.py tests/gateway/test_flush_memory_stale_guard.py` — no regressions
- [ ] `yaml.safe_load('cli-config.yaml.example')` — parses without error
- [ ] Manual: start a session, write a memory mid-session, confirm it appears in the next
      system prompt (set `HERMES_DEBUG=1` and grep for the memory block in logs)

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| `live_snapshot` I/O latency on spinning disk | Low (SSD common) | Cache `(mtime, content)` with 1-turn TTL |
| Breaking frozen-snapshot flush agent in gateway | Medium | Gate change behind `live_refresh` flag; flush agent always uses frozen snapshot |
| External provider prefetch bypasses throttle | Medium | Check `_turns_since_memory` / `nudge_interval` guard before calling |
| Built-in provider name not stable | Low | Use `isinstance` check rather than name string |
| Mid-session memory injection causes prompt length overflow | Low | `live_snapshot` already applies char limits from config |
