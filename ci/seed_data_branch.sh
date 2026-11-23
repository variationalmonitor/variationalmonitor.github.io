#!/bin/bash
# Build the first commit of the orphan `data` branch from the local store. It does not push, and it does not
# touch the working tree, the index or the current branch: the commit is made from a temporary folder.
#
#   ci/seed_data_branch.sh               assemble, verify with an offline build, create the local branch `data`
#   ci/seed_data_branch.sh --dry-run     assemble and verify only
#   ci/seed_data_branch.sh --replace     overwrite an existing local branch `data` (never the remote one)
#   ci/seed_data_branch.sh --keep        leave the assembled folder in place and print where it is
#
# Then, at cutover:   git push origin data
#
# Layout of the branch (its root is the store, VM_STORE):
#   snapshots/         raw Omni stats snapshots from the capture loop, kept 14 days      (capture writes)
#   snapshots_seed/    the hand-saved snapshots of 2, 4 and 11 Nov 2026 and five converted Internet Archive
#                      captures (22 Jan, 13, 17 and 25 Sep, 9 Oct 2026; originals in wayback/), kept for good
#   exec/              matched Omni and Hyperliquid samples, one file per UTC day        (capture writes)
#   capture/           one log file per UTC day, one line per tick                       (capture writes)
#   days/  platform.jsonl  listings_seen.json  reports.json                              (publish writes)
#   coingecko/ defillama/ docs/ polymarket/ telegram/        source caches               (publish writes)
#                      docs/ and telegram/ are Variational's own text (its docs, its Telegram and X posts), which
#                      the reports and token builders parse. A few of those posts and one docs link name a rival
#                      analytics firm; the owner keeps them verbatim (23 Nov 2026), so these two raw caches are
#                      the only exemption from the rival-name scan below.
#   onchain/aggregates/   the indexer's daily aggregates                                 (publish, daily)
#   onchain/state/        compact indexer state, about 12 MB                             (publish, weekly)
#   housekeeping.json     dates of the last history squash and the last on-chain state save
#
# Sources can be overridden: VM_STORE, SEED_SNAPSHOTS, SEED_SNAPSHOTS_SEED, SEED_ONCHAIN_STATE, VM_ONCHAIN_DATA,
# and SEED_GIT_DIR (the repository that receives the branch; default: this checkout).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-/usr/bin/python3}"; command -v "$PY" >/dev/null 2>&1 || PY=python3
STORE="${VM_STORE:-$ROOT/.cache/store}"
SNAP="${SEED_SNAPSHOTS:-$ROOT/.cache/snapshots}"
SNAP_SEED="${SEED_SNAPSHOTS_SEED:-$ROOT/.cache/snapshots_seed}"
OC_STATE="${SEED_ONCHAIN_STATE:-$ROOT/.cache/onchain/state}"
OC_DATA="${VM_ONCHAIN_DATA:-$ROOT/data/onchain}"
GIT_REPO="${SEED_GIT_DIR:-$ROOT/.git}"
BRANCH=data
DRY=0; REPLACE=0; KEEP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;; --replace) REPLACE=1 ;; --keep) KEEP=1 ;;
    *) echo "unknown option $arg"; exit 2 ;;
  esac
done

[ -d "$STORE" ] || { echo "no store at $STORE"; exit 1; }
# The owner's rule: a rival analytics firm's name appears nowhere on the public branch and its data is never
# used. The name is kept base64-encoded below so that this script does not carry it either. Every file of the
# assembled branch is searched, gzip files decompressed, file paths included; one hit refuses the seed. The one
# exemption: files under the top-level docs/ and telegram/ folders, Variational's own text kept verbatim.
scan_rival() {  # <folder>
  "$PY" - "$1" <<'PYEOF'
import base64, gzip, os, sys
name = base64.b64decode("ZW50cm9weQ==").lower()
EXEMPT = ("docs", "telegram")          # raw source caches of Variational's own docs and posts
root, hits, exempt, searched = sys.argv[1], [], [], 0
for dp, dn, fn in os.walk(root):
    dn[:] = [d for d in dn if d != ".git"]
    for f in fn:
        p = os.path.join(dp, f)
        rel = os.path.relpath(p, root)
        searched += 1
        with open(p, "rb") as fh:
            raw = fh.read()
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        if name in rel.lower().encode() or name in raw.lower():
            (exempt if rel.split(os.sep)[0] in EXEMPT and os.sep in rel else hits).append(rel)
print("  rival name scan: %d files searched, %d hit%s; %d in the exempt source caches (%s/)%s" % (
      searched, len(hits), "" if not hits else ": " + ", ".join(hits[:20]), len(exempt), "/, ".join(EXEMPT),
      "" if not exempt else ": " + ", ".join(exempt[:20])))
sys.exit(1 if hits else 0)
PYEOF
}
if [ "$DRY" = 0 ] && git --git-dir="$GIT_REPO" show-ref --verify --quiet "refs/heads/$BRANCH" && [ "$REPLACE" = 0 ]; then
  echo "a local branch '$BRANCH' already exists; use --replace to rebuild it"; exit 1
