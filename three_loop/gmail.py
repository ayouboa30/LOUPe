"""Small, read-only Gmail API client used by the local 3loop server.

The project deliberately does not make Google's client libraries mandatory:
the desktop bundle remains dependency-free and OAuth is implemented with the
standard library. A Google OAuth desktop client can be supplied through
``THREELOOP_GMAIL_CLIENT_ID``/``THREELOOP_GMAIL_CLIENT_SECRET`` or through
``~/.3loop/gmail_client.json``. Tokens never cross into the browser.
"""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_DEFAULT_QUERY = "in:anywhere -label:SPAM -category:promotions newer_than:1d"
GMAIL_MAX_MESSAGES = 25
_GMAIL_DETAIL_WORKERS = 6
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_STATE_TTL = 600.0


class GmailError(RuntimeError):
    """Base error for configuration, OAuth, and Gmail API failures."""


class GmailConfigurationError(GmailError):
    """The user has not supplied a Google OAuth client configuration."""


class GmailAuthError(GmailError):
    """OAuth credentials are missing, invalid, or expired."""


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str
    sender_name: str
    sender_email: str
    subject: str
    date: str
    snippet: str
    body: str
    labels: tuple[str, ...]

    def as_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "sender": self.sender_name or self.sender_email,
            "subject": self.subject or "(sans objet)",
            "date": self.date,
            "snippet": self.snippet,
            "labels": list(self.labels),
        }
        if include_body:
            value["body"] = self.body
        return value


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._ignored += 1
        elif tag.lower() in {"br", "p", "div", "li", "tr"} and self._ignored == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
        elif tag.lower() in {"p", "div", "li", "tr"} and self._ignored == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored == 0:
            self.parts.append(data)


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, ValueError):
        return value


def _decode_body(data: str) -> str:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _html_to_text(value: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    except Exception:
        value = html.unescape(value)
    return value


def _clean_text(value: str, *, limit: int = 20_000) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "").split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    return cleaned[:limit]


