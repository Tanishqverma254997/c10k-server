"""Socket-level regression tests against a real server process (stdlib only)."""
import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            env={**os.environ, "HOST": "127.0.0.1", "PORT": str(cls.port),
                 "MAX_HEADER_SIZE": "512", "MAX_BODY_SIZE": "1024"},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise RuntimeError(cls.process.stderr.read().decode())
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=.1):
                    return
            except OSError:
                time.sleep(.02)
        cls.process.terminate()
        cls.process.wait(timeout=7)
        raise RuntimeError("Server did not become ready")

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=7)
        errors = cls.process.stderr.read().decode()
        cls.process.stderr.close()
        if cls.process.returncode != 0:
            raise AssertionError(errors)

    def raw(self, request):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall(request)
            response = http.client.HTTPResponse(sock)
            response.begin()
            body = response.read()
            return response.status, response.getheaders(), body

    def test_health_head_status_and_keep_alive(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual((response.status, response.read()), (200, b"OK\n"))
            original_socket = conn.sock
            conn.request("HEAD", "/health")
            response = conn.getresponse()
            self.assertEqual(response.getheader("Content-Length"), "3")
            self.assertEqual(response.read(), b"")
            conn.request("GET", "/status")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"active_connections"', response.read())
            self.assertIs(conn.sock, original_socket)
        finally:
            conn.close()

    def test_binary_echo(self):
        status, _, body = self.raw(b"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nConnection: close\r\n\r\n\x00\xffhi")
        self.assertEqual((status, body), (200, b"\x00\xffhi"))

    def test_oversized_complete_and_partial_headers(self):
        for ending in (b"", b"\r\n\r\n"):
            with self.subTest(ending=ending):
                status, _, _ = self.raw(b"GET / HTTP/1.1\r\nX-Padding: " + b"x" * 513 + ending)
                self.assertEqual(status, 431)

    def test_reject_ambiguous_or_malformed_headers(self):
        cases = [
            b"Content-Length: 0\r\nContent-Length: 4",
            b"Content-Length: +0", b"Content-Length: 1_0",
            b"Content-Length : 0", b"Broken-Header",
            b"Host: second.example", b"X Test: value",
        ]
        for header in cases:
            with self.subTest(header=header):
                status, headers, _ = self.raw(b"POST /echo HTTP/1.1\r\nHost: localhost\r\n" + header + b"\r\n\r\n")
                self.assertEqual(status, 400)
                self.assertIn(("Connection", "close"), headers)

    def test_payload_and_transfer_encoding_limits(self):
        for header, expected in [(b"Content-Length: 1025", 413), (b"Transfer-Encoding: chunked", 501)]:
            status, _, _ = self.raw(b"POST /echo HTTP/1.1\r\nHost: localhost\r\n" + header + b"\r\n\r\n")
            self.assertEqual(status, expected)

    def test_connection_close_token(self):
        status, headers, _ = self.raw(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive, close\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertIn(("Connection", "close"), headers)


if __name__ == "__main__":
    unittest.main()
