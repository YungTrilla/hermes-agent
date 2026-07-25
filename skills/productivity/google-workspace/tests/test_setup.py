import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_setup():
    spec = importlib.util.spec_from_file_location("google_setup", SCRIPTS / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_email_only_scopes_are_oauth_scopes_with_no_api_key():
    setup = load_setup()
    scopes = setup.scopes_for_services("email")
    assert scopes == [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
    ]
    assert all("api_key" not in scope and "key=" not in scope for scope in scopes)


def test_email_only_token_is_not_reported_as_partial():
    setup = load_setup()
    email_scopes = setup.scopes_for_services("email")
    payload = {"scopes": email_scopes, "hermes_requested_scopes": email_scopes}
    assert setup._missing_scopes_from_payload(payload) == []


def test_live_check_uses_scope_neutral_token_introspection(monkeypatch):
    setup = load_setup()
    email_scopes = setup.scopes_for_services("email")

    class FakeCredentials:
        token = "secret-token-not-for-output"

        @classmethod
        def from_authorized_user_file(cls, path):
            return cls()

    modules = {
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.ModuleType("google.oauth2.credentials"),
    }
    setattr(modules["google.oauth2.credentials"], "Credentials", FakeCredentials)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(setup, "check_auth", lambda quiet=True: True)
    monkeypatch.setattr(
        setup,
        "_load_token_payload",
        lambda path: {"scopes": email_scopes, "hermes_requested_scopes": email_scopes},
    )
    monkeypatch.setattr(setup, "_live_token_scopes", lambda token: email_scopes)

    assert setup.check_auth_live() is True


def test_service_selection_rejects_unknown_names():
    setup = load_setup()
    with pytest.raises(ValueError):
        setup.scopes_for_services("email,unknown")


def test_profile_aware_home_honors_hermes_home(tmp_path):
    code = f"import sys;sys.path.insert(0,{str(SCRIPTS)!r});from _hermes_home import get_hermes_home;print(get_hermes_home())"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path)


def test_auth_url_cli_accepts_email_services_and_json_format(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "setup.py"), "--auth-url", "--services", "email", "--format", "json"],
        capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "unrecognized arguments" not in result.stderr
    assert "client secret" in result.stdout.lower()
