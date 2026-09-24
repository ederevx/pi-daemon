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

# A pi package install runs this script through scripts/postinstall.mjs
# with --package: the package already loads pi/extensions from its own
# clone, so package mode installs only the daemon, client, wrapper, unit,
# and seam modules to the manual paths, and drops any extension copies a
# prior manual install left in the agent home so the package stays the
# one loader source. The bare manual invocation is unchanged.
package_mode=0
if [[ "${1:-}" == "--package" ]]; then
  package_mode=1
  shift
fi
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

dest_extension="$pi_home/extensions/daemon.ts"
dest_offload="$pi_home/extensions/offload.ts"
dest_helper="$local_bin/pi-rc"
dest_helper_cmd="$local_bin/pi-rc.cmd"
dest_daemon="$local_bin/pi-daemon"
dest_platform="$local_bin/pi_platform.py"
dest_conpty="$local_bin/pi_conpty.py"
dest_settings="$local_bin/pi_settings.py"
dest_unit="$systemd_dir/pi-daemon.service"
dest_wrapper="$local_bin/pi"
dest_wrapper_cmd="$local_bin/pi.cmd"

mkdir -p "$pi_home/extensions" "$local_bin" "$systemd_dir" "$state_dir"

# The one-loader guard: a pinned pi package already loads this repo's
# extensions from its own clone, and a manual extension copy beside it
# aborts every new pi session with tool-conflict errors ("Tool "bash"
# conflicts ... offload.ts"). The bare manual invocation therefore
# refuses outright whenever a packages entry installs pi-daemon — a git
# pin or a local path whose final component is this package. --package
# mode is exempt: it is the package's own postinstall and exists to
# remove such copies. Uninstall the manual copy or drop the pin first;
# nothing on disk has been touched by the refusal.
if [[ $package_mode -eq 0 ]]; then
  pinned="$(python3 - "$pi_home/settings.json" <<'PYGUARD'
import json, sys
try:
    packages = json.load(open(sys.argv[1], encoding="utf-8")).get("packages", [])
except Exception:
    packages = []
for entry in packages:
    if not isinstance(entry, str):
        continue
    normalized = entry.replace("\\", "/").rstrip("/")
    if (normalized.startswith("git:") and "pi-daemon" in normalized) \
            or normalized.rsplit("/", 1)[-1] == "pi-daemon":
        print(entry)
        break
PYGUARD
)"
  if [[ -n "$pinned" ]]; then
    echo "install: pi-daemon is installed as a pi package ($pinned)." >&2
    echo "  A manual install would load its extensions twice and abort every" >&2
    echo "  new session with tool-conflict errors. Update the package instead" >&2
    echo "  (pi update git:github.com/ederevx/pi-daemon), drop the pin before" >&2
    echo "  a manual install, or use scripts/install.sh --package to refresh" >&2
    echo "  only the bin helpers and unit." >&2
    exit 1
  fi
fi

install_to() {
	# Atomic replacement, per shared convention: never truncate a file
	# that running software may read or execute — land the complete new
	# content via a same-directory temp file and rename it over.
	local mode="$1" src="$2" dest="$3" tmp="$3.tmp.$$"
	install -m "$mode" "$src" "$tmp"
	mv -f "$tmp" "$dest"
}

# In package mode the extensions are the package's, not this install's:
# remove the copies a prior manual install left so a session never loads
# both. A path goes when the previous manifest owned it, or when it is
# byte-identical to this clone's extension (a manual copy whose manifest
# is gone). Unrelated files are never touched.
if [[ $package_mode -eq 1 ]]; then
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
fi

if [[ $package_mode -eq 0 ]]; then
  install_to 644 "$repo_root/pi/extensions/daemon.ts" "$dest_extension"
  install_to 644 "$repo_root/pi/extensions/offload.ts" "$dest_offload"
fi

install_to 755 "$repo_root/pi/bin/pi-rc" "$dest_helper"
# PowerShell/cmd cannot execute shebang scripts, so a bare `pi-rc` would
# fall through to ShellExecute and open in the text editor; pi.cmd already
# solves the same problem for the wrapper. The shim delegates to the
# extensionless client above, so one implementation serves both platforms.
install_to 755 "$repo_root/pi/bin/pi-rc.cmd" "$dest_helper_cmd"
install_to 755 "$repo_root/pi/daemon/pi-daemon" "$dest_daemon"
# The OS-agnostic seam module and its lazy Windows ConPTY backend ship
# next to the scripts so both find pi_platform.py on sys.path.
install_to 644 "$repo_root/pi/lib/pi_platform.py" "$dest_platform"
install_to 644 "$repo_root/pi/lib/pi_conpty.py" "$dest_conpty"
install_to 644 "$repo_root/pi/lib/pi_settings.py" "$dest_settings"

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

owned=("$dest_daemon" "$dest_helper" "$dest_helper_cmd" "$dest_wrapper" "$dest_wrapper_cmd" "$dest_unit" "$dest_platform" "$dest_conpty" "$dest_settings")
if [[ $package_mode -eq 0 ]]; then
  owned=("$dest_daemon" "$dest_helper" "$dest_helper_cmd" "$dest_wrapper" "$dest_wrapper_cmd" "$dest_extension" "$dest_offload" "$dest_unit" "$dest_platform" "$dest_conpty" "$dest_settings")
fi
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
echo "  pi wrap:   $dest_wrapper"
echo "  pi cmd:    $dest_wrapper_cmd"
if [[ $package_mode -eq 0 ]]; then
  echo "  extension: $dest_extension"
  echo "  offload:   $dest_offload"
fi
echo "  unit:      $dest_unit"
echo "  manifest:  $manifest"
echo
echo "Usage: pi-rc start [name] [dir] [--fresh]; attach with: pi-rc attach [name] [dir]"
echo "Inside a hosted pi: /bg detaches it instantly; the pi keeps running."
echo "Outside hosting: /bg hands the session over; reattach with pi-rc attach."
