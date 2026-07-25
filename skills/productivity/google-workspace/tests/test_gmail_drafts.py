import argparse
import base64
import importlib.util
import json
import os
import subprocess
import sys
import types
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("google_api", SCRIPTS / "google_api.py")
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)


class Http404(Exception):
    def __init__(self):
        self.resp = SimpleNamespace(status=404)
        super().__init__("not found")


class Request:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def payload_from_raw(raw, message_id, thread_id=""):
    msg = message_from_bytes(base64.urlsafe_b64decode(raw.encode()))
    body = msg.get_payload(decode=True) or b""
    return {
        "id": message_id,
        "threadId": thread_id,
        "raw": raw,
        "labelIds": ["DRAFT"],
        "payload": {
            "mimeType": msg.get_content_type(),
            "headers": [{"name": k, "value": v} for k, v in msg.items()],
            "body": {"data": base64.urlsafe_b64encode(body).decode()},
        },
    }


class FakeDrafts:
    def __init__(self, state, calls):
        self.state = state
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(("drafts.create", kwargs))
        def run():
            did = f"d{len(self.state) + 1}"
            mid = f"m{len(self.state) + 1}"
            raw = kwargs["body"]["message"]["raw"]
            thread = kwargs["body"]["message"].get("threadId", "")
            self.state[did] = {"id": did, "message": payload_from_raw(raw, mid, thread)}
            return {"id": did, "message": {"id": mid, "threadId": thread}}
        return Request(run)

    def list(self, **kwargs):
        self.calls.append(("drafts.list", kwargs))
        return Request(lambda: {"drafts": [{"id": k, "message": {"id": v["message"]["id"]}} for k, v in self.state.items()]})

    def get(self, **kwargs):
        self.calls.append(("drafts.get", kwargs))
        def run():
            if kwargs["id"] not in self.state:
                raise Http404()
            return self.state[kwargs["id"]]
        return Request(run)

    def update(self, **kwargs):
        self.calls.append(("drafts.update", kwargs))
        def run():
            did = kwargs["id"]
            old = self.state[did]["message"]
            raw = kwargs["body"]["message"]["raw"]
            thread = kwargs["body"]["message"].get("threadId", "")
            self.state[did] = {"id": did, "message": payload_from_raw(raw, old["id"], thread)}
            return {"id": did, "message": {"id": old["id"], "threadId": thread}}
        return Request(run)

    def delete(self, **kwargs):
        self.calls.append(("drafts.delete", kwargs))
        return Request(lambda: self.state.pop(kwargs["id"], None) and {})

    def send(self, **kwargs):
        self.calls.append(("drafts.send", kwargs))
        def run():
            draft = self.state.pop(kwargs["body"]["id"])
            msg = dict(draft["message"])
            msg["labelIds"] = ["SENT"]
            return msg
        return Request(run)


class FakeMessages:
    def __init__(self, state, calls):
        self.state = state
        self.calls = calls

    def get(self, **kwargs):
        self.calls.append(("messages.get", kwargs))
        def run():
            for draft in self.state.values():
                if draft["message"]["id"] == kwargs["id"]:
                    return draft["message"]
            return {"id": kwargs["id"], "threadId": "thread-sent", "labelIds": ["SENT"], "payload": {"headers": [], "body": {}}}
        return Request(run)


class FakeService:
    def __init__(self):
        self.state = {}
        self.calls = []
        self._drafts = FakeDrafts(self.state, self.calls)
        self._messages = FakeMessages(self.state, self.calls)

    def users(self):
        return self

    def drafts(self):
        return self._drafts

    def messages(self):
        return self._messages


