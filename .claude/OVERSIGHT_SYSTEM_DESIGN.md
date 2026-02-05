# Claude Oversight System Design

## The Problem

**Documentation alone does not prevent Claude from repeating mistakes.**

The user has:
- Documented 44 mistakes in `CLAUDE_MISTAKES.md`
- Created a `/cm` skill to read the file
- Added "READ THIS" headers
- Put instructions in `CLAUDE.md`

**Result:** Claude still repeats the same patterns. Example: "change first grep result without checking all occurrences" happened 3+ times (documented as Mistake #30 AND #44).

### Why Documentation Fails

1. **No enforcement mechanism** - Reading != Following
2. **Context window pressure** - Long documents get "skimmed"
3. **Pattern blindness** - Claude doesn't recognize when it's about to repeat a mistake
4. **No feedback loop** - Mistakes aren't caught until damage is done
5. **Passive information** - Documentation requires active recall at the right moment

## Design Principles

1. **Enforce at the point of action** - Intercept BEFORE the mistake happens
2. **Make it impossible, not just documented** - Technical guardrails > instructions
3. **Provide real-time feedback** - Show relevant warnings when they matter
4. **Create friction for risky patterns** - Force explicit acknowledgment
5. **Automated verification** - Don't trust self-reports

---

## The Three-Layer Oversight System

### Layer 1: PreToolUse Hooks (Preventive)

Intercept tool calls BEFORE execution and check for known mistake patterns.

**File:** `.claude/hooks/mistake-guard.py`

```python
#!/usr/bin/env python3
"""
PreToolUse hook that blocks common mistake patterns.
Reads CLAUDE_MISTAKES.md and enforces specific checks.
"""
import json
import sys
import re

def check_grep_patterns(tool_input: dict) -> tuple[bool, str]:
    """
    Mistake #30, #44: Changing first grep result without checking all.
    When about to edit a config value, verify ALL occurrences were considered.
    """
    # This hook runs on Edit tool, checking if it's a config-like change
    if 'old_string' in tool_input:
        old_val = tool_input.get('old_string', '')
        # If it looks like a config value (number, bool, threshold, etc.)
        if re.match(r'^[\d.]+$|^(true|false)$|^0\.\d+$', old_val.strip()):
            return False, (
                "MISTAKE PREVENTION (#30, #44): Before editing config values, "
                "you MUST grep for ALL occurrences first. "
                "Have you checked: 1) TRADING_CONFIGS.py 2) All files with this value? "
                "Run: grep -rn '{old_val}' --include='*.py' and fix ALL relevant occurrences."
            )
    return True, ""

def check_ssh_commands(tool_input: dict) -> tuple[bool, str]:
    """
    Mistake #39: Not checking deploy.sh for SSH config.
    """
    command = tool_input.get('command', '')
    if 'ssh ' in command and '.ssh/' in command:
        return False, (
            "MISTAKE PREVENTION (#39): You're using ~/.ssh/ for SSH key. "
            "CHECK deploy.sh FIRST for the correct key path. "
            "Known config: $HOME/Downloads/polymarket-key.pem"
        )
    return True, ""

def check_kill_commands(tool_input: dict) -> tuple[bool, str]:
    """
    Mistake documented in header: Use systemctl, not kill.
    """
    command = tool_input.get('command', '')
    if 'kill' in command and ('polymarket' in command or 'bot' in command):
        return False, (
            "MISTAKE PREVENTION: Use systemctl to stop services, not kill. "
            "Command: sudo systemctl stop polymarket-bot"
        )
    return True, ""

def check_data_collection_script(tool_input: dict) -> tuple[bool, str]:
    """
    Mistake #25: Running observer.py instead of run_data_collection.py.
    """
    command = tool_input.get('command', '')
    if 'observer.py' in command and 'run_data_collection' not in command:
        return False, (
            "MISTAKE PREVENTION (#25): DO NOT run observer.py directly! "
            "Use scripts/run_data_collection.py which runs BOTH observer AND Binance logger."
        )
    return True, ""

def check_backtest_from_scratch(tool_input: dict) -> tuple[bool, str]:
    """
    Mistake #33: Writing backtest logic from scratch instead of copying.
    """
    content = tool_input.get('content', '')
    file_path = tool_input.get('file_path', '')

    if 'backtest' in file_path.lower():
        # Check if it imports from validated sources
        if 'from research.reference.TRADING_CONFIGS import' not in content:
            if 'spike_threshold' in content or 'lookback' in content:
                return False, (
                    "MISTAKE PREVENTION (#33): New backtest scripts MUST import from "
                    "TRADING_CONFIGS.py. Copy simulation logic from validated files "
                    "(test_obi_comparison_oos7.py), don't write from scratch."
                )
    return True, ""

def main():
    input_data = json.load(sys.stdin)
    tool_name = input_data.get('tool_name', '')
    tool_input = input_data.get('tool_input', {})

    checks = []

    if tool_name == 'Bash':
        checks.extend([
            check_ssh_commands(tool_input),
            check_kill_commands(tool_input),
            check_data_collection_script(tool_input),
        ])
    elif tool_name == 'Edit':
        checks.extend([
            check_grep_patterns(tool_input),
        ])
    elif tool_name == 'Write':
        checks.extend([
            check_backtest_from_scratch(tool_input),
        ])

    # Check for any failures
    for passed, reason in checks:
        if not passed:
            # Output JSON to block with reason
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason
                }
            }))
            sys.exit(0)

    # All checks passed
    sys.exit(0)

if __name__ == '__main__':
    main()
```

### Layer 2: Stop Hook with Reviewer Agent (Corrective)

After Claude finishes responding, spawn a reviewer agent to check for mistake patterns.

**File:** `.claude/hooks/review-for-mistakes.sh`

```bash
#!/bin/bash
# Stop hook: Triggers mistake review agent

INPUT=$(cat)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active')

# Prevent infinite loops
if [ "$STOP_HOOK_ACTIVE" = "true" ]; then
    exit 0
fi

# Output JSON to trigger review
cat << 'EOF'
{
    "decision": "block",
    "reason": "MANDATORY MISTAKE CHECK: Before completing, verify: 1) Did you grep ALL occurrences before editing config values? 2) Did you check file contents before declaring data unavailable? 3) Did you import from TRADING_CONFIGS.py for any backtest? If you violated any of these, FIX NOW."
}
EOF
```

**Alternative: Agent-based Stop Hook**

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "agent",
            "prompt": "Review the conversation for CLAUDE_MISTAKES.md violations. Check: 1) Were config values edited without grepping ALL occurrences first? 2) Were assumptions made about data availability without checking file contents? 3) Were backtest scripts written from scratch instead of copying from validated files? 4) Were SSH commands run without checking deploy.sh first? Context: $ARGUMENTS",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

### Layer 3: SessionStart Context Injection (Preventive)

Inject critical warnings at session start, but PARSED and SUMMARIZED, not the whole 800-line file.

**File:** `.claude/hooks/inject-mistake-context.py`

```python
#!/usr/bin/env python3
"""
SessionStart hook: Parse CLAUDE_MISTAKES.md and inject a FOCUSED summary.
The problem with reading the whole file: it's too long and gets skimmed.
Solution: Extract the TOP 5 most repeated/recent mistakes as a checklist.
"""
import json
import sys
import re
from pathlib import Path
from collections import Counter

def parse_mistakes(content: str) -> list[dict]:
    """Extract structured mistake data."""
    mistakes = []

    # Find all mistake sections (### N. or ### TITLE patterns)
    pattern = r'### (\d+)\. ([^\n]+)\n\*\*What happened:\*\* ([^\n]+)'
    matches = re.findall(pattern, content)

    for num, title, what in matches:
        mistakes.append({
            'number': int(num),
            'title': title.strip(),
            'what': what.strip()
        })

    return mistakes

def get_recent_mistakes(mistakes: list, n: int = 5) -> list:
    """Get the N most recent mistakes (highest numbers)."""
    sorted_mistakes = sorted(mistakes, key=lambda x: x['number'], reverse=True)
    return sorted_mistakes[:n]

def main():
    input_data = json.load(sys.stdin)

    # Read the mistakes file
    mistakes_path = Path(input_data.get('cwd', '.')) / 'CLAUDE_MISTAKES.md'

    if not mistakes_path.exists():
        sys.exit(0)

    content = mistakes_path.read_text()
    mistakes = parse_mistakes(content)
    recent = get_recent_mistakes(mistakes, 5)

    # Build focused context
    context_lines = [
        "=== ACTIVE MISTAKE PREVENTION ===",
        "Most recent mistakes (DO NOT REPEAT):",
        ""
    ]

    for m in recent:
        context_lines.append(f"#{m['number']}: {m['title']}")
        context_lines.append(f"   -> {m['what'][:100]}...")
        context_lines.append("")

    context_lines.extend([
        "MANDATORY CHECKS before ANY action:",
        "[ ] Config change? -> grep ALL occurrences first",
        "[ ] Backtest script? -> import from TRADING_CONFIGS.py",
        "[ ] SSH command? -> check deploy.sh for key path",
        "[ ] Data assumption? -> check file contents first",
        "[ ] Editing list? -> verify item count before/after"
    ])

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(context_lines)
        }
    }

    print(json.dumps(output))

if __name__ == '__main__':
    main()
```

---

## Configuration

**File:** `.claude/settings.local.json`

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/inject-mistake-context.py",
            "statusMessage": "Loading mistake prevention context..."
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/mistake-guard.py",
            "statusMessage": "Checking for known mistake patterns..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Review if Claude violated any documented mistakes: 1) Edited config without grepping ALL occurrences? 2) Made data assumptions without checking? 3) Wrote backtest from scratch? 4) Changed first grep result only? Context: $ARGUMENTS. Return {\"ok\": true} if safe, {\"ok\": false, \"reason\": \"specific violation\"} if violated.",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

---

## The Quiz System (Optional Layer 4)

For the most critical mistakes, require explicit acknowledgment.

**File:** `.claude/hooks/quiz-critical-patterns.py`

```python
#!/usr/bin/env python3
"""
UserPromptSubmit hook: If user's request involves critical patterns,
inject a quiz that Claude must answer correctly.
"""
import json
import sys
import re

CRITICAL_PATTERNS = {
    'config': {
        'triggers': ['threshold', 'config', 'parameter', 'value'],
        'quiz': (
            "QUIZ REQUIRED - Config Change Pattern:\n"
            "Q1: What must you do BEFORE changing any config value?\n"
            "A: Grep ALL occurrences and understand each one\n\n"
            "Q2: What's the source of truth for trading configs?\n"
            "A: research/reference/TRADING_CONFIGS.py\n\n"
            "Q3: After fixing, what must you do?\n"
            "A: Grep AGAIN to verify ALL are fixed\n\n"
            "Confirm you understand by stating your plan including these steps."
        )
    },
    'backtest': {
        'triggers': ['backtest', 'simulation', 'test run'],
        'quiz': (
            "QUIZ REQUIRED - Backtest Creation:\n"
            "Q1: Should you write backtest logic from scratch?\n"
            "A: NO - copy from validated files\n\n"
            "Q2: Where should config values come from?\n"
            "A: Import from TRADING_CONFIGS.py\n\n"
            "Q3: What validated file should you use as template?\n"
            "A: test_obi_comparison_oos7.py or fixed_cycling_grid_backtest.py"
        )
    }
}

def main():
    input_data = json.load(sys.stdin)
    prompt = input_data.get('prompt', '').lower()

    for pattern_name, pattern_data in CRITICAL_PATTERNS.items():
        if any(trigger in prompt for trigger in pattern_data['triggers']):
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": pattern_data['quiz']
                }
            }
            print(json.dumps(output))
            sys.exit(0)

    sys.exit(0)

if __name__ == '__main__':
    main()
```

---

## Mistake Categories and Enforcement

| Mistake Pattern | Hook Type | Enforcement |
|----------------|-----------|-------------|
| Edit config without grep ALL | PreToolUse(Edit) | Block + require grep |
| SSH with wrong key | PreToolUse(Bash) | Block + show correct path |
| Run observer.py directly | PreToolUse(Bash) | Block + redirect to run_data_collection.py |
| Kill bot with kill cmd | PreToolUse(Bash) | Block + suggest systemctl |
| Write backtest from scratch | PreToolUse(Write) | Block if no TRADING_CONFIGS import |
| Make data assumptions | Stop Agent | Review + force verification |
| Drop items from list | Stop Agent | Check item counts |
| Time estimates without data | UserPromptSubmit | Inject warning |

---

## Implementation Priority

### Phase 1: Immediate (Today)

1. Create `.claude/hooks/` directory
2. Implement `mistake-guard.py` for top 5 mistakes
3. Configure PreToolUse hooks in settings
4. Test with known mistake scenarios

### Phase 2: This Week

5. Implement SessionStart context injection
6. Add Stop hook with prompt-based review
7. Tune false positive rate

### Phase 3: Ongoing

8. Add new mistakes to the guard as they occur
9. Graduate stable checks to automated (exit 0)
10. Build metrics on blocked mistakes

---

## Why This Will Work

| Previous Approach | Problem | New Approach | Solution |
|-------------------|---------|--------------|----------|
| CLAUDE_MISTAKES.md | Passive, requires recall | PreToolUse hooks | Active interception |
| /cm skill | Must be invoked manually | SessionStart hook | Automatic injection |
| "READ THIS" headers | No enforcement | Block with exit code 2 | Technical barrier |
| Hope Claude learns | No memory across sessions | Hooks persist | Deterministic checks |
| Post-hoc correction | Damage already done | PreToolUse blocks | Preventive |

---

## Metrics to Track

1. **Blocks per session** - How often hooks prevent mistakes
2. **False positive rate** - Hooks blocking legitimate actions
3. **Repeat violations** - Same mistake after being blocked
4. **Time to resolution** - How quickly Claude fixes after block

---

## Appendix: Full Hook Configuration

```json
{
  "permissions": {
    "allow": [
      "Bash(python3:*)",
      "Bash(git:*)"
    ]
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/inject-mistake-context.py"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/check-bash-mistakes.py"
          }
        ]
      },
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/check-edit-mistakes.py"
          }
        ]
      },
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/check-write-mistakes.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/verify-edit-complete.py",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Did Claude violate documented mistakes? Check: config grep, data assumptions, backtest copying. $ARGUMENTS",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

---

## Summary

The oversight system transforms passive documentation into active enforcement:

1. **PreToolUse hooks** block known mistake patterns BEFORE they execute
2. **SessionStart hooks** inject focused context (not 800 lines, just top 5)
3. **Stop hooks** review work for violations before completion
4. **Quiz system** forces explicit acknowledgment for critical patterns

The key insight: **Claude reading documentation is not the same as Claude following it.** Technical enforcement at the tool-call level is the only reliable solution.
