# Generate Daily Report

Generate a daily P&L report with optional Telegram notification.

## Arguments

- `$ARGUMENTS` - Optional flags: `--date YYYY-MM-DD`, `--telegram`, `--save`

## Instructions

Run the daily report generator:

```bash
python scripts/daily_report.py $ARGUMENTS
```

### Options

- No arguments: Generate report for today
- `--date 2026-01-09`: Specific date
- `--telegram`: Send summary to Telegram
- `--save`: Save full report to `reports/` directory
- `--quiet`: Minimal output

### Example Usage

- `/daily-report` - Quick summary for today
- `/daily-report --date 2026-01-09 --save` - Generate and save specific date report
- `/daily-report --telegram` - Send today's summary to Telegram

### Output

The report includes:
1. **Telegram Message** (concise, Markdown-formatted)
   - Total P&L
   - Per-strategy breakdown
   - Key metrics (best Sharpe)
   - Warnings if any

2. **Full Report** (when using `--save`)
   - Detailed strategy breakdown
   - All metrics
   - Recommendations
