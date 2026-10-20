# c10k-server: High-Throughput HTTP Server from Scratch

A zero-dependency, high-concurrency HTTP/1.1 server built entirely with Python standard library sockets and Linux I/O multiplexing. Built to solve the classic C10K problem from first principles, this single-threaded server handles over 54,000 requests per second while concurrently maintaining 10,000 live persistent TCP connections using Linux epoll, non-blocking I/O, per-connection stream buffering, and HTTP keep-alive.

---

## Why This Project Exists: The C10K Problem

In 1999, Dan Kegel framed the **C10K problem**: how can a web server handle 10,000 simultaneous clients on a single commodity machine?

The naive modern intuition is often **multi-threading** (spawning one thread per connection). While the default ~8 MB stack per thread on Linux is reserved virtual address space rather than immediately committed physical memory (only touched pages consume real RAM), thread-per-connection fails at 10,000 connections due to:
1. **OS Thread Limits**: Operating systems enforce hard caps on thread creation (`/proc/sys/kernel/threads-max` and `ulimit -u`), causing `pthread_create` calls to fail once limits are reached.
2. **Scheduler Overhead & Context Switching**: When the operating system scheduler must juggle execution states across 10,000 threads, the CPU wastes immense time saving and restoring registers and thrashing CPU caches instead of processing network bytes.
3. **Kernel Memory Overhead**: Even before physical stack memory fills up, each thread requires kernel structures (task structs, kernel stacks) that place heavy pressure on system resources.

To handle 10,000+ concurrent connections on a single machine, we decouple connections from threads. Instead of thousands of threads, we use **a single thread** paired with non-blocking sockets and operating system event multiplexing (`epoll`), allowing the OS kernel to wake our process only when a socket actually has data ready.

---

## Architecture

The server is built around the **Reactor pattern** using Linux native epoll engine (exposed via Python standard `selectors` module):

```
                        +------------------------------------------+
                        |               Linux Kernel               |
                        |            (epoll multiplexer)           |
                        +------------------------------------------+
                                  |                      ^
                  Kernel returns  |                      | Process registers
              only ready sockets  |                      | interest (READ/WRITE)
                                  v                      |
                        +------------------------------------------+
                        |        Single-Threaded Event Loop        |
                        |            selector.select()             |
                        +------------------------------------------+
                                  |                      |
            Ready for READ        |                      | Ready for READ / WRITE
            (Listening socket)    |                      | (Client socket)
                                  v                      v
                       +--------------------+  +--------------------------------+
                       | accept_connection  |  | ClientConnection State Machine |
                       |--------------------|  |--------------------------------|
                       | while True:        |  | in_buffer (accumulates \r\n\r\n)|
                       |   sock.accept()    |  | out_buffer (safe non-blocking) |
                       |   TCP_NODELAY = 1  |  | handle_read() / handle_write() |
                       |   register(READ)   |  | keep_alive: loop on socket     |
                       +--------------------+  +--------------------------------+
```

### Core Architecture Components
- **`selectors.DefaultSelector()` (Linux `epoll`)**: Puts the process to sleep with **0% CPU** when idle. When network events occur, the kernel returns only the active sockets; its cost scales with the number of ready sockets, not total registered sockets.
- **Connection Draining (`accept_connection`)**: Employs an eager `while True: accept()` loop whenever the listening socket is readable to drain incoming connection bursts without letting the kernel backlog overflow.
- **Stream Buffering (`in_buffer`)**: TCP delivers an arbitrary stream of bytes, not discrete messages. Incoming bytes are buffered per connection until the HTTP header delimiter (`\r\n\r\n`) is detected.
- **Non-Blocking Transmit (`out_buffer`)**: Instead of blocking `sendall()`, uses non-blocking `send()`. If the OS socket transmit buffer fills up, the remainder is buffered in `out_buffer` and registered for `EVENT_WRITE` until empty.
- **HTTP/1.1 Keep-Alive**: Reuses existing TCP handshakes across multiple requests on the same connection, avoiding expensive 3-way handshakes and socket `TIME_WAIT` churn.
- **OS Limit Tuning**: Programmatically raises the process open file descriptor limit (`RLIMIT_NOFILE`) to 65,536 and expands the TCP backlog to `SOMAXCONN`.

---

## Requirements

