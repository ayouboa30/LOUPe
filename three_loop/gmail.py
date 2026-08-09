"""Small, read-only Gmail client used by the local 3loop server.

Uses IMAP with a Google "app password" instead of OAuth: OAuth would require
each user to create their own Google Cloud project and register a desktop
client before they could connect anything, which is far too much friction
for a beta tool. An app password is two clicks in Gmail's own settings
(IMAP must be enabled, and 2-step verification turned on to generate one)
and never leaves this machine - it is stored locally next to the app's other
local-only config, the same trust model the previous OAuth token already had.

Every fetch uses ``BODY.PEEK[]`` rather than plain ``FETCH ... RFC822`` so
reading a message never marks it as seen in the user's real inbox, matching
the "read-only" promise made in the UI.
"""

from __future__ import annotations

import email as email_lib
import imaplib
import json
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
GMAIL_DEFAULT_QUERY = "in:anywhere -label:SPAM -category:promotions newer_than:1d"
GMAIL_MAX_MESSAGES = 25


class GmailError(RuntimeError):
    """Base error for configuration and IMAP failures."""


class GmailConfigurationError(GmailError):
    """No Gmail address/app password has been saved yet."""


class GmailAuthError(GmailError):
    """The IMAP server rejected the stored address/app password."""


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


def _html_to_text(value: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    except Exception:
        value = value
    return value


def _clean_text(value: str, *, limit: int = 20_000) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\r", "").split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    return cleaned[:limit]


def _payload_text(parsed: Message) -> tuple[str, str]:
    """Return the preferred plain-text body and an HTML fallback."""

    plain: list[str] = []
    rich: list[str] = []
    parts = parsed.walk() if parsed.is_multipart() else [parsed]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        try:
            raw = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
        except (LookupError, ValueError):
            text = ""
        (plain if content_type == "text/plain" else rich).append(text)
    return _clean_text("\n".join(plain)), _clean_text(_html_to_text("\n".join(rich)))


def _message_from_email(uid: str, parsed: Message) -> GmailMessage:
    sender_name, sender_email = parseaddr(_decode_header(parsed.get("From", "")))
    date_value = parsed.get("Date", "")
    try:
        date_value = parsedate_to_datetime(date_value).isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    plain, rich = _payload_text(parsed)
    snippet = _clean_text(plain or rich, limit=600)
    return GmailMessage(
        id=uid,
        thread_id="",
        sender_name=sender_name.strip(),
        sender_email=sender_email.strip(),
        subject=_decode_header(str(parsed.get("Subject", ""))).strip(),
        date=str(date_value).strip(),
        snippet=snippet,
        body=plain or rich or snippet,
        labels=(),
    )


def _credentials_path() -> Path:
    return Path.home() / ".3loop" / "gmail_imap.json"


class GmailClient:
    """Read-only Gmail access over IMAP, authenticated with an app password."""

    def __init__(self, *, credentials_path: Path | None = None) -> None:
        self.credentials_path = credentials_path or _credentials_path()

    def status(self) -> dict[str, Any]:
        creds = self._read_credentials()
        configured = bool(creds and creds.get("email") and creds.get("app_password"))
        return {
            "configured": configured,
            "connected": configured,
            "email": str(creds.get("email", "")) if creds else "",
            "config_path": str(self.credentials_path),
        }

    def configure(self, email_address: str, app_password: str) -> dict[str, Any]:
        """Validate and persist an address + app password.

        Logging in immediately, rather than only on the next read, means a
        typo in the password is reported right where the user typed it.
        """

        normalized_email = str(email_address or "").strip()
        normalized_password = str(app_password or "").replace(" ", "").strip()
        if "@" not in normalized_email:
            raise GmailConfigurationError("Adresse Gmail invalide.")
        if not normalized_password:
            raise GmailConfigurationError("Le mot de passe d'application est obligatoire.")
        connection = self._login(normalized_email, normalized_password)
        try:
            connection.logout()
        except imaplib.IMAP4.error:
            pass
        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        value = {"email": normalized_email, "app_password": normalized_password}
        temporary = self.credentials_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.credentials_path)
        return {"configured": True, "connected": True, "email": normalized_email}

    def list_messages(
        self,
        *,
        query: str = GMAIL_DEFAULT_QUERY,
        limit: int = GMAIL_MAX_MESSAGES,
    ) -> list[GmailMessage]:
        safe_limit = max(1, min(GMAIL_MAX_MESSAGES, int(limit)))
        creds = self._read_credentials()
        if not creds or not creds.get("email") or not creds.get("app_password"):
            raise GmailConfigurationError("Connecte d'abord un compte Gmail (adresse + mot de passe d'application).")
        connection = self._login(str(creds["email"]), str(creds["app_password"]))
        try:
            status, _data = connection.select("INBOX", readonly=True)
            if status != "OK":
                raise GmailError("Impossible d'ouvrir la boîte de réception Gmail.")
            # X-GM-RAW is a Gmail-only IMAP extension accepting the exact
            # syntax as the Gmail search box, so the previous REST client's
            # query (date + category filtering included) still works as-is.
            status, data = connection.uid("SEARCH", None, "X-GM-RAW", f'"{query}"')
            if status != "OK":
                raise GmailError("Recherche Gmail impossible.")
            uids = data[0].split() if data and data[0] else []
            uids = uids[-safe_limit:]
            messages: list[GmailMessage] = []
            for uid in reversed(uids):
                message = self._fetch_message(connection, uid)
                if message is not None:
                    messages.append(message)
            return messages
        finally:
            try:
                connection.close()
            except imaplib.IMAP4.error:
                pass
            try:
                connection.logout()
            except imaplib.IMAP4.error:
                pass

    def _fetch_message(self, connection: imaplib.IMAP4_SSL, uid: bytes) -> GmailMessage | None:
        try:
            status, data = connection.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK" or not data or not isinstance(data[0], tuple):
                return None
            raw = data[0][1]
        except imaplib.IMAP4.error:
            return None
        parsed = email_lib.message_from_bytes(raw)
        try:
            return _message_from_email(uid.decode("ascii", errors="ignore"), parsed)
        except Exception:
            # One malformed message must not block the whole digest.
            return None

    def _login(self, email_address: str, app_password: str) -> imaplib.IMAP4_SSL:
        try:
            connection = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20)
        except OSError as exc:
            raise GmailAuthError(f"IMAP Gmail indisponible : {exc}") from exc
        try:
            connection.login(email_address, app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailAuthError(
                "Connexion IMAP refusée. Vérifie l’adresse et le mot de passe d’application "
                "(IMAP doit être activé dans les paramètres Gmail, sous Transfert et POP/IMAP)."
            ) from exc
        return connection

    def _read_credentials(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None


def fallback_classification(message: GmailMessage) -> str:
    """Classify safely into the three Gmail categories without a model."""

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
