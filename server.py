import socket
import selectors
import resource
import time
import signal
import sys
import os

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', 8080))
TARGET_FD_LIMIT = 65536
# Security Limits
MAX_HEADER_SIZE = int(os.environ.get('MAX_HEADER_SIZE', 8192))
MAX_BODY_SIZE = int(os.environ.get('MAX_BODY_SIZE', 1048576))
IDLE_TIMEOUT = float(os.environ.get('IDLE_TIMEOUT', 5.0))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', 15000))
SHUTDOWN_TIMEOUT = float(os.environ.get('SHUTDOWN_TIMEOUT', 5.0))

active_connections = 0
# Track active connection instances for periodic idle timeout sweeps & graceful shutdown
active_clients = set()
shutdown_requested = False

def sig_handler(signum, frame):
    """Signal handler for graceful shutdown on SIGINT (Ctrl+C) and SIGTERM."""
    global shutdown_requested
    sig_name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
    if shutdown_requested:
        print(f"\n[!] Second {sig_name} received. Immediate forced termination.")
        sys.exit(1)
    print(f"\n[!] Caught {sig_name}. Initiating graceful shutdown...")
    shutdown_requested = True

def tune_system_limits():
    """Raises open file descriptor limits for C10K."""
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_soft_limit = min(TARGET_FD_LIMIT, hard_limit)
    resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft_limit, hard_limit))
    current_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"[*] Tuned File Descriptors: {soft_limit} -> {current_soft} (Hard limit: {hard_limit})")

selector = selectors.DefaultSelector()

