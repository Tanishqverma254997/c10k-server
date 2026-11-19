# Azure deployment and measured baseline

## Verified deployment

- Host: Azure VM, Standard B2ts_v2 (2 vCPUs, 1 GiB RAM), Central India.
- OS: Ubuntu Server 24.04 LTS, x64; Standard SSD OS disk.
- Application source used for the verified deployment: commit `fe289b92dcc11eb0640b305347dc99278452748a` on `fix/deployment-readiness`.
- Application image: locally built `c10k-server:v1`.
- Public endpoint: https://tanishq-c10k-server.centralindia.cloudapp.azure.com/health
- Azure inbound rules: TCP 22 (SSH), 80 (HTTP), 443 (HTTPS).
- Backend container: `c10k-server`, published only at `127.0.0.1:8080`.
- HTTPS proxy: `c10k-caddy`, using `caddy:2` with host networking.
- Caddy configuration is mounted from `~/c10k-server/Caddyfile`.
- Certificate and configuration state persist in Docker volumes `c10k-caddy-data` and `c10k-caddy-config`.
- Both containers use `--restart unless-stopped` and log rotation (10 MiB per file, three files).
- Six socket-level regression tests passed on the VM. The backend container reported healthy; public HTTPS returned HTTP 200 with body `OK\n`.

These checks establish a working deployment and a short local concurrency baseline. They do not establish full HTTP compliance, sustained capacity, or 10,000 concurrent public TLS connections.

## Reproduce on a fresh Ubuntu VM

These commands assume fresh container names and the working directory shown. They are not an in-place upgrade script: on an existing deployment, inspect running containers first and retain the current image and certificate volumes for rollback.

Install Docker, Git, and the load generator:

```bash
sudo apt-get update &&
sudo apt-get install -y docker.io git wrk
sudo systemctl enable --now docker
git clone --branch fix/deployment-readiness https://github.com/Tanishqverma254997/c10k-server.git
cd c10k-server
python3 -m unittest discover -s tests -v
sudo docker build -t c10k-server:v1 .
```

For the same application source as the measured baseline, check out the source commit above before building. Python and Caddy image tags are mutable; the measurements do not imply an identical base-image digest on future builds.

### Host connection tracking

This VM initially had `net.netfilter.nf_conntrack_max=7168`. During high-concurrency tests its kernel logged `nf_conntrack: table full, dropping packet`. Increasing the limit allowed the subsequent 10,000-connection test to complete with a successful live connection-count probe.

The following was applied and saved on the deployed VM:

```bash
echo 'net.netfilter.nf_conntrack_max=65536' | sudo tee /etc/sysctl.d/99-c10k.conf
sudo sysctl -p /etc/sysctl.d/99-c10k.conf
```

For a fresh machine, ensure the module is available before loading this sysctl. If the module otherwise loads only when Docker starts, arrange for it to load before boot-time sysctl processing:

```bash
echo nf_conntrack | sudo tee /etc/modules-load.d/c10k.conf
sudo modprobe nf_conntrack
sudo sysctl -p /etc/sysctl.d/99-c10k.conf
```

The increased maximum permits more tracking entries; it does not preallocate all entries. Track memory use as concurrency increases. Persistence after a reboot has not yet been verified on this VM.

### Backend container

```bash
sudo docker run -d --name c10k-server --restart unless-stopped \
  --ulimit nofile=65536:65536 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -p 127.0.0.1:8080:8080 c10k-server:v1

curl --retry 5 --retry-connrefused --retry-delay 1 http://127.0.0.1:8080/health
```

Run the local URL from an SSH session on the VM. In a laptop browser, `127.0.0.1` addresses the laptop.

### Caddy and HTTPS

Point the hostname's DNS record at the VM's public IP and allow TCP ports 80 and 443 in Azure before starting Caddy. For another deployment, replace the hostname below with your own.

```bash
cat > ~/c10k-server/Caddyfile <<'EOF'
tanishq-c10k-server.centralindia.cloudapp.azure.com {
    reverse_proxy 127.0.0.1:8080
}
EOF

sudo docker pull caddy:2
sudo docker run --rm \
  -v "$HOME/c10k-server/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

sudo docker run -d --name c10k-caddy --restart unless-stopped \
  --network host \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v "$HOME/c10k-server/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v c10k-caddy-data:/data \
  -v c10k-caddy-config:/config \
  caddy:2
```

Caddy uses the host network so its upstream `127.0.0.1:8080` reaches the backend's host-side loopback binding. Do not change the proxy to bridge networking without also changing its upstream. Host networking also means Caddy's admin endpoint must stay on loopback; do not expose port 2019 publicly.

Do not delete the Caddy data volume when updating the proxy; it holds certificate state. Restrict SSH to the administrator's source IP where practical, retaining a working session until the revised rule is verified.

## Verified 10,000-connection baseline

