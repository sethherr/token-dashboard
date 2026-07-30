"""Client disconnects must not print tracebacks.

Browsers reset keep-alive and speculative connections constantly, and the SSE
stream is dropped on every navigation. The default socketserver behaviour
prints a full traceback for each, which drowns out real errors.
"""
import io
import socket
import sys
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stderr

from token_dashboard import server


class HandleErrorTests(unittest.TestCase):
    def setUp(self):
        # Build the class without binding a port.
        self.srv = server.QuietThreadingHTTPServer.__new__(server.QuietThreadingHTTPServer)

    def _capture(self, exc):
        buf = io.StringIO()
        try:
            raise exc
        except type(exc):
            with redirect_stderr(buf):
                try:
                    self.srv.handle_error(None, ("127.0.0.1", 1234))
                except Exception as e:  # delegation path writes then may re-raise
                    return buf.getvalue() + str(e)
        return buf.getvalue()

    def test_disconnects_are_silent(self):
        for exc in (ConnectionResetError(54, "Connection reset by peer"),
                    BrokenPipeError(32, "Broken pipe"),
                    ConnectionAbortedError(53, "Software caused connection abort"),
                    TimeoutError("timed out")):
            self.assertEqual(self._capture(exc), "", f"{type(exc).__name__} should be silent")

    def test_real_errors_still_surface(self):
        out = self._capture(ValueError("something actually broke"))
        self.assertIn("something actually broke", out)


class LiveDisconnectTests(unittest.TestCase):
    """End-to-end: hang up mid-request and assert nothing lands on stderr."""

    def test_abrupt_client_disconnect_prints_nothing(self):
        handler = server.build_handler(":memory:", None)
        httpd = server.QuietThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                for _ in range(5):
                    s = socket.create_connection(("127.0.0.1", port))
                    # Send a partial request, then reset rather than close cleanly.
                    s.sendall(b"GET /api/plan HTTP/1.1\r\nHost: x\r\n")
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                 __import__("struct").pack("ii", 1, 0))
                    s.close()
                time.sleep(0.4)
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertNotIn("Traceback", buf.getvalue())
        self.assertNotIn("ConnectionResetError", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
