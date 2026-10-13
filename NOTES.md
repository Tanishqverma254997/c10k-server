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
- **Head-of-Line Blocking:** If Client 1 is slow (or takes 5 seconds to process), Client 2 cannot even get accepted?they are stuck waiting in the operating system's queue.
- **The Core Bottleneck:** A single-threaded blocking server can only serve **1 client at a time**, making 10k concurrent connections completely impossible without concurrency.

## Step 3: Concurrency via Multi-Threading (Thread-per-Connection)
- **The Idea:** Offload each connected client to a separate worker thread (`threading.Thread`).
- **How it solves blocking:** The main thread only does `accept()` and immediately loops back to accept more connections, while background worker threads handle the blocking `recv()`, processing, and `sendall()`.
- **Result:** Multiple clients can be served concurrently at the same time.
- **Testing Concurrency via Bash:** Instead of manually switching terminals, launch parallel requests in the background with `&`:
  `for i in {1..5}; do curl http://127.0.0.1:8080/ & done; wait`
- **The Catch (The C10k Wall):** 
  - 10,000 threads ? ~8 MB default stack = **~78 GB of RAM** just for thread stacks!
  - Excessive CPU **context switching** thrashing.
  - Hitting OS thread limits (`ulimit -u`). Threads are NOT viable for 10k+ concurrent idle connections.

## Step 4: Non-Blocking Sockets & Polling
- **`sock.setblocking(False)`:** Tells the OS kernel to never put the thread to sleep when calling `accept()`, `recv()`, or `send()`.
- **`BlockingIOError` (EAGAIN / EWOULDBLOCK):** If no client is ready to be accepted or no bytes have arrived to be read, the kernel immediately raises this error instead of freezing.
- **Single-Threaded Concurrency:** One thread can maintain a list of thousands of sockets, asking each in turn if they have data ready.
- **The Problem with Busy-Polling:** Manually looping over 10,000 sockets in Python asking "are you ready?" is an $O(N)$ operation. 9,999 sockets return `BlockingIOError`, wasting massive CPU cycles every millisecond.

## Step 5: I/O Multiplexing & Epoll (Event-Driven Architecture)
- **The Analogy:** Instead of a teacher walking down every aisle asking each student "Do you have a question?", the teacher tells students: "Raise your hand, and the class monitor (OS kernel) will wake me up and tell me exactly who has their hand raised."
- **`epoll` on Linux:** Linux's event notification mechanism. Instead of scanning all 10,000 sockets, `epoll_wait` sleeps with **0% CPU** until an event occurs, and returns **only the sockets that are ready** ($O(1)$ scaling!).
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
- **Socket Backlog (`SOMAXCONN`):** The kernel connection queue where TCP handshakes wait before `accept()`. Replacing `listen(128)` with `listen(socket.SOMAXCONN)` prevents dropped connections during connection bursts.
- **`TCP_NODELAY`:** Disables Nagle's algorithm to immediately transmit packets without buffering 40ms delays.

## Step 8: HTTP Keep-Alive (Persistent Connections)
- **The Cost of Connection: Close:** Without Keep-Alive, every single request requires a full TCP 3-way handshake (`SYN`/`SYN-ACK`/`ACK`) and 4-way teardown (`FIN`/`ACK`), wasting round trips and trapping ports in `TIME_WAIT`.
- **Holding Connections Open:** With `Connection: keep-alive`, the TCP socket stays open after sending the HTTP response. The socket is reset to `EVENT_READ` waiting for the client's next request.
- **The C10K Prerequisite:** To achieve 10,000 concurrent active connections, clients must stay connected rather than immediately disconnecting.

## Step 9: The 10,000 Concurrent Connections Benchmark (C10K Conquered!)
- **Non-Blocking Client Benchmark:** Built `benchmark_10k.py` using non-blocking client sockets with `connect_ex` and `selectors.DefaultSelector()`.
- **The Result:** The server maintains 10,000 live concurrent TCP connections on a **single Python thread**, using minimal RAM (~a few tens of megabytes instead of 80 GB) and near-zero CPU when idle!
- **Connection Draining:** `while True: server_sock.accept()` loop ensures the server accepts all queued handshakes during high-burst connection storms.
﻿
## Step 10: Asynchronous Socket Verification & High-Concurrency Load Testing
- **Why Sockets Become Writable on Connect Failure:** Under POSIX networking standards, an asynchronous `connect()` sets the socket to writable when the connection handshake completes *regardless of whether it succeeded or failed*. This prevents event loops from hanging indefinitely.
- **`getsockopt(SOL_SOCKET, SO_ERROR)`:** The only definitive check to verify an asynchronous TCP handshake. If `0`, connection succeeded; otherwise, it returns the error code (e.g. `ECONNREFUSED`).
- **Load Testing with `wrk`:** Evaluated 10,000 concurrent keep-alive connections over 30 seconds. Achieved over **54,000 requests/sec** with **0 socket errors**, confirming the single-threaded epoll server handles massive concurrency without resource exhaustion.