def args(**kwargs):
    defaults = {"to": None, "subject": None, "body": None, "html": False,
                "query": "", "max": 10, "draft_id": None, "clear_to": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def run_python(func, service, namespace):
    with mock.patch.object(ga, "_gws_binary", return_value=None), \
         mock.patch.object(ga, "build_service", return_value=service):
        with mock.patch("builtins.print") as printer:
            func(namespace)
    return json.loads(printer.call_args.args[0])


def seed(service, to="a@example.com", subject="Old", body="Original", html=False, thread="thread-1"):
    raw = ga._build_raw_message(to=to, subject=subject, body=body, html=html)
    service.state["d1"] = {"id": "d1", "message": payload_from_raw(raw, "m1", thread)}


def test_create_plain_text_draft_with_recipient_uses_python_drafts_endpoint_and_verifies():
    service = FakeService()
    result = run_python(ga.gmail_draft_create, service, args(to="a@example.com", subject="Hello", body="Body"))
    assert result == {"status": "drafted", "draftId": "d1", "messageId": "m1", "threadId": "", "to": "a@example.com", "subject": "Hello", "body": "Body", "html": False}
    assert [name for name, _ in service.calls] == ["drafts.create", "drafts.get"]


def test_create_draft_without_recipient_and_html_encoding():
    service = FakeService()
    result = run_python(ga.gmail_draft_create, service, args(subject="HTML", body="<b>Hi</b>", html=True))
    assert result["to"] == ""
    assert result["body"] == "<b>Hi</b>"
    assert result["html"] is True
    create_body = service.calls[0][1]["body"]["message"]
    mime = message_from_bytes(base64.urlsafe_b64decode(create_body["raw"]))
    assert mime.get_content_type() == "text/html"
    assert "To" not in mime
    assert all(name != "drafts.send" for name, _ in service.calls)


def test_list_drafts_with_query_and_get_decodes_body():
    service = FakeService()
    seed(service)
    listed = run_python(ga.gmail_draft_list, service, args(query="to:a@example.com", max=7))
    assert listed[0]["draftId"] == "d1"
    assert listed[0]["body"] == "Original"
    assert service.calls[0] == ("drafts.list", {"userId": "me", "maxResults": 7, "q": "to:a@example.com"})
    got = run_python(ga.gmail_draft_get, service, args(draft_id="d1"))
    assert got["to"] == "a@example.com"
    assert got["subject"] == "Old"


def test_list_without_query_omits_q_parameter():
    service = FakeService()
    run_python(ga.gmail_draft_list, service, args(query="", max=10))
    assert service.calls[0][1] == {"userId": "me", "maxResults": 10}


def test_update_one_field_preserves_recipient_body_and_thread():
    service = FakeService()
    seed(service)
    result = run_python(ga.gmail_draft_update, service, args(draft_id="d1", subject="New"))
    assert (result["to"], result["subject"], result["body"], result["threadId"]) == ("a@example.com", "New", "Original", "thread-1")
    update = next(v for n, v in service.calls if n == "drafts.update")
    assert update["body"]["message"]["threadId"] == "thread-1"
    assert all(name != "drafts.send" for name, _ in service.calls)


def test_update_subject_preserves_cc_custom_headers_and_attachments():
    service = FakeService()
    message = EmailMessage()
    message["To"] = "a@example.com"
    message["Cc"] = "copy@example.com"
    message["Subject"] = "Old"
    message["X-Custom"] = "keep-me"
    message.set_content("Original")
    message.add_attachment(
        b"attachment bytes", maintype="application", subtype="octet-stream", filename="test.bin"
    )
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.state["d1"] = {"id": "d1", "message": payload_from_raw(raw, "m1", "thread-1")}

    run_python(ga.gmail_draft_update, service, args(draft_id="d1", subject="New"))

    update = next(v for n, v in service.calls if n == "drafts.update")
    updated = message_from_bytes(base64.urlsafe_b64decode(update["body"]["message"]["raw"]))
    assert updated["Subject"] == "New"
    assert updated["Cc"] == "copy@example.com"
    assert updated["X-Custom"] == "keep-me"
    attachments = [part for part in updated.walk() if part.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "test.bin"
    assert attachments[0].get_payload(decode=True) == b"attachment bytes"


def test_replace_body_removes_stale_multipart_alternative_and_keeps_attachment():
    message = EmailMessage()
    message.set_content("plain old")
    message.add_alternative("<p>html old</p>", subtype="html")
    message.add_attachment(
        b"attachment bytes", maintype="application", subtype="octet-stream", filename="test.bin"
    )

    ga._replace_body(message, "plain new", html=False)

    inline_text = [
        part.get_content()
        for part in message.walk()
        if not part.is_multipart()
        and part.get_content_maintype() == "text"
        and part.get_content_disposition() != "attachment"
    ]
    assert inline_text == ["plain new"]
    attachments = [part for part in message.walk() if part.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == b"attachment bytes"


def test_update_all_fields_and_explicit_clear_recipient():
    service = FakeService()
    seed(service)
    result = run_python(ga.gmail_draft_update, service, args(draft_id="d1", to="b@example.com", subject="New", body="Changed"))
    assert (result["to"], result["subject"], result["body"]) == ("b@example.com", "New", "Changed")
    result = run_python(ga.gmail_draft_update, service, args(draft_id="d1", clear_to=True))
    assert result["to"] == ""


def test_get_decodes_base64url_mime_body():
    service = FakeService()
    seed(service)
    result = run_python(ga.gmail_draft_get, service, args(draft_id="d1"))
    assert result["body"] == "Original"
    assert result["to"] == "a@example.com"


def test_get_decodes_nested_multipart_body_and_detects_selected_type():
    plain = base64.urlsafe_b64encode(b"Nested plain body").decode()
    html = base64.urlsafe_b64encode(b"<p>Nested HTML body</p>").decode()
    draft = {
        "id": "d1",
        "message": {
            "id": "m1",
            "threadId": "t1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "Subject", "value": "Nested"}],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"data": plain}},
                            {"mimeType": "text/html", "body": {"data": html}},
                        ],
                    },
                    {"mimeType": "application/octet-stream", "filename": "file.bin", "body": {}},
                ],
            },
        },
    }
    output = ga._draft_output(draft)
    assert output["body"] == "Nested plain body"
    assert output["html"] is False


