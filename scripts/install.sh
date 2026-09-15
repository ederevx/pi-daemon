#!/usr/bin/env bash
# Install the Pi background service from this repo into the Pi agent home.
#
# Copies the rc-background extension, the pi-rc launcher, the session-host
# tmux config, and the systemd user unit, recording every owned file in a
# manifest so uninstall removes exactly what this repo installed. Idempotent:
# re-running refreshes owned copies in place. Existing unrelated files are
# never touched.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pi_home="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
local_bin="$HOME/.local/bin"
config_dir="$HOME/.config/pi-background-service"
systemd_dir="$HOME/.config/systemd/user"
state_dir="$pi_home/.pi-background-service"
manifest="$state_dir/manifest.json"

[[ -d "$pi_home" ]] || { echo "install: missing Pi agent home: $pi_home" >&2; exit 1; }

dest_extension="$pi_home/extensions/rc-background.ts"
dest_helper="$local_bin/pi-rc"
dest_conf="$config_dir/tmux.conf"
dest_unit="$systemd_dir/pi-background-service.service"
dest_wrapper="$local_bin/pi"

mkdir -p "$pi_home/extensions" "$local_bin" "$config_dir" "$systemd_dir" "$state_dir"

install -m 644 "$repo_root/pi/extensions/rc-background.ts" "$dest_extension"
install -m 755 "$repo_root/pi/bin/pi-rc" "$dest_helper"
install -m 644 "$repo_root/pi/tmux/pi-rc.conf" "$dest_conf"
install -m 644 "$repo_root/pi/systemd/pi-background-service.service" "$dest_unit"

# The pi wrapper must point at the real pi binary. Resolve it by scanning
# PATH while skipping the wrapper's own directory, so an already-installed
# wrapper can never be mistaken for the real binary.
real_pi=""
old_ifs="$IFS"; IFS=:
for dir in $PATH; do
  [[ "$dir" == "$local_bin" ]] && continue
  if [[ -x "$dir/pi" ]]; then real_pi="$dir/pi"; break; fi
done
IFS="$old_ifs"
[[ -n "$real_pi" ]] || {
  echo "install: cannot resolve the real pi binary for the wrapper" >&2
  exit 1
}
sed "s|@REAL_PI@|$real_pi|" "$repo_root/pi/bin/pi-wrapper" > "$dest_wrapper"
chmod 755 "$dest_wrapper"

owned=("$dest_extension" "$dest_helper" "$dest_conf" "$dest_unit" "$dest_wrapper")
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
systemctl --user enable --now pi-background-service.service

echo "install: ok"
echo "  extension: $dest_extension"
echo "  launcher:  $dest_helper"
echo "  pi wrap:   $dest_wrapper"
echo "  tmux conf: $dest_conf"
echo "  unit:      $dest_unit"
echo "  manifest:  $manifest"
echo
echo "Usage: pi-rc start [name] [dir] [--fresh]; attach with: pi-rc attach [name]"
echo "Inside a hosted pi: /bg backgrounds it; the pi keeps running."
