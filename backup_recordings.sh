#!/bin/bash
# Back up recordings/ — the only copy of the data every result rests on.
#
# recordings/ is gitignored (15MB/day would wreck the repo), so the code that
# analyses the data is on GitHub and the data itself is on one laptop. Some of
# it cannot be re-fetched at any price:
#
#   walls     Deribit publishes no historical open interest
#   universe  Kalshi has no historical book API — a lost day is lost forever
#
# 21% of the record is already missing to macOS sleep. That is what losing
# data looks like; this script exists so it doesn't happen to the rest.
#
#   ./backup_recordings.sh              # incremental sync
#   ./backup_recordings.sh --verify     # sync, then checksum every file
#   DEST=/Volumes/X ./backup_recordings.sh   # somewhere else
#
# Incremental and idempotent: rsync only sends files whose size or mtime
# changed, so re-running costs seconds. Nothing is ever deleted at the
# destination — no --delete. If a local file vanishes, the backup keeps it.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recordings"
DEST="${DEST:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/kalshiArb-recordings}"

[ -d "$SRC" ] || { echo "no recordings/ at $SRC"; exit 1; }
mkdir -p "$DEST"

echo "  src   $SRC"
echo "  dest  $DEST"
echo "  size  $(du -sh "$SRC" | cut -f1) across $(ls "$SRC" | wc -l | tr -d ' ') files"
echo

# -a archive, -h human sizes, --partial so a killed run resumes rather than
# restarting, no --delete so the backup is append-only.
# --stats not --info=stats2: macOS ships rsync 2.6.9 (2006), which predates
# --info entirely. Keep to flags the stock binary knows.
rsync -ah --partial --stats "$SRC/" "$DEST/" | tail -6

echo
if [ "${1:-}" = "--verify" ]; then
    echo "  verifying by checksum (slower, reads every byte)..."
    # -c compares checksums, not size+mtime. -n dry run. Any output is a
    # file rsync would re-send, i.e. a real mismatch.
    out=$(rsync -acn --out-format='%n' "$SRC/" "$DEST/")
    if [ -z "$out" ]; then
        echo "  OK — all $(ls "$SRC" | wc -l | tr -d ' ') files match by checksum"
    else
        echo "  MISMATCH:"; echo "$out"; exit 1
    fi
else
    s=$(ls "$SRC" | wc -l | tr -d ' '); d=$(ls "$DEST" | wc -l | tr -d ' ')
    echo "  $s local files, $d in backup"
    [ "$d" -ge "$s" ] || { echo "  WARNING: backup has fewer files"; exit 1; }
    echo "  OK  (run with --verify to checksum every file)"
fi

echo "  done $(date '+%Y-%m-%d %H:%M')"
