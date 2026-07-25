import importlib.util
import os
import subprocess
import sys
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
