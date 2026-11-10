#!/usr/bin/env bash
# Uninstall the Pi background service: remove manifest-owned files, stop and
# disable the unit, and drop now-empty directories. Never touches unrelated
# files.
#
# A v3 manifest owns public files plus one published generation (and any
# earlier generation still retained). Only recorded files are removed, then
# their directories when empty: a manifest-provided root is never deleted
# recursively, so a directory holding anything this repo did not install
# survives.
set -euo pipefail

# One spelling per tree: a native Windows caller passes `C:\Users\...` while
# rm and the manifest lookup below resolve the same directory through the
# shell's own `/c/...` form.
shell_path() {
  local path="$1"
  if [[ "$path" == [A-Za-z]:[\\/]* ]]; then
    path="/$(printf '%s' "${path:0:1}" | tr 'A-Z' 'a-z')${path:2}"
    path="${path//\\//}"
  fi
  printf '%s' "$path"
}

pi_home="$(shell_path "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}")"
manifest="$pi_home/.pi-daemon/manifest.json"

# A native Windows interpreter ends its lines with \r\n; a path that keeps the
# \r exists nowhere, and the removal loop above would pass over every file
# the manifest records. python's output is read through tr, above, for that
# reason.

if [[ ! -f "$manifest" ]]; then
  echo "uninstall: no manifest at $manifest — nothing installed by this repo" >&2
  exit 1
fi

systemctl --user disable --now pi-daemon.service 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

mapfile -t recorded < <(python3 - "$manifest" <<'EOF' | tr -d '\r'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
seen = list(document.get("owned") or [])
# Every generation the manifest publishes or retains is described file by
# file in `trees`, so nothing is listed from disk: a directory holding a file
# this repo did not install survives, in a retained generation as much as in
# the active one.
trees = document.get("trees") or {}
for tree, files in sorted(trees.items()):
    for relative in sorted(files or {}):
        seen.append(tree.rstrip("/") + "/" + relative)
# The tree roots go in too: they are directories, so the removal loop passes
# over them, and their two parents - the release directory and the private
# tree above it - join the rmdir pass.
for tree in sorted(trees):
    seen.append(tree.rstrip("/"))
for path in seen:
    print(path)
EOF
)

for path in "${recorded[@]}"; do
  if [[ -f "$path" || -L "$path" ]]; then
    rm -f "$path"
    echo "uninstall: removed $path"
  fi
done

# Drop the directories this uninstall emptied, in reverse path order so a
# child goes before its parent. A directory that still holds a foreign file
# stays, and rmdir never recurses.
mapfile -t directories < <(for path in "${recorded[@]}"; do
  printf '%s\n' "$(dirname "$path")"
  printf '%s\n' "$(dirname "$(dirname "$path")")"
done | LC_ALL=C sort -ru)
for dir in "${directories[@]}"; do
  rmdir "$dir" 2>/dev/null || true
done

rm -f "$manifest"
rmdir "$pi_home/.pi-daemon" 2>/dev/null || true
# Legacy: older installs owned a tmux.conf copy here.
rmdir "$HOME/.config/pi-daemon" 2>/dev/null || true
echo "uninstall: ok"
