#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Activate venv if available
if [ -f venv/bin/activate ]; then
    source venv/bin/activate
fi

# Syntax check all modified Python files
echo "=== Syntax checking modified files ==="
ERRORS=0

for f in gateway/platforms/webhook.py hermes_state.py run_agent.py cli.py hermes_cli/commands.py tools/memory_health.py; do
    if [ -f "$f" ]; then
        if python -m py_compile "$f" 2>&1; then
            echo "PASS: $f"
        else
            echo "FAIL: $f"
            ERRORS=$((ERRORS + 1))
        fi
    fi
done

# Check that new file exists if story 3 ran
if [ -f "tools/memory_health.py" ]; then
    echo "=== Verifying memory_health.py imports ==="
    python -c "import sys; sys.path.insert(0, '.'); exec(open('tools/memory_health.py').read().split('class ')[0])" 2>&1 || {
        echo "FAIL: memory_health.py top-level imports broken"
        ERRORS=$((ERRORS + 1))
    }
fi

if [ $ERRORS -gt 0 ]; then
    echo "=== $ERRORS files failed ==="
    exit 1
fi

echo "=== All checks passed ==="
exit 0
