#!/bin/bash
# Runs G7, G1, G4, G3, G2, G5 sequentially, unattended, overnight.
#
# ORDER RATIONALE:
#   G7 first  - potential correctness bug (applicants possibly
#               scheduled on the WRONG DAY, not just a display
#               issue) - fix this before anything else touches
#               scheduling logic.
#   G1        - smarter panel distribution (builds on current
#               panel structure).
#   G4        - manual add/delete panel (depends on G1's result).
#   G3        - drag panel between rooms (depends on G4's manual-
#               cap-removal rule).
#   G2        - lock/unlock UI (independent).
#   G5        - restore preference column (independent, but placed
#               last; note G7 may already fix part of what G5
#               covers, since they touch overlapping ground - G5
#               still worth running in case its cause is different,
#               e.g. a frontend rendering regression vs G7's
#               parsing bug).
#
# Each `claude -p` call is a fresh, isolated context - no manual
# /clear needed. Push IS enabled this run - each prompt ends in
# "commit and push", so this script does NOT disable the git
# remote like earlier overnight runs did.
#
# Supports RESUME: if usage limit is hit mid-run, just re-run this
# script later - it automatically retries every 30 min for up to 6
# hours per session (rolling usage window), and skips sessions that
# already completed via marker files in .overnight_state/.
#
# DIRECTORY LAYOUT (put this at your Meridian project root):
#   meridian/
#     CLAUDE.md
#     g_run_overnight.sh          <- this file
#     g_overnight_prompts/
#       g7_day_preference_bug.txt
#       g1_panel_distribution.txt
#       g4_manual_add_delete_panel.txt
#       g3_drag_panel_between_rooms.txt
#       g2_lock_unlock_ui.txt
#       g5_restore_preference_column.txt
#     .overnight_state/           <- auto-created, tracks progress, gitignored
#
# USAGE:
#   chmod +x g_run_overnight.sh
#   caffeinate -i ./g_run_overnight.sh   (macOS, keeps machine awake)
#   ./g_run_overnight.sh                  (if not on macOS)
#
# If it stops partway (e.g. usage limit exhausted after 6h of
# retries), just run it again once your limit resets - completed
# sessions are skipped automatically.

set -uo pipefail

PROMPT_DIR="./g_overnight_prompts"
STATE_DIR="./.overnight_state"
LOG_FILE="g_overnight_log_$(date +%Y%m%d_%H%M%S).txt"

mkdir -p "$STATE_DIR"

SESSIONS=(
  "g7_day_preference_bug:G7 - Fix day preference conflation (17 vs 18) bug"
  "g1_panel_distribution:G1 - Smarter panel distribution across rooms"
  "g4_manual_add_delete_panel:G4 - Manual add/delete panel per room"
  "g3_drag_panel_between_rooms:G3 - Drag panel between rooms"
  "g2_lock_unlock_ui:G2 - Lock/unlock UI for candidates"
  "g5_restore_preference_column:G5 - Restore applicant preference column"
)

echo "=== Overnight run (G7, G1-G5) started at $(date) ===" | tee -a "$LOG_FILE"
echo "NOTE: push is ENABLED for this run - each session will push to main on success." | tee -a "$LOG_FILE"

for entry in "${SESSIONS[@]}"; do
  key="${entry%%:*}"
  label="${entry#*:}"
  prompt_file="$PROMPT_DIR/${key}.txt"
  marker_file="$STATE_DIR/${key}.done"

  echo "" | tee -a "$LOG_FILE"
  echo "----------------------------------------" | tee -a "$LOG_FILE"

  if [ -f "$marker_file" ]; then
    echo ">>> SKIPPING (already completed): $label" | tee -a "$LOG_FILE"
    echo ">>> Delete $marker_file to force a re-run of this session." | tee -a "$LOG_FILE"
    continue
  fi

  echo ">>> Starting: $label" | tee -a "$LOG_FILE"
  echo ">>> Time: $(date)" | tee -a "$LOG_FILE"
  echo "----------------------------------------" | tee -a "$LOG_FILE"

  if [ ! -f "$prompt_file" ]; then
    echo "!!! ERROR: prompt file not found at $prompt_file - skipping" | tee -a "$LOG_FILE"
    continue
  fi

  RETRY_WAIT_SECONDS=1800   # 30 min between retries
  MAX_RETRIES=12            # up to 6 hours of retrying per session
  attempt=0
  SESSION_OK=false

  while [ "$attempt" -lt "$MAX_RETRIES" ]; do
    attempt=$((attempt + 1))
    echo ">>> Attempt $attempt/$MAX_RETRIES at $(date)" | tee -a "$LOG_FILE"

    claude -p "$(cat "$prompt_file")" --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE"
    CLAUDE_EXIT=${PIPESTATUS[0]}

    if [ "$CLAUDE_EXIT" -eq 0 ]; then
      touch "$marker_file"
      echo ">>> Finished OK: $label at $(date)" | tee -a "$LOG_FILE"
      SESSION_OK=true
      break
    else
      echo "!!! Session exited with code $CLAUDE_EXIT (possibly usage limit hit): $label" | tee -a "$LOG_FILE"
      if [ "$attempt" -lt "$MAX_RETRIES" ]; then
        echo "!!! Waiting ${RETRY_WAIT_SECONDS}s (30 min) before retrying automatically..." | tee -a "$LOG_FILE"
        sleep "$RETRY_WAIT_SECONDS"
      fi
    fi
  done

  if [ "$SESSION_OK" != "true" ]; then
    echo "!!! Gave up on '$label' after $MAX_RETRIES attempts (~6 hours)." | tee -a "$LOG_FILE"
    echo "!!! NOT marking as done. Stopping so remaining sessions aren't attempted while limited." | tee -a "$LOG_FILE"
    break
  fi
done

echo "" | tee -a "$LOG_FILE"
echo "=== Overnight run (G7, G1-G5) finished at $(date) ===" | tee -a "$LOG_FILE"
echo "Review $LOG_FILE for full details of each session." | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "Push was ENABLED - check git log --oneline -10 to see what landed on main." | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"
echo "If usage limit was hit, just re-run this script after your limit resets -" | tee -a "$LOG_FILE"
echo "completed sessions in .overnight_state/ will be skipped automatically." | tee -a "$LOG_FILE"
