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


@pytest.mark.parametrize(
    "phase", ["headers", "body", "tls_handshake", "tls_body", "write"]
)
def test_total_deadline_interrupts_slow_or_blocked_receiver(phase, tmp_path):
    import threading
    import time
    from dataclasses import replace

    if phase == "tls_body":
        server_context, client_context = tls_contexts(tmp_path)
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
                if phase == "tls_body":
                    stream = server_context.wrap_socket(stream, server_side=True)
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
        if phase in {"body", "tls_body"}:
            stream.sendall(response)
            response = b"x" * 100
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
        context = (
            client_context if phase == "tls_body" else ssl.create_default_context()
        )
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
