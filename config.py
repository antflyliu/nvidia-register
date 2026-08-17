from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.toml"


@dataclass(frozen=True)
class CloudflareTempEmailConfig:
    api_url: str
    admin_auth: str
    domain: str


@dataclass(frozen=True)
class DuckMailConfig:
    api_url: str
    domain: str
    api_key: str | None


@dataclass(frozen=True)
class OutlookEmailConfig:
    api_url: str
    api_key: str
    source_group_id: int | None
    success_group_id: int | None
    failed_group_id: int | None
    skip_disabled: bool
    from_contains: str
    subject_contains: str
    folder: str


@dataclass(frozen=True)
class CaptchaConfig:
    mode: str
    yescaptcha_client_key: str | None
    yescaptcha_api_url: str
    captcharun_token: str | None
    captcharun_api_url: str
    local_solver_url: str
    classify_solver_url: str
    classify_humanize: bool
    poll_interval_seconds: int
    timeout_seconds: int


@dataclass(frozen=True)
class NvidiaConfig:
    output_csv: Path
    key_name: str
    account_name: str
    key_expiry_date: str


@dataclass(frozen=True)
class BrowserConfig:
    headless: bool
    close_delay_seconds: int
    engine: str  # "camoufox" | "chromium"；camoufox 产生 isTrusted 鼠标事件，过 hCaptcha checkbox 自动化检测


@dataclass(frozen=True)
class AppConfig:
    email_provider: str
    cloudflare_temp_email: CloudflareTempEmailConfig
    duckmail: DuckMailConfig
    outlook_email: OutlookEmailConfig
    captcha: CaptchaConfig
    nvidia: NvidiaConfig
    browser: BrowserConfig


def _require_str(data: dict[str, Any], path: str) -> str:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"Missing required config: {path}")
        current = current[part]
    if not isinstance(current, str) or not current.strip():
        raise ValueError(f"Missing required config: {path}")
    return current.strip()


def _get_str(data: dict[str, Any], path: str, default: str) -> str:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current if isinstance(current, str) and current.strip() else default


def _get_int(data: dict[str, Any], path: str, default: int) -> int:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current if isinstance(current, int) else default


def _get_optional_int(data: dict[str, Any], path: str) -> int | None:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if current is None or current == "":
        return None
    if isinstance(current, bool):
        return None
    if isinstance(current, int):
        return current
    if isinstance(current, str) and current.strip().lstrip("-").isdigit():
        return int(current.strip())
    return None


def _get_bool(data: dict[str, Any], path: str, default: bool) -> bool:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current if isinstance(current, bool) else default


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else SCRIPT_DIR / path


def init_config() -> None:
    if CONFIG_FILE.exists():
        print(f"Config already exists: {CONFIG_FILE}")
        return
    template = """
email_provider = "cloudflare_temp_email"

[cloudflare_temp_email]
api_url = ""
admin_auth = ""
domain = ""

[duckmail]
api_url = "https://api.duckmail.sbs"
domain = "duckmail.sbs"
api_key = ""

[outlook_email]
api_url = "http://127.0.0.1:5000"
api_key = ""
source_group_id = 1
success_group_id = 2
failed_group_id = 3
skip_disabled = false
from_contains = ""
subject_contains = ""
folder = "all"

[captcha]
mode = "manual" # manual | yescaptcha | captcharun
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
poll_interval_seconds = 3
timeout_seconds = 180

[nvidia]
output_csv = "accounts.csv"
key_name = "api"
account_name = "NVIDIA Build"
key_expiry_date = "2126-05-08T08:00:00Z"

[browser]
headless = false
close_delay_seconds = 5
"""
    CONFIG_FILE.write_text(template, encoding="utf-8")
    print(f"Created {CONFIG_FILE}")


