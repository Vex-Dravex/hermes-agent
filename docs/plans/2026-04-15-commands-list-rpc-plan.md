# Gateway `commands.list` RPC — Implementation Plan

## Goal

Expose a machine-readable operator surface so external clients (dashboards,
CI bots, platform integrations) can discover every available command and its
argument metadata without scraping human-readable `/help` text.

## Architecture

```
hermes_cli/commands.py          ← single source of truth (COMMAND_REGISTRY)
        │
        │  commands_list_payload()   ← new: structured JSON-serializable dict
        │
gateway/platforms/api_server.py ← GET /v1/commands (HTTP, auth-gated)
        │
gateway/run.py                  ← /commands.list operator message handler
                                   (dispatched like /help, /commands today)
```

**Response schema** (both surfaces return identical payload):

```json
{
  "schema_version": 1,
  "commands": [
    {
      "name": "background",
      "description": "Run a prompt in the background",
      "category": "Session",
      "aliases": ["bg"],
      "args_hint": "<prompt>",
      "subcommands": [],
      "cli_only": false,
      "gateway_only": false,
      "gateway_config_gate": null
    }
  ]
}
```

## Tech Stack

- **Python stdlib only** — `dataclasses`, `json`; no new dependencies
- **aiohttp** (`web.json_response`) — already used throughout `api_server.py`
- **pytest + unittest.mock** — mirrors existing test patterns

---

## Tasks

### 1. Add `commands_list_payload()` to `hermes_cli/commands.py`

**File:** `hermes_cli/commands.py`  
Insert after `gateway_help_lines()` (≈ line 317).

```python
def commands_list_payload() -> dict:
    """Return a machine-readable dict of every command and its metadata.

    Safe to JSON-serialise; suitable for HTTP responses and operator RPC.
    Schema version is bumped only when field names or semantics change.
    """
    return {
        "schema_version": 1,
        "commands": [
            {
                "name": cmd.name,
                "description": cmd.description,
                "category": cmd.category,
                "aliases": list(cmd.aliases),
                "args_hint": cmd.args_hint,
                "subcommands": list(cmd.subcommands),
                "cli_only": cmd.cli_only,
                "gateway_only": cmd.gateway_only,
                "gateway_config_gate": cmd.gateway_config_gate,
            }
            for cmd in COMMAND_REGISTRY
        ],
    }
```

**Verify:** `python -c "from hermes_cli.commands import commands_list_payload; import json; print(json.dumps(commands_list_payload(), indent=2)[:200])"`

---

### 2. Export the new symbol in `hermes_cli/commands.py`

No `__all__` exists, but the test file imports by name — nothing extra needed.
Confirm `commands_list_payload` is importable alongside peers:

```python
# sanity check used in Task 5
from hermes_cli.commands import commands_list_payload, COMMAND_REGISTRY
assert len(commands_list_payload()["commands"]) == len(COMMAND_REGISTRY)
```

---

### 3. Add HTTP handler to `gateway/platforms/api_server.py`

**File:** `gateway/platforms/api_server.py`  
Add method to `ApiServerAdapter` after `_handle_models` (≈ line 613):

```python
async def _handle_commands_list(self, request: "web.Request") -> "web.Response":
    """GET /v1/commands — machine-readable command registry for operator clients."""
    auth_err = self._check_auth(request)
    if auth_err:
        return auth_err
    from hermes_cli.commands import commands_list_payload
    return web.json_response(commands_list_payload())
```

---

### 4. Register the route in `gateway/platforms/api_server.py`

**File:** `gateway/platforms/api_server.py`  
In the router block (≈ line 2319), add after the `/v1/models` line:

```python
self._app.router.add_get("/v1/commands", self._handle_commands_list)
```

**Verify (requires running gateway):**
```bash
curl -s http://127.0.0.1:8642/v1/commands | python -m json.tool | head -20
```

---

### 5. Add `/commands.list` operator message handler in `gateway/run.py`

**File:** `gateway/run.py`  
Locate `_handle_commands_command` (≈ line 4514). Add a peer method:

```python
async def _handle_commands_list_command(self, event: "MessageEvent") -> str:
    """Handle /commands.list — return JSON command registry for operator clients."""
    import json
    from hermes_cli.commands import commands_list_payload
    return f"```json\n{json.dumps(commands_list_payload(), indent=2)}\n```"
```

