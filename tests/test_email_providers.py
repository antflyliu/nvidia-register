from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import (
    AppConfig,
    BrowserConfig,
    CaptchaConfig,
    CloudflareTempEmailConfig,
    DuckMailConfig,
    NvidiaConfig,
    OutlookEmailConfig,
    describe_config,
    load_config,
)
from email_providers import (
    CloudflareTempEmailProvider,
    DuckMailProvider,
    OutlookEmailProvider,
    TempEmailInbox,
    _extract_verification_code,
    build_email_provider,
)


def _outlook_config(**overrides) -> OutlookEmailConfig:
    data = {
        "api_url": "http://127.0.0.1:5000",
        "api_key": "test-key",
        "source_group_id": 1,
        "success_group_id": 2,
        "failed_group_id": 3,
        "skip_disabled": False,
        "from_contains": "",
        "subject_contains": "",
        "folder": "all",
    }
    data.update(overrides)
    return OutlookEmailConfig(**data)


def _app_config(provider: str = "outlook_email", **outlook_overrides) -> AppConfig:
    return AppConfig(
        email_provider=provider,
        cloudflare_temp_email=CloudflareTempEmailConfig(api_url="https://cf.example", admin_auth="x", domain="example.com"),
        duckmail=DuckMailConfig(api_url="https://api.duckmail.sbs", domain="duckmail.sbs", api_key=None),
        outlook_email=_outlook_config(**outlook_overrides),
        captcha=CaptchaConfig(
            mode="manual",
            yescaptcha_client_key=None,
            yescaptcha_api_url="https://api.yescaptcha.com",
            captcharun_token=None,
            captcharun_api_url="https://api.captcha-run.com",
            local_solver_url="http://127.0.0.1:5072",
            classify_solver_url="http://127.0.0.1:5072",
            classify_humanize=True,
            classify_fallback_local=False,
            poll_interval_seconds=3,
            timeout_seconds=180,
        ),
        nvidia=NvidiaConfig(
            output_csv=Path("accounts.csv"),
            key_name="api",
            account_name="NVIDIA Build",
            key_expiry_date="2126-05-08T08:00:00Z",
        ),
        browser=BrowserConfig(headless=False, close_delay_seconds=5, engine="chromium"),
    )


class BuildEmailProviderTests(unittest.TestCase):
    def test_build_outlook_provider(self):
        provider = build_email_provider(_app_config("outlook_email"))
        self.assertIsInstance(provider, OutlookEmailProvider)

    def test_build_existing_providers_unchanged(self):
        self.assertIsInstance(build_email_provider(_app_config("cloudflare_temp_email")), CloudflareTempEmailProvider)
        self.assertIsInstance(build_email_provider(_app_config("duckmail")), DuckMailProvider)

    def test_build_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            build_email_provider(_app_config("unknown"))


