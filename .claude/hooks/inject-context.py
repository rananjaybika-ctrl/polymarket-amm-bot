#!/usr/bin/env python3
"""
SessionStart hook: Inject focused mistake prevention context.

Instead of loading the entire 800-line CLAUDE_MISTAKES.md (which gets skimmed),
this extracts the TOP 5 most critical/recent mistakes and presents them as
an actionable checklist.

The key insight: quantity of documentation != quality of adherence.
Focused, contextual reminders are more effective.
"""
import json
import sys
import re
from pathlib import Path


def parse_mistakes(content: str) -> list[dict]:
    """
    Extract structured mistake data from CLAUDE_MISTAKES.md.

    Looks for patterns like:
    ### 44. CHANGED FIRST GREP RESULT...
    **What happened:** ...
    """
    mistakes = []

    # Pattern to match mistake headers and their descriptions
    # Handles both "### N. TITLE" and "### TITLE" formats
    sections = re.split(r'\n### ', content)

    for section in sections[1:]:  # Skip content before first ###
        lines = section.split('\n')
        if not lines:
            continue

        # Extract number and title from first line
        first_line = lines[0]
        match = re.match(r'(\d+)\.\s*(.+)', first_line)
        if match:
            number = int(match.group(1))
            title = match.group(2).strip()
        else:
            # No number, might be a header without number
            continue

        # Extract "What happened" content
        what_happened = ""
        for line in lines[1:]:
            if line.startswith('**What happened:**'):
                what_happened = line.replace('**What happened:**', '').strip()
                break

        # Extract fix/prevention content
        fix = ""
        for i, line in enumerate(lines[1:]):
            if line.startswith('**FIX') or line.startswith('**PREVENTION'):
                # Get the next few lines as the fix
                fix_lines = []
                for j in range(i + 1, min(i + 4, len(lines) - 1)):
                    fix_line = lines[1:][j]
                    if fix_line.startswith('**') or fix_line.startswith('###'):
                        break
                    if fix_line.strip():
                        fix_lines.append(fix_line.strip())
                fix = ' '.join(fix_lines)
                break

        mistakes.append({
            'number': number,
            'title': title[:60],  # Truncate long titles
            'what': what_happened[:150] if what_happened else "See CLAUDE_MISTAKES.md",
            'fix': fix[:100] if fix else ""
        })

    return mistakes


def get_priority_mistakes(mistakes: list) -> list:
    """
    Select the most important mistakes to highlight.

    Priority based on:
    1. Recency (higher numbers = more recent)
    2. Known repeat offenders (30, 44 - the grep issue)
    3. High-impact categories (config, data, backtest)
    """
    # Known critical mistake numbers
    critical_numbers = {30, 33, 39, 44, 25, 28, 35}

    # Separate critical and other mistakes
    critical = [m for m in mistakes if m['number'] in critical_numbers]
    others = [m for m in mistakes if m['number'] not in critical_numbers]

    # Sort others by recency
    others.sort(key=lambda x: x['number'], reverse=True)

    # Take all critical + top 2 recent
    result = critical + others[:2]

    # Sort final list by number for display
    result.sort(key=lambda x: x['number'], reverse=True)

    return result[:7]  # Max 7 to keep it focused


def build_context(mistakes: list) -> str:
    """Build the context string to inject."""
    lines = [
        "=" * 60,
        "ACTIVE MISTAKE PREVENTION SYSTEM",
        "=" * 60,
        "",
        "CRITICAL MISTAKES TO AVOID (from CLAUDE_MISTAKES.md):",
        ""
    ]

    for m in mistakes:
        lines.append(f"#{m['number']}: {m['title']}")
        if m['what']:
            lines.append(f"    Problem: {m['what'][:80]}...")
        lines.append("")

    lines.extend([
        "-" * 60,
        "MANDATORY CHECKLIST - Verify BEFORE acting:",
        "-" * 60,
        "",
        "[ ] CONFIG CHANGE? -> grep ALL occurrences first, not just the first match",
        "[ ] BACKTEST SCRIPT? -> import from TRADING_CONFIGS.py, copy from validated files",
        "[ ] SSH COMMAND? -> use ~/Downloads/polymarket-key.pem (check deploy.sh)",
        "[ ] DATA ASSUMPTION? -> check file contents/columns before declaring unavailable",
        "[ ] EDITING A LIST? -> count items before and after, verify no accidental deletions",
        "[ ] LONG PROCESS? -> check if already running with ps aux | grep",
        "[ ] KILLING BOT? -> use systemctl stop, not kill",
        "",
        "If you're about to do any of these, STOP and verify first.",
        "=" * 60,
    ])

    return "\n".join(lines)


def main():
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    cwd = input_data.get('cwd', '.')

    # Try to find CLAUDE_MISTAKES.md
    mistakes_path = Path(cwd) / 'CLAUDE_MISTAKES.md'

    if not mistakes_path.exists():
        # Try parent directories
        for parent in Path(cwd).parents:
            candidate = parent / 'CLAUDE_MISTAKES.md'
            if candidate.exists():
                mistakes_path = candidate
                break

    if not mistakes_path.exists():
        sys.exit(0)

    try:
        content = mistakes_path.read_text()
    except Exception:
        sys.exit(0)

    mistakes = parse_mistakes(content)
    if not mistakes:
        sys.exit(0)

    priority = get_priority_mistakes(mistakes)
    context = build_context(priority)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context
        }
    }

    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
