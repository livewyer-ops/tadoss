"""TADS authentication and configurable operation permissions."""

import base64
import binascii
import hmac
from dataclasses import dataclass
from typing import Any, cast

import jwt
from fastapi import Request

from tadoss.domain.validation import APIError
from tadoss.settings import Settings


@dataclass(frozen=True)
class Identity:
    subject: str
    admin: bool = False
    scopes: frozenset[str] = frozenset()
    method: str = "none"


class Authentication:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.require_auth()
        self.jwks = (
            jwt.PyJWKClient(settings.oauth2_jwks_uri, timeout=5, lifespan=300)
            if settings.oauth2_issuer
            else None
        )

    def authenticate(self, request: Request) -> Identity:
        settings = self.settings
        authorization = request.headers.get("authorization", "")
        scheme, _, credential = authorization.partition(" ")
        token = request.query_params.get("access_token")
        if authorization and token:
            raise APIError(400, "Supply only one authentication method")
        if (
            token
            and settings.api_token
            and hmac.compare_digest(token.encode(), settings.api_token.encode())
        ):
            return Identity("api-token", True, method="url-token")
        if scheme.lower() == "basic" and settings.basic_auth_password:
            try:
                user, _, password = (
                    base64.b64decode(credential, validate=True).decode().partition(":")
                )
                if hmac.compare_digest(
                    user.encode(), settings.basic_auth_username.encode()
                ) and hmac.compare_digest(
                    password.encode(), settings.basic_auth_password.encode()
                ):
                    return Identity(user, True, method="basic")
            except (ValueError, UnicodeError, binascii.Error):
                pass
        if scheme.lower() == "bearer" and self.jwks:
            try:
                key = self.jwks.get_signing_key_from_jwt(credential)
                claims = jwt.decode(
                    credential,
                    key.key,
                    algorithms=settings.oauth2_algorithms.split(","),
                    issuer=settings.oauth2_issuer,
                    audience=settings.oauth2_audience,
                    options={"require": ["exp", "sub", "iss", "aud"]},
                )
                subject = claims["sub"]
                scopes = _token_scopes(claims)
                if not isinstance(subject, str) or not subject:
                    raise ValueError("Invalid subject")
                identity = Identity(
                    subject,
                    settings.oauth2_admin_scope in scopes,
                    frozenset(scopes),
                    "jwt",
                )
                permission = (
                    "read"
                    if request.method in {"GET", "HEAD"}
                    else "delete"
                    if request.method == "DELETE"
                    else "write"
                )
                if (
                    not identity.admin
                    and getattr(settings, "oauth2_" + permission + "_scope")
                    not in identity.scopes
                ):
                    raise APIError(403, "Token does not permit this operation")
                return identity
            except (jwt.PyJWTError, ValueError, TypeError, AttributeError):
                pass
        if settings.dev and not authorization and not token:
            return Identity("development", True)
        raise APIError(401, "Valid authentication is required")


def _token_scopes(claims: dict[str, Any]) -> set[str]:
    raw_scope = claims.get("scope", claims.get("scp", ""))
    if isinstance(raw_scope, str):
        return {scope for scope in raw_scope.split() if scope}
    if isinstance(raw_scope, list):
        return {
            scope
            for scope in cast(list[object], raw_scope)
            if isinstance(scope, str) and scope
        }
    return set()
