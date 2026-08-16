from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from html import unescape
from typing import Protocol
from urllib.parse import quote, urlencode

import requests

from config import AppConfig, CloudflareTempEmailConfig, DuckMailConfig, OutlookEmailConfig


@dataclass(frozen=True)
class TempEmailInbox:
    address: str
    token: str


class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox:
        ...

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        ...


class CloudflareTempEmailProvider:
    def __init__(self, config: CloudflareTempEmailConfig):
        self.config = config

    def create_inbox(self, name: str) -> TempEmailInbox:
        response = requests.post(
            f"{self.config.api_url}/admin/new_address",
            headers={"x-admin-auth": self.config.admin_auth, "Content-Type": "application/json"},
            json={"name": name, "domain": self.config.domain, "enablePrefix": False},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        address = data.get("address", "")
        token = data.get("jwt", "")
        if not address or not token:
            raise RuntimeError(f"Email creation failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.config.api_url}/api/mails?limit=5&offset=0",
                    headers=headers,
                    timeout=15,
                )
                data = response.json()
                mails = data.get("results") or data.get("data") or []
                for mail in mails:
                    mail_id = mail.get("id") or mail.get("_id")
                    if not mail_id:
                        continue
                    detail_response = requests.get(
                        f"{self.config.api_url}/api/mail/{mail_id}",
                        headers=headers,
                        timeout=15,
                    )
                    code = _extract_verification_code(detail_response.json().get("raw", ""))
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(5) # 2 -> 5
        return None


class DuckMailProvider:
    def __init__(self, config: DuckMailConfig):
        self.config = config

    def create_inbox(self, name: str) -> TempEmailInbox:
        address = f"{name}@{self.config.domain}"
        password = f"dm_{secrets.token_hex(8)}"

        response = requests.post(
            f"{self.config.api_url}/accounts",
            headers=self._account_headers(),
            json={"address": address, "password": password},
            timeout=15,
        )
        response.raise_for_status()

        token_response = requests.post(
            f"{self.config.api_url}/token",
            headers={"Content-Type": "application/json"},
            json={"address": address, "password": password},
            timeout=15,
        )
        token_response.raise_for_status()
        data = token_response.json()
        token = data.get("token", "")
        if not token:
            raise RuntimeError(f"DuckMail token acquisition failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        headers = {"Authorization": f"Bearer {inbox.token}"}
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{self.config.api_url}/messages?page=1",
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
                messages = data.get("hydra:member") or []
                for message in messages:
                    message_id = message.get("id")
                    if not message_id:
                        continue
                    detail_response = requests.get(
                        f"{self.config.api_url}/messages/{message_id}",
                        headers=headers,
                        timeout=15,
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                    body = _duckmail_message_body(detail)
                    code = _extract_verification_code(body)
                    if code:
                        return code
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None

    def _account_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


class OutlookEmailProvider:
    """Reuse real Outlook mailboxes from an OutlookEmail /api/external pool."""

    def __init__(self, config: OutlookEmailConfig):
        self.config = config
        self._claimed: set[str] = set()

    def create_inbox(self, name: str) -> TempEmailInbox:
        del name  # Outlook accounts come from the pool; the generated prefix is unused.
        accounts = self._list_accounts()
        candidates = [account for account in accounts if self._is_claimable(account)]
        if not candidates:
            raise RuntimeError("No available Outlook accounts in pool")

        chosen = None
        for account in candidates:
            email = str(account.get("email") or "").strip()
            if self.config.skip_disabled and self._is_disabled(email):
                print(f"  skip disabled outlook account: {email}", flush=True)
                continue
            chosen = account
            break

        if chosen is None:
            raise RuntimeError("No available Outlook accounts after disabled-check")

        email = str(chosen.get("email") or "").strip()
        account_id = chosen.get("id")
        self._claimed.add(email.lower())
        return TempEmailInbox(address=email, token=str(account_id or email))

    def finalize_inbox(self, inbox: TempEmailInbox, *, success: bool) -> None:
        target = self.config.success_group_id if success else self.config.failed_group_id
        if target is None:
            return
        label = "success" if success else "failed"
        self._move_account(inbox.address, inbox.token, target, label)

    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None:
        deadline = time.time() + timeout_seconds
        logged_empty = False
        while time.time() < deadline:
            try:
                emails = self._list_emails(inbox.address)
                for mail in emails:
                    code = _extract_verification_code(_outlook_message_body(mail))
                    if code:
                        print(f"  outlook otp from {mail.get('subject') or 'mail'}: {code}", flush=True)
                        return code
                if emails:
                    subjects = ", ".join((mail.get("subject") or "(no subject)") for mail in emails[:3])
                    print(f"  email poll: {len(emails)} mail(s) matched filters, no code yet [{subjects}]", flush=True)
                elif not logged_empty:
                    print("  email poll: 0 mail(s) after local filters", flush=True)
                    logged_empty = True
            except Exception as exc:
                print(f"  email poll: {exc}", flush=True)
            time.sleep(2)
        return None

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.config.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.api_url.rstrip('/')}{path}"

    def _list_accounts(self) -> list[dict]:
        params: dict[str, int] = {}
        if self.config.source_group_id is not None:
            params["group_id"] = self.config.source_group_id
        response = requests.get(
            self._url("/api/external/accounts"),
            headers=self._headers(),
            params=params or None,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"Outlook accounts fetch failed: {data.get('error') or data}")
        accounts = data.get("accounts") or []
        if not isinstance(accounts, list):
            raise RuntimeError(f"Outlook accounts payload invalid: {data}")
        return accounts

    def _is_claimable(self, account: dict) -> bool:
        email = str(account.get("email") or "").strip()
        if not email or email.lower() in self._claimed:
            return False
        status = str(account.get("status") or "active").strip().lower()
        return status in {"", "active", "ok", "normal"}

    def _is_disabled(self, email: str) -> bool:
        try:
            response = requests.post(
                self._url("/api/external/accounts/disabled-check"),
                headers=self._headers(),
                json={"email": email, "recent_count": 5},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            results = data.get("results") or []
            if isinstance(results, list) and results:
                return bool(results[0].get("disabled"))
            return bool(data.get("disabled"))
        except Exception as exc:
            print(f"  outlook disabled-check: {exc}", flush=True)
            return False

    def _move_account(self, email: str, account_id: object, target_group_id: int, label: str) -> None:
        body: dict[str, object] = {
            "group_id": target_group_id,
            "email": email,
        }
        if account_id not in (None, "", "None"):
            try:
                body["account_id"] = int(str(account_id))
            except ValueError:
                pass
        if self.config.source_group_id is not None:
            body["from_group_id"] = self.config.source_group_id
        try:
            response = requests.post(
                self._url("/api/external/accounts/batch-update-group"),
                headers=self._headers(),
                json=body,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("success") is False:
                print(f"  outlook move [{label}] failed: {data.get('error') or data}", flush=True)
                return
            print(f"  outlook move [{label}] {email} -> group {target_group_id}", flush=True)
        except Exception as exc:
            print(f"  outlook move [{label}] failed: {exc}", flush=True)

    def _get_json(self, path: str, params: dict[str, str | int] | None = None) -> dict:
        url = self._url(path)
        if params:
            # OutlookEmail uses unquote(), not unquote_plus(). Encode spaces as %20.
            url = f"{url}?{urlencode(params, doseq=True, quote_via=quote)}"
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Outlook API returned invalid JSON: {data}")
        return data

    def _folder_candidates(self) -> list[str]:
        folder = (self.config.folder or "all").strip().lower() or "all"
        if folder == "all":
            return ["inbox", "junkemail"]
        return [folder]

    def _list_emails(self, email: str) -> list[dict]:
        collected: list[dict] = []
        errors: list[str] = []
        for folder in self._folder_candidates():
            try:
                data = self._get_json(
                    "/api/external/emails",
                    {
                        "email": email,
                        "folder": folder,
                        "top": 10,
                    },
                )
            except Exception as exc:
                errors.append(f"{folder}: {exc}")
                continue
            emails = data.get("emails") or []
            if data.get("success") is False and not emails:
                errors.append(f"{folder}: {data.get('error') or 'fetch failed'}")
                continue
            if data.get("partial") and not emails:
                errors.append(f"{folder}: {data.get('error') or 'partial failure'}")
                continue
            if isinstance(emails, list):
                collected.extend(emails)
        if not collected and errors:
            raise RuntimeError("; ".join(errors))
        return self._filter_emails(collected)

    def _filter_emails(self, emails: list[dict]) -> list[dict]:
        from_needle = (self.config.from_contains or "").strip().lower()
        subject_needle = (self.config.subject_contains or "").strip().lower()
        matched: list[dict] = []
        for mail in emails:
            sender = str(mail.get("from") or "")
            subject = str(mail.get("subject") or "")
            if from_needle and from_needle not in sender.lower():
                continue
            if subject_needle and subject_needle not in subject.lower():
                continue
            matched.append(mail)
        return matched


def _normalize_mail_text(raw_message: str) -> str:
    clean = unescape(re.sub(r"<[^>]+>", " ", raw_message or ""))
    for src, dst in {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\uff0d": "-",
        "\u00a0": " ",
    }.items():
        clean = clean.replace(src, dst)
    return re.sub(r"=\r?\n", "", clean)


def _extract_verification_code(raw_message: str) -> str | None:
    clean = _normalize_mail_text(raw_message)
    index = clean.lower().find("verification code")
    if index >= 0:
        snippet = clean[index : index + 500]
        match = re.search(r"(\d{3})\s*[-\u2013]\s*(\d{3})", snippet)
        if match:
            return match.group(1) + match.group(2)
    match = re.search(r"(?<!\d)(\d{3})[-\u2013](\d{3})(?!\d)", clean)
    if match:
        return match.group(1) + match.group(2)
    return None


def _duckmail_message_body(detail: dict) -> str:
    parts: list[str] = []
    text = detail.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text)

    html = detail.get("html") or []
    if isinstance(html, list):
        for item in html:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    elif isinstance(html, str) and html.strip():
        parts.append(html)

    return "\n".join(parts)


def _outlook_message_body(mail: dict) -> str:
    parts = [
        str(mail.get("subject") or ""),
        str(mail.get("body_preview") or ""),
        str(mail.get("body") or ""),
        str(mail.get("raw") or ""),
        str(mail.get("text") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def finalize_inbox(provider: TempEmailProvider, inbox: TempEmailInbox, *, success: bool) -> None:
    method = getattr(provider, "finalize_inbox", None)
    if callable(method):
        method(inbox, success=success)


def build_email_provider(config: AppConfig) -> TempEmailProvider:
    if config.email_provider == "cloudflare_temp_email":
        return CloudflareTempEmailProvider(config.cloudflare_temp_email)
    if config.email_provider == "duckmail":
        return DuckMailProvider(config.duckmail)
    if config.email_provider == "outlook_email":
        return OutlookEmailProvider(config.outlook_email)
    raise ValueError(f"Unsupported email provider: {config.email_provider}")
