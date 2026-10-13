import socket
import selectors
import resource
import time

HOST = '127.0.0.1'
PORT = 8080
TARGET_FD_LIMIT = 65536

active_connections = 0

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
        global active_connections
        self.sock = sock
        self.addr = addr
        self.in_buffer = bytearray()
        self.out_buffer = bytearray()
        self.keep_alive = True
        self.request_count = 0
        
        active_connections += 1
        if active_connections % 1000 == 0 or active_connections == 1:
            print(f"[+] Active Connections: {active_connections}")

    def handle_read(self):
        try:
            chunk = self.sock.recv(4096)
            if not chunk:
                self.close()
                return

            self.in_buffer.extend(chunk)

            if b"\r\n\r\n" in self.in_buffer:
                self.process_request()
        except BlockingIOError:
            pass
        except Exception as e:
            self.close()

    def process_request(self):
        delim_idx = self.in_buffer.find(b"\r\n\r\n")
        header_bytes = bytes(self.in_buffer[:delim_idx])
        del self.in_buffer[:delim_idx + 4]

        self.request_count += 1
        request_text = header_bytes.decode('utf-8', errors='replace')
        first_line = request_text.split("\r\n")[0]

        if "connection: close" in request_text.lower():
            self.keep_alive = False
        else:
            self.keep_alive = True

        conn_header = "keep-alive" if self.keep_alive else "close"
        body = f"Hello! Request #{self.request_count} on this socket. ({first_line})\n"
        
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body.encode('utf-8'))}\r\n"
            f"Connection: {conn_header}\r\n"
            "\r\n"
            f"{body}"
        )
        self.out_buffer.extend(response.encode('utf-8'))
        self.handle_write()

    def handle_write(self):
        try:
            sent = self.sock.send(self.out_buffer)
            del self.out_buffer[:sent]

            if not self.out_buffer:
                if self.keep_alive:
                    selector.modify(self.sock, selectors.EVENT_READ, data=self)
                    if b"\r\n\r\n" in self.in_buffer:
                        self.process_request()
                else:
                    self.close()
            else:
                selector.modify(self.sock, selectors.EVENT_WRITE, data=self)
        except BlockingIOError:
            selector.modify(self.sock, selectors.EVENT_WRITE, data=self)
        except Exception as e:
            self.close()

    def close(self):
        global active_connections
        try:
            selector.unregister(self.sock)
        except Exception:
            pass
        self.sock.close()
        active_connections -= 1
        if active_connections % 1000 == 0:
            print(f"[-] Active Connections: {active_connections}")

def accept_connection(server_sock):
    # Accept connections in a loop to drain kernel backlog quickly
    while True:
        try:
            client_sock, client_addr = server_sock.accept()
        except BlockingIOError:
            break
        except Exception:
            break
        client_sock.setblocking(False)
        client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        connection = ClientConnection(client_sock, client_addr)
        selector.register(client_sock, selectors.EVENT_READ, data=connection)

def start_server():
    tune_system_limits()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(socket.SOMAXCONN)
    server_sock.setblocking(False)

    selector.register(server_sock, selectors.EVENT_READ, data=accept_connection)

    print(f"[*] C10K Keep-Alive server listening on http://{HOST}:{PORT}")
    print(f"[*] Engine: {selector.__class__.__name__} ... (Press Ctrl+C to stop)")

    try:
        while True:
            events = selector.select()
            for key, mask in events:
                if callable(key.data):
                    key.data(key.fileobj)
                else:
                    conn = key.data
                    if mask & selectors.EVENT_READ:
                        conn.handle_read()
                    elif mask & selectors.EVENT_WRITE:
                        conn.handle_write()

    except KeyboardInterrupt:
        print("\n[*] Server shutting down.")
    finally:
        selector.close()
        server_sock.close()

if __name__ == '__main__':
    start_server()