- **Operating System**: Linux or Windows 11 with WSL2 (Ubuntu)
- **Python**: Python 3.11+ (developed and verified on Python 3.14)
- **Dependencies**: **None** (100% Python standard library: `socket`, `selectors`, `resource`, `time`, `signal`, `sys`, `os`)
- **Benchmarking Tools** (Optional for testing): `wrk` (`sudo apt install wrk`)
- **Container Runtime** (Optional for containerized run/deploy): `docker` 20.10+

---

## Project Structure

```text
c10k-server/
|-- server.py           # High-throughput epoll HTTP server implementation
|-- benchmark_10k.py    # Non-blocking 10,000 concurrent connection test harness
|-- Dockerfile          # Hardened Python 3.12-slim non-root container image
|-- .dockerignore       # Build context exclusion rules (.venv, .git, etc.)
|-- render.yaml         # Render Infrastructure-as-Code Blueprint specification
|-- NOTES.md            # Detailed step-by-step learning notes and systems theory
|-- README.md           # Project architecture, benchmarks, and documentation
\-- .gitignore          # Ignores .venv, __pycache__, and .env
```

---

## Setup and Run Steps

### 1. Enter the Project Directory
```bash
cd ~/c10k-server
```

### 2. Set Up Virtual Environment (Optional, no external packages required)
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Start the Server
```bash
python server.py
```
*The server will automatically tune file descriptor limits to 65,536 and begin listening on `http://127.0.0.1:8080`.*

### 4. Verify with a Single Request
In a separate terminal:
```bash
curl -i http://127.0.0.1:8080/
curl -i http://127.0.0.1:8080/health
```

### 5. Run the 10,000 Connection Test Harness
```bash
python benchmark_10k.py
```
*Launches 10,000 concurrent non-blocking TCP handshakes, verifies connection integrity via `SO_ERROR`, and holds all 10,000 sockets open simultaneously for 3 seconds.*

### 6. Run the `wrk` Load Benchmark
```bash
# Ensure client shell allows opening 10,000+ socket file descriptors
ulimit -n 65536
wrk -t 4 -c 10000 -d 30s --latency http://127.0.0.1:8080/
```

---

## Containerization (Docker)

The server includes a production-hardened `Dockerfile` built on `python:3.12-slim`:
- **Unprivileged Execution**: Runs as non-root user `appuser` (fixed numeric UID `10001`) with no login shell.
- **Dynamic Port & Host**: Defaults to `HOST=0.0.0.0` and `PORT=8080`, dynamically overridden by environment variables.
- **Zero-Dependency Health Check**: Standard library Python healthcheck polling `/health` every 30 seconds with a 3-second timeout.
- **Unbuffered Logs**: Configured with `ENV PYTHONUNBUFFERED=1` for real-time stdout event streaming.

### Build and Run with Docker

1. **Build the image:**
   ```bash
   docker build -t c10k-server:1.0 .
   ```

2. **Run container with raised file-descriptor limits:**
   ```bash
   docker run -d --name c10k -p 8080:8080 --ulimit nofile=65536:65536 c10k-server:1.0
   ```

3. **Verify container health and non-root execution:**
   ```bash
   # Check container health status (reports "healthy")
   docker inspect --format '{{.State.Health.Status}}' c10k

   # Verify non-root user UID (returns 10001)
   docker exec c10k id -u

   # Verify file descriptor limit (returns 65536)
   docker exec c10k cat /proc/1/limits | grep "Max open files"
   ```

---

## Deploying to Render (Free Tier)

Render offers a permanent **Free Tier** for Web Services (750 free instance hours/month, 512 MB RAM, 0.1 vCPU) requiring **no credit card** to start.

### How Render Networking & Port Binding Works
- Render automatically injects a dynamic `PORT` environment variable (typically `10000`) into your container.
- `server.py` reads `os.environ.get('PORT', 8080)` and binds to `0.0.0.0:$PORT` dynamically without requiring code modifications.
- Render terminates TLS at its edge proxy and routes public HTTPS traffic to the container port.

---

### Method 1: Infrastructure-as-Code via `render.yaml` Blueprint (Recommended)

The repository includes a ready-to-use [`render.yaml`](render.yaml) specification:

```yaml
services:
  - type: web
    name: c10k-server
    runtime: docker
    plan: free
    region: oregon
    dockerfilePath: ./Dockerfile
    dockerContext: .
    healthCheckPath: /health
    envVars:
      - key: HOST
        value: 0.0.0.0
```

