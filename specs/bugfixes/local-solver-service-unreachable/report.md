# Bugfix Report: local-solver-service-unreachable

**Date:** 2026-08-15
**Status:** Fixed

## Description of the Issue

`python .\main.py -n 1` with `captcha.mode = "local"` failed at the hCaptcha step
with a cryptic urllib3 connection error instead of a clear, actionable message:

```text
[2/4] Solving hCaptcha with local camoufox-turnstile...
  sitekey captured: 3443d8f6-da7a-4326-929f-4d7fc89ab0d1
  Captcha solver error: HTTPConnectionPool(host='127.0.0.1', port=5072): Max retries exceeded with url: /createTask (Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x...>: Failed to establish a new connection: [WinError 10061] 由于目标计算机积极拒绝，无法连接。'))
  Captcha failed
```

**Reproduction steps:**
1. Set `captcha.mode = "local"` and `local_solver_url = "http://127.0.0.1:5072"` in `config.toml`.
2. Do **not** start the camoufox-turnstile service on that port (or point at a port where nothing listens).
3. Run `python .\main.py -n 1`.
4. **Before fix:** the user sees the raw urllib3 `Max retries exceeded` / `WinError 10061` traceback, with no hint that the local service needs to be started or that the port/config may be wrong.

**Impact:** High for the `local` mode — the most common failure (service not running,
wrong port, or `solver_hcaptcha` misconfigured to `mock`) surfaces as an opaque
connection error that gives the operator no path to fix it.

## Investigation Summary

- **Symptoms examined:** The log shows `LocalSolver` trying `127.0.0.1:5072` and
  getting `WinError 10061` (connection refused). The service was not running on
  that port (the real-solve profile was on 5073, and the 5072 config had
  `solver_hcaptcha: mock` anyway).
- **Code inspected:** `captcha.py` `LocalSolver._create_task` (bare `requests.post`
  with no `RequestException` handling) and `_poll_task_result` (already caught
  `RequestException` but printed the raw error).
- **Hypotheses tested:**
  - Service logic bug → **ruled out** (protocol handshake verified earlier; failure
    is a TCP connect refusal before any request body reaches the service).
  - Wrong `local_solver_url` config → **partially related**: the URL was correct,
    but the service simply wasn't listening there. The real defect is that the
    client gives no actionable guidance on connect failure.

## Discovered Root Cause

`LocalSolver._create_task` used a bare `requests.post`. When the local
camoufox-turnstile service is unreachable, `requests` raises a
`requests.exceptions.ConnectionError` (urllib3 `Max retries exceeded`), which
propagates uncaught through `solve()` → `_solve_captcha_and_submit` and is printed
verbatim. The user gets a low-level urllib3/`WinError` message with no indication
that the fix is to start the service / check the port / verify the solver config.

**Defect type:** Missing error enrichment at a self-hosted service boundary —
a control-plane connect failure should be translated into an actionable message.

**Why it occurred:** `LocalSolver` was written as a thin YesCaptcha-compatible
client and mirrored `YesCaptchaSolver`'s bare `requests.post`. But unlike a
third-party API, the local service is operator-controlled, so a connect failure
is almost always a fixable local-state problem (service down / wrong port /
`mock` solver) — worth a clear message rather than a raw traceback.

**Contributing factors:** The service can be running on a different port (5073)
than the client expects (5072), and the 5072 config can be `solver_hcaptcha: mock`
(returns fake tokens) — both invisible to the operator from the raw error.

## Resolution for the Issue

**Changes made:**
- `captcha.py` `LocalSolver` — added `_unreachable(path, exc)` helper that builds
  a clear `RuntimeError`:
  `local solver service unreachable at <api_url><path>: <exc>` plus a fix hint
  (`请确认 camoufox-turnstile 服务已启动并监听该端口，且其 solver_hcaptcha 配置为
  camoufox（真实求解）而非 mock。`).
