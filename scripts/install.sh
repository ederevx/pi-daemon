#!/usr/bin/env bash
# Provision the daemon, the clients, the extension tree and the systemd user
# unit from this repo, recording every owned file in a manifest so uninstall
# removes exactly what this repo installed.
#
# pi-daemon installs as a pi package. The private tree is the install: each
# generation is built whole under ~/.local/lib/pi-daemon/releases/<id> and
# published by one atomic manifest replacement, so a command always runs a
# complete release and a running daemon keeps the release it started from.
# The public launchers are stable dispatchers that read the manifest.
#
# A bare run writes nothing. Idempotent: re-running either republishes the
# same generation or publishes a new one. Existing unrelated files are never
# touched, and the installer never stops or starts a daemon implicitly.
set -euo pipefail

usage() {
  echo "install: pi-daemon installs as a pi package:" >&2
  echo "  pi install git:github.com/ederevx/pi-daemon@<tag>" >&2
  echo "  bash scripts/install.sh --package refreshes the service files." >&2
  exit 2
}

sources_only=0
case "${1:-}" in
  --package) shift ;;
  # The provisioning stamp asks the installer what it would install right
  # now, so an added or removed module is noticed as readily as an edited
  # one; answered after the inventory functions are defined.
  --sources) sources_only=1; shift ;;
  "") usage ;;
  *) echo "install: unexpected argument: $1" >&2; exit 2 ;;
