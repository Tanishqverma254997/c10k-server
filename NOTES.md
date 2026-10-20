# C10K Server: Learning Notes

## Step 1: Sockets and the Anatomy of an HTTP Request
- **What is a Socket?** A socket is the operating system's software endpoint for network communication (like a telephone handset and jack).
- **IP & Port:** The IP address is the building's street address; the Port is the specific apartment number (e.g., Port 8080).
- **TCP vs HTTP:**
  - **TCP (Transport Layer):** Guarantees reliable, ordered byte-stream delivery between two endpoints. Think of it as the established phone call connection.
  - **HTTP (Application Layer):** The structured plain-text protocol spoken across the TCP stream (Request line, Headers, Blank line separator `\r\n\r\n`, and Body).
- **The Core Socket Lifecycle:**
  1. `socket()`: Create the network endpoint.
  2. `bind()`: Associate the socket with an IP address and port.
  3. `listen()`: Put the socket into listening mode (ready to accept incoming connections).
  4. `accept()`: Block and wait for a client to connect; returns a new socket dedicated to that client.
  5. `recv()` / `sendall()`: Read incoming bytes and transmit response bytes.
  6. `close()`: Terminate the connection.
- **Listening Socket vs. Connected Socket (The Front Desk Analogy):**
  - `server_sock` (Listening Socket): The hotel front desk receptionist. Stays at the front desk (Port 8080) to greet new visitors.
  - `client_sock` (Connected Socket): The private concierge assigned to one specific visitor. Used for all subsequent back-and-forth communication so the front desk is never abandoned.

## Step 2: The Blocking Problem (Head-of-Line Blocking)
- **Single Thread of Execution:** In standard sequential code, Python runs one line at a time.
- **The Trap:** While Python is paused on `recv()` or waiting on I/O for `client_sock`, the execution pointer has NOT returned to `server_sock.accept()`.
- **Head-of-Line Blocking:** If Client 1 is slow (or takes 5 seconds to process), the kernel completes the handshake and queues it in the backlog, so Client 2 is connected but not yet accepted by the program.
- **The Core Bottleneck:** A single-threaded blocking server can only serve **1 client at a time**, making 10k concurrent connections completely impossible without concurrency.

## Step 3: Concurrency via Multi-Threading (Thread-per-Connection)
- **The Idea:** Offload each connected client to a separate worker thread (`threading.Thread`).
- **How it solves blocking:** The main thread only does `accept()` and immediately loops back to accept more connections, while background worker threads handle the blocking `recv()`, processing, and `sendall()`.
- **Result:** Multiple clients can be served concurrently at the same time.
- **Testing Concurrency via Bash:** Instead of manually switching terminals, launch parallel requests in the background with `&`:
  `for i in {1..5}; do curl http://127.0.0.1:8080/ & done; wait`
- **The Catch (The C10k Wall):** 
  - While 8 MB per thread on Linux is reserved virtual address space rather than immediately committed physical memory (only touched pages use real memory), thread-per-connection fails at 10k because of OS thread limits, scheduler overhead, and context switching.

## Step 4: Non-Blocking Sockets & Polling
- **`sock.setblocking(False)`:** Tells the OS kernel to never put the thread to sleep when calling `accept()`, `recv()`, or `send()`.
- **`BlockingIOError` (EAGAIN / EWOULDBLOCK):** If no client is ready to be accepted or no bytes have arrived to be read, the kernel immediately raises this error instead of freezing.
- **Single-Threaded Concurrency:** One thread can maintain a list of thousands of sockets, asking each in turn if they have data ready.
- **The Problem with Busy-Polling:** Manually looping over 10,000 sockets in Python asking "are you ready?" is an $O(N)$ operation. 9,999 sockets return `BlockingIOError`, wasting massive CPU cycles every millisecond.

