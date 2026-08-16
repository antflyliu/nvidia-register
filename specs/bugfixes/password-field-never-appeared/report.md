# Bugfix Report: password-field-never-appeared

**Date:** 2026-08-15
**Status:** Fixed

## Description of the Issue

`python main.py -n 2` submitted the email, landed on `login.nvgs.nvidia.com/v1/login`, then tried to fill the password box. If `#registration_password` was not visible yet (or the page used a different password input), the script waited once for 30s, printed `password field never appeared`, and aborted the account.

**Reproduction steps:**
1. Run `python .\main.py -n 2`
2. Watch the flow navigate to `https://login.nvgs.nvidia.com/v1/login?...`
3. Observe `[1/4] Fill password` followed by `password field never appeared` and a failed account

**Impact:** A slightly slow or differently-id password form failed the whole registration. The Outlook mailbox was then marked failed.

## Investigation Summary

- **Symptoms examined:** The URL was `/v1/login`, not `create-account`. The clickable snapshot only showed a disabled Login button and social buttons, with no input details.
- **Code inspected:** `_submit_email_step`, `register_account`, `_print_clickable_snapshot` in `main.py`
- **Hypotheses tested:**
  - Single 30s wait then hard fail: confirmed
  - Selector too narrow (`#registration_password` only): confirmed
  - Diagnostic dump omitted inputs: confirmed

## Discovered Root Cause

The password step treated the first miss of `#registration_password` as terminal. NVIDIA login is an SPA: after email submit it lands on `/v1/login` and lazily renders either the registration or login password form. There was no multi-attempt detection with growing wait, and no fallback to generic password inputs.

**Defect type:** Missing retry / overly strict locator / premature failure

**Why it occurred:** The flow was written against a create-account page snapshot and did not tolerate `/v1/login` hydration delay or selector drift.

**Contributing factors:** Navigation success only matched the host (`**/login.nvgs.nvidia.com/**`), not password-form readiness.

## Resolution for the Issue

**Changes made:**
- `main.py`: `_password_wait_seconds` / `_wait_for_password_field` — 5 detections, waits 4s/8s/12s/16s/20s, short backoff between attempts
- `main.py`: selectors now include `#password`, `input[name=password]`, `input[type=password]`
- `main.py`: `_fill_visible_password_fields` fills registration pair or a single login password box
- `main.py`: diagnostic snapshot now also dumps inputs
- `tests/test_password_field_wait.py`: growing wait, first-miss must continue, late appear succeeds, fill both page types

**Approach rationale:** Matches existing captcha/verification retry style. Growing waits absorb SPA jitter instead of aborting the current account.

**Alternatives considered:**
- Raise the single timeout to 90s — still one detection; wrong selector still fails
- Reload when missing — can lose the just-submitted email state

## Regression Test

**Test file:** `tests/test_password_field_wait.py`
**Test name:** `test_wait_does_not_exit_on_first_miss`, `test_wait_retries_until_password_field_appears`, `test_password_wait_seconds_increase_each_attempt`

**What it verifies:** Wait grows per attempt; first miss is not fatal; a late field is accepted; registration confirm and login-only fields can be filled.

**Run command:** `python -m pytest tests/test_password_field_wait.py tests/test_email_providers.py -q`

## Affected Files

| File | Change |
|------|--------|
| `main.py` | Growing multi-detect wait, broader selectors, input snapshot |
| `tests/test_password_field_wait.py` | Regression tests |
| `specs/bugfixes/password-field-never-appeared/report.md` | This report |

## Verification

**Automated:**
- [x] Regression test passes
- [x] Full test suite passes (`18 passed`)
- [x] Unused `PlaywrightTimeoutError` import removed

**Manual verification:**
- Did not run a live NVIDIA registration (consumes mailbox/captcha quota). Use `python .\main.py -n 1` and confirm `[1/4] Fill password` prints `detecting password field (1/5, wait 4s)...` and continues when the field appears.

## Prevention

**Recommendations to avoid similar bugs:**
- For NVIDIA SPA form fields, use multi-detect with growing waits instead of one `wait_for`
- Keep both registration IDs and generic `input[type=password]` locators
- Dump inputs as well as buttons on failure

## Related

- User log: `password field never appeared` after navigating to `login.nvgs.nvidia.com/v1/login`
