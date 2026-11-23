#!/bin/bash
# Proof that the path plumbing (VM_STORE, VM_SNAPSHOTS, VM_ONCHAIN_DATA, VM_SITE_DATA) changes no number.
# Works on scratch copies; the working tree, the store and site/data are only read.
#
#   ci/path_proof.sh [workdir]
#
# 1. before / after   two copies of the tree and its store; in `before` the three plumbing lines are put back to
#                     their hardcoded form. Each copy runs every builder's --check, an offline rebuild in contract
#                     order, and --check again. Exit codes and every output file must be identical.
# 2. relocation       a third copy in the data-branch layout (store, snapshots and on-chain aggregates under one
#                     folder, no .cache, no data/), addressed only through ci/env.sh, must build the same files.
# Files are compared byte for byte, except meta.json's built_at. Re-run it whenever a builder changes.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-/usr/bin/python3}"; command -v "$PY" >/dev/null 2>&1 || PY=python3
W="${1:-$(mktemp -d "${TMPDIR:-/tmp}/vm-proof.XXXXXX")}"
mkdir -p "$W"; echo "work folder: $W"

copy_tree() {  # <target>
  mkdir -p "$1/.cache"
  rsync -a --exclude __pycache__ "$ROOT/pipeline" "$ROOT/ci" "$1/"
  rsync -a --exclude node_modules "$ROOT/site" "$1/"
  [ -d "$ROOT/data" ] && rsync -a "$ROOT/data" "$1/"
  [ -d "$ROOT/public" ] && rsync -a "$ROOT/public" "$1/"
  for d in store snapshots snapshots_seed telegram; do [ -d "$ROOT/.cache/$d" ] && rsync -a --exclude '*.tmp' "$ROOT/.cache/$d" "$1/.cache/"; done
}
run_all() {  # <tree> <label>   (environment as given)
  local out="$W/$2.exits"; : > "$out"
  ( cd "$1" || exit 2
    for b in market execution chain reports; do "$PY" pipeline/build_$b.py --check > "$W/$2.check0_$b.log" 2>&1; echo "check0 $b $?" >> "$out"; done
    "$PY" pipeline/build.py --offline > "$W/$2.build.log" 2>&1; echo "build $?" >> "$out"
    for b in market execution chain reports; do "$PY" pipeline/build_$b.py --check > "$W/$2.check1_$b.log" 2>&1; echo "check1 $b $?" >> "$out"; done )
}
manifest() {  # <label>=<folder> ...
  "$PY" - "$@" <<'PYEOF'
import hashlib, json, os, sys
for arg in sys.argv[1:]:
    label, root = arg.split("=", 1)
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            if f.endswith(".tmp") or f == ".DS_Store":
                continue
            p = os.path.join(dp, f)
            raw = open(p, "rb").read()
            if f == "meta.json":
                try:
                    doc = json.loads(raw); doc.pop("built_at", None); raw = json.dumps(doc, sort_keys=True).encode()
                except ValueError:
                    pass
            print("%s/%s %s" % (label, os.path.relpath(p, root), hashlib.sha256(raw).hexdigest()))
PYEOF
}

echo "== copying the tree three times (frozen at $(date -u +%H:%M:%SZ))"
copy_tree "$W/after"; rsync -a "$W/after/" "$W/before/"; rsync -a "$W/after/" "$W/env-src/"
"$PY" - "$W/before" <<'PYEOF' || { echo "could not put the plumbing lines back: one of them was changed by someone else"; exit 2; }
import sys
root = sys.argv[1]
edits = [
 ("pipeline/market/common.py",
  '# VM_SNAPSHOTS: one or more folders separated by os.pathsep (CI: the data branch\'s snapshots_seed and snapshots).\nSNAPSHOT_DIRS = [p for p in os.environ.get("VM_SNAPSHOTS", "").split(os.pathsep) if p] or [\n',
  'SNAPSHOT_DIRS = [\n'),
 ("pipeline/build_execution.py",
  'OUT = os.path.join(os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data"), "execution_venue.json")',
  'OUT = os.path.join(ROOT, "site", "data", "execution_venue.json")'),
 ("pipeline/build_chain.py",
  'SRC = os.environ.get("VM_ONCHAIN_DATA") or os.path.join(ROOT, "data", "onchain")\nOUT = os.environ.get("VM_SITE_DATA") or os.path.join(ROOT, "site", "data")',
  'SRC = os.path.join(ROOT, "data", "onchain")\nOUT = os.path.join(ROOT, "site", "data")'),
]
for path, new, old in edits:
    text = open(root + "/" + path).read()
    if text.count(new) != 1:
        print("not found exactly once in %s: %r" % (path, new[:70])); sys.exit(1)
    open(root + "/" + path, "w").write(text.replace(new, old))
print("before: three lines put back to their hardcoded form")
PYEOF

echo "== 1. before / after"
( unset VM_STORE VM_SNAPSHOTS VM_ONCHAIN_DATA VM_SITE_DATA; run_all "$W/before" before; run_all "$W/after" after )
manifest "site_data=$W/before/site/data" "store=$W/before/.cache/store" > "$W/before.manifest"
manifest "site_data=$W/after/site/data" "store=$W/after/.cache/store" > "$W/after.manifest"
FAIL=0
if diff "$W/before.exits" "$W/after.exits" > /dev/null; then echo "exit codes identical: $(tr '\n' ';' < "$W/after.exits")"; else echo "EXIT CODES DIFFER"; diff "$W/before.exits" "$W/after.exits"; FAIL=1; fi
if diff "$W/before.manifest" "$W/after.manifest" > "$W/ba.diff"; then echo "outputs identical: $(wc -l < "$W/after.manifest" | tr -d ' ') files (site/data and the store)"; else echo "OUTPUTS DIFFER:"; head -20 "$W/ba.diff"; FAIL=1; fi

echo "== 2. relocation into the data-branch layout"
E="$W/env"; mkdir -p "$E"
rsync -a "$W/env-src/pipeline" "$W/env-src/ci" "$E/"; rsync -a --exclude data "$W/env-src/site" "$E/"
mkdir -p "$E/.store/onchain/aggregates"
rsync -a "$W/env-src/.cache/store/" "$E/.store/"
rsync -a "$W/env-src/.cache/snapshots_seed" "$E/.store/" 2>/dev/null
mkdir -p "$E/.store/snapshots"; cp -p "$W/env-src/.cache/snapshots/"*.json.gz "$E/.store/snapshots/" 2>/dev/null
cp -p "$W/env-src/data/onchain/"*.json "$E/.store/onchain/aggregates/" 2>/dev/null
( cd "$E" && . ci/env.sh .store >/dev/null && run_all "$E" env )
manifest "site_data=$E/site/data" > "$W/env.manifest"
grep '^site_data/' "$W/after.manifest" > "$W/after.site.manifest"
if diff "$W/after.site.manifest" "$W/env.manifest" > "$W/env.diff"; then echo "relocated build identical: $(wc -l < "$W/env.manifest" | tr -d ' ') files in site/data"; else echo "RELOCATED BUILD DIFFERS:"; head -20 "$W/env.diff"; FAIL=1; fi
echo "relocated exit codes: $(tr '\n' ';' < "$W/env.exits")"
grep -q "^build 0" "$W/env.exits" || { echo "the relocated offline build failed: see $W/env.build.log"; FAIL=1; }
[ "$FAIL" = 0 ] && echo "PROOF OK" || echo "PROOF FAILED (logs in $W)"
exit "$FAIL"
