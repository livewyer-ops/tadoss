"""Disposable HTTPS OAuth/JWKS issuer and webhook receiver for qualification."""

import hmac
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

key = load_pem_private_key(Path("/credentials/key.pem").read_bytes(), password=None)
client_secret = Path("/credentials/client_secret").read_text()
hook_secret = Path("/credentials/webhook_token").read_text()
jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
jwk.update(kid="qualification", use="sig", alg="RS256")
lock = threading.Lock()
counts = {"deliveries": 0, "events": 0}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/jwks":
            self.send_json(200, {"keys": [jwk]})
        elif self.path == "/stats" and hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + hook_secret
        ):
            with lock:
                snapshot = dict(counts)
            self.send_json(200, snapshot)
        else:
            self.send_json(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_097_152:
            self.send_json(413, {})
            return
        body = self.rfile.read(length)
        if self.path == "/token":
            form = parse_qs(body.decode())
            if (
                form.get("grant_type") != ["client_credentials"]
                or form.get("client_id") != ["qualification"]
                or not hmac.compare_digest(
                    form.get("client_secret", [""])[0], client_secret
                )
            ):
                self.send_json(401, {})
                return
            token = jwt.encode(
                {
                    "iss": os.environ["ISSUER"],
                    "aud": "qualification",
                    "sub": "load-test",
                    "exp": time.time() + 300,
                    "scope": (
                        "tads-api/read tads-api/write tads-api/delete tads-api/admin"
                    ),
                },
                key,
                algorithm="RS256",
                headers={"kid": "qualification"},
            )
            self.send_json(
                200, {"access_token": token, "token_type": "Bearer", "expires_in": 300}
            )
        elif self.path == "/events" and hmac.compare_digest(
            self.headers.get("X-Qualification-Token", ""), hook_secret
        ):
            document = json.loads(body)
            with lock:
                counts["deliveries"] += 1
                counts["events"] += len(
                    document.get("event", {}).get("track_events", [])
                )
            self.send_json(200, {})
        else:
            self.send_json(401, {})

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)  # noqa: S104 cluster fixture
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/tls.crt", "/tls/tls.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
