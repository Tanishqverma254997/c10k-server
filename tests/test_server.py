"""Socket-level regression tests against a real server process (stdlib only)."""
import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import json
from html.parser import HTMLParser


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

    def test_dashboard_and_referenced_assets(self):
        status, headers, body = self.raw(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), headers)
        self.assertIn(("X-Content-Type-Options", "nosniff"), headers)
        self.assertIn(b'One process.', body)
        self.assertIn(b'not a guarantee of sustained performance', body)
        asset_paths = []

        class AssetParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag in ('script', 'img') and attributes.get('src', '').startswith('/'):
                    asset_paths.append(attributes['src'])
                if tag == 'link' and attributes.get('href', '').startswith('/'):
                    asset_paths.append(attributes['href'])

        AssetParser().feed(body.decode())
        self.assertGreaterEqual(len(set(asset_paths)), 3)
        for path in set(asset_paths):
            with self.subTest(path=path):
                status, headers, asset = self.raw(f"GET {path}?v=1 HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())
                self.assertEqual(status, 200)
                self.assertTrue(asset)
                types = dict(headers)
                self.assertEqual(int(types['Content-Length']), len(asset))
                self.assertNotIn('text/html', types['Content-Type'])

    def test_asset_head_methods_and_no_file_disclosure(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request('HEAD', '/assets/app.js')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertGreater(int(response.getheader('Content-Length')), 0)
            self.assertEqual(response.read(), b'')
            conn.request('GET', '/assets/app.js')
            response = conn.getresponse()
            self.assertIn(b'checkStatus', response.read())
        finally:
            conn.close()
        for path, method, expected in [
            ('/', 'POST', 405), ('/assets/app.css', 'POST', 405),
            ('/assets/../server.py', 'GET', 404), ('/server.py', 'GET', 404),
            ('/assets/%2e%2e/server.py', 'GET', 404), ('/.git/config', 'GET', 404),
        ]:
            with self.subTest(path=path, method=method):
                status, _, _ = self.raw(f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
                self.assertEqual(status, expected)

    def test_status_exposes_real_process_metrics(self):
        status, headers, body = self.raw(b'GET /status HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        self.assertEqual(status, 200)
        self.assertIn(('Cache-Control', 'no-store'), headers)
        data = json.loads(body)
        self.assertGreaterEqual(data['active_connections'], 1)
        self.assertGreaterEqual(data['peak_connections'], data['active_connections'])
        self.assertGreaterEqual(data['uptime_seconds'], 0)

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