- `captcha.py` `LocalSolver._create_task` — wrapped the `requests.post` +
  `response.json()` in `try/except requests.RequestException`, re-raising via
  `_unreachable("/createTask", exc)`. The existing `errorId` / missing-`taskId`
  `RuntimeError`s are unchanged.
- `captcha.py` `LocalSolver._poll_task_result` — unchanged behavior (still retries
  on transient network errors so a single blip doesn't fail the task); the
  `_unreachable` helper is shared for consistency if a future path needs it.

**Approach rationale:** The connect failure is a boundary condition — enrich it at
the point where the URL and the exception are both known. Raising a `RuntimeError`
keeps parity with the existing `_create_task` error contract (which already raises
`RuntimeError`), so `_solve_captcha_and_submit`'s `except Exception` path continues
to treat it as a per-account failure.

**Alternatives considered:**
- Let the raw urllib3 error propagate — rejected: this is the bug under test.
- Catch only `ConnectionError` (not all `RequestException`) — rejected: timeouts
  (`ConnectTimeout`) are equally "service unreachable" and should get the same
  clear message; `RequestException` is the correct umbrella.
- Add a pre-flight health check / `socket` probe before `createTask` — rejected:
  an extra round-trip on every solve adds latency and complexity; the connect
  error already tells us everything, it just needs a better message.

## Regression Test

**Test file:** `tests/test_local_solver.py`
**Test names:**
- `LocalSolverUnreachableTests.test_create_task_raises_actionable_error_when_service_unreachable`
- `LocalSolverUnreachableTests.test_create_task_raises_actionable_error_on_timeout`
- `LocalSolverUnreachableTests.test_poll_keeps_retrying_on_network_error`

**What it verifies:** a `ConnectionError` (the exact urllib3 `Max retries exceeded`
shape from the bug log) and a `ConnectTimeout` from `_create_task` both raise a
`RuntimeError` whose message contains the service URL (`127.0.0.1:5072`),
`unreachable`, and the actionable hints (`camoufox-turnstile`, `solver_hcaptcha`).
The poll path still retries on a transient network error (no regression).

**Run command:**
`python -m unittest discover -s tests -p "test_local_solver.py" -v`

## Affected Files

| File | Change |
|------|--------|
| `captcha.py` | `LocalSolver._unreachable` helper; `_create_task` wraps `requests.post` in `except requests.RequestException` → clear actionable `RuntimeError` |
| `tests/test_local_solver.py` | New `LocalSolverUnreachableTests` (3 tests) |
| `specs/bugfixes/local-solver-service-unreachable/report.md` | This report |

## Verification

**Automated:**
- [x] Regression tests pass (`LocalSolverUnreachableTests` → 3 ok)
- [x] Full `test_local_solver.py` passes (16 ok)
- [x] Full test suite passes (`python -m unittest discover -s tests` → 34 ok)

**Manual verification:**
- Reproduced the exact bug scenario (service not listening on 5072) and confirmed
  the new message:
  `local solver service unreachable at http://127.0.0.1:5072/createTask: HTTPConnectionPool(...): Max retries exceeded ...` followed by the fix hint.

## Prevention

**Recommendations to avoid similar bugs:**
- Any client talking to a **self-hosted / operator-controlled** service should
  translate connect failures into an actionable message (service down / wrong
  port / wrong config), not surface the raw urllib3 traceback.
- Keep the fix hint concrete: name the service, the expected port, and the config
  field that most commonly causes a silent wrong-result (`solver_hcaptcha: mock`).
- For transient network blips during **polling**, keep retrying (don't fail the
  whole task on one `RequestException`) — only the initial `createTask` connect
  failure is worth an immediate, loud error.

## Related

- User log: `python .\main.py -n 1` with `mode=local` → `WinError 10061` on
  `127.0.0.1:5072/createTask`.
- Prior work: `d00be7e` added `LocalSolver` (local mode); the service-side cookie
  + checkbox-wait support landed in `grok-register` commit `3508132`.
