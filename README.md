# c10k-server: High-Throughput HTTP Server from Scratch

A zero-dependency, high-concurrency HTTP/1.1 server built entirely with Python standard library sockets and Linux I/O multiplexing. Built to solve the classic C10K problem from first principles, this single-threaded server handles over 54,000 requests per second while concurrently maintaining 10,000 live persistent TCP connections using Linux `epoll`, non-blocking I/O, per-connection stream buffering, and HTTP keep-alive.

---

## Why This Project Exists: The C10K Problem

In 1999, Dan Kegel framed the **C10K problem**: how can a web server handle 10,000 simultaneous clients on a single commodity machine?

The naive modern intuition is often **multi-threading** (spawning one thread per connection). However, thread-per-connection hits an immovable architectural wall at 10,000 connections:
1. **Memory Exhaustion**: The default thread stack size on Linux is ~8 MB. Allocating stacks for 10,000 threads consumes roughly **~78 GB of RAM** just for thread stack space alone, causing immediate out-of-memory (OOM) crashes.
2. **CPU Context-Switching Thrashing**: When the operating system scheduler has to constantly juggle CPU register states across 10,000 threads, the CPU spends virtually all its time switching context rather than processing network bytes.
3. **OS Thread Limits**: Systems enforce limits on maximum threads per user (`ulimit -u`), causing `pthread_create` to fail.

To handle 10,000+ concurrent connections on a single machine, we decouple connections from threads. Instead of thousands of threads, we use **a single thread** paired with non-blocking sockets and operating system event multiplexing (`epoll`), allowing the OS kernel to wake our process only when a socket actually has data ready.

---

## Architecture

The server is built around the **Reactor pattern** using Linux's native `epoll` engine (exposed via Python's standard `selectors` module):

```
                        +------------------------------------------+
                        |               Linux Kernel               |
                        |            (epoll multiplexer)           |
                        +------------------------------------------+
                                  |                      ^
               Kernel wakes loop  |                      | Registers ready
             with ready events    |                      | sockets (READ/WRITE)
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
- **`selectors.DefaultSelector()` (Linux `epoll`)**: Puts the process to sleep with **0% CPU** when idle. When network events occur, the kernel returns only the active sockets in $O(1)$ time.
- **Connection Draining (`accept_connection`)**: Employs an eager `while True: accept()` loop whenever the listening socket is readable to drain incoming connection bursts without letting the kernel backlog overflow.
- **Stream Buffering (`in_buffer`)**: TCP delivers an arbitrary stream of bytes, not discrete messages. Incoming bytes are buffered per connection until the HTTP header delimiter (`\r\n\r\n`) is detected.
- **Non-Blocking Transmit (`out_buffer`)**: Instead of blocking `sendall()`, uses non-blocking `send()`. If the OS socket transmit buffer fills up, the remainder is buffered in `out_buffer` and registered for `EVENT_WRITE` until empty.
- **HTTP/1.1 Keep-Alive**: Reuses existing TCP handshakes across multiple requests on the same connection, avoiding expensive 3-way handshakes and socket `TIME_WAIT` churn.
- **OS Limit Tuning**: Programmatically raises the process open file descriptor limit (`RLIMIT_NOFILE`) to 65,536 and expands the TCP backlog to `SOMAXCONN`.

---

## Requirements

- **Operating System**: Linux or Windows 11 with WSL2 (Ubuntu)
- **Python**: Python 3.11+ (developed and verified on Python 3.14)
- **Dependencies**: **None** (100% Python standard library: `socket`, `selectors`, `resource`, `time`)
- **Benchmarking Tools** (Optional for testing): `wrk` (`sudo apt install wrk`)

---

## Project Structure

```text
c10k-server/
??? server.py           # High-throughput epoll HTTP server implementation
??? benchmark_10k.py    # Non-blocking 10,000 concurrent connection test harness
??? NOTES.md            # Detailed step-by-step learning notes and systems theory
??? README.md           # Project architecture, benchmarks, and documentation
??? .gitignore          # Ignores .venv, __pycache__, and .env
```

---

## Setup and Run Steps

### 1. Clone & Enter Project Directory
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
```

### 5. Run the 10,000 Connection Test Harness
```bash
python benchmark_10k.py
```
*Launches 10,000 concurrent non-blocking TCP handshakes, verifies connection integrity via `SO_ERROR`, and holds all 10,000 sockets open simultaneously for 10 seconds.*

### 6. Run the `wrk` Load Benchmark
```bash
# Ensure client shell allows opening 10,000+ socket file descriptors
ulimit -n 65536
wrk -t 4 -c 10000 -d 30s --latency http://127.0.0.1:8080/
```

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

At first glance, an average latency of **182.16 ms** might seem high for a simple HTTP response whose Python execution time is only ~15 microseconds ($0.015\text{ ms}$). However, this is directly explained by **Little's Law** from queueing theory:

$$L = \lambda \times W$$

Where:
- $L$ = Number of concurrent requests in the system ($10,000$)
- $\lambda$ = System throughput in requests per second ($54,421.13\text{ req/s}$)
- $W$ = Average time each request spends in the system (Average Latency)

Solving for average response latency $W$:

$$W = \frac{L}{\lambda} = \frac{10,000}{54,421.13\text{ req/s}} \approx 0.1837\text{ seconds} \approx 183.7\text{ ms}$$

Our empirically measured average latency of **182.16 ms** matches theoretical Little's Law to within 1%. 

This reveals a fundamental systems insight: **the measured 182 ms is virtually 100% queueing delay**. Because 10,000 requests are submitted concurrently to a single CPU thread, each request waits in line while preceding requests in the batch are processed. The absence of tail latency jitter (p99 is 208 ms, standard deviation is only 9 ms) demonstrates smooth, predictable event loop scheduling with zero connection starvation.

---

## Known Limitations

The current `server.py` implementation is an educational, minimalist baseline. It intentionally omits several production features:
1. **No Timeouts**: Sockets do not track idle or read/write timestamps. Slow or stalled clients (e.g. Slowloris attacks) can hold connections open indefinitely.
2. **No Header Size Limit**: Incoming headers are accumulated in `in_buffer` until `\r\n\r\n` is encountered. A client streaming endless headers without a boundary will cause unbounded buffer memory growth.
3. **No Connection Cap**: The server accepts all incoming TCP handshakes without enforcing a maximum active connection ceiling (`max_connections`), which could lead to resource exhaustion under extreme connection storms.
4. **No Request Body Parsing**: Only HTTP headers are parsed. Payloads sent via `POST` or `PUT` methods (`Content-Length` or `Transfer-Encoding: chunked`) are not read or consumed.
5. **No Graceful Shutdown**: Halting the process (`SIGINT` / `Ctrl+C`) terminates immediately without draining active client buffers or allowing in-flight responses to finish transmitting.

---

## Roadmap

- [ ] **Phase 1: Hardening**
  - Implement per-connection idle timeouts and Slowloris mitigation
  - Enforce maximum header size limit (e.g. 8 KB max) with HTTP 431 / 400 responses
  - Add active connection limit cap (`MAX_CONNECTIONS`)
- [ ] **Phase 2: Protocol Completeness**
  - Parse `Content-Length` and stream request bodies
  - Support chunked transfer encoding
  - Basic URL routing table and static file serving
- [ ] **Phase 3: Multi-Core Scaling**
  - Prefork multi-worker architecture utilizing Linux `SO_REUSEPORT` across all available CPU cores