esac
[[ $# -eq 0 ]] || { echo "install: unexpected argument: $1" >&2; exit 2; }

die() { echo "install: $*" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
local_bin="$HOME/.local/bin"
lib_root="$HOME/.local/lib/pi-daemon"
releases="$lib_root/releases"
systemd_dir="$HOME/.config/systemd/user"
state_dir="$pi_home/.pi-daemon"
manifest="$state_dir/manifest.json"
transaction="$state_dir/install-transaction.json"
backups_root="$state_dir/install-backups"
lock="$state_dir/install.lock"

[[ -d "$pi_home" ]] || die "missing Pi agent home: $pi_home"
# pi-daemon is a stdlib-only Python 3 daemon; the same interpreter is used
# by the installer's own JSON work.
command -v python3 >/dev/null 2>&1 || \
  die "python3 not found on PATH (required for pi-daemon)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || \
  die "python3 >= 3.8 required, found: $(python3 -V 2>&1)"

# The two service roots: the operator's own, else the same defaults the
# unit will declare. Everything below - the unit, defaults.json, the
# migration and the manifest - reads these values, never its own idea.
new_state="${PI_DAEMON_STATE_DIR:-$HOME/.local/state/pi-daemon}"
if [[ -n "${PI_DAEMON_RUNTIME_DIR:-}" ]]; then
  new_runtime="$PI_DAEMON_RUNTIME_DIR"
elif [[ -n "${XDG_RUNTIME_DIR:-}" ]]; then
  new_runtime="$XDG_RUNTIME_DIR/pi-daemon"
else
  new_runtime="${TMPDIR:-/tmp}/pi-daemon-$(id -u)"
fi

# -- the source inventory ------------------------------------------------
#
# One list owns what this repo installs: the copy phase, the manifest's
# sources and `--sources` (which the provisioner compares) all read it, so a
# module added to the package can never go unnoticed.

seam_modules=(pi_platform.py pi_conpty.py pi_settings.py pi_transcript.py
              pi_services.py pi_rpc_client.py)

extension_sources() {
  if git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "$repo_root" ls-files -z -- pi/extensions | tr '\0' '\n'
  else
    ( cd "$repo_root" && find pi/extensions -type f \
        ! -name '*.pyc' ! -path '*__pycache__*' )
  fi
}

tree_sources() {
  printf '%s\n' pi/daemon/pi-daemon pi/bin/pi-rc pi/bin/pi-comms \
    pi/bin/pi-daemon-migrate
  printf 'pi/lib/%s\n' "${seam_modules[@]}"
  ( cd "$repo_root" && find pi/lib/pi_daemon -type f -name '*.py' \
      ! -path '*__pycache__*' )
  extension_sources
}

template_sources() {
  printf '%s\n' pi/bin/pi-launcher.in pi/bin/pi-wrapper pi/bin/pi-wrapper.cmd \
    pi/bin/pi-rc.cmd pi/bin/pi-daemon-migrate.cmd \
    pi/systemd/pi-daemon.service scripts/install.sh
}

all_sources() { { tree_sources; template_sources; } | LC_ALL=C sort -u; }

# --sources prints the exact source inventory and every digest, then stops.
if [[ "$sources_only" == 1 ]]; then
  [[ $# -eq 0 ]] || { echo "install: unexpected argument: $1" >&2; exit 2; }
  all_sources | python3 -c '
import hashlib, json, os, sys
root = sys.argv[1]
document = {}
for line in sys.stdin:
    rel = line.strip()
    if not rel:
        continue
    with open(os.path.join(root, rel), "rb") as handle:
        document[rel] = hashlib.sha256(handle.read()).hexdigest()
print(json.dumps(document, sort_keys=True, indent=2))' "$repo_root"
  exit 0
fi

# -- the transaction and its recovery ------------------------------------

recover_transaction() {
  [[ -f "$transaction" ]] || return 0
  python3 - "$transaction" "$manifest" "$releases" <<'PY'
import json
import os
import shutil
import sys

transaction, manifest, releases = sys.argv[1:4]
try:
    with open(transaction, encoding="utf-8") as handle:
        document = json.load(handle)
except (OSError, ValueError):
    sys.exit(0)
for entry in document.get("backups") or []:
    path, backup = entry.get("path"), entry.get("backup")
    try:
        if backup is None:
            if os.path.isfile(path):
                os.unlink(path)
        else:
            shutil.copy2(backup, path)
    except OSError:
        pass
# The generation the interrupted run was publishing is unreferenced: the
# manifest still names the previous one, so drop it rather than leave an
# orphan in the release directory.
root = document.get("active_root") or ""
try:
    with open(manifest, encoding="utf-8") as handle:
        active = json.load(handle).get("active_root")
except (OSError, ValueError):
    active = None
if root and root != active and os.path.dirname(root) == releases:
    shutil.rmtree(root, ignore_errors=True)
PY
  rm -f "$transaction"
  echo "install: restored the previous installation after an interrupted run" >&2
}

# -- refusals before anything public changes ----------------------------

# `systemctl --user` answers for the whole user manager, not for the home this
# install targets, so the question is narrowed first: is the unit this install
# owns on disk, and does it declare the two roots this run resolves? A unit
# rendered for other roots - another home, an isolated test tree - is not
# serving this install, and its daemon must not stop an install here.
service_active() {
  local unit="$systemd_dir/pi-daemon.service"
  [[ -f "$unit" ]] || return 1
  grep -Fqx "Environment=\"PI_DAEMON_STATE_DIR=$new_state\"" "$unit" || return 1
  grep -Fqx "Environment=\"PI_DAEMON_RUNTIME_DIR=$new_runtime\"" "$unit" || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user is-active --quiet pi-daemon.service >/dev/null 2>&1
}

endpoint_answers() {
  [[ -f "$1" ]] || return 1
  python3 - "$1" <<'PY'
import json
import socket
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
    host, port = str(data["host"]), int(data["port"])
except Exception:
    sys.exit(1)
try:
    with socket.create_connection((host, port), timeout=1.0):
        pass
except OSError:
    sys.exit(1)
sys.exit(0)
PY
}

legacy_endpoint() {
  [[ -n "${XDG_RUNTIME_DIR:-}" ]] || return 1
  local candidate
  for candidate in "$XDG_RUNTIME_DIR/pi-pty-host.sock" \
                   "$XDG_RUNTIME_DIR/pi-pty-host/control.json"; do
    printf '%s\n' "$candidate"
  done
}

# A translation must not run beside a daemon: the legacy writer would move
# the sources under it, and the new daemon holds the instance lock the
# migration needs. Exit 76 is the maintenance contract the provisioner
# reports instead of retrying forever; 75 stays "another install holds the
# lock", which is retried later.
maintenance() {
  echo "install: $*" >&2
  echo "install: nothing was changed; stop the daemon and re-run" >&2
  exit 76
}

refuse_while_serving() {
  local endpoint
  if service_active; then
    maintenance "pi-daemon.service is active (systemctl --user stop pi-daemon.service)"
  fi
  while IFS= read -r endpoint; do
    if endpoint_answers "$endpoint"; then
      maintenance "a daemon is serving $endpoint"
    fi
  done < <(legacy_endpoint || true)
  if endpoint_answers "$new_runtime/control.json"; then
    maintenance "a daemon is serving $new_runtime/control.json"
  fi
}

# -- one published generation -------------------------------------------

build_tree() {
  local stage="$1" rel dest mode
  while IFS= read -r rel; do
    case "$rel" in
      pi/daemon/pi-daemon) dest="daemon/pi-daemon"; mode=755 ;;
      pi/bin/pi-rc|pi/bin/pi-comms|pi/bin/pi-daemon-migrate)
        dest="bin/${rel##*/}"; mode=755 ;;
      pi/lib/*) dest="lib/${rel#pi/lib/}"; mode=644 ;;
      pi/extensions/*) dest="extensions/${rel#pi/extensions/}"; mode=644 ;;
      *) continue ;;
    esac
    mkdir -p "$stage/$(dirname "$dest")"
    install -m "$mode" "$repo_root/$rel" "$stage/$dest"
  done < <(tree_sources)
  python3 - "$stage/defaults.json" "$new_state" "$new_runtime" "$pi_home" <<'PY'
import json
import sys

out, state, runtime, agent = sys.argv[1:5]
with open(out, "w", encoding="utf-8") as handle:
    json.dump({
        "PI_DAEMON_STATE_DIR": state,
        "PI_DAEMON_RUNTIME_DIR": runtime,
        "PI_CODING_AGENT_DIR": agent,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

content_id() {
  python3 - "$1" <<'PY'
import hashlib
import os
import sys

root = sys.argv[1]
lines = []
for base, directories, files in os.walk(root):
    directories.sort()
    for name in sorted(files):
        path = os.path.join(base, name)
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        lines.append("%s  %s\n" % (relative, digest))
print(hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()[:16])
PY
}

render_launcher() {
  local target="$1" dest="$2"
  sed "s|@TARGET@|$target|" "$repo_root/pi/bin/pi-launcher.in" \
    > "$dest.tmp.$$"
  chmod 755 "$dest.tmp.$$"
  mv -f "$dest.tmp.$$" "$dest"
}

write_manifest() {
  local out="$1" retained="$2" sources_file="$3"
  shift 3
  python3 - "$out" "$repo_root" "$active_root" "$retained" "$sources_file" "$@" <<'PY'
import hashlib
import json
import os
import sys

out, repo, active, retained, sources_file = sys.argv[1:6]
owned = sys.argv[6:]


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


tree = {}
for base, directories, files in os.walk(active):
    directories.sort()
    for name in sorted(files):
        path = os.path.join(base, name)
        relative = os.path.relpath(path, active).replace(os.sep, "/")
        tree[relative] = digest(path)
sources = {}
with open(sources_file, encoding="utf-8") as handle:
    for line in handle:
        relative = line.strip()
        if relative:
            sources[relative] = digest(os.path.join(repo, relative))
document = {
    "version": 3,
    "repo": repo,
    "mode": "package",
    "active_root": active,
    "owned": owned,
    "hashes": {path: digest(path) for path in owned
               if os.path.isfile(path)},
    "trees": {active: tree},
    "retained": json.loads(retained),
    "sources": sources,
}
with open(out, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

# -- the run -------------------------------------------------------------

mkdir -p "$local_bin" "$systemd_dir" "$state_dir" "$releases" \
  "$new_state" "$new_runtime"

# One installer at a time: every session start reconciles provisioning, and
# hosted sessions respawn together after a reload, so concurrent runs must
# not interleave the manifest or the systemctl calls. A lock older than ten
# minutes is a crashed run's leftover.
find "$lock" -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null || true
# 75 (EX_TEMPFAIL) tells the caller to retry rather than to repair.
mkdir "$lock" 2>/dev/null || {
  echo "install: another install is running ($lock)" >&2
  exit 75
}
trap 'rmdir "$lock" 2>/dev/null' EXIT

recover_transaction

# Older installs could place extension copies in the agent home, where a
# session would load them beside the package's own and abort on tool
# conflicts. A copy goes when a previous manifest owned it, or when it is
# byte-identical to this clone's extension (a copy whose manifest is gone).
# Unrelated files are never touched.
for ext in daemon offload; do
  src="$repo_root/pi/extensions/$ext.ts"
  dst="$pi_home/extensions/$ext.ts"
  [[ -f "$dst" ]] || continue
  if { [[ -f "$manifest" ]] && grep -qF "\"$dst\"" "$manifest"; } \
      || [[ "$(sha256sum "$dst" | cut -d' ' -f1)" == \
            "$(sha256sum "$src" | cut -d' ' -f1)" ]]; then
    rm -f "$dst"
  fi
done

stage="$releases/.staging.$$"
rm -rf "$stage"
mkdir -p "$stage"
build_tree "$stage"
candidate="$releases/$(content_id "$stage")"
if [[ -d "$candidate" ]]; then
  rm -rf "$stage"
else
  # Same filesystem, so the rename publishes a complete generation or
  # nothing: no launcher can see a half-copied release.
  mv "$stage" "$candidate"
fi

# The migration runs from the candidate generation, with the exact roots the
# unit and defaults.json will use, and before any public file changes: a
# translation that fails leaves the previous install running. `--force` is
# never added here; it is the operator's explicit decision.
migrate_env=(env "PI_DAEMON_STATE_DIR=$new_state" \
  "PI_DAEMON_RUNTIME_DIR=$new_runtime" "PI_CODING_AGENT_DIR=$pi_home" \
  PYTHONDONTWRITEBYTECODE=1)
plan="$state_dir/migration-plan.$$"
if ! "${migrate_env[@]}" "$candidate/bin/pi-daemon-migrate" --dry-run \
    > "$plan" 2>"$plan.err"; then
  cat "$plan.err" >&2 || true
  # A held instance lock (3) means a new daemon is running: that is the
  # maintenance case, not a broken translation.
  if grep -q "instance lock" "$plan.err" 2>/dev/null; then
    rm -f "$plan" "$plan.err"
    maintenance "the new daemon holds the instance lock"
  fi
  rm -f "$plan" "$plan.err"
  die "migration preview failed; nothing was installed or started"
fi
rm -f "$plan.err"
action="$(python3 - "$plan" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get("action") or "")
except Exception:
    print("")
PY
)"
rm -f "$plan"
if [[ "$action" == "translate" ]]; then
  # Only a translation needs the daemon stopped: it is the step that
  # writes the new stores and refuses beside a live instance.
  refuse_while_serving
fi
if ! output="$("${migrate_env[@]}" "$candidate/bin/pi-daemon-migrate" 2>&1)"; then
  printf '%s\n' "$output" >&2
  die "migration failed; nothing was installed or started"
fi

# The real pi binary must be resolved by PATH while skipping the wrapper's
# own directory and the daemon's hosted-session shim. An install launched
# from a hosted Pi inherits the shim first on PATH; treating that Bash shim
# as Pi's Node entry bakes it into the front and makes Node try to import a
# shell script.
real_pi=""
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
shim_pi="$state_home/pi-pty-host/pi-shim/pi"
old_ifs="$IFS"; IFS=:
for dir in $PATH; do
  [[ "$dir" == "$local_bin" ]] && continue
  candidate_pi="$dir/pi"
  [[ -e "$shim_pi" && "$candidate_pi" -ef "$shim_pi" ]] && continue
  if [[ -x "$candidate_pi" ]]; then real_pi="$candidate_pi"; break; fi
done
IFS="$old_ifs"
[[ -n "$real_pi" ]] || die "cannot resolve the real pi binary for the wrapper"

public=("$local_bin/pi-daemon" "$local_bin/pi-rc" "$local_bin/pi-rc.cmd"
        "$local_bin/pi-comms" "$local_bin/pi-daemon-migrate"
        "$local_bin/pi-daemon-migrate.cmd" "$local_bin/pi"
        "$local_bin/pi.cmd" "$systemd_dir/pi-daemon.service")

# Record what a failed promotion must restore, then promote. The manifest,
# several launchers and the unit cannot be replaced as one operation, so an
# interrupted run is recovered from this journal rather than guessed at.
backups="$backups_root/$(date +%s)-$$"
mkdir -p "$backups"
{
  printf '[\n'
  first=1
  index=0
  for path in "${public[@]}"; do
    backup=null
    if [[ -e "$path" ]]; then
      backup="$backups/$index-$(basename "$path")"
      cp -p "$path" "$backup"
      backup="\"$backup\""
    fi
    [[ $first -eq 1 ]] || printf ',\n'
    first=0
    index=$((index + 1))
    printf '  {"path": "%s", "backup": %s}' "$path" "$backup"
  done
  printf '\n]\n'
} > "$state_dir/transaction-backups.$$"
python3 - "$transaction" "$candidate" "$state_dir/transaction-backups.$$" <<'PY'
import json
import sys

out, active, backups = sys.argv[1:4]
with open(backups, encoding="utf-8") as handle:
    entries = json.load(handle)
with open(out, "w", encoding="utf-8") as handle:
    json.dump({"version": 1, "active_root": active, "backups": entries},
              handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
rm -f "$state_dir/transaction-backups.$$"

# A same-directory temporary file and a rename: running software keeps the
# bytes it opened, and a reader never sees a truncated command.
install_if_changed() {
  local src="$1" dest="$2" tmp="$2.tmp.$$"
  if cmp -s "$src" "$dest"; then
    return 0
  fi
  cp "$src" "$tmp"
  chmod 755 "$tmp"
  mv -f "$tmp" "$dest"
}

for pair in "daemon/pi-daemon:$local_bin/pi-daemon" \
            "bin/pi-rc:$local_bin/pi-rc" \
            "bin/pi-comms:$local_bin/pi-comms" \
            "bin/pi-daemon-migrate:$local_bin/pi-daemon-migrate"; do
  render_launcher "${pair%%:*}" "${pair#*:}.rendered"
  install_if_changed "${pair#*:}.rendered" "${pair#*:}"
  rm -f "${pair#*:}.rendered"
done
install_if_changed "$repo_root/pi/bin/pi-rc.cmd" "$local_bin/pi-rc.cmd"
install_if_changed "$repo_root/pi/bin/pi-daemon-migrate.cmd" \
  "$local_bin/pi-daemon-migrate.cmd"

sed "s|@REAL_PI@|$real_pi|" "$repo_root/pi/bin/pi-wrapper" \
  > "$local_bin/pi.tmp.$$"
chmod 755 "$local_bin/pi.tmp.$$"
mv -f "$local_bin/pi.tmp.$$" "$local_bin/pi"
install_if_changed "$repo_root/pi/bin/pi-wrapper.cmd" "$local_bin/pi.cmd"

sed "s|@REAL_PI@|$real_pi|;s|@PI_BIN_DIR@|$(dirname "$real_pi")|;\
s|@STATE_DIR@|$new_state|;s|@RUNTIME_DIR@|$new_runtime|;\
s|@LAUNCHER@|$local_bin/pi-daemon|;s|@LAUNCHER_RC@|$local_bin/pi-rc|;\
s|@ENDPOINT@|$new_runtime/control.json|" \
  "$repo_root/pi/systemd/pi-daemon.service" > "$systemd_dir/pi-daemon.service.tmp.$$"
mv -f "$systemd_dir/pi-daemon.service.tmp.$$" "$systemd_dir/pi-daemon.service"

# The published generation this manifest selects; earlier ones are retained
# because a running process may still be executing them.
previous_active=""
if [[ -f "$manifest" ]]; then
  previous_active="$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("active_root") or "")
except Exception:
    print("")' "$manifest")"
fi
retained="["
separator=""
if [[ -n "$previous_active" && "$previous_active" != "$candidate" \
      && -d "$previous_active" ]]; then
  retained+="$separator\"$previous_active\""
  separator=", "
fi
if [[ -f "$manifest" ]]; then
  while IFS= read -r kept; do
    [[ -n "$kept" ]] || continue
    [[ "$kept" == "$candidate" || "$kept" == "$previous_active" ]] && continue
    [[ -d "$kept" ]] || continue
    retained+="$separator\"$kept\""
    separator=", "
  done < <(python3 -c 'import json,sys
try:
    print("\n".join(json.load(open(sys.argv[1], encoding="utf-8")).get("retained") or []))
except Exception:
    pass' "$manifest")
fi
retained+="]"

# The flat seam modules of a v2 install are no longer owned: the generation
# carries them and the public entry is a dispatcher. The list is read from
# the manifest this write replaces.
retire="$state_dir/retire.$$"
: > "$retire"
if [[ -f "$manifest" ]]; then
  python3 - "$manifest" "$retire" <<'PY'
import json
import os
import sys

manifest, out = sys.argv[1:3]
flat = {"pi_platform.py", "pi_conpty.py", "pi_settings.py",
        "pi_transcript.py", "pi_services.py", "pi_rpc_client.py"}
try:
    with open(manifest, encoding="utf-8") as handle:
        document = json.load(handle)
except (OSError, ValueError):
    document = {}
with open(out, "w", encoding="utf-8") as handle:
    for path in document.get("owned") or []:
        if os.path.basename(path) in flat:
            handle.write(path + "\n")
PY
fi

active_root="$candidate"
all_sources > "$state_dir/sources.$$"
write_manifest "$manifest.tmp" "$retained" "$state_dir/sources.$$" "${public[@]}"
mv "$manifest.tmp" "$manifest"
rm -f "$state_dir/sources.$$"
while IFS= read -r retired; do
  [[ -n "$retired" ]] || continue
  [[ -e "$retired" ]] && rm -f "$retired"
done < "$retire"
rm -f "$retire"
rm -f "$transaction"
rm -rf "$backups"
rmdir "$backups_root" 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  # Enabled by default, matching the codex-remote-control pattern. Linger
  # makes the user manager start at boot; already-enabled linger is a no-op.
  loginctl enable-linger "${USER:-$(id -un)}" 2>/dev/null || \
    echo "install: warning — could not enable linger; the service starts at login only" >&2
  systemctl --user enable --now pi-daemon.service
else
  # No systemd (for example Windows/Git Bash): the daemon extension starts
  # the daemon on demand, so no unit is needed here.
  echo "install: systemd not present; the daemon starts on demand"
fi

echo "install: ok"
echo "  release:   $active_root"
echo "  launcher:  $local_bin/pi-daemon"
echo "  clients:   $local_bin/pi-rc, $local_bin/pi-comms, $local_bin/pi-daemon-migrate"
echo "  roots:     state $new_state"
echo "             runtime $new_runtime"
echo "  unit:      $systemd_dir/pi-daemon.service"
echo "  manifest:  $manifest"
echo
echo "Usage: pi-rc start [name] [dir] [--fresh]; attach with: pi-rc attach [name] [dir]"
echo "Inside a hosted pi: /bg detaches it instantly; the pi keeps running."
echo "Outside hosting: /bg hands the session over; reattach with pi-rc attach."
