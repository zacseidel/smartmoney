#!/bin/bash
#
# Weekly refresh, driven by the launchd agent in
# ~/Library/LaunchAgents/com.zacseidel.smartmoney.weekly.plist
# (source of truth: scripts/com.zacseidel.smartmoney.weekly.plist).
#
# The agent fires Wednesday at 02:00 local time and again at 07:00 / 12:00 / 18:00 as
# catch-ups. This script decides whether a given firing should actually do the work:
#
#   * on battery  -> skip (the run takes ~an hour of network + CPU); a later firing
#                    that day will pick it up once the laptop is plugged in.
#   * already ran -> skip. A stamp file records the ISO week of the last successful
#                    run, so the catch-up firings are no-ops after the first success.
#
# Everything is logged to ~/Library/Logs/smartmoney/. Run by hand any time with:
#   scripts/weekly_update.sh            # same guards as the scheduled run
#   scripts/weekly_update.sh --force    # ignore the battery + already-ran guards
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/smartmoney"
STAMP="$LOG_DIR/last-success-week"
PYTHON="/Users/zacseidel/opt/anaconda3/bin/python3"

# launchd starts jobs with a bare PATH; git/gh live in /usr/local/bin and the push at the
# end of the run shells out to both.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/update-$(date +%Y-%m-%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

say() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*"; }

say "=== smartmoney weekly update ==="
say "repo=$REPO log=$LOG"

if [[ $FORCE -eq 0 ]]; then
  if ! pmset -g ps | grep -q "AC Power"; then
    say "SKIP: on battery power — waiting for a later firing with the charger connected."
    exit 0
  fi
  # %V is the ISO week number; paired with the ISO year it is unique per week.
  week="$(date +%G-W%V)"
  if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$week" ]]; then
    say "SKIP: already completed a run this week ($week)."
    exit 0
  fi
fi

say "On AC power, no run yet this week — starting update.py"
cd "$REPO" || exit 1
"$PYTHON" src/update.py
status=$?

if [[ $status -eq 0 ]]; then
  date +%G-W%V > "$STAMP"
  say "DONE: update.py succeeded."
else
  say "FAILED: update.py exited $status — a later firing this week will retry."
fi

# Keep a quarter's worth of logs, discard the rest.
find "$LOG_DIR" -name 'update-*.log' -mtime +90 -delete 2>/dev/null

exit $status
