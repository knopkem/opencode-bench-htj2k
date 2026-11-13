#!/usr/bin/env bash
set -euo pipefail

mkdir -p /home/agent /home/agent/.config

# Keep the mounted config read-only on the host, but give OpenCode a writable
# XDG config directory inside the container. The harness mounts the user's
# OpenCode config directory at /config/opencode.
if [ -d /config/opencode ]; then
  rm -rf /home/agent/.config/opencode
  cp -a /config/opencode /home/agent/.config/opencode
fi
mkdir -p /home/agent/.local/share/opencode

exec "$@"
