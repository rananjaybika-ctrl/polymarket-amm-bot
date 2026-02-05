#!/usr/bin/env python3
"""
PreToolUse hook that blocks common mistake patterns from CLAUDE_MISTAKES.md.

This hook intercepts tool calls and checks for patterns known to cause problems.
Returns a deny decision with explanation when a pattern is detected.

Usage: Configure in .claude/settings.local.json under hooks.PreToolUse
"""
import json
import sys
import re


def check_config_edit_without_grep(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistakes #30, #44: Changing first grep result without checking all occurrences.

    ACTUALLY RUNS GREP and BLOCKS if multiple occurrences exist.
    """
    import subprocess

    if tool_name != 'Edit':
        return True, ""

    old_string = tool_input.get('old_string', '')
    file_path = tool_input.get('file_path', '')

    # Skip very short strings (too many false positives)
    if len(old_string.strip()) < 3:
        return True, ""

    # Detect config-like values: numbers, decimals, booleans, or config files
    config_patterns = [
        r'^[\d.]+$',           # Pure numbers like "0.90" or "72"
        r'^(true|false)$',     # Booleans
        r'^0\.\d+$',           # Decimal thresholds like "0.02"
        r'^\d+\.\d+$',         # Floats
    ]

    is_config_value = any(re.match(p, old_string.strip(), re.IGNORECASE) for p in config_patterns)
    is_config_file = any(x in file_path.lower() for x in ['config', 'settings', 'trading_configs', 'enhanced_spike', 'run_paper_bot'])

    if not (is_config_value or is_config_file):
        return True, ""

    # ACTUALLY RUN GREP to find all occurrences
    try:
        # Search for the exact string being replaced
        result = subprocess.run(
            ['grep', '-rn', '--include=*.py', old_string],
            capture_output=True, text=True, timeout=10, cwd='.'
        )

        if result.returncode == 0 and result.stdout.strip():
            matches = [line for line in result.stdout.strip().split('\n') if line]
            # Filter out archive/test files
            relevant_matches = [m for m in matches if 'archive' not in m.lower() and '__pycache__' not in m]

            if len(relevant_matches) > 1:
                # BLOCK: Multiple occurrences found
                return False, (
                    f"🚫 BLOCKED (Mistake #30, #44): Found {len(relevant_matches)} occurrences of this value!\n\n"
                    f"You CANNOT edit just ONE occurrence. ALL must be updated together.\n\n"
                    f"OCCURRENCES FOUND:\n"
                    f"{chr(10).join(relevant_matches[:10])}\n"
                    f"{'...(truncated)' if len(relevant_matches) > 10 else ''}\n\n"
                    f"ACTION REQUIRED:\n"
                    f"1. Identify which occurrences need updating (class default vs instance)\n"
                    f"2. Update ALL relevant occurrences in ONE session\n"
                    f"3. Grep AFTER to verify consistency\n\n"
                    f"To proceed: Edit all occurrences, or confirm only THIS one should change."
                )
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        pass

    return True, ""


def check_ssh_key_path(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake #39: Not checking deploy.sh for SSH config before trying default paths.
    """
    if tool_name != 'Bash':
        return True, ""

    command = tool_input.get('command', '')

    # Check if using SSH with standard key paths
    if 'ssh ' in command and ('.ssh/' in command or 'id_rsa' in command or 'id_ed25519' in command):
        return False, (
            f"MISTAKE PREVENTION (#39): You're using a standard SSH key path.\n\n"
            f"For this project, the SSH key is at: $HOME/Downloads/polymarket-key.pem\n"
            f"Server IP: 54.170.244.221\n\n"
            f"CHECK deploy.sh if you need to verify: grep -n 'AWS_KEY\\|ssh.*-i' deploy.sh\n\n"
            f"Correct command format:\n"
            f"ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 '<command>'"
        )

    return True, ""


def check_kill_vs_systemctl(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake documented in header: Use systemctl to stop services, not kill.
    """
    if tool_name != 'Bash':
        return True, ""

    command = tool_input.get('command', '')

    # Check for kill commands targeting bot processes
    if 'kill' in command.lower():
        bot_indicators = ['polymarket', 'bot', 'paper', 'trading']
        if any(indicator in command.lower() for indicator in bot_indicators):
            return False, (
                f"MISTAKE PREVENTION: Do NOT use kill to stop the bot.\n\n"
                f"The bot runs as a systemd service. Use:\n"
                f"ssh -i ~/Downloads/polymarket-key.pem ubuntu@54.170.244.221 'sudo systemctl stop polymarket-bot'\n\n"
                f"Using kill will cause systemd to auto-restart the service."
            )

    return True, ""


def check_observer_direct_run(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake #25: Running observer.py directly instead of run_data_collection.py.
    """
    if tool_name != 'Bash':
        return True, ""

    command = tool_input.get('command', '')

    # Check for direct observer.py execution
    if 'observer.py' in command:
        if 'run_data_collection' not in command:
            return False, (
                f"MISTAKE PREVENTION (#25): DO NOT run observer.py directly!\n\n"
                f"This only captures 5Hz binance_price. For proper data collection you need:\n"
                f"  python3 scripts/run_data_collection.py --hours N\n\n"
                f"This wrapper runs BOTH the observer AND the 60Hz Binance logger.\n"
                f"Running observer alone will result in missing data for spike detection backtests."
            )

    return True, ""


def check_backtest_imports(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake #33: Writing backtest logic from scratch instead of copying from validated files.
    """
    if tool_name != 'Write':
        return True, ""

    file_path = tool_input.get('file_path', '')
    content = tool_input.get('content', '')

    # Only check backtest files
    if 'backtest' not in file_path.lower():
        return True, ""

    # Check for simulation logic without proper imports
    simulation_indicators = ['spike_threshold', 'lookback_ticks', 'time_stop', 'z_score', 'entry_price']
    has_simulation_logic = any(ind in content for ind in simulation_indicators)

    # Check for proper imports
    has_trading_config_import = 'from research.reference.TRADING_CONFIGS import' in content
    has_config_import = 'TRADING_CONFIGS' in content or 'AGGRESSIVE_CONFIG' in content

    if has_simulation_logic and not has_config_import:
        return False, (
            f"MISTAKE PREVENTION (#33): New backtest scripts MUST import from TRADING_CONFIGS.py.\n\n"
            f"DO NOT write simulation logic from scratch. Instead:\n"
            f"1. Copy simulation logic from test_obi_comparison_oos7.py or fixed_cycling_grid_backtest.py\n"
            f"2. Add: from research.reference.TRADING_CONFIGS import AGGRESSIVE_CONFIG\n"
            f"3. Use config values, don't hardcode them\n\n"
            f"This prevents backtest/live config drift which has caused significant losses."
        )

    return True, ""


def check_process_status_first(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake #28: Giving instructions without checking if process is already running.
    """
    if tool_name != 'Bash':
        return True, ""

    command = tool_input.get('command', '')

    # Check for starting long-running processes
    start_indicators = ['nohup', '&', 'python3 scripts/run', 'python scripts/run']

    if any(ind in command for ind in start_indicators):
        # Check if it's a data collection or bot start
        if 'data_collection' in command or 'paper_bot' in command or 'live_bot' in command:
            return False, (
                f"MISTAKE PREVENTION (#28): Before starting this process, CHECK if it's already running.\n\n"
                f"Run first: ps aux | grep -E 'data_collection|paper_bot|observer' | grep -v grep\n\n"
                f"Also check for existing output files to avoid overwriting data."
            )

    return True, ""


def check_time_estimates(tool_input: dict, tool_name: str) -> tuple[bool, str]:
    """
    Mistake #4: Wrong time estimates without real progress data.

    This is tricky to catch automatically, but we can flag risky patterns.
    """
    # This would need conversation context, which hooks don't have access to easily
    # Skip for now - better handled by Stop hook
    return True, ""


def main():
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)  # Allow if input is invalid

    tool_name = input_data.get('tool_name', '')
    tool_input = input_data.get('tool_input', {})

    # Run all checks
    checks = [
        check_config_edit_without_grep,
        check_ssh_key_path,
        check_kill_vs_systemctl,
        check_observer_direct_run,
        check_backtest_imports,
        check_process_status_first,
    ]

    for check_fn in checks:
        passed, reason = check_fn(tool_input, tool_name)
        if not passed:
            # Output JSON to block with reason
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason
                }
            }
            print(json.dumps(output))
            sys.exit(0)

    # All checks passed - allow the tool call
    sys.exit(0)


if __name__ == '__main__':
    main()