class OutlookEmailProviderTests(unittest.TestCase):
    def test_create_inbox_picks_active_account_without_moving(self):
        provider = OutlookEmailProvider(_outlook_config())
        accounts_response = Mock()
        accounts_response.json.return_value = {
            "success": True,
            "accounts": [
                {"id": 9, "email": "used@outlook.com", "status": "disabled"},
                {"id": 11, "email": "fresh@outlook.com", "status": "active"},
            ],
        }

        with patch("email_providers.requests.get", return_value=accounts_response) as get_mock, patch(
            "email_providers.requests.post"
        ) as post_mock:
            inbox = provider.create_inbox("nv123")

        self.assertEqual(inbox.address, "fresh@outlook.com")
        self.assertEqual(inbox.token, "11")
        get_mock.assert_called_once()
        self.assertEqual(get_mock.call_args.args[0], "http://127.0.0.1:5000/api/external/accounts")
        self.assertEqual(get_mock.call_args.kwargs["params"]["group_id"], 1)
        post_mock.assert_not_called()

    def test_finalize_inbox_moves_to_success_or_failed_group(self):
        provider = OutlookEmailProvider(_outlook_config())
        inbox = TempEmailInbox("fresh@outlook.com", "11")
        move_response = Mock()
        move_response.json.return_value = {"success": True, "moved_count": 1}

        with patch("email_providers.requests.post", return_value=move_response) as post_mock:
            provider.finalize_inbox(inbox, success=True)
            provider.finalize_inbox(inbox, success=False)

        self.assertEqual(post_mock.call_count, 2)
        success_body = post_mock.call_args_list[0].kwargs["json"]
        failed_body = post_mock.call_args_list[1].kwargs["json"]
        self.assertEqual(success_body["group_id"], 2)
        self.assertEqual(failed_body["group_id"], 3)
        self.assertEqual(success_body["from_group_id"], 1)
        self.assertEqual(failed_body["email"], "fresh@outlook.com")
        self.assertEqual(success_body["account_id"], 11)

    def test_create_inbox_skips_disabled_when_enabled(self):
        provider = OutlookEmailProvider(_outlook_config(skip_disabled=True))
        accounts_response = Mock()
        accounts_response.json.return_value = {
            "success": True,
            "accounts": [
                {"id": 1, "email": "banned@outlook.com", "status": "active"},
                {"id": 2, "email": "ok@outlook.com", "status": "active"},
            ],
        }
        check_banned = Mock()
        check_banned.json.return_value = {"success": True, "results": [{"email": "banned@outlook.com", "disabled": True}]}
        check_ok = Mock()
        check_ok.json.return_value = {"success": True, "results": [{"email": "ok@outlook.com", "disabled": False}]}

        with patch("email_providers.requests.get", return_value=accounts_response), patch(
            "email_providers.requests.post", side_effect=[check_banned, check_ok]
        ) as post_mock:
            inbox = provider.create_inbox("nv123")

        self.assertEqual(inbox.address, "ok@outlook.com")
        self.assertEqual(post_mock.call_count, 2)
        self.assertTrue(all("/disabled-check" in call.args[0] for call in post_mock.call_args_list))

    def test_create_inbox_does_not_reuse_claimed_account(self):
        provider = OutlookEmailProvider(_outlook_config())
        accounts_response = Mock()
        accounts_response.json.return_value = {
            "success": True,
            "accounts": [
                {"id": 1, "email": "first@outlook.com", "status": "active"},
                {"id": 2, "email": "second@outlook.com", "status": "active"},
            ],
        }
        with patch("email_providers.requests.get", return_value=accounts_response):
            first = provider.create_inbox("a")
            second = provider.create_inbox("b")
        self.assertEqual(first.address, "first@outlook.com")
        self.assertEqual(second.address, "second@outlook.com")

    def test_poll_verification_code_extracts_nvidia_code(self):
        provider = OutlookEmailProvider(_outlook_config(from_contains="nvidia.com", subject_contains="NVIDIA Account"))
        mail_response = Mock()
        mail_response.json.return_value = {
            "success": True,
            "emails": [
                {
                    "subject": "Welcome to NVIDIA Cloud Account!",
                    "from": "noreply-cloud-accounts@nvidia.com",
                    "body_preview": "Welcome to NVIDIA Cloud Account",
                },
                {
                    "subject": "NVIDIA Account Created",
                    "from": "account@nvidia.com",
                    "body_preview": "NVIDIA Account\r\n\r\nYour verification code is:\r\n\r\n122-977\r\n",
                },
            ],
        }
        junk_response = Mock()
        junk_response.json.return_value = {"success": True, "emails": []}
        with patch("email_providers.requests.get", side_effect=[mail_response, junk_response]) as get_mock:
            code = provider.poll_verification_code(TempEmailInbox("fresh@outlook.com", "11"), timeout_seconds=1)

        self.assertEqual(code, "122977")
        requested = [call.args[0] for call in get_mock.call_args_list]
        self.assertTrue(any("folder=inbox" in url for url in requested))
        self.assertTrue(any("folder=junkemail" in url for url in requested))
        self.assertTrue(all("from_contains=" not in url and "subject_contains=" not in url for url in requested))
        self.assertEqual(get_mock.call_args_list[0].kwargs["headers"]["X-API-Key"], "test-key")

    def test_local_filters_keep_spaced_subject(self):
        provider = OutlookEmailProvider(_outlook_config(from_contains="nvidia.com", subject_contains="NVIDIA Account"))
        inbox_response = Mock()
        inbox_response.json.return_value = {
            "success": True,
            "emails": [
                {"subject": "Random promo", "from": "ads@nvidia.com", "body_preview": "sale"},
                {
                    "subject": "NVIDIA Account Created",
                    "from": "account@nvidia.com",
                    "body_preview": "Your verification code is:\n126-026",
                },
            ],
        }
        junk_response = Mock()
        junk_response.json.return_value = {"success": True, "emails": []}
        with patch("email_providers.requests.get", side_effect=[inbox_response, junk_response]):
            emails = provider._list_emails("aftdvbrnre@outlook.com")
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["subject"], "NVIDIA Account Created")

    def test_extract_verification_code_unchanged(self):
        self.assertEqual(_extract_verification_code("verification code 111-222"), "111222")
        self.assertEqual(
            _extract_verification_code("NVIDIA Account\r\nYour verification code is:\r\n\r\n122-977\r\n"),
            "122977",
        )
        self.assertIsNone(_extract_verification_code("hello world"))


class ConfigDescribeTests(unittest.TestCase):
    def test_describe_outlook_config(self):
        # describe_config 现走 logger（log.info），不再 print 到 stdout；
        # 用 assertLogs 捕获 nvidia-register logger 输出。
        with self.assertLogs("nvidia-register", level="INFO") as cm:
            describe_config(_app_config("outlook_email"))
        output = "\n".join(cm.output)
        self.assertIn("EMAIL_PROVIDER: outlook_email", output)
        self.assertIn("http://127.0.0.1:5000", output)




class OutlookConfigValidationTests(unittest.TestCase):
    def test_outlook_config_requires_three_distinct_groups(self):
        original = Path(__file__).resolve().parents[1] / "config.toml"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                """
email_provider = "outlook_email"
[outlook_email]
api_url = "http://127.0.0.1:5000"
api_key = "k"
source_group_id = 1
success_group_id = 1
failed_group_id = 3
""",
                encoding="utf-8",
            )
            with patch("config.CONFIG_FILE", path):
                with self.assertRaises(ValueError):
                    load_config()


if __name__ == "__main__":
    unittest.main()
