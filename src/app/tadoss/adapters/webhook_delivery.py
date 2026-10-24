"""HTTP delivery with destination checks on the addresses actually connected to."""

import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
from contextlib import suppress
from urllib.parse import urlsplit

from tadoss.domain.validation import APIError


def allowed(host, settings):
    return any(
        host == value or (value.startswith(".") and host.endswith(value))
        for value in settings.webhook_allowed_hosts.lower().split(",")
        if value
    )


def validate_url(url, settings):
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or (port is not None and not 0 < port <= 65535)
        ):
            raise ValueError()
        if any(ord(char) < 33 or ord(char) > 126 for char in url):
            raise ValueError()
        host = parsed.hostname.lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if host in {"metadata.google.internal", "instance-data.ec2.internal"} or (
            address
            and (
                str(address) in {"169.254.169.254", "fd00:ec2::254"}
                or (not address.is_global and not allowed(host, settings))
            )
        ):
            raise ValueError()
        return parsed
    except (ValueError, UnicodeError):
        raise APIError(400, "Invalid or restricted webhook destination") from None


def deliver(url, body, header, secret, settings):
    parsed = validate_url(url, settings)
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if str(ip) in {"169.254.169.254", "fd00:ec2::254"} or (
            not ip.is_global and not allowed(host, settings)
        ):
            raise APIError(400, "Webhook destination resolves to a restricted address")
    if not addresses:
        raise OSError("Webhook destination has no addresses")
    # HTTPConnection retains the original Host; TLS verifies the original name.
    connection = http.client.HTTPConnection(
        host, port, timeout=settings.webhook_timeout_seconds
    )
    deadline = time.monotonic() + settings.webhook_timeout_seconds
    expired = threading.Event()
    socket_lock = threading.Lock()
    family, socktype, protocol, _, address = addresses[0]
    connection.sock = socket.socket(family, socktype, protocol)
    active_socket = connection.sock

    def remaining():
        budget = deadline - time.monotonic()
        if expired.is_set() or budget <= 0:
            raise TimeoutError("Webhook deadline exceeded")
        return budget

    def interrupt():
        expired.set()
        # shutdown interrupts blocked reads/writes without recycling the fd.
        with socket_lock, suppress(OSError):
            active_socket.shutdown(socket.SHUT_RDWR)

    timer = threading.Timer(settings.webhook_timeout_seconds, interrupt)
    timer.daemon = True
    timer.start()
    try:
        connection.sock.settimeout(remaining())
        connection.sock.connect(address)
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            with socket_lock:
                remaining()
                connection.sock = context.wrap_socket(
                    connection.sock, server_hostname=host, do_handshake_on_connect=False
                )
                active_socket = connection.sock
            connection.sock.settimeout(remaining())
            connection.sock.do_handshake()
        connection.sock.settimeout(remaining())
        headers = {"Content-Type": "application/json"}
        if header:
            headers[header] = secret
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("POST", path, json.dumps(body).encode(), headers)
        response = connection.getresponse()
        status = response.status
        response.read(65536)
        remaining()
        if 300 <= status < 400:
            raise APIError(400, "Webhook redirects are not permitted")
        return status
    finally:
        timer.cancel()
        timer.join()
        connection.close()