The user ran `wrk` on the same VM as the backend, targeting the backend's internal Docker IP. The test bypassed Caddy, TLS, and Docker's published loopback port. It ran for 10 seconds using two client threads.

| Measurement | Observed |
| --- | ---: |
| Requested benchmark connections | 10,000 |
| Active connections reported during the test | 10,001 |
| Completed requests | 298,147 |
| Elapsed time | 10.02 s |
| Requests per second | 29,746.23 |
| Average latency | 319.20 ms |
| Median latency | 323.04 ms |
| 99th percentile latency | 420.43 ms |
| Maximum latency | 447.77 ms |
| Reported socket errors or non-success responses | None shown |

The count of 10,001 includes the separate `/status` request. It confirms 10,000 benchmark connections were active at that sample; it does not prove that count was sustained throughout the run.

Before raising conntrack capacity, a 10,000-connection run logged the 7,000 milestone but no 8,000 milestone, and a new status connection timed out. Kernel logs confirmed connection-tracking exhaustion. Earlier failures through the published port cannot be attributed solely to Docker port forwarding: those runs also occurred before the host limit was corrected.

Reproduce the verified test from the VM's SSH terminal:

```bash
server_ip=$(sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' c10k-server)
(
  ulimit -n 30000 &&
  wrk -t2 -c10000 -d10s --latency "http://$server_ip:8080/health"
) &
load_pid=$!
sleep 3
curl --max-time 5 "http://$server_ip:8080/status"
wait "$load_pid"
```

Retrieve the IP for each run rather than hardcoding it: Docker may assign a different address after recreation. Check the status probe, error counters, and post-test health as well as throughput. A clean `wrk` error summary by itself does not prove all requested connections were established.

## Operational checks and follow-up work

### Update the existing VM to the dashboard release

The root route now serves a self-contained dashboard; no frontend build or JavaScript runtime is required on the VM. The Python process preloads a fixed allowlist of HTML, CSS, JavaScript, and SVG files, and Docker includes these assets. The API routes remain available. The dashboard fetches same-origin status and echo responses; it never starts a load test from a visitor's browser.

From the VM's existing SSH session:

```bash
cd ~/c10k-server
git fetch origin
git switch main
git pull --ff-only origin main
sudo bash scripts/deploy.sh
```

The deployment script runs Python regression tests, builds an image tagged with the source revision, and verifies a candidate container with no external network access. Only then does it briefly stop and replace the backend on loopback port 8080. It verifies the new container and restores the previous backend automatically if the replacement fails. Caddy and its certificate volumes continue running. The script uses the known settings of this deployment; adapt it before using on a host with different environment variables, ports, mounts, or resource limits. Do not run two updates concurrently.

Successful updates retain the previous stopped container and its image for manual rollback. Each update can retain another backup; inspect and remove obsolete backups once the release is verified. The script does not change host sysctls or Azure resources, and has not yet been executed on the user's Azure VM.

The script defaults to a 65,536 file-descriptor limit, as verified on this Azure VM. For development hosts with a lower hard limit, set `C10K_NOFILE_LIMIT` to a supported value when running the script. The workspace container smoke test used 16,384; that override does not change the Azure default.

Open the existing HTTPS hostname at `/` after updating. `/health` still returns plain text. The root route previously returned a text greeting; it now returns HTML. `/status` adds real `peak_connections` and `uptime_seconds` fields. All process metrics reset when the backend restarts, so the live peak is distinct from the recorded benchmark.

Development checks (Node.js is only needed for JavaScript checks):

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_frontend.cjs
```

The dashboard handles loading, server failures, timeouts, UTF-8 request limits, clipboard failures, keyboard navigation, reduced motion, and narrow layouts. Its static asset routes do not allow arbitrary file paths. Echo output is rendered as text. JavaScript is self-hosted and governed by a same-origin Content Security Policy. Browser visual verification depends on an available browser QA environment.

```bash
sudo docker ps
sudo docker logs --tail 40 c10k-server
sudo docker logs --tail 40 c10k-caddy
sudo sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
curl --max-time 5 http://127.0.0.1:8080/health
```

Next validation and improvements:

1. Verify the sysctl and container recovery after a planned reboot. Check that `nf_conntrack` loads early enough for the saved setting to apply.
2. Capture sustained resource use, kernel drop-counter deltas, and the effects of burstable VM CPU credits. The client and server currently compete for the same VM resources.
3. Measure public HTTPS from an independent client; keep those results separate from direct-backend measurements.
4. Add peak connection counts and request/error metrics so future benchmarks do not depend on screenshots or log milestones.
5. Convert the manually operated deployment into versioned automation with a tested rollback path.
6. Monitor Azure credit consumption. VM shutdown does not eliminate disk and public-IP costs. Auto-shutdown was initially configured but was later shown switched off; verify its current saved state before relying on it.

No 10,000-connection memory peak, sustained-load capacity, reboot verification, or independent-client HTTPS load result has been recorded yet.