## Step 5: I/O Multiplexing & Epoll (Event-Driven Architecture)
- **The Analogy:** Instead of a teacher walking down every aisle asking each student "Do you have a question?", the teacher tells students: "Raise your hand, and the class monitor (OS kernel) will wake me up and tell me exactly who has their hand raised."
- **`epoll` on Linux:** Linux's event notification mechanism. Instead of scanning all 10,000 sockets, `epoll_wait` sleeps with **0% CPU** until an event occurs, and returns **only the sockets that are ready** (cost scales with the number of ready sockets, not total registered sockets).
- **Python's `selectors` Module:** Standard library wrapper around the OS's best polling mechanism (`selectors.EpollSelector` on Linux, `KqueueSelector` on macOS).
- **The Event Loop Pattern:**
  1. Register sockets with the selector for specific events (`EVENT_READ` / `EVENT_WRITE`) along with a callback function.
  2. `events = selector.select()`: OS wakes the thread up when events occur.
  3. Dispatch callbacks for only the ready sockets.

## Step 6: The TCP Stream Nature & Per-Connection State
- **TCP is a Stream, Not Packets:** TCP provides a continuous stream of bytes without message boundaries (like water from a garden hose, not letters in envelopes).
- **The Read Buffer Problem:** A single `recv()` might return only half of an HTTP request, or multiple requests stuck together. A server must buffer incoming bytes (`in_buffer`) until the protocol boundary delimiter `\r\n\r\n` is detected.
- **The Non-Blocking Write Problem:** `sendall()` can block or crash if the OS TCP send buffer is full. Production servers use non-blocking `send()`, an `out_buffer`, and register for `EVENT_WRITE` when the buffer cannot be emptied in one go.
- **Connection State Machine:** Each connection needs its own state object managing its buffers and event registrations.

## Step 7: OS Kernel Tuning for C10K
- **"Everything is a File" & File Descriptors (FDs):** Linux represents each socket as an integer FD.
- **`RLIMIT_NOFILE` (`ulimit -n`):** By default, Linux caps each process's open file descriptors to protect against resource leaks. For 10,000 connections, we raise `RLIMIT_NOFILE` using Python's `resource` module up to 65,536.
- **Socket Backlog (`SOMAXCONN`):** The kernel connection queue where TCP handshakes wait before `accept()`. Replacing `listen(128)` with `listen(socket.SOMAXCONN)` prevents dropped connections during connection bursts. Note that the real listen backlog is capped by `net.core.somaxconn` (check it via `sysctl net.core.somaxconn` or `cat /proc/sys/net/core/somaxconn`).
- **`TCP_NODELAY`:** Disables Nagle's algorithm to immediately transmit packets without buffering 40ms delays.

## Step 8: HTTP Keep-Alive (Persistent Connections)
- **The Cost of Connection: Close:** Without Keep-Alive, every single request requires a full TCP 3-way handshake (`SYN`/`SYN-ACK`/`ACK`) and 4-way teardown (`FIN`/`ACK`), wasting round trips and trapping ports in `TIME_WAIT`.
- **Holding Connections Open:** With `Connection: keep-alive`, the TCP socket stays open after sending the HTTP response. The socket is reset to `EVENT_READ` waiting for the client's next request.
- **The C10K Prerequisite:** To achieve 10,000 concurrent active connections, clients must stay connected rather than immediately disconnecting.

## Step 9: 10,000 Idle Connections Held
- **Non-Blocking Client Benchmark:** Built `benchmark_10k.py` using non-blocking client sockets with `connect_ex` and `selectors.DefaultSelector()`.
- **The Result:** The server maintains 10,000 live concurrent TCP connections on a **single Python thread**.
- **Resource Measurement TODO:** [TODO: Measure and record exact resident set size (RSS) memory and idle CPU usage under 10k connections].
- **Connection Draining:** `while True: server_sock.accept()` loop ensures the server accepts all queued handshakes during high-burst connection storms.

