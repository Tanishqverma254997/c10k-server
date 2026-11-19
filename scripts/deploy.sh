#!/usr/bin/env bash
# Run on the existing Linux Docker host: sudo bash scripts/deploy.sh
set -Eeuo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
# The VM checkout belongs to azureuser even when this script runs through sudo.
revision="$(git -c safe.directory="$repo_dir" rev-parse --short=12 HEAD)"
image="c10k-server:$revision"
nofile_limit="${C10K_NOFILE_LIMIT:-65536}"
if [[ ! "$nofile_limit" =~ ^[0-9]+$ ]]; then
  echo 'C10K_NOFILE_LIMIT must be a positive integer.' >&2
  exit 1
fi
candidate="c10k-candidate-$$"
previous_name=""
deployment_complete=false
new_created=false

finish() {
  result=$?
  trap - EXIT
  docker rm -f "$candidate" >/dev/null 2>&1 || true
  if ! "$deployment_complete"; then
    if "$new_created"; then docker rm -f c10k-server >/dev/null 2>&1 || true; fi
    if [[ -n "$previous_name" ]]; then
      echo "Restoring the previous backend..." >&2
      docker rename "$previous_name" c10k-server
      docker start c10k-server >/dev/null
    fi
  fi
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

verify() {
  docker exec "$1" python -c '
import json, time, urllib.request
for attempt in range(10):
    try:
        base = "http://127.0.0.1:8080"
        assert urllib.request.urlopen(base + "/health", timeout=2).read() == b"OK\n"
        page = urllib.request.urlopen(base + "/", timeout=2).read()
        assert b"One process." in page and b"/assets/app.js" in page
        for path in ("/assets/app.js", "/assets/app.css", "/favicon.svg"):
            assert urllib.request.urlopen(base + path, timeout=2).status == 200
        status = json.load(urllib.request.urlopen(base + "/status", timeout=2))
        assert status["status"] == "ok" and "uptime_seconds" in status
        break
    except Exception:
        if attempt == 9: raise
        time.sleep(1)
'
}

echo "Running regression tests before building $image..."
python3 -m unittest discover -s tests -v
docker build --label "org.opencontainers.image.revision=$(git -c safe.directory="$repo_dir" rev-parse HEAD)" -t "$image" .

# Validate the candidate without occupying the live backend port.
docker run -d --name "$candidate" --network none \
  --ulimit "nofile=$nofile_limit:$nofile_limit" "$image" >/dev/null
verify "$candidate"

if docker container inspect c10k-server >/dev/null 2>&1; then
  backup="c10k-server-backup-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  docker stop --time 10 c10k-server >/dev/null
  if ! docker rename c10k-server "$backup"; then
    docker start c10k-server >/dev/null
    exit 1
  fi
  previous_name="$backup"
fi

# Caddy continues running and keeps its certificate volumes throughout.
new_created=true
docker run -d --name c10k-server --restart unless-stopped \
  --ulimit "nofile=$nofile_limit:$nofile_limit" \
  --log-opt max-size=10m --log-opt max-file=3 \
  -p 127.0.0.1:8080:8080 "$image" >/dev/null
verify c10k-server
deployment_complete=true
echo "Backend deployed: $image"
if [[ -n "$previous_name" ]]; then echo "Stopped rollback container retained: $previous_name"; fi
echo "Caddy configuration and certificate volumes were not changed."
