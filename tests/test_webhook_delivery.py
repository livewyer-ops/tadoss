import http.client
import socket
import ssl
from unittest.mock import Mock, patch

import pytest
from tadoss.adapters.webhook_delivery import deliver, validate_url
from tadoss.domain.validation import APIError
from tadoss.settings import Settings


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "https://user:pass@example.com/",
        "file:///tmp/key",
        "http://[::1]/",
        "https://example.com/#secret",
    ],
)
def test_restricted_destination(url):
    with pytest.raises(APIError):
        validate_url(url, Settings())


def test_dns_resolution_is_checked_and_connection_is_pinned():
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    connection = Mock()
    connection.getresponse.return_value.status = 204
    with (
        patch("socket.getaddrinfo", return_value=[address]) as dns,
        patch("socket.socket") as connect,
        patch("http.client.HTTPConnection", return_value=connection),
        patch("ssl.create_default_context") as tls,
    ):
        assert deliver("https://receiver.example/events", {}, "", "", Settings()) == 204
        dns.assert_called_once()
        connect.return_value.connect.assert_called_once_with(("93.184.216.34", 443))
        assert (
            tls.return_value.wrap_socket.call_args.kwargs["do_handshake_on_connect"]
            is False
        )
        tls.return_value.wrap_socket.return_value.do_handshake.assert_called_once()
        tls.return_value.wrap_socket.return_value.shutdown.assert_not_called()
        assert (
            tls.return_value.wrap_socket.call_args.kwargs["server_hostname"]
            == "receiver.example"
        )
        connection.getresponse.return_value.status = 302
        with pytest.raises(APIError, match="redirects"):
            deliver("https://receiver.example/events", {}, "", "", Settings())
    address = (*address[:4], ("10.0.0.1", 443))
    with (
        patch("socket.getaddrinfo", return_value=[address]),
        patch("socket.socket") as connect,
        pytest.raises(APIError),
    ):
        deliver("https://receiver.example/events", {}, "", "", Settings())
    connect.assert_not_called()


@pytest.mark.parametrize("phase", ["headers", "tls_handshake", "write"])
def test_total_deadline_interrupts_slow_or_blocked_receiver(phase, tmp_path):
    import threading
    import time
    from dataclasses import replace

    stopped = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(2)
    port = listener.getsockname()[1]

    def receive():
        try:
            with listener.accept()[0] as stream:
                stream.settimeout(2)
                with stream:
                    respond(stream)
        except (OSError, TimeoutError):
            pass

    def respond(stream):
        if phase in {"tls_handshake", "write"}:
            stopped.wait(2)
            return
        request = b""
        while b"\r\n\r\n" not in request:
            request += stream.recv(8192)
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
        for byte in response:
            stream.sendall(bytes([byte]))
            if stopped.wait(0.03):
                break

    thread = threading.Thread(target=receive)
    thread.start()
    settings = replace(
        Settings(), webhook_allowed_hosts="127.0.0.1", webhook_timeout_seconds=0.25
    )
    started = time.monotonic()
    try:
        context = ssl.create_default_context()
        with (
            patch("ssl.create_default_context", return_value=context),
            pytest.raises((OSError, http.client.HTTPException)),
        ):
            deliver(
                f"{'https' if phase.startswith('tls_') else 'http'}://127.0.0.1:{port}",
                {"data": "x" * (20_000_000 if phase == "write" else 1)},
                "",
                "",
                settings,
            )
        assert time.monotonic() - started < 1.25
    finally:
        stopped.set()
        listener.close()
        thread.join(timeout=3)
    assert not thread.is_alive()


def tls_contexts(tmp_path):
    import ipaddress
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    return server_context, ssl.create_default_context(cafile=str(cert_path))


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("secure", [False, True])
def test_delivery_closes_an_unfinished_large_response_without_reading_it(
    status, secure, tmp_path
):
    from concurrent.futures import ThreadPoolExecutor
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    release = Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(status)
            self.send_header("Content-Length", str(1024 * 1024 * 1024))
            self.end_headers()
            self.wfile.flush()
            release.wait(timeout=5)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    context = ssl.create_default_context()
    if secure:
        server_context, context = tls_contexts(tmp_path)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with patch("ssl.create_default_context", return_value=context):
                    future = executor.submit(
                        deliver,
                        f"{'https' if secure else 'http'}://127.0.0.1:{server.server_port}/",
                        {},
                        "",
                        "",
                        Settings(
                            webhook_allowed_hosts="127.0.0.1", webhook_timeout_seconds=3
                        ),
                    )
                    assert future.result(timeout=1) == status
            finally:
                release.set()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_connection_tries_other_validated_addresses_before_sending():
    from tadoss.adapters import webhook_delivery

    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ip in ("93.184.216.34", "93.184.216.35")
    ]
    first, second = Mock(), Mock()
    first.connect.side_effect = OSError("unreachable")
    connection = Mock()
    connection.getresponse.return_value.status = 200
    with (
        patch("socket.getaddrinfo", return_value=addresses) as dns,
        patch("socket.socket", side_effect=[first, second]),
        patch("http.client.HTTPConnection", return_value=connection) as http,
        patch("ssl.create_default_context") as tls,
        patch.object(
            webhook_delivery.time,
            "monotonic",
            side_effect=[0, 0, 0.1, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1],
        ),
    ):
        assert (
            deliver(
                "https://receiver.example/events",
                {},
                "",
                "",
                Settings(webhook_timeout_seconds=2),
            )
            == 200
        )
    dns.assert_called_once()
    http.assert_called_once_with("receiver.example", 443, timeout=2)
    first.connect.assert_called_once_with(addresses[0][4])
    second.connect.assert_called_once_with(addresses[1][4])
    assert second.settimeout.call_args.args[0] < first.settimeout.call_args.args[0]
    tls.return_value.wrap_socket.assert_called_once_with(
        second, server_hostname="receiver.example", do_handshake_on_connect=False
    )
    connection.request.assert_called_once()
    connection.getresponse.return_value.read.assert_not_called()
    connection.getresponse.return_value.close.assert_called_once()


def test_post_failure_does_not_try_another_address():
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))] * 2
    connection = Mock()
    connection.request.side_effect = OSError("connection lost after writing")
    with (
        patch("socket.getaddrinfo", return_value=addresses),
        patch("socket.socket") as connect,
        patch("http.client.HTTPConnection", return_value=connection),
        pytest.raises(OSError, match="connection lost"),
    ):
        deliver("http://receiver.example/events", {}, "", "", Settings())
    connect.assert_called_once()
    connection.request.assert_called_once()


def test_all_resolved_addresses_are_checked_before_any_connection():
    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))
        for ip in ("93.184.216.34", "169.254.169.254")
    ]
    with (
        patch("socket.getaddrinfo", return_value=addresses),
        patch("socket.socket") as connect,
        pytest.raises(APIError, match="restricted"),
    ):
        deliver(
            "http://receiver.example/",
            {},
            "",
            "",
            Settings(webhook_allowed_hosts="receiver.example"),
        )
    connect.assert_not_called()
