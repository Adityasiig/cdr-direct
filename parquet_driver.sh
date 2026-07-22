#!/bin/bash
LOG=/var/log/parquet-backfill.log
: > "$LOG"
echo "$(date '+%F %T') [parquet] 30-day backfill start" >> "$LOG"
t0=$(date +%s)
python3 /opt/cdr-direct/build_parquet.py --days 30 >> "$LOG" 2>&1
t1=$(date +%s)
echo "$(date '+%F %T') [parquet] === DONE in $((t1-t0))s ===" >> "$LOG"