def _payload_text(payload: dict[str, Any]) -> tuple[str, str]:
    """Return the preferred plain-text body and an HTML fallback."""

    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = str(part.get("mimeType", "")).lower()
        body = part.get("body") or {}
        data = body.get("data") if isinstance(body, dict) else None
        if data:
            decoded = _decode_body(str(data))
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                rich.append(decoded)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return _clean_text("\n".join(plain)), _clean_text(_html_to_text("\n".join(rich)))


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in payload.get("headers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).lower()
        if name and name not in result:
            result[name] = _decode_header(str(item.get("value", "")))
    return result


def _message_from_api(raw: dict[str, Any]) -> GmailMessage:
    payload = raw.get("payload") or {}
    headers = _header_map(payload)
    sender_name, sender_email = parseaddr(headers.get("from", ""))
    date_value = headers.get("date", "")
    try:
        date_value = parsedate_to_datetime(date_value).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    plain, rich = _payload_text(payload)
    body = plain or rich or str(raw.get("snippet", ""))
    return GmailMessage(
        id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        sender_name=sender_name.strip(),
        sender_email=sender_email.strip(),
        subject=headers.get("subject", "").strip(),
        date=date_value.strip(),
        snippet=_clean_text(str(raw.get("snippet", "")), limit=600),
        body=body,
        labels=tuple(str(value) for value in raw.get("labelIds") or []),
    )


def _client_config() -> tuple[str, str, str]:
    client_id = os.environ.get("THREELOOP_GMAIL_CLIENT_ID", "").strip()
    client_secret = os.environ.get("THREELOOP_GMAIL_CLIENT_SECRET", "").strip()
    source = "variables d'environnement"
    path = Path.home() / ".3loop" / "gmail_client.json"
    if not client_id and path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            value = value.get("installed", value.get("web", value)) if isinstance(value, dict) else {}
            client_id = str(value.get("client_id", "")).strip()
            client_secret = str(value.get("client_secret", "")).strip()
            source = str(path)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    return client_id, client_secret, source


_PENDING_STATES: dict[str, tuple[float, str]] = {}
_PENDING_STATES_LOCK = threading.Lock()


class GmailClient:
    """OAuth and Gmail REST client restricted to ``gmail.readonly``."""

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.token_path = token_path or (Path.home() / ".3loop" / "gmail_token.json")
        self._opener = opener

    @property
    def client_configured(self) -> bool:
        return bool(_client_config()[0])

    def status(self) -> dict[str, Any]:
        client_id, _secret, source = _client_config()
        token = self._read_token()
        connected = bool(token and (token.get("refresh_token") or token.get("access_token")))
        return {
            "configured": bool(client_id),
            "connected": connected,
            "email": str(token.get("email", "")) if token else "",
            "scope": GMAIL_SCOPE,
            "config_source": source if client_id else "",
            "config_path": str(Path.home() / ".3loop" / "gmail_client.json"),
        }

    def configure_client(self, client_id: str, client_secret: str = "") -> dict[str, Any]:
        """Persist OAuth client credentials entered in the local UI.

        The secret is written only to the backend's local config file and is
        never returned. Changing the OAuth client invalidates the old Gmail
        token so it cannot accidentally be reused with another client.
        """

        normalized_id = str(client_id or "").strip()
        normalized_secret = str(client_secret or "").strip()
        if not normalized_id:
            raise GmailConfigurationError("Le Client ID Google est obligatoire.")
        if len(normalized_id) > 512 or len(normalized_secret) > 2048:
            raise GmailConfigurationError("Les identifiants OAuth sont trop longs.")
        old_id, old_secret, _source = _client_config()
        path = Path.home() / ".3loop" / "gmail_client.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "installed": {
                "client_id": normalized_id,
                "client_secret": normalized_secret,
            }
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        if old_id and (old_id != normalized_id or old_secret != normalized_secret):
            try:
                self.token_path.unlink()
            except FileNotFoundError:
                pass
        return {"configured": True, "config_path": str(path)}

    def begin_authorization(self, redirect_uri: str) -> str:
        client_id, _secret, _source = _client_config()
        if not client_id:
            raise GmailConfigurationError(
                "Configure un client OAuth Google de type application de bureau dans "
                "~/.3loop/gmail_client.json ou avec THREELOOP_GMAIL_CLIENT_ID."
            )
        state = secrets.token_urlsafe(32)
        with _PENDING_STATES_LOCK:
            now = time.time()
            for key, (created, _redirect) in list(_PENDING_STATES.items()):
                if now - created > _STATE_TTL:
                    _PENDING_STATES.pop(key, None)
            _PENDING_STATES[state] = (now, redirect_uri)
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GMAIL_SCOPE,
            "access_type": "offline",
            "state": state,
        }
        # Ask for consent only on the first authorization. Reusing a saved
        # refresh token must not present the consent screen on every manual
        # reconnect, while offline access still guarantees persistence.
        saved_token = self._read_token()
        if not saved_token or not saved_token.get("refresh_token"):
            params["prompt"] = "consent"
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    def complete_authorization(self, *, code: str, state: str, redirect_uri: str) -> None:
        if not code or not state:
            raise GmailAuthError("Retour OAuth Gmail incomplet.")
        with _PENDING_STATES_LOCK:
            pending = _PENDING_STATES.pop(state, None)
        if pending is None or time.time() - pending[0] > _STATE_TTL or pending[1] != redirect_uri:
            raise GmailAuthError("État OAuth Gmail invalide ou expiré.")
        client_id, client_secret, _source = _client_config()
        if not client_id:
            raise GmailConfigurationError("Configuration OAuth Gmail absente.")
        form = {
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
        if client_secret:
            form["client_secret"] = client_secret
        token = self._post_form(_TOKEN_URL, form)
        if not token.get("access_token"):
            raise GmailAuthError("Google n'a pas renvoyé de jeton d'accès.")
        token["expires_at"] = time.time() + max(0, int(token.get("expires_in", 3600)))
        self._write_token(token)

    def list_messages(
        self,
        *,
        query: str = GMAIL_DEFAULT_QUERY,
        limit: int = GMAIL_MAX_MESSAGES,
    ) -> list[GmailMessage]:
        token = self._access_token()
        safe_limit = max(1, min(GMAIL_MAX_MESSAGES, int(limit)))
        data = self._api_json(
            "/messages",
            token,
            params={"maxResults": str(safe_limit), "q": query[:500]},
        )
        message_ids = [
            str(item.get("id", ""))
            for item in data.get("messages") or []
            if isinstance(item, dict) and item.get("id")
        ]
        if not message_ids:
            return []

        def load_message(message_id: str) -> GmailMessage | None:
            try:
                raw = self._api_json(f"/messages/{message_id}", token, params={"format": "full"})
                return _message_from_api(raw)
            except GmailError:
                # Keep one problematic message from blocking the daily digest.
                return None

        # Gmail returns one lightweight list response followed by one detail
        # response per message. Fetch details in a small bounded pool: enough
        # to remove the 25-request serial network delay without hammering the
        # API or creating an unbounded thread count.
        workers = min(_GMAIL_DETAIL_WORKERS, len(message_ids))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gmail") as pool:
            return [message for message in pool.map(load_message, message_ids) if message is not None]

    def profile_email(self) -> str:
        token = self._access_token()
        profile = self._api_json("/profile", token)
        email = str(profile.get("emailAddress", "")).strip()
        if email:
            saved = self._read_token() or {}
            saved["email"] = email
            self._write_token(saved)
        return email

    def _access_token(self) -> str:
        token = self._read_token()
        if not token:
            raise GmailAuthError("Connecte d'abord un compte Gmail.")
        if token.get("access_token") and float(token.get("expires_at", 0)) > time.time() + 30:
            return str(token["access_token"])
        refresh = str(token.get("refresh_token", ""))
        if not refresh:
            raise GmailAuthError("Le jeton Gmail a expiré. Reconnecte le compte.")
        client_id, client_secret, _source = _client_config()
        form = {"client_id": client_id, "refresh_token": refresh, "grant_type": "refresh_token"}
        if client_secret:
            form["client_secret"] = client_secret
        refreshed = self._post_form(_TOKEN_URL, form)
        token.update(refreshed)
        token["refresh_token"] = refresh
        token["expires_at"] = time.time() + max(0, int(refreshed.get("expires_in", 3600)))
        self._write_token(token)
        return str(token.get("access_token", ""))

    def _api_json(self, path: str, access_token: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{_GMAIL_API}{path}{query}",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        try:
            with self._opener(request, timeout=20) as response:
                value = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            detail = getattr(exc, "reason", None) or str(exc)
            raise GmailError(f"API Gmail indisponible : {detail}") from exc
        if not isinstance(value, dict):
            raise GmailError("Réponse Gmail invalide.")
        if value.get("error"):
            raise GmailError(str(value["error"]))
        return value

    def _post_form(self, url: str, values: dict[str, str]) -> dict[str, Any]:
        request = Request(
            url,
            data=urlencode(values).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=20) as response:
                value = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            detail = getattr(exc, "reason", None) or str(exc)
            raise GmailAuthError(f"OAuth Gmail indisponible : {detail}") from exc
        if not isinstance(value, dict) or value.get("error"):
            detail = value.get("error_description") or value.get("error") if isinstance(value, dict) else "réponse invalide"
            raise GmailAuthError(f"OAuth Gmail refusé : {detail}")
        return value

    def _read_token(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_token(self, token: dict[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.token_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(token, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.token_path)


def fallback_classification(message: GmailMessage) -> str:
    """Classify safely into the three Gmail categories without a model."""

    labels = set(message.labels)
    if "CATEGORY_PROMOTIONS" in labels:
        return "publicité"
    text = f"{message.subject} {message.body} {message.sender_email}".casefold()
    advertising_terms = (
        "promotion", "promotions", "promo", "soldes", "réduction", "reduction",
        "remise", "coupon", "newsletter", "marketing", "désabonner", "desabonner",
        "offre spéciale", "offre speciale", "vente privée", "vente privee",
    )
    if any(term in text for term in advertising_terms):
        return "publicité"
    work_terms = (
        "travail", "réunion", "reunion", "meeting", "projet", "client", "équipe",
        "equipe", "collègue", "collegue", "deadline", "contrat", "mission",
        "candidature", "entretien", "jira", "slack", "teams", "office",
        "action requise", "à faire", "a faire",
    )
    if any(term in text for term in work_terms):
        return "travail"
    return "autre"


def fallback_summary(message: GmailMessage) -> str:
    text = message.body or message.snippet or "Aucun contenu exploitable."
    text = " ".join(text.split())
    if len(text) > 360:
        text = text[:357].rsplit(" ", 1)[0] + "…"
    return text
