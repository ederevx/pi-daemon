#!/usr/bin/env bash
# Provision the daemon, the clients, the seam modules and the systemd user
# unit from this repo into the Pi agent home, recording every owned file in
# a manifest so uninstall removes exactly what this repo installed.
#
# pi-daemon installs as a pi package: the package loads pi/extensions from
# its own clone, and scripts/postinstall.mjs plus the session-start
# reconciliation in pi/extensions/provision.ts call this script with
# --package to place everything outside the clone. A bare run writes
# nothing. Idempotent: re-running refreshes owned copies in place, and
# existing unrelated files are never touched.
set -euo pipefail

if [[ "${1:-}" != "--package" ]]; then
  [[ $# -eq 0 ]] || { echo "install: unexpected argument: $1" >&2; exit 2; }
  echo "install: pi-daemon installs as a pi package:" >&2
  echo "  pi install git:github.com/ederevx/pi-daemon@<tag>" >&2
  echo "  bash scripts/install.sh --package refreshes the helpers and the unit." >&2
  exit 2
fi
shift
[[ $# -eq 0 ]] || { echo "install: unexpected argument: $1" >&2; exit 2; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
local_bin="$HOME/.local/bin"
systemd_dir="$HOME/.config/systemd/user"
state_dir="$pi_home/.pi-daemon"
manifest="$state_dir/manifest.json"

[[ -d "$pi_home" ]] || { echo "install: missing Pi agent home: $pi_home" >&2; exit 1; }
# pi-daemon is a stdlib-only Python 3 daemon; the same interpreter is used
# by uninstall's manifest reading.
command -v python3 >/dev/null 2>&1 || {
  echo "install: python3 not found on PATH (required for pi-daemon)" >&2
  exit 1
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)' || {
  echo "install: python3 >= 3.8 required, found: $(python3 -V 2>&1)" >&2
  exit 1
}

dest_helper="$local_bin/pi-rc"
dest_helper_cmd="$local_bin/pi-rc.cmd"
dest_comms="$local_bin/pi-comms"
dest_daemon="$local_bin/pi-daemon"
dest_platform="$local_bin/pi_platform.py"
dest_conpty="$local_bin/pi_conpty.py"
dest_settings="$local_bin/pi_settings.py"
dest_transcript="$local_bin/pi_transcript.py"
dest_services="$local_bin/pi_services.py"
dest_rpc_client="$local_bin/pi_rpc_client.py"
dest_unit="$systemd_dir/pi-daemon.service"
dest_wrapper="$local_bin/pi"
dest_wrapper_cmd="$local_bin/pi.cmd"

mkdir -p "$local_bin" "$systemd_dir" "$state_dir"

# One installer at a time: every session start reconciles provisioning, and
# hosted sessions respawn together after a reload, so concurrent runs must
# not interleave the manifest or the systemctl calls. A lock older than ten
# minutes is a crashed run's leftover.
lock="$state_dir/install.lock"
find "$lock" -maxdepth 0 -mmin +10 -exec rmdir {} \; 2>/dev/null || true
mkdir "$lock" 2>/dev/null || {
  echo "install: another install is running ($lock)" >&2
  exit 75
}
trap 'rmdir "$lock" 2>/dev/null' EXIT

sources=()
install_to() {
	# Atomic replacement, per shared convention: never truncate a file
	# that running software may read or execute — land the complete new
	# content via a same-directory temp file and rename it over.
	local mode="$1" src="$2" dest="$3" tmp="$3.tmp.$$"
	install -m "$mode" "$src" "$tmp"
	mv -f "$tmp" "$dest"
	sources+=("$src")
}

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

install_to 755 "$repo_root/pi/bin/pi-rc" "$dest_helper"
# PowerShell/cmd cannot execute shebang scripts, so a bare `pi-rc` would
# fall through to ShellExecute and open in the text editor; pi.cmd already
# solves the same problem for the wrapper. The shim delegates to the
# extensionless client above, so one implementation serves both platforms.
install_to 755 "$repo_root/pi/bin/pi-rc.cmd" "$dest_helper_cmd"
install_to 755 "$repo_root/pi/bin/pi-comms" "$dest_comms"
install_to 755 "$repo_root/pi/daemon/pi-daemon" "$dest_daemon"
# The OS-agnostic seam module and its lazy Windows ConPTY backend ship
# next to the scripts so both find pi_platform.py on sys.path.
install_to 644 "$repo_root/pi/lib/pi_platform.py" "$dest_platform"
install_to 644 "$repo_root/pi/lib/pi_conpty.py" "$dest_conpty"
install_to 644 "$repo_root/pi/lib/pi_settings.py" "$dest_settings"
install_to 644 "$repo_root/pi/lib/pi_transcript.py" "$dest_transcript"
install_to 644 "$repo_root/pi/lib/pi_services.py" "$dest_services"
install_to 644 "$repo_root/pi/lib/pi_rpc_client.py" "$dest_rpc_client"

# The real pi binary must be resolved by PATH while skipping the
# wrapper's own directory and the daemon's hosted-session shim. An install
# launched from a hosted Pi inherits the shim first on PATH; treating that
# Bash shim as Pi's Node entry bakes it into the front and makes Node try to
# import a shell script. Its bin dir is substituted into both the wrapper
# (REAL_PI) and the unit (hosted children's PATH).
real_pi=""
state_home="${XDG_STATE_HOME:-$HOME/.local/state}"
shim_pi="$state_home/pi-pty-host/pi-shim/pi"
old_ifs="$IFS"; IFS=:
for dir in $PATH; do
  [[ "$dir" == "$local_bin" ]] && continue
  candidate="$dir/pi"
  [[ -e "$shim_pi" && "$candidate" -ef "$shim_pi" ]] && continue
  if [[ -x "$candidate" ]]; then real_pi="$candidate"; break; fi
done
IFS="$old_ifs"
[[ -n "$real_pi" ]] || {
  echo "install: cannot resolve the real pi binary for the wrapper" >&2
  exit 1
}
sed "s|@REAL_PI@|$real_pi|;s|@PI_BIN_DIR@|$(dirname "$real_pi")|" \
  "$repo_root/pi/systemd/pi-daemon.service" > "$dest_unit.tmp.$$"
mv -f "$dest_unit.tmp.$$" "$dest_unit"
sed "s|@REAL_PI@|$real_pi|" "$repo_root/pi/bin/pi-wrapper" > "$dest_wrapper.tmp.$$"
mv -f "$dest_wrapper.tmp.$$" "$dest_wrapper"
chmod 755 "$dest_wrapper"
# Windows resolves a bare `pi` through pi.cmd; it delegates to the same bash
# wrapper, so the guarding logic is not duplicated per platform.
install_to 755 "$repo_root/pi/bin/pi-wrapper.cmd" "$dest_wrapper_cmd"
# The unit and the wrapper are rendered from templates, so the manifest
# records those templates as the sources their destinations came from.
sources+=("$repo_root/pi/systemd/pi-daemon.service" "$repo_root/pi/bin/pi-wrapper")
# An installer-only advance must re-provision too, so the script records
# itself: it is the map every destination below came from.
sources+=("$repo_root/scripts/install.sh")

owned=("$dest_daemon" "$dest_helper" "$dest_helper_cmd" "$dest_comms" "$dest_wrapper" "$dest_wrapper_cmd" "$dest_unit" "$dest_platform" "$dest_conpty" "$dest_settings" "$dest_transcript" "$dest_services" "$dest_rpc_client")
{
  printf '{\n'
  printf '  "version": 2,\n'
  printf '  "repo": "%s",\n' "$repo_root"
  printf '  "mode": "package",\n'
  printf '  "owned": [\n'
  for i in "${!owned[@]}"; do
    printf '    "%s"%s\n' "${owned[$i]}" "$([[ $i -lt $((${#owned[@]} - 1)) ]] && printf ,)"
  done
  printf '  ],\n'
  printf '  "hashes": {\n'
  for i in "${!owned[@]}"; do
    printf '    "%s": "%s"%s\n' "${owned[$i]}" "$(sha256sum "${owned[$i]}" | cut -d' ' -f1)" \
      "$([[ $i -lt $((${#owned[@]} - 1)) ]] && printf ,)"
  done
  printf '  },\n'
  printf '  "sources": {\n'
  for i in "${!sources[@]}"; do
    printf '    "%s": "%s"%s\n' "${sources[$i]#"$repo_root"/}" \
      "$(sha256sum "${sources[$i]}" | cut -d' ' -f1)" \
      "$([[ $i -lt $((${#sources[@]} - 1)) ]] && printf ,)"
  done
  printf '  }\n}\n'
} > "$manifest.tmp"
mv "$manifest.tmp" "$manifest"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
# Enabled by default, matching the codex-remote-control pattern. Linger
# makes the user manager start at boot; already-enabled linger is a no-op.
loginctl enable-linger "${USER:-$(id -un)}" 2>/dev/null || \
  echo "install: warning — could not enable linger; the service starts at login only"
systemctl --user enable --now pi-daemon.service
else
  # No systemd (for example Windows/Git Bash): the daemon extension starts
  # the daemon on demand, so no unit is needed here.
  echo "install: systemd not present; the daemon starts on demand"
fi

echo "install: ok"
echo "  daemon:    $dest_daemon"
echo "  platform:  $dest_platform"
echo "  conpty:    $dest_conpty"
echo "  launcher:  $dest_helper"
echo "  comms:     $dest_comms"
echo "  pi wrap:   $dest_wrapper"
echo "  pi cmd:    $dest_wrapper_cmd"
echo "  unit:      $dest_unit"
echo "  manifest:  $manifest"
echo
echo "Usage: pi-rc start [name] [dir] [--fresh]; attach with: pi-rc attach [name] [dir]"
echo "Inside a hosted pi: /bg detaches it instantly; the pi keeps running."
echo "Outside hosting: /bg hands the session over; reattach with pi-rc attach."
