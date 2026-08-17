# Bugfix: classify 模式 fallback 导致 camoufox-turnstile 服务端开浏览器

## 现象
执行 `python main.py -n 1` 时，`config.toml` 已配 `captcha.mode=classify`，
期望 camoufox-turnstile 服务端**不打浏览器**、纯 `POST /v1/classify` LLM 判图。
但实测服务端启动了 camoufox 浏览器并自己点 checkbox，最终
`ChallengeSignal.EXECUTION_TIMEOUT` 失败。

## 根因
用户看到的"开浏览器"请求不是 `/v1/classify`，是 `/createTask`
（HCaptchaTaskProxyless），这两个服务端路由完全不同：

- `POST /v1/classify` → `classify_handler`：纯 LLM 判图，**不开浏览器**
  （实测 `classify ok type=grid`，全程无 `hcaptcha solve.start`）。
- `POST /createTask`（type 含 `HCaptcha`）→ `solve_hcaptcha_task` →
  `CamoufoxHCaptchaSolver` → 开 camoufox 浏览器点 checkbox 取图求解。

nvidia-register 的 `ClassifySolver.solve()`（`captcha.py:551-578`）在两处
fallback 到 `LocalSolver`：
1. `_click_checkbox` 失败 → `_fallback_local`
2. `_solve_rounds` 返回 None（判图/回填/no pass）→ `_fallback_local`

`LocalSolver.solve()` 走 `/createTask`，触发服务端开浏览器。

**触发 fallback 的诱因**：NVIDIA hCaptcha 对 camoufox 优先下发 drag 挑战
（memory `hcaptcha-drag-challenge-priority`），而 classify_model
`claude-sonnet-4-6` 经反代 127.0.0.1:8317（CLIProxyAPI exe）对 drag 请求
返回 404/SSL EOF（反代上游凭证 `insufficient_quota`）→ classify 502 →
`_classify` 返回 None → fallback local。grid/point 正常，仅 drag 挂。

服务端日志铁证（2026-08-17 19:35 真机）：
```
19:35:35 WARNING classify failed type=drag err=LLM request failed: <urlopen error EOF...>
19:35:38 WARNING classify failed type=drag err=LLM HTTP 404: 404 page not found
19:35:38 INFO  hcaptcha solve.start cr=1 href=https://login.nvgs.nvidia.com/...
19:35:38 INFO  camoufox window maximize headless=False      ← 浏览器开了
19:35:57 INFO  hcaptcha checkbox.click cr=1 mode=inline
19:43:02 INFO  hcaptcha wait#1 signal=EXECUTION_TIMEOUT
```

对照实测（单独打 /v1/classify）：仅 `classify ok type=grid elapsed_ms=6984`，
零 `hcaptcha solve.start`，证明服务端 classify 路由本身正确。

## 修复（nvidia-register 端）
新增配置 `captcha.classify_fallback_local`（默认 **false**）：
- `ClassifySolver.__init__` 新增 `fallback_local` 形参，
  `self._fallback_local_enabled`。
- `_fallback_local` 在开关 False 时直接返回 False + 打日志，绝不调
  `LocalSolver`、绝不打 `/createTask`，保持 mode=classify 的无浏览器语义。
- `build_captcha_solver` 把 `config.classify_fallback_local` 传入。
- 旧行为（真机兜底）通过显式置 `true` 恢复，向后兼容。

文件：
- `captcha.py`：`ClassifySolver.__init__`、`_fallback_local`、
  `build_captcha_solver` 三处。
- `config.py`：`CaptchaConfig` 加 `classify_fallback_local: bool = False`，
  `load_config` 读取，`init_config` 模板不变（manual 模式无需此字段）。
- `config.toml` / `config.toml.example`：新增带注释的开关项。
- `tests/test_classify_solver.py`：新增回归用例
  `test_default_no_fallback_does_not_call_local_solver`、
  `test_fallback_enabled_preserves_old_behavior`。

## 反代 drag 路由（第二部分，未改代码）
反代 127.0.0.1:8317 是第三方编译 exe `CLIProxyAPI`（无源码），drag 404/403
是上游凭证额度耗尽（日志 `insufficient_quota` / `oc/mimo-v2.5-free (403)`），
属反代运维而非路由代码 bug。建议保持 `classify_model` 当前的
`claude-sonnet-4-6` 或换可用凭证；本次只做 nvidia-register 端"默认不 fallback"
使其即便 drag 判图失败也不再开浏览器。

## 验证
- `tests/test_classify_solver.py`：12 passed（含 2 新增）
- `tests/test_local_solver.py`：16 passed，合计 28 passed
- `python -c "load_config()"`：`mode=classify fallback=False humanize=False`
  符合预期默认
- `ruff check`：新增改动 0 错（仅预存 `checkbox_el` F841 与本修复无关）

## 回滚
删除 `classify_fallback_local` 配置项或将值置 `true` 即恢复原 fallback 行为。