#### Deployment Steps:
1. Push your repository to GitHub or GitLab:
   ```bash
   git add .
   git commit -m "Configure Render Blueprint deployment"
   git push origin main
   ```
2. Log into the [Render Dashboard](https://dashboard.render.com).
3. Click **Blueprints** in the top navigation $\rightarrow$ Click **New Blueprint Instance**.
4. Select your `c10k-server` repository and click **Connect**.
5. Render reads `render.yaml`, configures the Docker runtime, sets `/health` as the deployment health check, and builds the service.
6. Click **Apply**. Render will build and deploy your container.

---

### Method 2: Manual Setup via Render Dashboard

If you prefer deploying manually without Blueprints:
1. Push your code to GitHub or GitLab.
2. Log into [Render Dashboard](https://dashboard.render.com) $\rightarrow$ Click **New +** $\rightarrow$ **Web Service**.
3. Connect your repository.
4. Configure the service settings:
   - **Name**: `c10k-server`
   - **Language / Runtime**: **Docker**
   - **Instance Type**: **Free**
5. Expand **Advanced Settings**:
   - **Health Check Path**: `/health`
   - (Optional) **Environment Variables**: Add `HOST=0.0.0.0` (already set by Dockerfile).
6. Click **Create Web Service**.

---

### Verifying with `curl` Against the Public Render URL

Once Render displays **Deploy live**, verify your public endpoints (replace `<YOUR_APP>` with your Render service name, e.g., `c10k-server-xxxx.onrender.com`):

#### 1. Verify Health Check
```bash
curl -i https://<YOUR_APP>.onrender.com/health
```
**Expected Response:**
```http
HTTP/2 200 
content-type: text/plain; charset=utf-8
content-length: 3

OK
```

#### 2. Verify Root HTTP Endpoint
```bash
curl -i https://<YOUR_APP>.onrender.com/
```
**Expected Response:**
```http
HTTP/2 200 
content-type: text/plain; charset=utf-8

Hello! Request #1 on this socket. (GET / HTTP/1.1)
```

#### 3. Verify HTTP/1.1 Persistent Keep-Alive Connection Reuse
```bash
curl -v https://<YOUR_APP>.onrender.com/ https://<YOUR_APP>.onrender.com/health
```
In the verbose output, verify:
`* Re-using existing connection with host <YOUR_APP>.onrender.com`

---

### Render Free-Tier Limitations & Benchmarking Caveats

When evaluating your server on Render's free tier, keep the following platform constraints in mind:
- **Inactivity Spin-Down (Sleep):** Free web services automatically spin down after **15 minutes of zero incoming HTTP traffic**. The next inbound request will wake the container, resulting in a **30 to 60-second cold start**.
- **No Arbitrary Raw TCP Ports:** Render public web services terminate TLS/TCP at Cloudflare/Envoy edge proxies and only route HTTP/HTTPS and WebSockets over HTTP. Arbitrary raw TCP socket exposure is not available on Render free web services.
- **Proxy Gateway Timeouts:** Cloudflare and Render edge proxies enforce a ~100-second HTTP request timeout limit.
- **Resource Limits:** Free tier instances are allocated 512 MB RAM and 0.1 vCPU, which is sufficient for functional testing but will hit compute ceilings under high-concurrency stress tests.

---

## Benchmark Environment

- **CPU**: `[FILL IN: e.g., AMD Ryzen 7 5800H / Intel Core i7-12700H (X cores / Y threads)]`
- **Memory (RAM)**: `[FILL IN: e.g., 16 GB / 32 GB DDR4/DDR5]`
- **OS / Runtime**: Windows 11 with WSL2 (`[FILL IN: Linux kernel version / Ubuntu version]`)

---

## Benchmark Configuration
- **Tool**: `wrk` 4.1.0 (epoll-based)
- **Target URL**: `http://127.0.0.1:8080/`
- **Duration**: 30.07s
- **Concurrent Connections**: 10,000
- **Client Threads**: 4
- **Process FD Limit (`ulimit -n`)**: 65,536
- **Server Implementation**: Single-threaded Python 3.14 + `selectors` (`epoll`) + HTTP/1.1 Keep-Alive

## Baseline (before hardening)
| Metric | Measured Value |
| :--- | :--- |
| **Total Requests Completed** | 1,636,243 |
| **Throughput (Requests/sec)** | 54,421.13 req/s |
| **Data Transferred** | 243.96 MB |
| **Transfer Rate** | 8.11 MB/s |
| **Average Latency** | 182.16 ms |
| **Latency Standard Deviation** | 9.18 ms |
| **Maximum Latency** | 215.56 ms |
| **50th Percentile (p50) Latency** | 181.74 ms |
| **75th Percentile (p75) Latency** | 184.99 ms |
| **90th Percentile (p90) Latency** | 188.03 ms |
| **99th Percentile (p99) Latency** | 208.55 ms |
| **Connect Errors** | 0 |
| **Read Errors** | 0 |
| **Write Errors** | 0 |
| **Timeout Errors** | 0 |

---

## Reading These Results: Little's Law & Queueing Time

At first glance, an average latency of **182.16 ms** might seem high for a simple HTTP response whose service time is only about 18 microseconds per request (1 / 54,421 req/s), which includes parsing, formatting, and syscalls. However, this is directly explained by **Little's Law** from queueing theory:

$$L = \lambda \times W$$

Where:
- $L$ = Number of concurrent requests in the system ($10,000$)
- $\lambda$ = System throughput in requests per second ($54,421.13\text{ req/s}$)
- $W$ = Average time each request spends in the system (Average Latency)

Solving for average response latency $W$:

$$W = \frac{L}{\lambda} = \frac{10,000}{54,421.13\text{ req/s}} \approx 0.1837\text{ seconds} \approx 183.7\text{ ms}$$

Our empirically measured average latency of **182.16 ms** matches theoretical Little's Law to within 1%. 

This reveals a fundamental systems insight: **the measured 182 ms is almost entirely queueing delay**. Because 10,000 requests are submitted concurrently to a single CPU thread, each request waits in line while preceding requests in the batch are processed. The absence of tail latency jitter (p99 is 208 ms, standard deviation is only 9 ms) demonstrates smooth, predictable event loop scheduling with 0 timeouts and 0 errors during the run.

---

## Benchmark Caveats

While the benchmark validates the architectural capability of holding and serving 10,000 concurrent connections, several real-world factors should be noted:
- **Shared Machine Resources**: Both `wrk` and the Python server ran on the same host machine, competing for the same underlying CPU cores, caches, and memory bandwidth.
- **Loopback Interface Traffic**: Tests were conducted over `127.0.0.1`, which bypasses physical network hardware, NIC interrupts, cable latency, and real-world packet drops.
- **Minimal Static Payload**: The server returns a lightweight static response (~156 bytes), minimizing memory allocation, serialization, and TCP fragmentation overhead.
- **Closed-Loop Load Generation**: `wrk` is a closed-loop load generator (each client connection waits to receive a response before issuing its next request). Closed-loop benchmarks mask queueing spikes compared to open-loop Poisson arrival traffic, making measured tail latencies (p99) optimistic.
- **WSL2 Virtualization Overhead**: Running Linux within WSL2 introduces Hyper-V virtualization and Windows-to-WSL network translation overhead compared to bare-metal Linux.

---

## Known Limitations

The current `server.py` implementation is an educational, minimalist baseline. It intentionally omits several production features:
1. **No Chunked Transfer Encoding**: Only fixed-length request bodies specified by `Content-Length` are parsed (`Transfer-Encoding: chunked` streaming is not yet supported).
2. **Single Core Only**: Runs on a single process and thread, utilizing at most one CPU core and leaving remaining cores idle.

---

## Roadmap

- [x] **Phase 1: Hardening**
  - Implement per-connection idle timeouts and Slowloris mitigation
  - Enforce maximum header size limit (8 KB max) with HTTP 431 / 400 responses
  - Add active connection limit cap (`MAX_CONNECTIONS = 15000`) with graceful 503 rejection
  - Request body parsing with `Content-Length` framing and `/echo` route
  - Graceful shutdown draining buffers on `SIGINT` / `SIGTERM`
- [x] **Containerization & Cloud Deployment**
  - Zero-dependency hardened `Dockerfile` with non-root UID 10001
  - Docker internal health checks and file descriptor tuning
  - Render Infrastructure-as-Code Blueprint (`render.yaml`) deployment
- [ ] **Phase 2: Protocol Extensions**
  - Support chunked transfer encoding (`Transfer-Encoding: chunked`)
  - Static file serving using non-blocking zero-copy operations (`sendfile`)
- [ ] **Phase 3: Multi-Core Scaling**
  - Prefork multi-worker architecture utilizing Linux `SO_REUSEPORT` across all available CPU cores