class ClientConnection:
    """Tracks state and persistent connection lifecycle for an individual client."""
    def __init__(self, sock, addr):
        global active_connections, active_clients
        self.sock = sock
        self.addr = addr
        self.in_buffer = bytearray()
        self.out_buffer = bytearray()
        self.keep_alive = True
        self.request_count = 0
        # Idempotency flag: prevents double-decrement of active_connections
        self.closed = False
        # Timestamp of most recent read/write activity for idle sweeps
        self.last_activity = time.time()
        
        active_clients.add(self)
        active_connections += 1
        if active_connections % 1000 == 0 or active_connections == 1:
            print(f"[+] Active Connections: {active_connections}")

    def handle_read(self):
        # Update activity timestamp whenever client sends bytes
        self.last_activity = time.time()
        try:
            chunk = self.sock.recv(4096)
            if not chunk:
                self.close()
                return

            self.in_buffer.extend(chunk)
            if not self.out_buffer:
                self.maybe_process()
        except BlockingIOError:
            pass
        except Exception:
            self.close()

    def maybe_process(self):
        """Inspects in_buffer to determine if a complete HTTP request (headers + body) is available."""
        while self.in_buffer and not self.out_buffer and not self.closed:
            delim_idx = self.in_buffer.find(b"\r\n\r\n")
            if delim_idx == -1:
                # Reject malformed header blocks using bare \n\n delimiter
                if b"\n\n" in self.in_buffer:
                    self.send_error_and_close("400 Bad Request", "400 Bad Request: Bare LF in header block\n")
                    return
                if len(self.in_buffer) > MAX_HEADER_SIZE:
                    # DoS Protection: reject requests whose headers exceed 8 KB without boundary
                    self.send_error_and_close(
                        status="431 Request Header Fields Too Large",
                        body="431 Request Header Fields Too Large\n"
                    )
                return

            # Extract header text to parse Request-Line and Content-Length
            header_bytes = bytes(self.in_buffer[:delim_idx])

            # Security check: reject NUL bytes, bare \r, or bare \n in header block
            if b"\x00" in header_bytes:
                self.send_error_and_close("400 Bad Request", "400 Bad Request: NUL byte forbidden in headers\n")
                return

            stripped_headers = header_bytes.replace(b"\r\n", b"")
            if b"\r" in stripped_headers or b"\n" in stripped_headers:
                self.send_error_and_close("400 Bad Request", "400 Bad Request: Bare CR or LF in headers\n")
                return

            header_text = header_bytes.decode('utf-8', errors='replace')
            lines = header_text.split("\r\n")
            first_line = lines[0] if lines else ""

            # 1. Parse and validate Request-Line: METHOD PATH VERSION
            parts = first_line.split(" ")
            if len(parts) != 3:
                self.send_error_and_close("400 Bad Request", "400 Bad Request: Malformed request line\n")
                return

            method, path, version = parts

            # Validate HTTP version (only HTTP/1.0 and HTTP/1.1 are supported)
            if version not in ("HTTP/1.0", "HTTP/1.1"):
                self.send_error_and_close("400 Bad Request", f"400 Bad Request: Unsupported protocol {version}\n")
                return

            # 2. Parse Headers
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    key, val = line.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            # Reject Transfer-Encoding (chunked, etc.) with 501 Not Implemented
            if "transfer-encoding" in headers:
                self.send_error_and_close(
                    status="501 Not Implemented",
                    body="501 Not Implemented: Transfer-Encoding is not supported\n"
                )
                return

            # 3. Parse and validate Content-Length
            content_length = 0
            if "content-length" in headers:
                try:
                    content_length = int(headers["content-length"])
                    if content_length < 0:
                        raise ValueError()
                except ValueError:
                    self.send_error_and_close("400 Bad Request", "400 Bad Request: Invalid Content-Length header\n")
                    return
            elif method in ("POST", "PUT", "PATCH"):
                # RFC 7230: POST/PUT without Content-Length requires explicit 411
                self.send_error_and_close("411 Length Required", "411 Length Required: Content-Length header required\n")
                return

            # 4. Security limit: Payload Too Large
            if content_length > MAX_BODY_SIZE:
                self.send_error_and_close(
                    status="413 Payload Too Large",
                    body=f"413 Payload Too Large: Exceeds maximum limit of {MAX_BODY_SIZE} bytes\n"
                )
                return

            # 5. Check if full body has arrived
            total_needed = (delim_idx + 4) + content_length
            if len(self.in_buffer) < total_needed:
                # Still waiting for remaining body bytes
                return

            # Extract body bytes and consume request from in_buffer
            body_bytes = bytes(self.in_buffer[delim_idx + 4 : total_needed])
            del self.in_buffer[:total_needed]

            self.dispatch_request(method, path, version, headers, body_bytes, first_line)

    def dispatch_request(self, method, path, version, headers, body_bytes, first_line):
        self.request_count += 1

        # Connection lifecycle: HTTP/1.1 defaults to keep-alive; HTTP/1.0 defaults to close
        conn_header = headers.get("connection", "").lower()
        if version == "HTTP/1.1":
            self.keep_alive = (conn_header != "close")
        else:  # HTTP/1.0
            self.keep_alive = (conn_header == "keep-alive")

        # Routing table
        if path == "/echo":
            if method not in ("POST", "GET"):
                self.send_response(
                    status="405 Method Not Allowed",
                    body=f"405 Method Not Allowed: {method}\n",
                    version=version,
                    extra_headers={"Allow": "GET, POST"}
                )
                return
            content_type = headers.get("content-type", "text/plain; charset=utf-8")
            self.send_response(
                status="200 OK",
                body=body_bytes,
                version=version,
                extra_headers={"Content-Type": content_type}
            )
        elif path == "/":
            if method not in ("GET", "HEAD"):
                self.send_response(
                    status="405 Method Not Allowed",
                    body=f"405 Method Not Allowed: {method}\n",
                    version=version,
                    extra_headers={"Allow": "GET, HEAD"}
                )
                return
            body = f"Hello! Request #{self.request_count} on this socket. ({first_line})\n"
            self.send_response("200 OK", body, version=version, is_head=(method == "HEAD"))
        elif path == "/health":
            if method not in ("GET", "HEAD"):
                self.send_response(
                    status="405 Method Not Allowed",
                    body=f"405 Method Not Allowed: {method}\n",
                    version=version,
                    extra_headers={"Allow": "GET, HEAD"}
                )
                return
            self.send_response("200 OK", "OK\n", version=version, is_head=(method == "HEAD"))
        elif path == "/status":
            if method not in ("GET", "HEAD"):
                self.send_response(
                    status="405 Method Not Allowed",
                    body=f"405 Method Not Allowed: {method}\n",
                    version=version,
                    extra_headers={"Allow": "GET, HEAD"}
                )
                return
            body = f'{{"status": "ok", "active_connections": {active_connections}}}\n'
            self.send_response(
                "200 OK",
                body,
                version=version,
                extra_headers={"Content-Type": "application/json"},
                is_head=(method == "HEAD")
            )
        else:
            self.send_response(
                status="404 Not Found",
                body=f"404 Not Found: {path}\n",
                version=version,
                is_head=(method == "HEAD")
            )

    def send_error_and_close(self, status, body):
        """Sends an HTTP error response, forces Connection: close, and flushes output."""
        self.keep_alive = False
        body_bytes = body.encode('utf-8') if isinstance(body, str) else body
        response = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode('utf-8') + body_bytes
        self.out_buffer = bytearray(response)
        self.handle_write()

    def send_response(self, status, body, version="HTTP/1.1", extra_headers=None, is_head=False):
        """Constructs an HTTP response, queues it into out_buffer, and initiates write."""
        body_bytes = body.encode('utf-8') if isinstance(body, str) else body
        conn_header = "keep-alive" if self.keep_alive else "close"
        
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(body_bytes)),
            "Connection": conn_header,
        }
        if extra_headers:
            headers.update(extra_headers)

        header_lines = [f"{version} {status}"]
        for k, v in headers.items():
            header_lines.append(f"{k}: {v}")
        response_str = "\r\n".join(header_lines) + "\r\n\r\n"

        if is_head:
            # HEAD requests receive identical headers to GET, but with no body
            self.out_buffer.extend(response_str.encode('utf-8'))
        else:
            self.out_buffer.extend(response_str.encode('utf-8') + body_bytes)
        self.handle_write()

    def handle_write(self):
        # Update activity timestamp whenever bytes are transmitted
        self.last_activity = time.time()
        try:
            sent = self.sock.send(self.out_buffer)
            del self.out_buffer[:sent]

            if not self.out_buffer:
                if self.keep_alive:
                    selector.modify(self.sock, selectors.EVENT_READ, data=self)
                    if self.in_buffer:
                        self.maybe_process()
                else:
                    self.close()
            else:
                selector.modify(self.sock, selectors.EVENT_WRITE, data=self)
        except BlockingIOError:
            selector.modify(self.sock, selectors.EVENT_WRITE, data=self)
        except Exception:
            self.close()

    def close(self):
        global active_connections, active_clients
        if self.closed:
            return
        self.closed = True

        # Remove from active tracking set
        active_clients.discard(self)

        try:
            selector.unregister(self.sock)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

        active_connections -= 1
        if active_connections % 1000 == 0:
            print(f"[-] Active Connections: {active_connections}")

