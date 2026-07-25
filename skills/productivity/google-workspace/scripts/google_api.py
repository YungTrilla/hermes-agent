#!/usr/bin/env python3
"""Google Workspace API CLI for Hermes Agent.

Uses the Google Workspace CLI (`gws`) when available, but preserves the
existing Hermes-facing JSON contract and falls back to the Python client
libraries if `gws` is not installed.

Usage:
  python google_api.py gmail search "is:unread" [--max 10]
  python google_api.py gmail get MESSAGE_ID
  python google_api.py gmail draft create --to user@example.com --subject "Hi" --body "Hello"
  python google_api.py gmail draft list [--query "to:user@example.com"] [--max 10]
  python google_api.py gmail draft get DRAFT_ID
  python google_api.py gmail draft update DRAFT_ID [--to ADDRESS] [--subject TEXT] [--body TEXT]
  python google_api.py gmail draft delete DRAFT_ID
  python google_api.py gmail draft send DRAFT_ID
  python google_api.py gmail send --to user@example.com --subject "Hi" --body "Hello"
  python google_api.py gmail reply MESSAGE_ID --body "Thanks"
  python google_api.py calendar list [--from DATE] [--to DATE] [--calendar primary]
  python google_api.py calendar create --summary "Meeting" --start DATETIME --end DATETIME
  python google_api.py drive search "budget report" [--max 10]
  python google_api.py contacts list [--max 20]
  python google_api.py sheets get SHEET_ID RANGE
  python google_api.py sheets update SHEET_ID RANGE --values '[[...]]'
  python google_api.py sheets append SHEET_ID RANGE --values '[[...]]'
  python google_api.py docs get DOC_ID
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email import encoders, message_from_bytes, policy
from email.message import EmailMessage
from email.mime.text import MIMEText
from pathlib import Path

# Ensure sibling modules (_hermes_home) are importable when run standalone.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _hermes_home import get_hermes_home

HERMES_HOME = get_hermes_home()
TOKEN_PATH = HERMES_HOME / "google_token.json"
CLIENT_SECRET_PATH = HERMES_HOME / "google_client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


class AuthenticationError(RuntimeError):
    """Google OAuth credentials are missing or invalid."""


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _ensure_authenticated():
    if not TOKEN_PATH.exists():
        raise AuthenticationError(
            f"Not authenticated. Run: python {Path(__file__).parent / 'setup.py'} --check"
        )


def _stored_token_scopes() -> list[str]:
    try:
        data = json.loads(TOKEN_PATH.read_text())
    except Exception:
        return list(SCOPES)
    scopes = data.get("scopes")
    if isinstance(scopes, list) and scopes:
        return scopes
    return list(SCOPES)


def _gws_binary() -> str | None:
    override = os.getenv("HERMES_GWS_BIN")
    if override:
        return override
    return shutil.which("gws")


def _gws_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = str(TOKEN_PATH)
    return env


def _run_gws(
    parts: list[str], *, params: dict | None = None, body: dict | None = None,
    allow_not_found: bool = False,
):
    binary = _gws_binary()
    if not binary:
        raise RuntimeError("gws not installed")

    _ensure_authenticated()

    cmd = [binary, *parts]
    if params is not None:
        cmd.extend(["--params", json.dumps(params)])
    if body is not None:
        cmd.extend(["--json", json.dumps(body)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        env=_gws_env(),
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "Unknown gws error"
        if allow_not_found and ("404" in err or "not found" in err.lower()):
            return None
        print(err, file=sys.stderr)
        sys.exit(result.returncode or 1)

    stdout = result.stdout.strip()
    if not stdout:
        return {}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        print("ERROR: Unexpected non-JSON output from gws:", file=sys.stderr)
        print(stdout, file=sys.stderr)
        sys.exit(1)


def _headers_dict(msg: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
        if h.get("name")
    }


def _decode_body_data(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _find_message_body_part(part: dict, mime_type: str) -> dict | None:
    if part.get("filename"):
        return None
    if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
        return part
    for child in part.get("parts", []):
        found = _find_message_body_part(child, mime_type)
        if found is not None:
            return found
    return None


def _extract_message_body_with_type(msg: dict) -> tuple[str, bool]:
    payload = msg.get("payload", {})
    for mime_type in ("text/plain", "text/html"):
        part = _find_message_body_part(payload, mime_type)
        if part is not None:
            return _decode_body_data(part["body"]["data"]), mime_type == "text/html"
    if payload.get("body", {}).get("data"):
        return _decode_body_data(payload["body"]["data"]), payload.get("mimeType") == "text/html"
    return "", False


def _extract_message_body(msg: dict) -> str:
    return _extract_message_body_with_type(msg)[0]


class VerificationError(RuntimeError):
    """A server-side read-back did not confirm a Gmail mutation."""


class UserInputError(ValueError):
    """The requested operation is unsafe or incomplete."""


def _is_not_found(exc: Exception) -> bool:
    return getattr(getattr(exc, "resp", None), "status", None) == 404 or "404" in str(exc)


def _build_raw_message(*, to: str | None, subject: str, body: str, html: bool) -> str:
    message = MIMEText(body, "html" if html else "plain")
    if to:
        message["To"] = to
    message["Subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def _draft_output(draft: dict, *, status: str | None = None) -> dict:
    message = draft.get("message", {})
    headers = _headers_dict(message)
    body, html = _extract_message_body_with_type(message)
    result = {
        "draftId": draft.get("id", ""),
        "messageId": message.get("id", ""),
        "threadId": message.get("threadId", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "body": body,
        "html": html,
    }
    if status:
        result = {"status": status, **result}
    return result


def _get_draft(
    backend: str, service, draft_id: str, *, allow_not_found: bool = False,
    message_format: str = "full",
):
    if backend == "gws":
        return _run_gws(
            ["gmail", "users", "drafts", "get"],
            params={"userId": "me", "id": draft_id, "format": message_format},
            allow_not_found=allow_not_found,
        )
    try:
        return service.users().drafts().get(
            userId="me", id=draft_id, format=message_format,
        ).execute()
    except Exception as exc:
        if allow_not_found and _is_not_found(exc):
            return None
        raise


def _verify_draft_fields(actual: dict, expected: dict) -> None:
    mismatches = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items()
        if actual.get(key) != value
    }
    if mismatches:
        raise VerificationError(f"Gmail draft read-back mismatch: {json.dumps(mismatches, ensure_ascii=False)}")


def _decode_raw_message(raw: str):
    padded = raw + "=" * (-len(raw) % 4)
    return message_from_bytes(base64.urlsafe_b64decode(padded), policy=policy.default)


def _current_draft_from_raw(draft: dict) -> tuple[dict, EmailMessage]:
    raw = draft.get("message", {}).get("raw", "")
    if not raw:
        raise VerificationError("Gmail did not return the draft's raw MIME message")
    message = _decode_raw_message(raw)
    body_part = message.get_body(preferencelist=("plain", "html")) if message.is_multipart() else message
    body = body_part.get_content() if body_part is not None else ""
    current = {
        "to": str(message.get("To", "")),
        "subject": str(message.get("Subject", "")),
        "body": body,
        "html": bool(body_part and body_part.get_content_type() == "text/html"),
        "threadId": draft.get("message", {}).get("threadId", ""),
    }
    return current, message


def _replace_header(message, name: str, value: str) -> None:
    while name in message:
        del message[name]
    if value:
        message[name] = value


def _replace_body(message, body: str, *, html: bool) -> None:
    subtype = "html" if html else "plain"
    if not message.is_multipart():
        _set_text_content_exact(message, body, subtype=subtype)
        return
    candidates = [
        part for part in message.walk()
        if not part.is_multipart()
        and part.get_content_maintype() == "text"
        and part.get_content_disposition() != "attachment"
    ]
    if not candidates:
        raise UserInputError("Cannot update the body of a draft with no editable text MIME part")
    preferred = next((part for part in candidates if part.get_content_subtype() == subtype), candidates[0])
    _set_text_content_exact(preferred, body, subtype=subtype)


def _set_text_content_exact(part, body: str, *, subtype: str) -> None:
    while "Content-Transfer-Encoding" in part:
        del part["Content-Transfer-Encoding"]
    part.set_type(f"text/{subtype}")
    part.set_param("charset", "utf-8", header="Content-Type", replace=True)
    part.set_payload(body.encode("utf-8"))
    encoders.encode_base64(part)


def _extract_doc_text(doc: dict) -> str:
    text_parts = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph", {})
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun", {})
            if text_run.get("content"):
                text_parts.append(text_run["content"])
    return "".join(text_parts)


def _datetime_with_timezone(value: str) -> str:
    if not value:
        return value
    if "T" not in value:
        return value
    if value.endswith("Z"):
        return value
    tail = value[10:]
    if "+" in tail or "-" in tail:
        return value
    return value + "Z"


def get_credentials():
    """Load and refresh credentials from token file."""
    _ensure_authenticated()

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_payload = json.loads(TOKEN_PATH.read_text())
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), _stored_token_scopes())
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        refreshed_payload = _normalize_authorized_user_payload(json.loads(creds.to_json()))
        if token_payload.get("hermes_requested_scopes"):
            refreshed_payload["hermes_requested_scopes"] = token_payload["hermes_requested_scopes"]
        TOKEN_PATH.write_text(json.dumps(refreshed_payload, indent=2))
    if not creds.valid:
        print("Token is invalid. Re-run setup.", file=sys.stderr)
        sys.exit(1)
    return creds


def build_service(api, version):
    credentials = get_credentials()
    from googleapiclient.discovery import build

    return build(api, version, credentials=credentials)


# =========================================================================
# Gmail
# =========================================================================


def gmail_search(args):
    if _gws_binary():
        results = _run_gws(
            ["gmail", "users", "messages", "list"],
            params={"userId": "me", "q": args.query, "maxResults": args.max},
        )
        messages = results.get("messages", [])
        output = []
        for msg_meta in messages:
            msg = _run_gws(
                ["gmail", "users", "messages", "get"],
                params={
                    "userId": "me",
                    "id": msg_meta["id"],
                    "format": "metadata",
                    "metadataHeaders": ["From", "To", "Subject", "Date"],
                },
            )
            headers = _headers_dict(msg)
            output.append(
                {
                    "id": msg["id"],
                    "threadId": msg["threadId"],
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "snippet": msg.get("snippet", ""),
                    "labels": msg.get("labelIds", []),
                }
            )
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return

    service = build_service("gmail", "v1")
    results = service.users().messages().list(
        userId="me", q=args.query, maxResults=args.max
    ).execute()
    messages = results.get("messages", [])
    if not messages:
        print("No messages found.")
        return

    output = []
    for msg_meta in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_meta["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = _headers_dict(msg)
        output.append({
            "id": msg["id"],
            "threadId": msg["threadId"],
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "snippet": msg.get("snippet", ""),
            "labels": msg.get("labelIds", []),
        })
    print(json.dumps(output, indent=2, ensure_ascii=False))



def gmail_get(args):
    if _gws_binary():
        msg = _run_gws(
            ["gmail", "users", "messages", "get"],
            params={"userId": "me", "id": args.message_id, "format": "full"},
        )
        headers = _headers_dict(msg)
        result = {
            "id": msg["id"],
            "threadId": msg["threadId"],
            "from": headers.get("from", ""),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", ""),
            "date": headers.get("date", ""),
            "labels": msg.get("labelIds", []),
            "body": _extract_message_body(msg),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    service = build_service("gmail", "v1")
    msg = service.users().messages().get(
        userId="me", id=args.message_id, format="full"
    ).execute()

    headers = _headers_dict(msg)
    result = {
        "id": msg["id"],
        "threadId": msg["threadId"],
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "labels": msg.get("labelIds", []),
        "body": _extract_message_body(msg),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def gmail_draft_create(args):
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    raw = _build_raw_message(to=args.to, subject=args.subject, body=args.body, html=args.html)
    request_body = {"message": {"raw": raw}}
    if backend == "gws":
        created = _run_gws(
            ["gmail", "users", "drafts", "create"],
            params={"userId": "me"}, body=request_body,
        )
    else:
        created = service.users().drafts().create(userId="me", body=request_body).execute()
    draft_id = created.get("id", "")
    if not draft_id:
        raise VerificationError("Gmail did not return a draft ID after creation")
    verified = _get_draft(backend, service, draft_id)
    output = _draft_output(verified, status="drafted")
    _verify_draft_fields(output, {
        "to": args.to or "", "subject": args.subject, "body": args.body, "html": args.html,
    })
    print(json.dumps(output, indent=2, ensure_ascii=False))


def gmail_draft_list(args):
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    params = {"userId": "me", "maxResults": args.max}
    if args.query:
        params["q"] = args.query
    if backend == "gws":
        result = _run_gws(["gmail", "users", "drafts", "list"], params=params)
    else:
        result = service.users().drafts().list(**params).execute()
    output = []
    for item in result.get("drafts", []):
        output.append(_draft_output(_get_draft(backend, service, item["id"])))
    print(json.dumps(output, indent=2, ensure_ascii=False))


def gmail_draft_get(args):
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    draft = _get_draft(backend, service, args.draft_id)
    print(json.dumps(_draft_output(draft), indent=2, ensure_ascii=False))


def gmail_draft_update(args):
    if args.to is None and args.subject is None and args.body is None and not args.clear_to and not args.html:
        raise UserInputError("Draft update requires at least one changed field")
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    raw_draft = _get_draft(backend, service, args.draft_id, message_format="raw")
    current, mime_message = _current_draft_from_raw(raw_draft)
    to = "" if args.clear_to else (args.to if args.to is not None else current["to"])
    subject = args.subject if args.subject is not None else current["subject"]
    body_text = args.body if args.body is not None else current["body"]
    html = True if args.html else current["html"]
    if args.to is not None or args.clear_to:
        _replace_header(mime_message, "To", to)
    if args.subject is not None:
        _replace_header(mime_message, "Subject", subject)
    if args.body is not None or args.html:
        _replace_body(mime_message, body_text, html=html)
    message = {"raw": base64.urlsafe_b64encode(mime_message.as_bytes()).decode()}
    if current["threadId"]:
        message["threadId"] = current["threadId"]
    request_body = {"message": message}
    if backend == "gws":
        _run_gws(
            ["gmail", "users", "drafts", "update"],
            params={"userId": "me", "id": args.draft_id}, body=request_body,
        )
    else:
        service.users().drafts().update(
            userId="me", id=args.draft_id, body=request_body,
        ).execute()
    output = _draft_output(_get_draft(backend, service, args.draft_id), status="drafted")
    expected = {"threadId": current["threadId"]}
    if args.to is not None or args.clear_to:
        expected["to"] = to
    if args.subject is not None:
        expected["subject"] = subject
    if args.body is not None:
        expected["body"] = body_text
    if args.html:
        expected["html"] = True
    _verify_draft_fields(output, expected)
    print(json.dumps(output, indent=2, ensure_ascii=False))


def gmail_draft_delete(args):
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    if backend == "gws":
        _run_gws(
            ["gmail", "users", "drafts", "delete"],
            params={"userId": "me", "id": args.draft_id},
        )
    else:
        service.users().drafts().delete(userId="me", id=args.draft_id).execute()
    if _get_draft(backend, service, args.draft_id, allow_not_found=True) is not None:
        raise VerificationError(f"Draft {args.draft_id} still exists after deletion")
    print(json.dumps({"status": "deleted", "draftId": args.draft_id}, indent=2))


def gmail_draft_send(args):
    backend = "gws" if _gws_binary() else "python"
    service = None if backend == "gws" else build_service("gmail", "v1")
    if backend == "gws":
        sent = _run_gws(
            ["gmail", "users", "drafts", "send"],
            params={"userId": "me"}, body={"id": args.draft_id},
        )
        message = _run_gws(
            ["gmail", "users", "messages", "get"],
            params={"userId": "me", "id": sent["id"], "format": "metadata"},
        )
    else:
        sent = service.users().drafts().send(
            userId="me", body={"id": args.draft_id},
        ).execute()
        message = service.users().messages().get(
            userId="me", id=sent["id"], format="metadata",
        ).execute()
    if message.get("id") != sent.get("id"):
        raise VerificationError("Sent Gmail message could not be read back")
    if _get_draft(backend, service, args.draft_id, allow_not_found=True) is not None:
        raise VerificationError(f"Draft {args.draft_id} still exists after sending")
    print(json.dumps({
        "status": "sent", "draftId": args.draft_id,
        "messageId": sent["id"], "threadId": sent.get("threadId", ""),
    }, indent=2))



def gmail_send(args):
    if _gws_binary():
        message = MIMEText(args.body, "html" if args.html else "plain")
        message["To"] = args.to
        message["Subject"] = args.subject
        if args.cc:
            message["Cc"] = args.cc
        if args.from_header:
            message["From"] = args.from_header

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body = {"raw": raw}
        if args.thread_id:
            body["threadId"] = args.thread_id

        result = _run_gws(
            ["gmail", "users", "messages", "send"],
            params={"userId": "me"},
            body=body,
        )
        print(json.dumps({"status": "sent", "id": result["id"], "threadId": result.get("threadId", "")}, indent=2))
        return

    service = build_service("gmail", "v1")
    message = MIMEText(args.body, "html" if args.html else "plain")
    message["To"] = args.to
    message["Subject"] = args.subject
    if args.cc:
        message["Cc"] = args.cc
    if args.from_header:
        message["From"] = args.from_header

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {"raw": raw}

    if args.thread_id:
        body["threadId"] = args.thread_id

    result = service.users().messages().send(userId="me", body=body).execute()
    print(json.dumps({"status": "sent", "id": result["id"], "threadId": result.get("threadId", "")}, indent=2))



def gmail_reply(args):
    if _gws_binary():
        original = _run_gws(
            ["gmail", "users", "messages", "get"],
            params={
                "userId": "me",
                "id": args.message_id,
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Message-ID"],
            },
        )
        headers = _headers_dict(original)

        subject = headers.get("subject", "")
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"

        message = MIMEText(args.body)
        message["To"] = headers.get("from", "")
        message["Subject"] = subject
        if args.from_header:
            message["From"] = args.from_header
        if headers.get("message-id"):
            message["In-Reply-To"] = headers["message-id"]
            message["References"] = headers["message-id"]

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = _run_gws(
            ["gmail", "users", "messages", "send"],
            params={"userId": "me"},
            body={"raw": raw, "threadId": original["threadId"]},
        )
        print(json.dumps({"status": "sent", "id": result["id"], "threadId": result.get("threadId", "")}, indent=2))
        return

    service = build_service("gmail", "v1")
    original = service.users().messages().get(
        userId="me", id=args.message_id, format="metadata",
        metadataHeaders=["From", "Subject", "Message-ID"],
    ).execute()
    headers = _headers_dict(original)

    subject = headers.get("subject", "")
    if not subject.startswith("Re:"):
        subject = f"Re: {subject}"

    message = MIMEText(args.body)
    message["To"] = headers.get("from", "")
    message["Subject"] = subject
    if args.from_header:
        message["From"] = args.from_header
    if headers.get("message-id"):
        message["In-Reply-To"] = headers["message-id"]
        message["References"] = headers["message-id"]

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {"raw": raw, "threadId": original["threadId"]}

    result = service.users().messages().send(userId="me", body=body).execute()
    print(json.dumps({"status": "sent", "id": result["id"], "threadId": result.get("threadId", "")}, indent=2))



def gmail_labels(args):
    if _gws_binary():
        results = _run_gws(["gmail", "users", "labels", "list"], params={"userId": "me"})
        labels = [{"id": l["id"], "name": l["name"], "type": l.get("type", "")} for l in results.get("labels", [])]
        print(json.dumps(labels, indent=2))
        return

    service = build_service("gmail", "v1")
    results = service.users().labels().list(userId="me").execute()
    labels = [{"id": l["id"], "name": l["name"], "type": l.get("type", "")} for l in results.get("labels", [])]
    print(json.dumps(labels, indent=2))



def gmail_modify(args):
    body = {}
    if args.add_labels:
        body["addLabelIds"] = args.add_labels.split(",")
    if args.remove_labels:
        body["removeLabelIds"] = args.remove_labels.split(",")

    if _gws_binary():
        result = _run_gws(
            ["gmail", "users", "messages", "modify"],
            params={"userId": "me", "id": args.message_id},
            body=body,
        )
        print(json.dumps({"id": result["id"], "labels": result.get("labelIds", [])}, indent=2))
        return

    service = build_service("gmail", "v1")
    result = service.users().messages().modify(userId="me", id=args.message_id, body=body).execute()
    print(json.dumps({"id": result["id"], "labels": result.get("labelIds", [])}, indent=2))


# =========================================================================
# Calendar
# =========================================================================


def calendar_list(args):
    now = datetime.now(timezone.utc)
    time_min = _datetime_with_timezone(args.start or now.isoformat())
    time_max = _datetime_with_timezone(args.end or (now + timedelta(days=7)).isoformat())

    if _gws_binary():
        results = _run_gws(
            ["calendar", "events", "list"],
            params={
                "calendarId": args.calendar,
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": args.max,
                "singleEvents": True,
                "orderBy": "startTime",
            },
        )
        events = []
        for e in results.get("items", []):
            events.append({
                "id": e["id"],
                "summary": e.get("summary", "(no title)"),
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
                "location": e.get("location", ""),
                "description": e.get("description", ""),
                "status": e.get("status", ""),
                "htmlLink": e.get("htmlLink", ""),
            })
        print(json.dumps(events, indent=2, ensure_ascii=False))
        return

    service = build_service("calendar", "v3")
    results = service.events().list(
        calendarId=args.calendar, timeMin=time_min, timeMax=time_max,
        maxResults=args.max, singleEvents=True, orderBy="startTime",
    ).execute()

    events = []
    for e in results.get("items", []):
        events.append({
            "id": e["id"],
            "summary": e.get("summary", "(no title)"),
            "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
            "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
            "location": e.get("location", ""),
            "description": e.get("description", ""),
            "status": e.get("status", ""),
            "htmlLink": e.get("htmlLink", ""),
        })
    print(json.dumps(events, indent=2, ensure_ascii=False))



def calendar_create(args):
    event = {
        "summary": args.summary,
        "start": {"dateTime": args.start},
        "end": {"dateTime": args.end},
    }
    if args.location:
        event["location"] = args.location
    if args.description:
        event["description"] = args.description
    if args.attendees:
        event["attendees"] = [{"email": e.strip()} for e in args.attendees.split(",") if e.strip()]

    if _gws_binary():
        result = _run_gws(
            ["calendar", "events", "insert"],
            params={"calendarId": args.calendar},
            body=event,
        )
        print(json.dumps({
            "status": "created",
            "id": result["id"],
            "summary": result.get("summary", ""),
            "htmlLink": result.get("htmlLink", ""),
        }, indent=2))
        return

    service = build_service("calendar", "v3")
    result = service.events().insert(calendarId=args.calendar, body=event).execute()
    print(json.dumps({
        "status": "created",
        "id": result["id"],
        "summary": result.get("summary", ""),
        "htmlLink": result.get("htmlLink", ""),
    }, indent=2))



def calendar_delete(args):
    if _gws_binary():
        _run_gws(["calendar", "events", "delete"], params={"calendarId": args.calendar, "eventId": args.event_id})
        print(json.dumps({"status": "deleted", "eventId": args.event_id}))
        return

    service = build_service("calendar", "v3")
    service.events().delete(calendarId=args.calendar, eventId=args.event_id).execute()
    print(json.dumps({"status": "deleted", "eventId": args.event_id}))


# =========================================================================
# Drive
# =========================================================================


def drive_search(args):
    query = args.query if args.raw_query else f"fullText contains '{args.query}'"
    if _gws_binary():
        results = _run_gws(
            ["drive", "files", "list"],
            params={
                "q": query,
                "pageSize": args.max,
                "fields": "files(id, name, mimeType, modifiedTime, webViewLink)",
            },
        )
        print(json.dumps(results.get("files", []), indent=2, ensure_ascii=False))
        return

    service = build_service("drive", "v3")
    results = service.files().list(
        q=query, pageSize=args.max, fields="files(id, name, mimeType, modifiedTime, webViewLink)",
    ).execute()
    files = results.get("files", [])
    print(json.dumps(files, indent=2, ensure_ascii=False))


def drive_get(args):
    """Get metadata for a single Drive file by ID."""
    fields = "id, name, mimeType, modifiedTime, size, webViewLink, parents, owners(emailAddress)"
    if _gws_binary():
        result = _run_gws(
            ["drive", "files", "get"],
            params={"fileId": args.file_id, "fields": fields},
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    service = build_service("drive", "v3")
    result = service.files().get(fileId=args.file_id, fields=fields).execute()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def drive_upload(args):
    """Upload a local file to Drive. Falls through to Python client even when gws
    is installed, because gws doesn't do multipart uploads."""
    import mimetypes
    from googleapiclient.http import MediaFileUpload

    local_path = Path(args.path).expanduser()
    if not local_path.exists():
        print(f"ERROR: file not found: {local_path}", file=sys.stderr)
        sys.exit(1)

    mime = args.mime_type or mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    metadata = {"name": args.name or local_path.name}
    if args.parent:
        metadata["parents"] = [args.parent]

    service = build_service("drive", "v3")
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=True)
    result = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, name, mimeType, webViewLink",
    ).execute()
    print(json.dumps({
        "status": "uploaded",
        "id": result["id"],
        "name": result.get("name", ""),
        "mimeType": result.get("mimeType", ""),
        "webViewLink": result.get("webViewLink", ""),
    }, indent=2, ensure_ascii=False))