def load_config() -> AppConfig:
    if not CONFIG_FILE.exists():
        print(f"Missing config file: {CONFIG_FILE}")
        print("Run: python main.py --init")
        sys.exit(1)

    with CONFIG_FILE.open("rb") as file:
        data = tomllib.load(file)

    email_provider = _get_str(data, "email_provider", "cloudflare_temp_email").lower()
    if email_provider not in {"cloudflare_temp_email", "duckmail", "outlook_email"}:
        raise ValueError(f"Unsupported email_provider: {email_provider}")

    use_cloudflare_temp_email = email_provider == "cloudflare_temp_email"
    use_duckmail = email_provider == "duckmail"
    use_outlook_email = email_provider == "outlook_email"

    cloudflare_api_url = (
        _require_str(data, "cloudflare_temp_email.api_url")
        if use_cloudflare_temp_email
        else _get_str(data, "cloudflare_temp_email.api_url", "")
    ).rstrip("/")
    cloudflare_admin_auth = (
        _require_str(data, "cloudflare_temp_email.admin_auth")
        if use_cloudflare_temp_email
        else _get_str(data, "cloudflare_temp_email.admin_auth", "")
    )
    cloudflare_domain = (
        _require_str(data, "cloudflare_temp_email.domain")
        if use_cloudflare_temp_email
        else _get_str(data, "cloudflare_temp_email.domain", "")
    )

    duckmail_domain = (
        _require_str(data, "duckmail.domain")
        if use_duckmail
        else _get_str(data, "duckmail.domain", "")
    )
    duckmail_api_key = _get_str(data, "duckmail.api_key", "") or None

    outlook_api_url = (
        _require_str(data, "outlook_email.api_url")
        if use_outlook_email
        else _get_str(data, "outlook_email.api_url", "http://127.0.0.1:5000")
    ).rstrip("/")
    outlook_api_key = (
        _require_str(data, "outlook_email.api_key")
        if use_outlook_email
        else _get_str(data, "outlook_email.api_key", "")
    )
    outlook_folder = _get_str(data, "outlook_email.folder", "all").lower() or "all"
    if outlook_folder not in {"inbox", "junkemail", "deleteditems", "all"}:
        raise ValueError("outlook_email.folder must be inbox, junkemail, deleteditems or all")

    source_group_id = _get_optional_int(data, "outlook_email.source_group_id")
    if source_group_id is None:
        source_group_id = _get_optional_int(data, "outlook_email.group_id")
    success_group_id = _get_optional_int(data, "outlook_email.success_group_id")
    if success_group_id is None:
        success_group_id = _get_optional_int(data, "outlook_email.used_group_id")
    failed_group_id = _get_optional_int(data, "outlook_email.failed_group_id")
    if use_outlook_email:
        if source_group_id is None:
            raise ValueError("outlook_email.source_group_id is required when email_provider = 'outlook_email'")
        if success_group_id is None:
            raise ValueError("outlook_email.success_group_id is required when email_provider = 'outlook_email'")
        if failed_group_id is None:
            raise ValueError("outlook_email.failed_group_id is required when email_provider = 'outlook_email'")
        if len({source_group_id, success_group_id, failed_group_id}) != 3:
            raise ValueError(
                "outlook_email.source_group_id / success_group_id / failed_group_id must be three different groups"
            )

    captcha_mode = _get_str(data, "captcha.mode", "manual").lower()
    if captcha_mode not in {"manual", "yescaptcha", "captcharun", "local", "classify"}:
        raise ValueError("captcha.mode must be 'manual', 'yescaptcha', 'captcharun', 'local' or 'classify'")
    yescaptcha_client_key = _get_str(data, "captcha.yescaptcha_client_key", "") or None
    if captcha_mode == "yescaptcha" and not yescaptcha_client_key:
        raise ValueError("captcha.yescaptcha_client_key is required when captcha.mode = 'yescaptcha'")
    captcharun_token = _get_str(data, "captcha.captcharun_token", "") or None
    if captcha_mode == "captcharun" and not captcharun_token:
        raise ValueError("captcha.captcharun_token is required when captcha.mode = 'captcharun'")
    local_solver_url = _get_str(data, "captcha.local_solver_url", "http://127.0.0.1:5072").rstrip("/")
    classify_solver_url = _get_str(data, "captcha.classify_solver_url", "http://127.0.0.1:5072").rstrip("/")
    # classify 模式 checkbox 点击是否加贝塞尔真人轨迹。普通 chromium 需 True；
    # camoufox(humanize=True) 浏览器内核已真人化，设 False 更快。默认 True。
    classify_humanize = _get_bool(data, "captcha.classify_humanize", True)

    browser_engine = _get_str(data, "browser.engine", "camoufox").lower()
    if browser_engine not in {"camoufox", "chromium"}:
        raise ValueError("browser.engine must be 'camoufox' or 'chromium'")

    return AppConfig(
        email_provider=email_provider,
        cloudflare_temp_email=CloudflareTempEmailConfig(
            api_url=cloudflare_api_url,
            admin_auth=cloudflare_admin_auth,
            domain=cloudflare_domain,
        ),
        duckmail=DuckMailConfig(
            api_url=_get_str(data, "duckmail.api_url", "https://api.duckmail.sbs").rstrip("/"),
            domain=duckmail_domain,
            api_key=duckmail_api_key,
        ),
        outlook_email=OutlookEmailConfig(
            api_url=outlook_api_url,
            api_key=outlook_api_key,
            source_group_id=source_group_id,
            success_group_id=success_group_id,
            failed_group_id=failed_group_id,
            skip_disabled=_get_bool(data, "outlook_email.skip_disabled", False),
            from_contains=_get_str(data, "outlook_email.from_contains", ""),
            subject_contains=_get_str(data, "outlook_email.subject_contains", ""),
            folder=outlook_folder,
        ),
        captcha=CaptchaConfig(
            mode=captcha_mode,
            yescaptcha_client_key=yescaptcha_client_key,
            yescaptcha_api_url=_get_str(data, "captcha.yescaptcha_api_url", "https://api.yescaptcha.com").rstrip("/"),
            captcharun_token=captcharun_token,
            captcharun_api_url=_get_str(data, "captcha.captcharun_api_url", "https://api.captcha-run.com").rstrip("/"),
            local_solver_url=local_solver_url,
            classify_solver_url=classify_solver_url,
            classify_humanize=classify_humanize,
            poll_interval_seconds=_get_int(data, "captcha.poll_interval_seconds", 3),
            timeout_seconds=_get_int(data, "captcha.timeout_seconds", 180),
        ),
        nvidia=NvidiaConfig(
            output_csv=_resolve_path(_get_str(data, "nvidia.output_csv", "accounts.csv")),
            key_name=_get_str(data, "nvidia.key_name", "api"),
            account_name=_get_str(data, "nvidia.account_name", "NVIDIA Build"),
            key_expiry_date=_get_str(data, "nvidia.key_expiry_date", "2126-05-08T08:00:00Z"),
        ),
        browser=BrowserConfig(
            headless=_get_bool(data, "browser.headless", False),
            close_delay_seconds=_get_int(data, "browser.close_delay_seconds", 10),
            engine=browser_engine,
        ),
    )


def describe_config(config: AppConfig) -> None:
    if config.email_provider == "cloudflare_temp_email":
        email_api = config.cloudflare_temp_email.api_url
        email_domain = config.cloudflare_temp_email.domain
    elif config.email_provider == "outlook_email":
        email_api = config.outlook_email.api_url
        email_domain = (
            f"A={config.outlook_email.source_group_id} "
            f"B={config.outlook_email.success_group_id} "
            f"C={config.outlook_email.failed_group_id}"
        )
    else:
        email_api = config.duckmail.api_url
        email_domain = config.duckmail.domain

    print(f"  EMAIL_PROVIDER: {config.email_provider}")
    print(f"  EMAIL_API:      {email_api}")
    print(f"  EMAIL_DOMAIN:   {email_domain}")
    print(f"  CAPTCHA_MODE:   {config.captcha.mode}")
    print(f"  BROWSER_ENGINE: {config.browser.engine}")
    print(f"  OUTPUT_CSV:     {config.nvidia.output_csv}")
    print(f"  CONFIG_FILE:    {CONFIG_FILE}")