## Step 10: Asynchronous Socket Verification & High-Concurrency Load Testing
- **Why Sockets Become Writable on Connect Failure:** Under POSIX networking standards, an asynchronous `connect()` sets the socket to writable when the connection handshake completes *regardless of whether it succeeded or failed*. This prevents event loops from hanging indefinitely.
- **`getsockopt(SOL_SOCKET, SO_ERROR)`:** The standard way to check and verify an asynchronous TCP handshake. If `0`, connection succeeded; otherwise, it returns the error code (e.g. `ECONNREFUSED`).
- **Load Testing with `wrk`:** Evaluated 10,000 concurrent keep-alive connections over 30 seconds. Achieved over **54,000 requests/sec** with 0 errors and 0 timeouts in a 30-second run, confirming the single-threaded epoll server handles massive concurrency.

## Step 11: Little's Law and Benchmark Caveats
- **Little's Law ($L = \lambda \times W$):** Concurrency = Throughput $\times$ Latency ($10,000 \approx 54,421.13\text{ req/s} \times 0.1837\text{ s}$).
- **Queueing Time Under Saturation:** While actual per-request service time is only about 18 microseconds, the measured ~182 ms latency under 10k saturation is almost entirely queueing delay as requests wait in line in the single-threaded event loop.
- **Benchmark Caveats:**
  - **Shared Machine:** Server and load generator (`wrk`) run on the same machine, competing for CPU cores, memory bandwidth, and cache.
  - **Loopback Interface:** `127.0.0.1` traffic bypasses physical network hardware, NIC queues, switch latency, and packet loss.
  - **Tiny Static Response:** The response payload is minimal (~156 bytes), minimizing serialization, memory allocation, and fragmentation overhead.
  - **Closed-Loop Load Generator:** `wrk` is a closed-loop load generator where each connection waits for a response before sending the next request, masking queueing spikes and making tail latency (p99) optimistic compared to open-loop Poisson arrival traffic.

## Hardening Feature 1: Idempotent Connection Teardown
- **Vulnerability/Bug Prevented:** Metric corruption (underflow) and resource leaks via double-closing sockets.
- **The Problem:** In an asynchronous event loop, multiple code paths (e.g., read EOF, write errors, client resets, and periodic timeout sweeps) can independently invoke conn.close() on the same connection instance. Without an idempotency guard, ctive_connections is decremented multiple times, causing connection counters to drift negative and breaking future connection caps (MAX_CONNECTIONS).
- **The Fix:** Add a self.closed boolean guard to ClientConnection. If already set to True, subsequent calls return immediately without modifying server counters or re-closing the socket.

## Hardening Feature 2: Maximum Header Size Limit (HTTP 431)
- **Vulnerability/Attack Prevented:** Unbounded Memory Allocation / Denial of Service (DoS).
- **The Problem:** Without a size limit, an attacker or buggy client can stream an endless sequence of HTTP headers without ever sending the 

 delimiter. Because the server accumulates bytes in in_buffer waiting for the boundary, memory consumption grows without bound until the host crashes via the OOM (Out-of-Memory) killer.
- **The Fix:** Introduce MAX_HEADER_SIZE = 8192 (8 KB). In handle_read(), if len(in_buffer) > MAX_HEADER_SIZE before 

 is found, the server immediately returns 431 Request Header Fields Too Large, sets Connection: close, flushes the error response, and terminates the socket.

## Hardening Feature 3: Idle Timeout (Periodic Event Loop Sweep)
- **Vulnerability/Attack Prevented:** Slowloris attacks, stalled client leaks, and file descriptor exhaustion.
- **The Problem:** Clients can connect and remain completely idle (or send single bytes periodically). Without an idle timeout, dead or malicious connections tie up operating system socket file descriptors indefinitely, starving legitimate incoming traffic.
- **The Fix:**
  - Track self.last_activity = time.time() in ClientConnection and update it whenever data is received (handle_read) or sent (handle_write).
  - Maintain an ctive_clients set of live connections.
  - Set a 1-second timeout on the event loop: selector.select(timeout=1.0).
  - Perform a periodic sweep once per second iterating over list(active_clients); any connection with 