Then wire it into the command dispatch block where `/help` and `/commands`
are matched (search for `_handle_help_command` in `_handle_message`):

```python
elif command == "commands.list":
    return await self._handle_commands_list_command(event)
```

Also add `"commands.list"` to `GATEWAY_KNOWN_COMMANDS` by adding a registry
entry in `hermes_cli/commands.py`:

```python
CommandDef("commands.list", "Machine-readable JSON command registry (operator use)", "Info",
           gateway_only=True),
```

---

### 6. Unit tests in `tests/hermes_cli/test_commands.py`

**File:** `tests/hermes_cli/test_commands.py`  
Append a new test class:

```python
from hermes_cli.commands import commands_list_payload

class TestCommandsListPayload:
    def test_returns_dict_with_schema_version(self):
        payload = commands_list_payload()
        assert payload["schema_version"] == 1

    def test_commands_key_matches_registry_length(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        payload = commands_list_payload()
        assert len(payload["commands"]) == len(COMMAND_REGISTRY)

    def test_every_command_has_required_fields(self):
        required = {"name", "description", "category", "aliases",
                    "args_hint", "subcommands", "cli_only",
                    "gateway_only", "gateway_config_gate"}
        for entry in commands_list_payload()["commands"]:
            assert required <= entry.keys(), f"Missing fields in {entry['name']}"

    def test_aliases_and_subcommands_are_lists(self):
        for entry in commands_list_payload()["commands"]:
            assert isinstance(entry["aliases"], list)
            assert isinstance(entry["subcommands"], list)

    def test_payload_is_json_serialisable(self):
        import json
        json.dumps(commands_list_payload())  # must not raise
```

**Run:** `pytest tests/hermes_cli/test_commands.py::TestCommandsListPayload -v`

---

### 7. Integration tests for the HTTP endpoint

**File:** `tests/gateway/test_commands_list_rpc.py` *(new file)*

Mirror the mock setup from `test_discord_slash_commands.py` and
`test_voice_command.py`:

```python
"""Tests for GET /v1/commands — machine-readable command registry endpoint."""

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


def _make_adapter():
    """Instantiate ApiServerAdapter with minimal mocks."""
    # Ensure aiohttp doesn't need a real event loop at import time
    import sys
    aiohttp_mock = MagicMock()
    aiohttp_mock.web.json_response = lambda data: data  # return plain dict for assertions
    sys.modules.setdefault("aiohttp", aiohttp_mock)

    from gateway.platforms.api_server import ApiServerAdapter
    from gateway.config import PlatformConfig, Platform
    cfg = MagicMock(spec=PlatformConfig)
    cfg.platform = Platform.API
    cfg.options = {}
    adapter = ApiServerAdapter.__new__(ApiServerAdapter)
    adapter._check_auth = lambda req: None  # bypass auth
    return adapter


class TestCommandsListEndpoint:
    def test_payload_schema_version(self):
        from hermes_cli.commands import commands_list_payload
        payload = commands_list_payload()
        assert payload["schema_version"] == 1

    def test_all_entries_serialisable(self):
        from hermes_cli.commands import commands_list_payload
        payload = commands_list_payload()
        # Raises if any value is not JSON-safe
        json.dumps(payload)

    def test_commands_list_command_in_registry(self):
        from hermes_cli.commands import COMMAND_REGISTRY
        names = {cmd.name for cmd in COMMAND_REGISTRY}
        assert "commands.list" in names

    def test_commands_list_is_gateway_only(self):
        from hermes_cli.commands import resolve_command
        cmd = resolve_command("commands.list")
        assert cmd is not None
        assert cmd.gateway_only is True
```

**Run:** `pytest tests/gateway/test_commands_list_rpc.py -v`

---

## Verification Sequence

```bash
# 1. Registry function
python -c "from hermes_cli.commands import commands_list_payload; import json; print(len(commands_list_payload()['commands']), 'commands')"

# 2. Unit tests
pytest tests/hermes_cli/test_commands.py::TestCommandsListPayload -v

# 3. Integration tests
pytest tests/gateway/test_commands_list_rpc.py -v

# 4. Full test suite regression check
pytest tests/hermes_cli/test_commands.py tests/gateway/test_discord_slash_commands.py tests/gateway/test_voice_command.py -v

# 5. Live HTTP check (gateway must be running)
curl -s http://127.0.0.1:8642/v1/commands | python -m json.tool | grep '"schema_version"'
```
