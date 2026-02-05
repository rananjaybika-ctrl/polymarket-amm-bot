#!/usr/bin/env python3
"""
PostToolUse hook for Edit tool: Verify the edit was complete.

After an Edit completes, check if there might be other occurrences
of the same value that should also have been changed.

This is an async hook - it runs in the background and reports findings
on the next turn.
"""
import json
import sys
import subprocess
import re
from pathlib import Path


def main():
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = input_data.get('tool_input', {})
    old_string = tool_input.get('old_string', '')
    file_path = tool_input.get('file_path', '')
    cwd = input_data.get('cwd', '.')

    # Only check for config-like values
    config_patterns = [
        r'^[\d.]+$',           # Numbers
        r'^(true|false)$',     # Booleans
        r'^0\.\d+$',           # Decimal thresholds
    ]

    is_config_value = any(re.match(p, old_string.strip(), re.IGNORECASE) for p in config_patterns)

    if not is_config_value:
        sys.exit(0)

    # Run grep to find other occurrences
    try:
        result = subprocess.run(
            ['grep', '-rn', old_string, '--include=*.py'],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0 and result.stdout.strip():
            matches = result.stdout.strip().split('\n')
            num_matches = len(matches)

            if num_matches > 0:
                # Filter out archive/cache files
                relevant = [m for m in matches if 'archive' not in m.lower() and '__pycache__' not in m]
                if relevant:
                    output = {
                        "additionalContext": (
                            f"⚠️ ORPHANED VALUES DETECTED (Mistake #30, #44)!\n\n"
                            f"After your edit, '{old_string}' still exists in {len(relevant)} location(s):\n"
                            f"{chr(10).join(relevant[:5])}\n"
                            f"{'...(more)' if len(relevant) > 5 else ''}\n\n"
                            f"🚨 YOU MUST UPDATE THESE TOO or this will cause config mismatches!\n"
                            f"Do NOT declare work 'done' until all occurrences are fixed."
                        )
                    }
                    print(json.dumps(output))

    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    sys.exit(0)


if __name__ == '__main__':
    main()