ow - conn.last_activity > IDLE_TIMEOUT is immediately closed and reaped.

## Hardening Feature 4: Max Connections Cap with HTTP 503
- **Vulnerability/Failure Prevented:** Process collapse under connection storms, file descriptor starvation (EMFILE), and ungraceful TCP resets.
- **The Problem:** Without a capacity ceiling, accepting unbounded connections will exhaust operating system file descriptors or RAM, causing the process to crash and dropping active legitimate users without warning.
- **The Fix:**
  - Define MAX_CONNECTIONS = 10000.
  - In ccept_connection(), check if ctive_connections >= MAX_CONNECTIONS.
  - If at capacity, immediately accept the socket off the kernel backlog, transmit 503 Service Unavailable with Retry-After: 5 and Connection: close, and close the socket.
  - This protects the health of established connections while providing a standard, graceful backoff signal to incoming clients.


## Hardening Feature 5: Request Line Validation, Routing (404/405/400), and HTTP/1.0 Handling
- **Vulnerability/Failure Prevented:** Protocol confusion attacks, state de-synchronization, resource hoarding by legacy clients, and silent failure on unhandled routes or methods.
- **The Problem:**
  - The naive server responded `200 OK` to *every* request line, even non-existent paths, unsupported HTTP verbs (`POST`, `DELETE`), malformed garbage, or unsupported protocol versions (`HTTP/2.0`).
  - It assumed HTTP/1.1 persistent connections (`keep-alive`) for all clients. An HTTP/1.0 client (which by specification defaults to one-shot requests without keep-alive) would leave the connection dangling open until the idle timeout reaped it, wasting file descriptors and memory.
- **The Fix:**
  - **Request-Line Validation (HTTP 400):** Split the first line into `METHOD PATH VERSION`. If it does not contain exactly 3 tokens or specifies an unsupported HTTP version (anything other than `HTTP/1.0` and `HTTP/1.1`), the server immediately rejects it with `400 Bad Request` and closes the socket.
  - **HTTP/1.0 vs HTTP/1.1 Semantics:**
    - `HTTP/1.1`: Defaults to `Connection: keep-alive` unless `Connection: close` is explicitly specified.
    - `HTTP/1.0`: Defaults to `Connection: close` unless `Connection: keep-alive` is explicitly requested.
  - **Method Enforcement (HTTP 405):** Restrict allowed methods to `GET` and `HEAD`. Any other method returns `405 Method Not Allowed` with an `Allow: GET, HEAD` header. `HEAD` requests execute the routing logic and return identical headers to `GET`, but omit the response body.
  - **URL Routing (HTTP 200 vs 404):** Match requested paths against recognized endpoints (`/`, `/health`, `/status`). Any unrecognized path cleanly returns `404 Not Found`.


## Hardening Feature 6: Content-Length Body Parsing & Maximum Body Size (HTTP 413 / 411)
- **Vulnerability/Attack Prevented:** Unbounded Memory Allocation (OOM crash), HTTP Request Smuggling / Connection Desynchronization, and un-delimited streaming body stalls.
- **The Problem:**
  - In HTTP/1.1 persistent connections (`keep-alive`), requests flow sequentially over the same TCP socket. If a client transmits a request containing a body (e.g. `POST /echo`), and the server only reads up to `\r\n\r\n`, the unread payload bytes remain stranded in the socket receive buffer. When the next request arrives, those stranded body bytes are mistakenly parsed as HTTP headers, corrupting the connection state.
  - Furthermore, without a maximum payload threshold (`MAX_BODY_SIZE`), an attacker can send a request claiming `Content-Length: 10000000000` (10 GB) and flood the server with bytes until Python exhausts system RAM and gets terminated by the Linux Out-Of-Memory (OOM) killer.
