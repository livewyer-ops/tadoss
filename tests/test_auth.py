import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request
from tadoss.auth import Authentication
from tadoss.domain.validation import APIError
from tadoss.settings import Settings


def request(header="", query="", method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/tracks",
            "headers": [(b"authorization", header.encode())],
            "query_string": query.encode(),
        }
    )


def test_auth_requires_explicit_configuration():
    with pytest.raises(ValueError):
        Authentication(Settings())
    auth = Authentication(Settings(api_token="secret", basic_auth_password="password"))
    assert auth.authenticate(request(query="access_token=secret")).admin
    basic = base64.b64encode(b"tadoss:password").decode()
    assert auth.authenticate(request("Basic " + basic)).subject == "tadoss"
    for invalid in [
        request("Bearer secret"),
        request("Basic invalid"),
        request(query="access_token=bad"),
    ]:
        with pytest.raises(APIError) as error:
            auth.authenticate(invalid)
        assert error.value.status == 401


def test_jwt_signature_claims_and_permissions():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        oauth2_issuer="https://issuer.test",
        oauth2_audience="tadoss",
        oauth2_jwks_uri="https://issuer.test/jwks",
    )
    auth = Authentication(settings)
    auth.jwks = Mock(
        get_signing_key_from_jwt=Mock(
            return_value=SimpleNamespace(key=key.public_key())
        )
    )
    claims = {
        "iss": settings.oauth2_issuer,
        "aud": "tadoss",
        "sub": "alice",
        "scope": "tads-api/read",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert auth.authenticate(request("Bearer " + token)).subject == "alice"
    with pytest.raises(APIError) as error:
        auth.authenticate(request("Bearer " + token, method="POST"))
    assert error.value.status == 403
    for change in [
        {"aud": "wrong"},
        {"iss": "https://wrong.test"},
        {"exp": 0},
        {"sub": ""},
    ]:
        with pytest.raises(APIError) as error:
            auth.authenticate(
                request(
                    "Bearer " + jwt.encode({**claims, **change}, key, algorithm="RS256")
                )
            )
        assert error.value.status == 401


def test_jwks_rotation_and_unavailable_key_server():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    keys = [
        rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)
    ]
    state = {"index": 0, "unavailable": False, "clock": 1000.0}

    class JWKS(BaseHTTPRequestHandler):
        def do_GET(self):
            if state["unavailable"]:
                self.send_error(503)
                return
            document = json.loads(
                jwt.algorithms.RSAAlgorithm.to_jwk(keys[state["index"]].public_key())
            )
            document.update(kid=str(state["index"]), use="sig", alg="RS256")
            body = json.dumps({"keys": [document]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), JWKS)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        clock_patch = patch(
            "jwt.jwks_client.time", SimpleNamespace(monotonic=lambda: state["clock"])
        )
        clock_patch.start()
        auth = Authentication(
            Settings(
                dev=True,
                oauth2_issuer="https://issuer.test",
                oauth2_audience="tadoss",
                oauth2_jwks_uri=f"http://127.0.0.1:{server.server_port}/keys",
            )
        )
        claims = {
            "iss": "https://issuer.test",
            "aud": "tadoss",
            "sub": "reader",
            "scope": "tads-api/read",
            "exp": datetime.now(UTC) + timedelta(minutes=1),
        }
        for index, key in enumerate(keys):
            state["index"] = index
            state["clock"] += 31
            token = jwt.encode(
                claims, key, algorithm="RS256", headers={"kid": str(index)}
            )
            assert auth.authenticate(request("Bearer " + token)).subject == "reader"
        state["unavailable"] = True
        state["clock"] += 31
        token = jwt.encode(
            claims, keys[0], algorithm="RS256", headers={"kid": "unknown"}
        )
        with pytest.raises(APIError) as error:
            auth.authenticate(request("Bearer " + token))
        assert error.value.status == 401
    finally:
        clock_patch.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "scope_claims,permitted",
    [
        ({"scope": " tads-api/read tads-api/read "}, True),
        ({"scp": "tads-api/read"}, True),
        ({"scope": ["tads-api/read", 42, None, ""]}, True),
        ({"scp": ["tads-api/read"]}, True),
        ({"scope": "", "scp": "tads-api/read"}, False),
        ({"scope": None, "scp": "tads-api/admin"}, False),
        ({"scope": ["tads-api/read tads-api/admin"]}, False),
        ({"scope": {"tads-api/admin": True}}, False),
        ({}, False),
    ],
)
def test_signed_jwt_scope_formats_preserve_operation_permissions(
    scope_claims, permitted
):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Authentication(
        Settings(
            oauth2_issuer="https://issuer.test",
            oauth2_audience="tadoss",
            oauth2_jwks_uri="https://issuer.test/jwks",
        )
    )
    auth.jwks = Mock(
        get_signing_key_from_jwt=Mock(
            return_value=SimpleNamespace(key=key.public_key())
        )
    )
    claims = {
        "iss": "https://issuer.test",
        "aud": "tadoss",
        "sub": "reader",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **scope_claims,
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    if permitted:
        identity = auth.authenticate(request("Bearer " + token))
        assert identity.subject == "reader"
        assert identity.scopes == frozenset({"tads-api/read"})
        assert not identity.admin
    else:
        with pytest.raises(APIError) as error:
            auth.authenticate(request("Bearer " + token))
        assert error.value.status == 403
    for method in ("POST", "DELETE"):
        with pytest.raises(APIError) as error:
            auth.authenticate(request("Bearer " + token, method=method))
        assert error.value.status == 403