def test_delete_exact_draft_and_verify_not_found():
    service = FakeService()
    seed(service)
    result = run_python(ga.gmail_draft_delete, service, args(draft_id="d1"))
    assert result == {"status": "deleted", "draftId": "d1"}
    assert [n for n, _ in service.calls][-2:] == ["drafts.delete", "drafts.get"]


def test_delete_fails_when_verification_still_finds_draft():
    service = FakeService()
    seed(service)
    service._drafts.delete = lambda **kwargs: Request(lambda: {})
    with pytest.raises(ga.VerificationError):
        run_python(ga.gmail_draft_delete, service, args(draft_id="d1"))


def test_send_exact_draft_verifies_message_and_draft_absence():
    service = FakeService()
    seed(service)
    result = run_python(ga.gmail_draft_send, service, args(draft_id="d1"))
    assert result["status"] == "sent"
    assert result["draftId"] == "d1"
    assert result["messageId"] == "m1"
    assert [n for n, _ in service.calls][-3:] == ["drafts.send", "messages.get", "drafts.get"]


def test_gws_create_command_construction_and_verification():
    created = {"id": "d1", "message": {"id": "m1", "threadId": ""}}
    raw = ga._build_raw_message(to="a@example.com", subject="Hi", body="Body", html=False)
    fetched = {"id": "d1", "message": payload_from_raw(raw, "m1")}
    with mock.patch.object(ga, "_gws_binary", return_value="gws"), \
         mock.patch.object(ga, "_run_gws", side_effect=[created, fetched]) as runner, \
         mock.patch("builtins.print"):
        ga.gmail_draft_create(args(to="a@example.com", subject="Hi", body="Body"))
    assert runner.call_args_list[0].args[0] == ["gmail", "users", "drafts", "create"]
    assert runner.call_args_list[0].kwargs["params"] == {"userId": "me"}
    assert "raw" in runner.call_args_list[0].kwargs["body"]["message"]


def test_base64url_decoder_accepts_unpadded_gmail_data():
    encoded = base64.urlsafe_b64encode("hello ✓".encode()).decode().rstrip("=")
    msg = {"payload": {"body": {"data": encoded}}}
    assert ga._extract_message_body(msg) == "hello ✓"


