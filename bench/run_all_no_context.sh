#!/bin/bash
# Run no-context on ALL Lite tasks. Per-task: build instance image -> bench run
# (agent) -> patch auto-collected into predictions/ -> rmi instance image to
# bound disk. Build/run failures are logged and skipped. Sequential.
set -uo pipefail
cd /home/neverland/workspace/SWEContextBench
# shellcheck disable=SC1091
source bench/.venv/bin/activate 2>/dev/null || true
export SWECB_ROOT=/home/neverland/workspace/SWEContextBench

LOG=/tmp/run_all_no_context.log
: > "$LOG"
SUMMARY=/tmp/run_all_summary.csv
echo "instance_id,build,patch" > "$SUMMARY"

mapfile -t IDS < <(python3 -c "from bench.tasks import load_tasks; [print(t.instance_id) for t in load_tasks('lite')]")
TOTAL=${#IDS[@]}
i=0
for iid in "${IDS[@]}"; do
  i=$((i+1))
  echo "=== [$i/$TOTAL] $iid ===" | tee -a "$LOG"
  IMG="sweb.simple.$(python3 -c "import sys;print(sys.argv[1].replace('__','.').lower())" "$iid"):latest"
  bstat="skipped"; pstat="no"

  # 1) build instance image if missing
  if ! docker images -q "$IMG" 2>/dev/null | grep -q .; then
    echo "  building $IMG ..." | tee -a "$LOG"
    if timeout 1800 python3 -m swebench_memory.harness.build_instance \
        --dataset_name "cases/SWEContextBench Lite/$iid.json" >>"$LOG" 2>&1; then
      bstat="built"
    else
      bstat="FAILED"; echo "  BUILD FAILED $iid, skipping" | tee -a "$LOG"
      echo "$iid,$bstat,$pstat" >> "$SUMMARY"; continue
    fi
  else
    bstat="cached"
  fi

  # 2) run agent (no-context) -> patch into predictions/
  if timeout 2400 bench run --setting no-context --tasks "$iid" --limit 1 >>"$LOG" 2>&1; then
    if [ -f "predictions/${iid}_preds.json" ]; then
      pstat="yes"; echo "  ✓ patch collected $iid" | tee -a "$LOG"
    else
      pstat="empty"; echo "  ! no patch $iid" | tee -a "$LOG"
    fi
  else
    pstat="RUN_FAILED"; echo "  RUN FAILED $iid" | tee -a "$LOG"
  fi

  # 3) free disk: remove this instance image (base shared; eval builds its own)
  docker rmi "$IMG" >/dev/null 2>&1 || true

  echo "$iid,$bstat,$pstat" >> "$SUMMARY"
done

echo "=== ALL DONE ===" | tee -a "$LOG"
echo "summary -> $SUMMARY"; cat "$SUMMARY"