def reject_over_capacity(client_sock, client_addr):
    """Sends an HTTP 503 Service Unavailable response and closes socket immediately."""
    try:
        body = "503 Service Unavailable: Server at maximum capacity\n"
        response = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            "Retry-After: 5\r\n"
            "Connection: close\r\n"
            "\r\n"
            f"{body}"
        )
        client_sock.setblocking(False)
        client_sock.sendall(response.encode('utf-8'))
    except Exception:
        pass
    finally:
        client_sock.close()

def accept_connection(server_sock):
    while True:
        try:
            client_sock, client_addr = server_sock.accept()
        except BlockingIOError:
            break
        except Exception:
            break

        # Capacity Protection: Gracefully reject connection if server is at max limit
        if active_connections >= MAX_CONNECTIONS:
            reject_over_capacity(client_sock, client_addr)
            continue

        client_sock.setblocking(False)
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection = ClientConnection(client_sock, client_addr)
        selector.register(client_sock, selectors.EVENT_READ, data=connection)

def start_server():
    global shutdown_requested
    tune_system_limits()

    # Register OS signals for graceful shutdown
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(socket.SOMAXCONN)
    server_sock.setblocking(False)

    selector.register(server_sock, selectors.EVENT_READ, data=accept_connection)

    print(f"[*] C10K server listening on http://{HOST}:{PORT}")
    print(f"[*] Limits: Max Conns={MAX_CONNECTIONS}, Header={MAX_HEADER_SIZE}B, Body={MAX_BODY_SIZE}B, Idle Timeout={IDLE_TIMEOUT}s")
    print(f"[*] Engine: {selector.__class__.__name__} ... (Press Ctrl+C or kill <pid> to stop)")

    last_sweep = time.time()

    while not shutdown_requested:
        # Wake up on I/O events, or at least once every 1.0s to run maintenance sweeps
        events = selector.select(timeout=1.0)
        for key, mask in events:
            if callable(key.data):
                key.data(key.fileobj)
            else:
                conn = key.data
                if mask & selectors.EVENT_READ:
                    conn.handle_read()
                elif mask & selectors.EVENT_WRITE:
                    conn.handle_write()

        # Periodic Idle Sweep: Close connections inactive for more than IDLE_TIMEOUT seconds
        now = time.time()
        if now - last_sweep >= 1.0:
            last_sweep = now
            for conn in list(active_clients):
                if now - conn.last_activity > IDLE_TIMEOUT:
                    conn.close()

    # ========================================================
    # Graceful Shutdown Sequence
    # ========================================================
    print(f"[*] Closing listening socket. Refusing new incoming connections.")
    try:
        selector.unregister(server_sock)
    except Exception:
        pass
    try:
        server_sock.close()
    except Exception:
        pass

    # Instruct all active connections to disallow further keep-alive requests
    for conn in list(active_clients):
        conn.keep_alive = False
        if not conn.out_buffer:
            conn.close()

    if active_clients:
        print(f"[*] Draining {len(active_clients)} in-flight connection(s) (grace period: {SHUTDOWN_TIMEOUT}s)...")
        drain_deadline = time.time() + SHUTDOWN_TIMEOUT
        while active_clients and time.time() < drain_deadline:
            remaining_time = max(0.05, drain_deadline - time.time())
            events = selector.select(timeout=min(0.2, remaining_time))
            for key, mask in events:
                if not callable(key.data):
                    conn = key.data
                    if mask & selectors.EVENT_WRITE:
                        conn.handle_write()
                    elif mask & selectors.EVENT_READ:
                        # Close connection if client sends more input during shutdown
                        conn.close()

            # Close any connections that have finished transmitting their out_buffer
            for conn in list(active_clients):
                if not conn.out_buffer:
                    conn.close()

    if active_clients:
        print(f"[!] Force-closing {len(active_clients)} lingering connection(s) after timeout.")
        for conn in list(active_clients):
            conn.close()
    else:
        print("[*] All active connections drained cleanly.")

    selector.close()
    print("[*] Graceful shutdown complete. Process exiting.")

if __name__ == '__main__':
    start_server()