def test_gws_update_delete_and_send_use_exact_draft_id():
    raw_old = ga._build_raw_message(to="a@example.com", subject="Old", body="Body", html=False)
    raw_new = ga._build_raw_message(to="a@example.com", subject="New", body="Body", html=False)
    old = {"id": "d1", "message": payload_from_raw(raw_old, "m1", "t1")}
    new = {"id": "d1", "message": payload_from_raw(raw_new, "m1", "t1")}
    with mock.patch.object(ga, "_gws_binary", return_value="gws"), \
         mock.patch.object(ga, "_run_gws", side_effect=[old, {"id": "d1"}, new]) as runner, \
         mock.patch("builtins.print"):
        ga.gmail_draft_update(args(draft_id="d1", subject="New"))
    assert runner.call_args_list[1].args[0] == ["gmail", "users", "drafts", "update"]
    assert runner.call_args_list[1].kwargs["params"]["id"] == "d1"

    with mock.patch.object(ga, "_gws_binary", return_value="gws"), \
         mock.patch.object(ga, "_run_gws", side_effect=[{}, None]) as runner, \
         mock.patch("builtins.print"):
        ga.gmail_draft_delete(args(draft_id="d1"))
    assert runner.call_args_list[0].args[0] == ["gmail", "users", "drafts", "delete"]
    assert runner.call_args_list[0].kwargs["params"]["id"] == "d1"

    with mock.patch.object(ga, "_gws_binary", return_value="gws"), \
         mock.patch.object(ga, "_run_gws", side_effect=[{"id": "m1", "threadId": "t1"}, {"id": "m1"}, None]) as runner, \
         mock.patch("builtins.print"):
        ga.gmail_draft_send(args(draft_id="d1"))
    assert runner.call_args_list[0].args[0] == ["gmail", "users", "drafts", "send"]
    assert runner.call_args_list[0].kwargs["body"] == {"id": "d1"}


def test_cli_help_exposes_all_draft_operations_and_argument_validation():
    script = str(SCRIPTS / "google_api.py")
    env = {**os.environ, "HERMES_GWS_BIN": ""}
    result = subprocess.run([sys.executable, script, "gmail", "draft", "--help"], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    for command in ("create", "list", "get", "update", "delete", "send"):
        assert command in result.stdout
    bad = subprocess.run([sys.executable, script, "gmail", "draft", "update", "d1"], capture_output=True, text=True, env=env)
    assert bad.returncode != 0


def test_main_emits_structured_nonzero_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["google_api.py", "gmail", "draft", "get", "missing"])
    monkeypatch.setattr(ga, "gmail_draft_get", lambda _args: (_ for _ in ()).throw(ga.VerificationError("nope")))
    with pytest.raises(SystemExit) as exc:
        ga.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().err)["error"] == "verification_failed"


def test_missing_oauth_credentials_is_structured_nonzero_failure(tmp_path):
    script = str(SCRIPTS / "google_api.py")
    result = subprocess.run(
        [sys.executable, script, "gmail", "draft", "create", "--subject", "Test", "--body", "Body"],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_GWS_BIN": ""},
    )
    assert result.returncode == 1
    error = json.loads(result.stderr)
    assert error["status"] == "error"
    assert error["error"] == "not_authenticated"
    assert "token" not in error.get("details", {})


def test_expired_oauth_token_refreshes_and_is_saved_profile_locally(tmp_path, monkeypatch):
    token_path = tmp_path / "google_token.json"
    token_path.write_text(json.dumps({
        "token": "old",
        "refresh_token": "refresh",
        "scopes": ["scope"],
        "hermes_requested_scopes": ["scope"],
    }))

    class FakeCredentials:
        expired = True
        refresh_token = "refresh"
        valid = False

        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            assert path == str(token_path)
            assert scopes == ["scope"]
            return cls()

        def refresh(self, request):
            self.expired = False
            self.valid = True

        def to_json(self):
            return json.dumps({"token": "new", "refresh_token": "refresh", "scopes": ["scope"]})

    modules = {
        "google": types.ModuleType("google"),
        "google.oauth2": types.ModuleType("google.oauth2"),
        "google.oauth2.credentials": types.ModuleType("google.oauth2.credentials"),
        "google.auth": types.ModuleType("google.auth"),
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": types.ModuleType("google.auth.transport.requests"),
    }
    modules["google.oauth2.credentials"].Credentials = FakeCredentials
    modules["google.auth.transport.requests"].Request = object
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(ga, "TOKEN_PATH", token_path)

    creds = ga.get_credentials()
    assert creds.valid is True
    saved = json.loads(token_path.read_text())
    assert saved["token"] == "new"
    assert saved["type"] == "authorized_user"
    assert saved["hermes_requested_scopes"] == ["scope"]
