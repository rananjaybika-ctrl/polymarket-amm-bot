#!/usr/bin/env python3
"""
UserPromptSubmit hook: Validate trading formulas in Claude's responses.

When Claude is about to send a message containing trading formulas,
this hook checks if the formula appears to be paraphrased vs quoted from code.

Mistake #54: Writing wrong formula in summary while code is correct.
- Wrote "bid at best_bid + 3c offset" when code was "expensive_ask - offset"
- Mixed up variable names and direction

DETECTION:
- Look for formula patterns: "bid at", "entry at", "exit when", "= X + Y", "= X - Y"
- If found without code block or line reference, flag for verification

This hook runs on Stop (before response is finalized) to catch formula errors.
"""
import json
import sys
import re


def extract_trading_formulas(text: str) -> list[str]:
    """
    Find potential trading formula descriptions in text.

    Returns list of suspicious phrases that might be paraphrased formulas.
    """
    patterns = [
        # Entry/exit descriptions
        r'(?:bid|entry|buy|sell|exit)\s+(?:at|when|if)\s+[^.]+(?:[\+\-\*\/][^.]+)?',
        # Formula-like patterns
        r'(?:price|ask|bid|entry|exit|offset|cost)\s*[\+\-\*\/=]\s*\d+',
        # Variable combinations with operators
        r'(?:best_bid|best_ask|expensive_ask|expensive_bid|entry_bid|entry_ask)\s*[\+\-]\s*(?:\d+|offset|cents?)',
    ]

    formulas = []
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        formulas.extend(matches)

    return formulas


def has_code_reference(text: str, formula: str) -> bool:
    """
    Check if the formula is backed by a code reference.

    Good patterns (should pass):
    - "Line 418: entry_bid = expensive_ask - offset"
    - "```python\nentry_bid = expensive_ask - offset\n```"
    - "The code shows: entry_bid = expensive_ask - offset"

    Bad patterns (should flag):
    - "bid at best_bid + 3c offset" (no code reference)
    """
    # Check for code blocks containing similar variables
    code_block_pattern = r'```(?:python)?\s*\n[^`]*```'
    code_blocks = re.findall(code_block_pattern, text, re.DOTALL)

    # Check for line number references
    line_ref_pattern = r'[Ll]ine\s+\d+|:\d+\)'
    has_line_ref = bool(re.search(line_ref_pattern, text))

    # Check for "The code shows/is" patterns
    code_ref_pattern = r'(?:code|actual|implementation)\s+(?:shows?|is|uses?|has)'
    has_code_ref = bool(re.search(code_ref_pattern, text, re.IGNORECASE))

    # If there's a code block, line reference, or explicit code reference, pass
    if code_blocks or has_line_ref or has_code_ref:
        return True

    return False


def check_formula_accuracy(text: str) -> tuple[bool, str]:
    """
    Main validation function.

    Returns:
        (passed, reason) - passed=True if OK, False if needs verification
    """
    formulas = extract_trading_formulas(text)

    if not formulas:
        return True, ""

    # Check each formula for code backing
    unverified = []
    for formula in formulas:
        if not has_code_reference(text, formula):
            unverified.append(formula)

    if unverified:
        return False, (
            f"⚠️ FORMULA VERIFICATION NEEDED (Mistake #54 Prevention)\n\n"
            f"Found trading formulas without code references:\n"
            f"{chr(10).join('  - ' + f for f in unverified[:5])}\n\n"
            f"BEFORE SENDING, verify:\n"
            f"1. Is this EXACTLY what the code says? (copy-paste, don't paraphrase)\n"
            f"2. Are variable names correct? (best_bid vs expensive_ask)\n"
            f"3. Is the operator direction correct? (+ vs -)\n\n"
            f"RECOMMENDED: Include the actual code snippet with line number."
        )

    return True, ""


def main():
    """
    Hook entry point.

    For Stop hook: receives conversation context in $ARGUMENTS
    For UserPromptSubmit: receives the message being submitted
    """
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        # If no input, check environment variable
        import os
        text = os.environ.get('ARGUMENTS', '')
        if not text:
            sys.exit(0)
        input_data = {'text': text}

    # Get the text to check
    # For Stop hook, the prompt includes conversation context
    # For now, we rely on the Stop hook prompt to do semantic checking
    # This script provides the pattern matching

    text = input_data.get('text', '') or input_data.get('content', '') or str(input_data)

    passed, reason = check_formula_accuracy(text)

    if not passed:
        output = {
            "hookSpecificOutput": {
                "additionalContext": reason
            }
        }
        print(json.dumps(output))

    sys.exit(0)


if __name__ == '__main__':
    main()