fi
if git --git-dir="$GIT_REPO" show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  echo "WARNING: origin/$BRANCH already exists. Seeding again and pushing would need a force push and would drop what CI has written since."
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/vm-seed.XXXXXX")"
TREE="$WORK/tree"
cleanup() { [ "$KEEP" = 1 ] && echo "assembled folder kept at $TREE" || rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$TREE"

echo "== assembling from $STORE"
rsync -a --exclude '*.tmp' --exclude '.DS_Store' --exclude 'telegram/page_latest.html' "$STORE"/ "$TREE"/
mkdir -p "$TREE/snapshots" "$TREE/snapshots_seed" "$TREE/capture" "$TREE/onchain/aggregates"
# raw snapshots: every file must be a complete, valid stats response
check_and_copy() {  # <source folder> <target folder>
  local n=0 bad=0
  for f in "$1"/*.json.gz; do
    [ -e "$f" ] || continue
    if "$PY" - "$f" "$ROOT" <<'PYEOF'
import gzip, sys
sys.path.insert(0, sys.argv[2] + "/ci")
import capture
with gzip.open(sys.argv[1], "rb") as fh:
    capture.validate(fh.read())
PYEOF
    then cp -p "$f" "$2"/; n=$((n + 1)); else echo "  SKIPPED invalid snapshot $f"; bad=$((bad + 1)); fi
  done
  echo "  $(basename "$2"): $n snapshots copied, $bad invalid"
  if [ "$bad" != 0 ]; then echo "invalid snapshot files in $1: move them aside and run again"; exit 1; fi
}
check_and_copy "$SNAP_SEED" "$TREE/snapshots_seed"
check_and_copy "$SNAP" "$TREE/snapshots"
# the interim Mac loop's own logs, for the record
[ -f "$SNAP/log.txt" ] && cp -p "$SNAP/log.txt" "$TREE/capture/mac-loop.log"
[ -f "$SNAP/hl_log.txt" ] && cp -p "$SNAP/hl_log.txt" "$TREE/capture/mac-loop-hl.log"

# on-chain: daily aggregates always; the compact state when the local indexer has exported it
TODAY="$(date -u +%Y-%m-%d)"
OC_SAVED=null
if ls "$OC_DATA"/*.json >/dev/null 2>&1; then cp -p "$OC_DATA"/*.json "$TREE/onchain/aggregates/"; fi
OC_DAY="$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['coverage']['last_complete_day'])" "$TREE/onchain/aggregates/daily.json" 2>/dev/null || echo unknown)"
if [ -f "$OC_STATE/registry.bin.gz" ] && [ -f "$OC_STATE/agg_state.json.gz" ] && [ -f "$OC_STATE/anchors.json" ]; then
  mkdir -p "$TREE/onchain/state"
  cp -p "$OC_STATE/registry.bin.gz" "$OC_STATE/agg_state.json.gz" "$OC_STATE/anchors.json" "$TREE/onchain/state/"
  printf '{\n "last_complete_day": "%s",\n "saved_at": "%sT00:00:00Z",\n "seeded": true\n}\n' "$OC_DAY" "$TODAY" > "$TREE/onchain/state/STATE_INFO.json"
  OC_SAVED="\"$TODAY\""
  echo "  on-chain state: included ($(du -sh "$TREE/onchain/state" | cut -f1)), aggregates complete through $OC_DAY"
else
  echo "  ON-CHAIN STATE MISSING at $OC_STATE (the local indexer has not exported it yet)."
  echo "  Without it CI cannot advance the on-chain files and the gate will hold the site once they are 26 hours old."
  echo "  Run this script again after '/usr/bin/python3 pipeline/onchain/run.py local' has finished."
fi

cat > "$TREE/housekeeping.json" <<EOF
{
 "last_squash": "$TODAY",
 "onchain_state_saved": $OC_SAVED,
 "seeded_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
cat > "$TREE/.gitignore" <<'EOF'
*.tmp
*.next
.DS_Store
telegram/page_latest.html
onchain/state/ci_window.sqlite*
EOF
cat > "$TREE/README.md" <<'EOF'
# Variational Monitor: data branch

The pipeline's store. Written only by GitHub Actions (`capture.yml` every 30 minutes, `publish.yml` every hour);
do not edit by hand. Code lives on `main`; this branch shares no history with it.

- `snapshots/` raw Omni stats API responses, one per half hour, kept 14 days (the API has no history)
- `snapshots_seed/` snapshots saved by hand on 2, 4 and 11 Nov 2026, and five Internet Archive captures (22 Jan, 13, 17 and 25 Sep, 9 Oct 2026) converted to the same form; the archived originals are in `wayback/`
- `exec/` matched Omni and Hyperliquid samples per UTC day; `capture/` one log line per tick
- `days/` sealed UTC days, `platform.jsonl` one line per snapshot, `reports.json`
- `coingecko/`, `defillama/`, `docs/`, `polymarket/`, `telegram/` source caches
- `onchain/aggregates/` daily on-chain aggregates; `onchain/state/` compact indexer state, rewritten weekly

History is squashed to one commit about once a week, so old snapshots do not accumulate. See `ci/datastore.py` on `main`.
EOF

scan_rival "$TREE" || { echo "seed refused: the assembled branch names the rival firm"; exit 1; }

echo "== verifying: offline build and every builder's check against a copy of the assembled store"
VERIFY="$WORK/verify"; SITE_TMP="$WORK/site-data"
rsync -a "$TREE"/ "$VERIFY"/
(
  cd "$ROOT"
  export VM_STORE="$VERIFY" VM_SNAPSHOTS="$VERIFY/snapshots_seed:$VERIFY/snapshots" \
         VM_ONCHAIN_DATA="$VERIFY/onchain/aggregates" VM_SITE_DATA="$SITE_TMP"
  "$PY" pipeline/build.py --offline
  for b in market execution chain reports; do
    "$PY" "pipeline/build_$b.py" --check > "$WORK/check_$b.log" 2>&1 \
      && echo "check $b ok" || { echo "check $b FAILED"; tail -5 "$WORK/check_$b.log"; exit 1; }
  done
)
# the build may only add to the store; a sealed day must come out byte for byte
for f in "$TREE"/days/*.json; do
  cmp -s "$f" "$VERIFY/days/$(basename "$f")" || { echo "sealed day $(basename "$f") changed during the verification build"; exit 1; }
done
echo "  sealed days unchanged by the build: $(ls "$TREE"/days/*.json | wc -l | tr -d ' ')"

echo "== contents"
"$PY" "$ROOT/ci/datastore.py" status --dir "$TREE"
FILES=$(find "$TREE" -type f | wc -l | tr -d ' ')

if [ "$DRY" = 1 ]; then echo "dry run: no commit created ($FILES files)"; exit 0; fi

echo "== committing to local branch '$BRANCH' (not pushed)"
export GIT_DIR="$GIT_REPO" GIT_WORK_TREE="$TREE" GIT_INDEX_FILE="$WORK/index"
(cd "$TREE" && git add -A .)
TREE_SHA="$(git write-tree)"
COMMIT="$(git commit-tree "$TREE_SHA" -m "Seed the data branch from the local store ($TODAY)

$FILES files. Raw snapshots through $(ls "$TREE/snapshots" | tail -1 | cut -c1-14). On-chain aggregates through $OC_DAY.")"
git update-ref "refs/heads/$BRANCH" "$COMMIT"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
echo "branch '$BRANCH' -> $COMMIT ($FILES files, no parent)"
echo "next:  git push origin $BRANCH        (run it right after a capture tick, see the cutover checklist)"
