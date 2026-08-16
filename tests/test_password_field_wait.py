from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (
    _fill_visible_password_fields,
    _password_wait_seconds,
    _wait_for_password_field,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeLocator:
    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self._elements = elements

    @property
    def first(self) -> "FakeLocator":
        return FakeLocator(self._elements[:1])

    def nth(self, index: int) -> "FakeLocator":
        return FakeLocator(self._elements[index : index + 1])

    async def count(self) -> int:
        return len(self._elements)

    async def fill(self, value: str) -> None:
        if not self._elements:
            raise RuntimeError("no matching password field")
        self._elements[0]["value"] = value


def _selector_matches(element: dict[str, Any], selector: str) -> bool:
    for part in (piece.strip() for piece in selector.split(",")):
        if _one_selector_matches(element, part):
            return True
    return False


def _one_selector_matches(element: dict[str, Any], selector: str) -> bool:
    require_visible = ":visible" in selector
    raw = selector.replace(":visible", "")
    if require_visible and not element.get("visible", True):
        return False
    if raw.startswith("#"):
        return element.get("id") == raw[1:]
    if 'input[type="password"]' in raw:
        return element.get("type") == "password"
    if 'input[name="' in raw:
        name = raw.split('input[name="', 1)[1].split('"', 1)[0]
        return element.get("name") == name
    return False


class FakePage:
    def __init__(self, inputs: list[dict[str, Any]]) -> None:
        self.inputs = inputs

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator([item for item in self.inputs if _selector_matches(item, selector)])


class DelayedPasswordPage:
    """Password field stays missing until enough locator polls have happened."""

    def __init__(self, appear_at_poll: int | None) -> None:
        self.polls = 0
        self.appear_at_poll = appear_at_poll

    def locator(self, selector: str) -> FakeLocator:
        self.polls += 1
        if self.appear_at_poll is not None and self.polls >= self.appear_at_poll:
            return FakeLocator(
                [{"id": "registration_password", "type": "password", "visible": True, "value": ""}]
            )
        return FakeLocator([])


class PasswordWaitScheduleTests(unittest.TestCase):
    def test_password_wait_seconds_increase_each_attempt(self) -> None:
        # Bug: a single 30s wait then hard-fail is too brittle.
        waits = [_password_wait_seconds(attempt) for attempt in range(1, 6)]
        self.assertEqual(waits, [4.0, 8.0, 12.0, 16.0, 20.0])
        for earlier, later in zip(waits, waits[1:]):
            self.assertGreater(later, earlier)

    def test_password_wait_seconds_rejects_invalid_attempt(self) -> None:
        with self.assertRaises(ValueError):
            _password_wait_seconds(0)


class PasswordFieldWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_retries_until_password_field_appears(self) -> None:
        # Field is absent on the first detections, then becomes visible.
        page = DelayedPasswordPage(appear_at_poll=4)
        clock = FakeClock()

        found = await _wait_for_password_field(
            page,
            attempts=5,
            base_seconds=1.0,
            poll_interval=1.0,
            time_fn=clock.time,
            sleep_fn=clock.sleep,
        )

        self.assertTrue(found)
        self.assertGreaterEqual(page.polls, 4)

    async def test_wait_does_not_exit_on_first_miss(self) -> None:
        page = DelayedPasswordPage(appear_at_poll=None)
        clock = FakeClock()

        found = await _wait_for_password_field(
            page,
            attempts=3,
            base_seconds=1.0,
            poll_interval=1.0,
            time_fn=clock.time,
            sleep_fn=clock.sleep,
        )

        self.assertFalse(found)
        # Three growing windows (1s+2s+3s) plus backoffs still poll many times.
        # The contract is: do not give up after the first detection.
        self.assertGreater(page.polls, 1)


class FillPasswordFieldsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fill_registration_password_and_confirm(self) -> None:
        page = FakePage(
            [
                {"id": "registration_password", "type": "password", "visible": True, "value": ""},
                {
                    "id": "registration_passwordConfirm",
                    "type": "password",
                    "visible": True,
                    "value": "",
                },
            ]
        )

        ok = await _fill_visible_password_fields(page, "Secret123!")

        self.assertTrue(ok)
        self.assertEqual(page.inputs[0]["value"], "Secret123!")
        self.assertEqual(page.inputs[1]["value"], "Secret123!")

    async def test_fill_login_password_when_registration_ids_missing(self) -> None:
        page = FakePage(
            [{"id": "password", "type": "password", "name": "password", "visible": True, "value": ""}]
        )

        ok = await _fill_visible_password_fields(page, "Secret123!")

        self.assertTrue(ok)
        self.assertEqual(page.inputs[0]["value"], "Secret123!")


if __name__ == "__main__":
    unittest.main()