- **The Fix:**
  - Define `MAX_BODY_SIZE = 1048576` (1 MB max payload).
  - Inspect `in_buffer` for `\r\n\r\n`.
  - Parse `Content-Length` header:
    - If non-integer or negative: reject with `400 Bad Request` and close socket.
    - If missing on methods requiring payload framing (`POST`, `PUT`, `PATCH`): reject with `411 Length Required` and close socket.
    - If `content_length > MAX_BODY_SIZE`: immediately abort with `413 Payload Too Large` and close socket.
  - Frame the complete request: calculate `total_needed = (delim_idx + 4) + content_length`.
  - If `len(in_buffer) < total_needed`, remain in read state and wait for remaining body chunks.
  - Once `len(in_buffer) >= total_needed`, precisely extract `body_bytes` and consume only `total_needed` bytes from `in_buffer`, preserving any subsequent pipelined requests.
  - Added a `/echo` route to demonstrate round-trip request body parsing and verification.


## Hardening Feature 7: Graceful Shutdown on SIGINT and SIGTERM
- **Vulnerability/Failure Prevented:** Connection drops, TCP reset errors (RST), incomplete payload truncation, and data loss during process deployments or terminations.
- **The Problem:**
  - Standard process termination (`Ctrl+C` / `SIGINT` or `kill <pid>` / `SIGTERM`) abruptly aborts the process. In-flight HTTP responses sitting in socket send buffers (`out_buffer`) are instantly discarded, and active client connections are severed with an abrupt TCP Reset (`RST`), causing failed requests on the client side.
  - Clients whose handshakes were completed in the kernel accept backlog are left hanging without a response.
- **The Fix:**
  - **Signal Interception:** Register handlers for `signal.SIGINT` and `signal.SIGTERM`. On signal arrival, set `shutdown_requested = True` (a second signal forces immediate exit).
  - **Close Listener First:** The server immediately unregisters and closes `server_sock`. New incoming connections are instantly rejected by the OS kernel with `ECONNREFUSED` instead of queuing up in limbo.
  - **Disable Keep-Alive:** Iterate over all connections in `active_clients` and set `conn.keep_alive = False`, signaling that no further requests will be processed after the current in-flight response.
  - **Drain Outbound Buffers with Grace Period:** Run an active drain loop with a 5.0-second deadline (`SHUTDOWN_TIMEOUT`). The loop repeatedly calls `selector.select()` to flush pending `out_buffer` bytes to clients and immediately closes each socket as soon as its buffer drains.
  - **Final Cleanup:** Any stubborn connections lingering past the deadline are force-closed, `selector.close()` releases all epoll file descriptors, and the server process exits cleanly with status 0.


## Change 1: Reject Transfer-Encoding with 501 Not Implemented
- **Vulnerability/Attack Prevented:** HTTP Request Smuggling (CL.TE / TE.CL desynchronization attacks) and framing ambiguity.
- **The Problem:** In HTTP/1.1, if a client sends a `Transfer-Encoding: chunked` header (or other transfer codings) to a server that only frames via `Content-Length`, the server and any reverse proxy ahead of it will disagree on request boundaries. An attacker can exploit this discrepancy to smuggle hidden HTTP requests inside the body of another request.
- **The Fix:**
  - In `maybe_process()`, after parsing headers, immediately check `if "transfer-encoding" in headers:`.
  - If present, immediately respond with `501 Not Implemented` (`501 Not Implemented: Transfer-Encoding is not supported
`), set `Connection: close`, flush, and terminate the socket.


