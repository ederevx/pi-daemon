#!/usr/bin/env bash
# Install the Pi background service from this repo into the Pi agent home.
#
# Copies the daemon and offload extensions, the pi-rc client, the
# pi-daemon PTY host daemon, and the systemd user unit, recording every
# owned file in a manifest so uninstall removes exactly what this repo
# installed.
# Idempotent: re-running refreshes owned copies in place. Existing
# unrelated files are never touched.
set -euo pipefail

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

dest_extension="$pi_home/extensions/daemon.ts"
dest_offload="$pi_home/extensions/offload.ts"
dest_subagents="$pi_home/extensions/dsubagents-views.ts"
dest_front="$local_bin/pi-agent-entry.mjs"
dest_helper="$local_bin/pi-rc"
dest_daemon="$local_bin/pi-daemon"
dest_unit="$systemd_dir/pi-daemon.service"
dest_wrapper="$local_bin/pi"

mkdir -p "$pi_home/extensions" "$local_bin" "$systemd_dir" "$state_dir"

install_to() {
	# Atomic replacement, per shared convention: never truncate a file
	# that running software may read or execute — land the complete new
	# content via a same-directory temp file and rename it over.
	local mode="$1" src="$2" dest="$3" tmp="$3.tmp.$$"
	install -m "$mode" "$src" "$tmp"
	mv -f "$tmp" "$dest"
}

install_to 644 "$repo_root/pi/extensions/daemon.ts" "$dest_extension"
install_to 644 "$repo_root/pi/extensions/offload.ts" "$dest_offload"
install_to 644 "$repo_root/pi/extensions/dsubagents-views.ts" "$dest_subagents"
install_to 755 "$repo_root/pi/bin/pi-rc" "$dest_helper"
install_to 755 "$repo_root/pi/daemon/pi-daemon" "$dest_daemon"

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

# The pi-compatible front entry loads the real pi entry in-process, so
# it needs the resolved (symlink-free) entry path baked in.
real_entry="$(realpath "$real_pi")"
sed "s|@REAL_ENTRY@|$real_entry|" "$repo_root/pi/daemon/pi-agent-entry.mjs" \
  > "$dest_front.tmp.$$"
mv -f "$dest_front.tmp.$$" "$dest_front"
chmod 755 "$dest_front"

owned=("$dest_daemon" "$dest_helper" "$dest_wrapper" "$dest_extension" "$dest_offload" "$dest_subagents" "$dest_front" "$dest_unit")
{
  printf '{\n'
  printf '  "version": 1,\n'
  printf '  "repo": "%s",\n' "$repo_root"
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
  printf '  }\n}\n'
} > "$manifest.tmp"
mv "$manifest.tmp" "$manifest"

systemctl --user daemon-reload
# Enabled by default, matching the codex-remote-control pattern. Linger
# makes the user manager start at boot; already-enabled linger is a no-op.
loginctl enable-linger "${USER:-$(id -un)}" 2>/dev/null || \
  echo "install: warning — could not enable linger; the service starts at login only"
systemctl --user enable --now pi-daemon.service

echo "install: ok"
echo "  daemon:    $dest_daemon"
echo "  launcher:  $dest_helper"
echo "  pi wrap:   $dest_wrapper"
echo "  extension: $dest_extension"
echo "  offload:   $dest_offload
  subagents: $dest_subagents"
echo "  pi front:  $dest_front"
echo "  unit:      $dest_unit"
echo "  manifest:  $manifest"
echo
echo "Usage: pi-rc start [name] [dir] [--fresh]; attach with: pi-rc attach [name] [dir]"
echo "Inside a hosted pi: /bg detaches it instantly; the pi keeps running."
echo "Outside hosting: /bg hands the session over; reattach with pi-rc attach."
