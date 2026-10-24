import os
from unittest.mock import patch

import pytest
from tadoss.settings import Settings


@pytest.mark.parametrize(
    "name,value",
    [
        ("TADOSS_POOL_MIN_SIZE", "99"),
        ("TADOSS_DELETE_MAX_ATTEMPTS", "0"),
        ("TADOSS_POOL_TIMEOUT", "nan"),
        ("TADOSS_DOCS", "maybe"),
        ("TADOSS_OAUTH2_ISSUER", "http://issuer.test"),
        ("TADOSS_PORT", "65536"),
        ("TADOSS_SCHEMA_TARGET", "future"),
    ],
)
def test_invalid_configuration_names_setting(name, value):
    with (
        patch.dict(os.environ, {name: value}),
        pytest.raises(ValueError, match=r"TADOSS_|OAuth2"),
    ):
        Settings.load()


def test_secret_file_and_external_ca(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("secret\n")
    with patch.dict(os.environ, {"TADOSS_API_TOKEN_FILE": str(secret)}):
        assert Settings.load().api_token == "secret"
        with (
            patch.dict(os.environ, {"TADOSS_API_TOKEN": "inline"}),
            pytest.raises(ValueError),
        ):
            Settings.load()
    settings = Settings(
        database="postgresql://user:pass@db/test?sslmode=disable",
        database_ssl_root_cert="/ca.pem",
    )
    assert "sslmode=verify-full" in settings.database_url()


def test_boolean_file_is_parsed(tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("false\n")
    with patch.dict(os.environ, {"TADOSS_DOCS_FILE": str(flag)}):
        assert Settings.load().docs is False


def test_delete_attempts_file(tmp_path):
    attempts = tmp_path / "attempts"
    attempts.write_text("3\n")
    with patch.dict(os.environ, {"TADOSS_DELETE_MAX_ATTEMPTS_FILE": str(attempts)}):
        assert Settings.load().delete_max_attempts == 3