def drive_download(args):
    """Download a Drive file to a local path. Google-native files (Docs/Sheets/Slides)
    must be exported; binary files are downloaded as-is."""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    service = build_service("drive", "v3")

    # Look up the file to decide download vs export.
    meta = service.files().get(fileId=args.file_id, fields="id, name, mimeType").execute()
    mime = meta.get("mimeType", "")
    name = meta.get("name", args.file_id)

    # Map Google-native MIME types to a sensible export default.
    native_export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.drawing": ("image/png", ".png"),
    }

    out_path = Path(args.output).expanduser() if args.output else Path.cwd() / name

    if mime in native_export_map:
        export_mime = args.export_mime or native_export_map[mime][0]
        default_ext = native_export_map[mime][1]
        if not args.output and not out_path.suffix:
            out_path = out_path.with_suffix(default_ext)
        request = service.files().export_media(fileId=args.file_id, mimeType=export_mime)
    else:
        request = service.files().get_media(fileId=args.file_id)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = io.FileIO(str(out_path), "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.close()

    print(json.dumps({
        "status": "downloaded",
        "id": args.file_id,
        "name": name,
        "path": str(out_path),
        "mimeType": mime,
    }, indent=2, ensure_ascii=False))


def drive_create_folder(args):
    body = {
        "name": args.name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if args.parent:
        body["parents"] = [args.parent]

    if _gws_binary():
        result = _run_gws(
            ["drive", "files", "create"],
            params={"fields": "id, name, webViewLink"},
            body=body,
        )
        print(json.dumps({
            "status": "created",
            "id": result["id"],
            "name": result.get("name", ""),
            "webViewLink": result.get("webViewLink", ""),
        }, indent=2, ensure_ascii=False))
        return

    service = build_service("drive", "v3")
    result = service.files().create(body=body, fields="id, name, webViewLink").execute()
    print(json.dumps({
        "status": "created",
        "id": result["id"],
        "name": result.get("name", ""),
        "webViewLink": result.get("webViewLink", ""),
    }, indent=2, ensure_ascii=False))


def drive_share(args):
    permission = {
        "type": args.type,
        "role": args.role,
    }
    if args.type in {"user", "group"}:
        if not args.email:
            print("ERROR: --email is required for type=user or type=group", file=sys.stderr)
            sys.exit(1)
        permission["emailAddress"] = args.email
    elif args.type == "domain":
        if not args.domain:
            print("ERROR: --domain is required for type=domain", file=sys.stderr)
            sys.exit(1)
        permission["domain"] = args.domain

    if _gws_binary():
        result = _run_gws(
            ["drive", "permissions", "create"],
            params={
                "fileId": args.file_id,
                "sendNotificationEmail": args.notify,
            },
            body=permission,
        )
        print(json.dumps({
            "status": "shared",
            "permissionId": result.get("id", ""),
            "fileId": args.file_id,
            "role": permission["role"],
            "type": permission["type"],
        }, indent=2, ensure_ascii=False))
        return

    service = build_service("drive", "v3")
    result = service.permissions().create(
        fileId=args.file_id,
        body=permission,
        sendNotificationEmail=args.notify,
        fields="id",
    ).execute()
    print(json.dumps({
        "status": "shared",
        "permissionId": result.get("id", ""),
        "fileId": args.file_id,
        "role": permission["role"],
        "type": permission["type"],
    }, indent=2, ensure_ascii=False))


def drive_delete(args):
    """Trash or permanently delete a Drive file. Defaults to trash (reversible)."""
    if args.permanent:
        if _gws_binary():
            _run_gws(["drive", "files", "delete"], params={"fileId": args.file_id})
            print(json.dumps({"status": "deleted", "fileId": args.file_id, "permanent": True}))
            return
        service = build_service("drive", "v3")
        service.files().delete(fileId=args.file_id).execute()
        print(json.dumps({"status": "deleted", "fileId": args.file_id, "permanent": True}))
        return

    # Trash (reversible). Use files.update with trashed=True.
    body = {"trashed": True}
    if _gws_binary():
        _run_gws(
            ["drive", "files", "update"],
            params={"fileId": args.file_id},
            body=body,
        )
        print(json.dumps({"status": "trashed", "fileId": args.file_id, "permanent": False}))
        return

    service = build_service("drive", "v3")
    service.files().update(fileId=args.file_id, body=body).execute()
    print(json.dumps({"status": "trashed", "fileId": args.file_id, "permanent": False}))


# =========================================================================
# Contacts
# =========================================================================


def contacts_list(args):
    if _gws_binary():
        results = _run_gws(
            ["people", "people", "connections", "list"],
            params={
                "resourceName": "people/me",
                "pageSize": args.max,
                "personFields": "names,emailAddresses,phoneNumbers",
            },
        )
        contacts = []
        for person in results.get("connections", []):
            names = person.get("names", [{}])
            emails = person.get("emailAddresses", [])
            phones = person.get("phoneNumbers", [])
            contacts.append({
                "name": names[0].get("displayName", "") if names else "",
                "emails": [e.get("value", "") for e in emails],
                "phones": [p.get("value", "") for p in phones],
            })
        print(json.dumps(contacts, indent=2, ensure_ascii=False))
        return

    service = build_service("people", "v1")
    results = service.people().connections().list(
        resourceName="people/me",
        pageSize=args.max,
        personFields="names,emailAddresses,phoneNumbers",
    ).execute()
    contacts = []
    for person in results.get("connections", []):
        names = person.get("names", [{}])
        emails = person.get("emailAddresses", [])
        phones = person.get("phoneNumbers", [])
        contacts.append({
            "name": names[0].get("displayName", "") if names else "",
            "emails": [e.get("value", "") for e in emails],
            "phones": [p.get("value", "") for p in phones],
        })
    print(json.dumps(contacts, indent=2, ensure_ascii=False))


# =========================================================================
# Sheets
# =========================================================================


def sheets_get(args):
    if _gws_binary():
        result = _run_gws(
            ["sheets", "spreadsheets", "values", "get"],
            params={"spreadsheetId": args.sheet_id, "range": args.range},
        )
        print(json.dumps(result.get("values", []), indent=2, ensure_ascii=False))
        return

    service = build_service("sheets", "v4")
    result = service.spreadsheets().values().get(
        spreadsheetId=args.sheet_id, range=args.range,
    ).execute()
    print(json.dumps(result.get("values", []), indent=2, ensure_ascii=False))



def sheets_update(args):
    values = json.loads(args.values)
    body = {"values": values}

    if _gws_binary():
        result = _run_gws(
            ["sheets", "spreadsheets", "values", "update"],
            params={
                "spreadsheetId": args.sheet_id,
                "range": args.range,
                "valueInputOption": "USER_ENTERED",
            },
            body=body,
        )
        print(json.dumps({"updatedCells": result.get("updatedCells", 0), "updatedRange": result.get("updatedRange", "")}, indent=2))
        return

    service = build_service("sheets", "v4")
    result = service.spreadsheets().values().update(
        spreadsheetId=args.sheet_id, range=args.range,
        valueInputOption="USER_ENTERED", body=body,
    ).execute()
    print(json.dumps({"updatedCells": result.get("updatedCells", 0), "updatedRange": result.get("updatedRange", "")}, indent=2))



def sheets_append(args):
    values = json.loads(args.values)
    body = {"values": values}

    if _gws_binary():
        result = _run_gws(
            ["sheets", "spreadsheets", "values", "append"],
            params={
                "spreadsheetId": args.sheet_id,
                "range": args.range,
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            body=body,
        )
        print(json.dumps({"updatedCells": result.get("updates", {}).get("updatedCells", 0)}, indent=2))
        return

    service = build_service("sheets", "v4")
    result = service.spreadsheets().values().append(
        spreadsheetId=args.sheet_id, range=args.range,
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", body=body,
    ).execute()
    print(json.dumps({"updatedCells": result.get("updates", {}).get("updatedCells", 0)}, indent=2))


def sheets_create(args):
    """Create a new spreadsheet. Returns the new spreadsheet ID and URL."""
    body = {"properties": {"title": args.title}}
    if args.sheet_name:
        body["sheets"] = [{"properties": {"title": args.sheet_name}}]

    if _gws_binary():
        result = _run_gws(["sheets", "spreadsheets", "create"], body=body)
        print(json.dumps({
            "status": "created",
            "spreadsheetId": result.get("spreadsheetId", ""),
            "title": result.get("properties", {}).get("title", ""),
            "spreadsheetUrl": result.get("spreadsheetUrl", ""),
        }, indent=2, ensure_ascii=False))
        return

    service = build_service("sheets", "v4")
    result = service.spreadsheets().create(
        body=body, fields="spreadsheetId,properties,spreadsheetUrl",
    ).execute()
    print(json.dumps({
        "status": "created",
        "spreadsheetId": result.get("spreadsheetId", ""),
        "title": result.get("properties", {}).get("title", ""),
        "spreadsheetUrl": result.get("spreadsheetUrl", ""),
    }, indent=2, ensure_ascii=False))


# =========================================================================
# Docs
# =========================================================================


def docs_get(args):
    if _gws_binary():
        doc = _run_gws(["docs", "documents", "get"], params={"documentId": args.doc_id})
        result = {
            "title": doc.get("title", ""),
            "documentId": doc.get("documentId", ""),
            "body": _extract_doc_text(doc),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    service = build_service("docs", "v1")
    doc = service.documents().get(documentId=args.doc_id).execute()
    result = {
        "title": doc.get("title", ""),
        "documentId": doc.get("documentId", ""),
        "body": _extract_doc_text(doc),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def docs_create(args):
    """Create a new Doc. Optionally seed it with initial body text."""
    body = {"title": args.title}

    if _gws_binary():
        doc = _run_gws(["docs", "documents", "create"], body=body)
    else:
        service = build_service("docs", "v1")
        doc = service.documents().create(body=body).execute()

    doc_id = doc.get("documentId", "")

    if args.body and doc_id:
        _docs_insert_text(doc_id, args.body, index=1)

    print(json.dumps({
        "status": "created",
        "documentId": doc_id,
        "title": doc.get("title", ""),
        "url": f"https://docs.google.com/document/d/{doc_id}/edit" if doc_id else "",
    }, indent=2, ensure_ascii=False))


def docs_append(args):
    """Append text to the end of an existing Doc."""
    if _gws_binary():
        doc = _run_gws(["docs", "documents", "get"], params={"documentId": args.doc_id})
    else:
        service = build_service("docs", "v1")
        doc = service.documents().get(documentId=args.doc_id).execute()

    # The end-of-body index is one less than the segment endIndex of the body
    # (trailing newline is always at length-1). Docs indexes are 1-based; use
    # endIndex - 1 to insert before the final newline.
    content = doc.get("body", {}).get("content", [])
    end_index = 1
    for element in content:
        ei = element.get("endIndex")
        if isinstance(ei, int) and ei > end_index:
            end_index = ei
    insert_index = max(end_index - 1, 1)

    text = args.text if args.text.endswith("\n") else args.text + "\n"
    _docs_insert_text(args.doc_id, text, index=insert_index)

    print(json.dumps({
        "status": "appended",
        "documentId": args.doc_id,
        "inserted_at": insert_index,
        "characters": len(text),
    }, indent=2, ensure_ascii=False))


def _docs_insert_text(doc_id: str, text: str, index: int) -> None:
    """Send a batchUpdate with a single insertText request."""
    requests = [{
        "insertText": {
            "location": {"index": index},
            "text": text,
        }
    }]
    if _gws_binary():
        _run_gws(
            ["docs", "documents", "batchUpdate"],
            params={"documentId": doc_id},
            body={"requests": requests},
        )
        return

    service = build_service("docs", "v1")
    service.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


# =========================================================================
# CLI parser
# =========================================================================


def main():
    parser = argparse.ArgumentParser(description="Google Workspace API for Hermes Agent")
    sub = parser.add_subparsers(dest="service", required=True)

    # --- Gmail ---
    gmail = sub.add_parser("gmail")
    gmail_sub = gmail.add_subparsers(dest="action", required=True)

    p = gmail_sub.add_parser("search")
    p.add_argument("query", help="Gmail search query (e.g. 'is:unread')")
    p.add_argument("--max", type=int, default=10)
    p.set_defaults(func=gmail_search)

    p = gmail_sub.add_parser("get")
    p.add_argument("message_id")
    p.set_defaults(func=gmail_get)

    draft = gmail_sub.add_parser("draft", help="Create, inspect, update, discard, or send Gmail drafts")
    draft_sub = draft.add_subparsers(dest="draft_action", required=True)

    p = draft_sub.add_parser("create", help="Create and verify a draft without sending")
    p.add_argument("--to", default=None, help="Recipient (optional)")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--html", action="store_true", help="Store body as HTML")
    p.set_defaults(func=gmail_draft_create)

    p = draft_sub.add_parser("list", help="List drafts and read their current fields")
    p.add_argument("--query", default="", help="Gmail draft search query")
    p.add_argument("--max", type=int, default=10)
    p.set_defaults(func=gmail_draft_list)

    p = draft_sub.add_parser("get", help="Read one exact draft")
    p.add_argument("draft_id")
    p.set_defaults(func=gmail_draft_get)

    p = draft_sub.add_parser("update", help="Update supplied fields and preserve all others")
    p.add_argument("draft_id")
    to_group = p.add_mutually_exclusive_group()
    to_group.add_argument("--to", default=None)
    to_group.add_argument("--clear-to", action="store_true", help="Explicitly remove the recipient")
    p.add_argument("--subject", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--html", action="store_true", help="Store the resulting body as HTML")
    p.set_defaults(func=gmail_draft_update)

    p = draft_sub.add_parser("delete", help="Discard one exact draft and verify it is gone")
    p.add_argument("draft_id")
    p.set_defaults(func=gmail_draft_delete)

    p = draft_sub.add_parser("send", help="Send one exact draft (agent approval required)")
    p.add_argument("draft_id")
    p.set_defaults(func=gmail_draft_send)

    p = gmail_sub.add_parser("send")
    p.add_argument("--to", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--cc", default="")
    p.add_argument("--from", dest="from_header", default="", help="Custom From header (e.g. '\"Agent Name\" <user@example.com>')")
    p.add_argument("--html", action="store_true", help="Send body as HTML")
    p.add_argument("--thread-id", default="", help="Thread ID for threading")
    p.set_defaults(func=gmail_send)

    p = gmail_sub.add_parser("reply")
    p.add_argument("message_id", help="Message ID to reply to")
    p.add_argument("--body", required=True)
    p.add_argument("--from", dest="from_header", default="", help="Custom From header (e.g. '\"Agent Name\" <user@example.com>')")
    p.set_defaults(func=gmail_reply)

    p = gmail_sub.add_parser("labels")
    p.set_defaults(func=gmail_labels)

    p = gmail_sub.add_parser("modify")
    p.add_argument("message_id")
    p.add_argument("--add-labels", default="", help="Comma-separated label IDs to add")
    p.add_argument("--remove-labels", default="", help="Comma-separated label IDs to remove")
    p.set_defaults(func=gmail_modify)

    # --- Calendar ---
    cal = sub.add_parser("calendar")
    cal_sub = cal.add_subparsers(dest="action", required=True)

    p = cal_sub.add_parser("list")
    p.add_argument("--start", default="", help="Start time (ISO 8601)")
    p.add_argument("--end", default="", help="End time (ISO 8601)")
    p.add_argument("--max", type=int, default=25)
    p.add_argument("--calendar", default="primary")
    p.set_defaults(func=calendar_list)

    p = cal_sub.add_parser("create")
    p.add_argument("--summary", required=True)
    p.add_argument("--start", required=True, help="Start (ISO 8601 with timezone)")
    p.add_argument("--end", required=True, help="End (ISO 8601 with timezone)")
    p.add_argument("--location", default="")
    p.add_argument("--description", default="")
    p.add_argument("--attendees", default="", help="Comma-separated email addresses")
    p.add_argument("--calendar", default="primary")
    p.set_defaults(func=calendar_create)

    p = cal_sub.add_parser("delete")
    p.add_argument("event_id")
    p.add_argument("--calendar", default="primary")
    p.set_defaults(func=calendar_delete)

    # --- Drive ---
    drv = sub.add_parser("drive")
    drv_sub = drv.add_subparsers(dest="action", required=True)

    p = drv_sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--max", type=int, default=10)
    p.add_argument("--raw-query", action="store_true", help="Use query as raw Drive API query")
    p.set_defaults(func=drive_search)

    p = drv_sub.add_parser("get")
    p.add_argument("file_id")
    p.set_defaults(func=drive_get)

    p = drv_sub.add_parser("upload")
    p.add_argument("path", help="Local file path to upload")
    p.add_argument("--name", default="", help="Override file name in Drive (defaults to local filename)")
    p.add_argument("--parent", default="", help="Parent folder ID")
    p.add_argument("--mime-type", default="", help="Override MIME type (auto-detected if omitted)")
    p.set_defaults(func=drive_upload)

    p = drv_sub.add_parser("download")
    p.add_argument("file_id")
    p.add_argument("--output", default="", help="Local output path (defaults to ./<name> in cwd)")
    p.add_argument("--export-mime", default="", help="Export MIME for Google-native files (overrides defaults: pdf for Docs/Slides, csv for Sheets, png for Drawings)")
    p.set_defaults(func=drive_download)

    p = drv_sub.add_parser("create-folder")
    p.add_argument("name")
    p.add_argument("--parent", default="", help="Parent folder ID (defaults to root)")
    p.set_defaults(func=drive_create_folder)

    p = drv_sub.add_parser("share")
    p.add_argument("file_id")
    p.add_argument("--role", default="reader", choices=["reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"])
    p.add_argument("--type", default="user", choices=["user", "group", "domain", "anyone"])
    p.add_argument("--email", default="", help="Email address (required for type=user or type=group)")
    p.add_argument("--domain", default="", help="Domain (required for type=domain)")
    p.add_argument("--notify", action="store_true", help="Send notification email")
    p.set_defaults(func=drive_share)

    p = drv_sub.add_parser("delete")
    p.add_argument("file_id")
    p.add_argument("--permanent", action="store_true", help="Permanently delete (default is trash, which is reversible)")
    p.set_defaults(func=drive_delete)

    # --- Contacts ---
    con = sub.add_parser("contacts")
    con_sub = con.add_subparsers(dest="action", required=True)

    p = con_sub.add_parser("list")
    p.add_argument("--max", type=int, default=50)
    p.set_defaults(func=contacts_list)

    # --- Sheets ---
    sh = sub.add_parser("sheets")
    sh_sub = sh.add_subparsers(dest="action", required=True)

    p = sh_sub.add_parser("get")
    p.add_argument("sheet_id")
    p.add_argument("range")
    p.set_defaults(func=sheets_get)

    p = sh_sub.add_parser("update")
    p.add_argument("sheet_id")
    p.add_argument("range")
    p.add_argument("--values", required=True, help="JSON array of arrays")
    p.set_defaults(func=sheets_update)

    p = sh_sub.add_parser("append")
    p.add_argument("sheet_id")
    p.add_argument("range")
    p.add_argument("--values", required=True, help="JSON array of arrays")
    p.set_defaults(func=sheets_append)

    p = sh_sub.add_parser("create")
    p.add_argument("--title", required=True, help="Spreadsheet title")
    p.add_argument("--sheet-name", default="", help="Name of the first tab (defaults to 'Sheet1')")
    p.set_defaults(func=sheets_create)

    # --- Docs ---
    docs = sub.add_parser("docs")
    docs_sub = docs.add_subparsers(dest="action", required=True)

    p = docs_sub.add_parser("get")
    p.add_argument("doc_id")
    p.set_defaults(func=docs_get)

    p = docs_sub.add_parser("create")
    p.add_argument("--title", required=True, help="Document title")
    p.add_argument("--body", default="", help="Initial body text (optional)")
    p.set_defaults(func=docs_create)

    p = docs_sub.add_parser("append")
    p.add_argument("doc_id")
    p.add_argument("--text", required=True, help="Text to append to the end of the document")
    p.set_defaults(func=docs_append)

    args = parser.parse_args()
    try:
        args.func(args)
    except (VerificationError, UserInputError, AuthenticationError) as exc:
        if isinstance(exc, VerificationError):
            error_type = "verification_failed"
        elif isinstance(exc, AuthenticationError):
            error_type = "not_authenticated"
        else:
            error_type = "invalid_request"
        print(json.dumps({"status": "error", "error": error_type, "message": str(exc)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
