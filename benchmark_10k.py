import socket
import selectors
import resource
import time

HOST = '127.0.0.1'
PORT = 8080
TARGET_CONNECTIONS = 10000

def check_connection(sock):
    """
    Checks if a non-blocking socket successfully connected.
    Returns (is_successful: bool, error_code: int).
    """
    # SO_ERROR returns 0 if the connection succeeded, or the errno (e.g. ECONNREFUSED) if failed
    err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    return (err == 0, err)

def run_benchmark():
    # Unlock client file descriptor limit so we can open 10,000 outgoing sockets
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))
    current_soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    print(f"[*] Benchmark FD Limit set to: {current_soft}")

    selector = selectors.DefaultSelector()
    all_sockets = []
    successful_sockets = []
    failed_sockets = []

    print(f"[*] Launching {TARGET_CONNECTIONS} non-blocking connections to {HOST}:{PORT}...")
    start_time = time.time()

    # Step 1: Initiate 10,000 non-blocking TCP handshakes
    for i in range(1, TARGET_CONNECTIONS + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        # connect_ex returns errno (e.g. EINPROGRESS) without raising an exception
        s.connect_ex((HOST, PORT))
        # In non-blocking sockets, EVENT_WRITE signals that the TCP handshake completed (or failed)
        selector.register(s, selectors.EVENT_WRITE, data=i)
        all_sockets.append(s)

        # Periodically drain epoll events so we don't back up the OS queue
        if i % 1000 == 0:
            events = selector.select(timeout=0)
            for key, mask in events:
                sock = key.fileobj
                selector.unregister(sock)
                is_ok, err = check_connection(sock)
                if is_ok:
                    successful_sockets.append(sock)
                else:
                    failed_sockets.append((sock, err))
            total_handled = len(successful_sockets) + len(failed_sockets)
            print(f"[*] Sent {i}/{TARGET_CONNECTIONS} requests (Handled: {total_handled} | Success: {len(successful_sockets)} | Failed: {len(failed_sockets)})...")

    # Step 2: Wait for remaining handshakes to finish
    print("[*] Waiting for all remaining handshakes to complete...")
    timeout_time = time.time() + 15
    while (len(successful_sockets) + len(failed_sockets)) < TARGET_CONNECTIONS and time.time() < timeout_time:
        events = selector.select(timeout=0.2)
        if not events:
            continue
        for key, mask in events:
            sock = key.fileobj
            selector.unregister(sock)
            is_ok, err = check_connection(sock)
            if is_ok:
                successful_sockets.append(sock)
            else:
                failed_sockets.append((sock, err))
                
        total_handled = len(successful_sockets) + len(failed_sockets)
        if total_handled % 1000 == 0:
            print(f"[+] Progress: {total_handled}/{TARGET_CONNECTIONS} (Success: {len(successful_sockets)}, Failed: {len(failed_sockets)})")

    elapsed = time.time() - start_time
    print(f"\n=======================================================")
    print(f" BENCHMARK RESULTS ({elapsed:.2f}s):")
    print(f" Successful connections: {len(successful_sockets)}")
    print(f" Failed connections:     {len(failed_sockets)}")
    print(f"=======================================================")

    if failed_sockets:
        errors = {}
        for _, err in failed_sockets:
            errors[err] = errors.get(err, 0) + 1
        print(f"[!] Failure breakdown by errno: {errors}")

    if successful_sockets:
        print(f"[*] Holding {len(successful_sockets)} active sockets open for 10 seconds...")
        print(f"[*] Check your server terminal to verify the count!")
        time.sleep(10)

    print("\n[*] Gracefully closing all sockets...")
    for s in all_sockets:
        s.close()
    selector.close()
    print("[*] Benchmark complete!")

if __name__ == '__main__':
    run_benchmark()