## Change 2: Header Name vs. Value Case Sensitivity (RFC 7230 §3.2)
- **Vulnerability/Failure Prevented:** Header value corruption and loss of case fidelity in bearer tokens, MIME types, and multi-part boundaries.
- **The Problem:** Previously, the server executed `val.strip().lower()`, lowercasing all header values. While HTTP header *names* are case-insensitive, HTTP header *values* frequently contain case-sensitive tokens, URLs, hashes, and MIME parameters. Lowercasing values corrupts application-level data.
- **The Fix:**
  - Store header names lowercase: `headers[key.strip().lower()] = val.strip()`, preserving exact casing of all values.
  - In `dispatch_request()`, explicitly normalize `Connection` header values to lowercase: `conn_header = headers.get("connection", "").lower()`.
  - Both `Connection: Close`, `Connection: CLOSE`, and `Connection: KEEP-ALIVE` are correctly recognized case-insensitively while all other header values retain their original casing.


## Change 3: Reject Bare CR, Bare LF, and NUL Bytes in Headers (RFC 7230 §3 / RFC 9112 §2.2)
- **Vulnerability/Attack Prevented:** HTTP Request Smuggling, HTTP Response Splitting, and C-string truncation attacks (e.g., in upstream proxies like Nginx or HAProxy).
- **The Problem:** 
  - Standard HTTP headers must be delimited strictly by CRLF (`\r\n`). If an attacker embeds a bare `\n` or bare `\r` within a header value, different proxies interpret the boundary differently. Some treat bare `\n` as a newline (splitting one header into two or fabricating a new request), while others treat it as raw text.
  - Furthermore, NUL bytes (`\x00`) in header blocks cause C-based network software to truncate header values early, bypassing security checks and route restrictions.
- **The Fix:**
  - Before parsing headers, inspect `header_bytes`:
    - If `b"\x00"` is found, reject immediately with `400 Bad Request`.
    - Strip all valid CRLF sequences (`header_bytes.replace(b"\r\n", b"")`). If any remaining `\r` or `\n` persists, reject immediately with `400 Bad Request`.
    - If a client terminates headers with bare `\n\n` instead of `\r\n\r\n`, reject with `400 Bad Request`.


## Change 4: Dynamic Configuration via Environment Variables
- **Failure Prevented:** Hardcoded deployment constraints, inflexibility across dev/staging/benchmarking environments, and inability to scale past 10,000 connections without editing source code.
- **The Problem:** Previously, network host/port and all security limits (`MAX_CONNECTIONS`, `IDLE_TIMEOUT`, `MAX_HEADER_SIZE`, `MAX_BODY_SIZE`) were hardcoded in `server.py`. Tuning limits for benchmarks or containerized deployments required modifying the Python source file.
- **The Fix:**
  - Read configuration values from `os.environ` with sensible fallbacks:
    - `HOST`: defaults to `'127.0.0.1'`
    - `PORT`: defaults to `8080`
    - `MAX_CONNECTIONS`: defaults to `15000` (raised default capacity ceiling)
    - `IDLE_TIMEOUT`: defaults to `5.0`
    - `MAX_HEADER_SIZE`: defaults to `8192` (8 KB)
    - `MAX_BODY_SIZE`: defaults to `1048576` (1 MB)
    - `SHUTDOWN_TIMEOUT`: defaults to `5.0`


## Change 5: Benchmark Hold Time Alignment & Environmental Tuning
- **Failure Prevented:** Premature connection termination during idle connection capacity benchmarks.
- **The Problem:** Previously, `benchmark_10k.py` held connected sockets open for a hardcoded 10 seconds. Since the hardened server enforces an `IDLE_TIMEOUT` of 5.0 seconds on inactive connections, any idle benchmark run lasting longer than 5 seconds was reaped mid-hold by the server's sweep timer.
- **The Fix:**
  - In `benchmark_10k.py`, set default `HOLD_TIME = 3.0` seconds (shorter than the server's 5.0s `IDLE_TIMEOUT`).
  - Added dynamic environment variable overrides for `HOST`, `PORT`, `TARGET_CONNECTIONS`, and `HOLD_TIME`:
    - `TARGET_CONNECTIONS`: defaults to `10000`
    - `HOLD_TIME`: defaults to `3.0`
    - `HOST`: defaults to `'127.0.0.1'`
    - `PORT`: defaults to `8080`
