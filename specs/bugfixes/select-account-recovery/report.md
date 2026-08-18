# Bugfix Report: select-account-recovery

**Date:** 2026-08-18
**Status:** Fixed

## Description of the Issue

「[阶段C] 处理注册后跳转」阶段,select-account 页面(cloudaccounts.nvidia.com)加载不出来时,代码死循环打印 `创建组织页：填组织名...` 直到 300s 阶段C 超时,该账号建 key 失败。

**典型日志(用户反馈):**
```
2026-08-18 18:28:36 [INFO] consent 页：点提交...
2026-08-18 18:29:02 [INFO] 创建组织页：填组织名...
2026-08-18 18:29:16 [INFO] 创建组织页：填组织名...
2026-08-18 18:29:53 [INFO] 创建组织页：填组织名...
2026-08-18 18:30:38 [INFO] 创建组织页：填组织名...
2026-08-18 18:31:20 [ERROR] 阶段C 超时，未能建 key
```
约 2.5 分钟内同一句 INFO 反复出现 4 次,间隔从 14s/37s/45s 不规则拉长(每轮 `sleep(4)` + 轮询开销),最终 300s deadline 触发 `阶段C 超时`,账号失败。

**Reproduction steps:**
1. 走完注册到 consent 页点提交,跳到 select-account 页
2. select-account 页因网络/风控加载缓慢或卡白屏(DOM 未渲染出组织名输入框)
3. 状态机 select-account 分支每 4s 尝试填组织名,`_create_org` 因 `text_input.count()==0` 返回 False
4. 旧代码丢弃返回值,无脑 `sleep(4)` 继续 → 死循环到 deadline

**Impact:** 中-高。select-account 是 NVIDIA 注册链最重一跳,加载不出来较常见;旧逻辑无恢复机制导致该账号必然超时失败,浪费前面整套注册+过验证码(hCaptcha/YesCaptcha 额度)的努力。批量跑账号时此命中率不低。

## Investigation Summary

系统化调查 `finalize_and_create_key` 状态机(main.py:1060-1114 原版):

- **Symptoms examined:** 同一 INFO 日志反复出现、无 WARNING/ERROR 直到超时 → 没有任何"检测到失败/尝试恢复"的痕迹。
- **Code inspected:**
  - `finalize_and_create_key`:发现 select-account 分支(行 1096-1101 旧)`await _create_org(...)` 丢弃返回值
  - `_create_org`(行 1194-1214):`text_input.count()==0` 返回 False、按钮 `wait_for` 超时也回 False —— 返回值语义是"表单是否就绪并可填可点",但调用方没用
  - `_recover_from_navigation_error`(行 926-944):已存在 reload + goto build.nvidia.com 双级恢复,但只在 `chrome-error` URL 分支被调;卡白屏(DOM 空但 URL 正常)不触发

- **Hypotheses tested:**
  - H1 "页面真没加载出来,需要更长等待" —— 否。盲等只会到超时,真机已证。
  - H2 "应检测白屏 + reload" —— 是,且 `_recover_from_navigation_error` 已实现该能力,只需在 select-account 分支接入。
  - H3 "直接 reload 即可,不需 goto 兜底" —— 否。reload 可能仍卡(网络/风控),需 goto build.nvidia.com 让 NGC user-context 重探测接回(复用 `_recover` 的二级 goto)。

## Discovered Root Cause

**根因:** `finalize_and_create_key` 的 select-account 分支丢弃了 `_create_org` 的 `bool` 返回值,且无计数/无恢复出口;`_create_org` 失败(表单未渲染)时调用方无感知,只能 `sleep(4)` 盲循环到 300s deadline。同状态机其它分支(chrome-error/passkey)都有恢复路径,唯独 select-account 没有 —— 设计遗漏。

**Defect type:** Missing validation / 缺失恢复路径 + 返回值丢弃(Logic error 子类)。

**Why it occurred:**
1. `_create_org` 早期返回 `bool` 暡含"表单是否就绪",但状态机分支没消费它
2. select-account 在真机开发期通常加载正常,卡白屏是低频但确实存在的场景,设计时没覆盖
3. 配套的 `_recover_from_navigation_error` 只挂在 chrome-error 分支,没推广到"卡加载但非 chrome-error"的场景

**Contributing factors:**
- select-account 是注册链最后一跳(已过 hCaptcha),失败成本最高,却给了最小恢复投入
- `page.url` 在卡白屏时仍是合法 URL(非 chrome-error),现有 URL-only 的故障检测识别不了

## Resolution for the Issue

**Changes made:**
- `main.py:596` — 新增常量 `_SELECT_ACCOUNT_RELOAD_AFTER = 3`(select-account 连续失败几次后触发刷新/重进;选 3 平衡"尽快脱离卡死"与"避免单次抖动误触发")。
- `main.py:1071` — `finalize_and_create_key` 局部新增 `sa_fail_streak` 计数(每账号独立)。
- `main.py:1105-1127` — select-account 分支消费 `_create_org` 返回值:成功重置计数并 `sleep(4)` continue;失败累加计数,未达阈值 `sleep(4)` 重试,达阈值调用 `_recover_from_navigation_error`(reload 当前页),reload 仍失败则再调一次让其 goto build.nvidia.com 兜底接回流程;全程带 log.warning 落盘成败与连续次数。

**Approach rationale:** 复用已有 `_recover_from_navigation_error`(双级恢复:reload → goto),不在 select-account 分支另写恢复;计数+阈值避免单次抖动误触发刷新;把"表单未渲染"升格为可感知信号(`_create_org` 返回值本就有),而非 URL-only 故障检测。

**Alternatives considered:**
- **直接 reload 不计数** — 否。首拍抖动/慢渲染会被误判失败频繁刷新,反打断正常加载。
- **加宽 `_create_org` 内部 `wait_for` 超时** — 否。只延后第一次失败,本质死循环仍在。
- **检测 DOM content readiness 而非靠 `_create_org` 返回值** — 否。需新写 page.evaluate 探测,`_create_org` 返回值已是现成信号,多写一遍是冗余。

## Regression Test

**Test file:** `tests/test_select_account_recovery.py`
**Test name:** `SelectAccountRecoveryTests.test_consecutive_create_org_failures_trigger_reload_then_key`

**What it verifies:**
- select-account 连续 `_create_org` 失败达 `_SELECT_ACCOUNT_RELOAD_AFTER` 阈值 → `_recover_from_navigation_error` 被调用(至少 1 次),不死循环
- 恢复后 `_get_org_name` 返回 orgName → 行为接回正常链路,`_create_key_in_browser` 被调一次并返回 key,证明流程没卡超时
- `_create_org` 失败次数 ≥ 阈值(验证不是失败 1 次就 reload)

配套测试 `test_under_threshold_keeps_retrying_without_reload`:前 2 次失败未达阈值不触发 reload,避免抖动误触发。及 `SelectAccountRecoveryDecisionTests.test_threshold_is_positive_small`:常量契约 `_SELECT_ACCOUNT_RELOAD_AFTER ∈ [2,5]`。

**Run command:** `python -m pytest tests/test_select_account_recovery.py -q`

## Affected Files

| File | Change |
|------|--------|
| `main.py` | 新增 `_SELECT_ACCOUNT_RELOAD_AFTER` 常量;`finalize_and_create_key` select-account 分支消费 `_create_org` 返回值 + 连续失败计数 + 达阈值调 `_recover_from_navigation_error` |
| `tests/test_select_account_recovery.py` | 新增 3 个回归测试(行为级 + 常量契约) |

## Verification

**Automated:**
- [x] Regression test passes(2 行为级 + 1 常量契约)
- [x] Full test suite passes(52 passed,原 50 + 新增 2)
- [x] main.py 0 print 残留、语法 OK

**Manual verification:**
- 日志推演:阈值 3 → 失败次数 ≥3 触发 reload,日志从无限重复 INFO 变为「创建组织页表单未就绪 (连续 N 次失败)」WARNING + 「创建组织页 reload ...」告警,事后可对账恢复动作;最终 reload/reload→goto 救回后建 key 成功。

## Prevention

**Recommendations to avoid similar bugs:**
- 状态机各分支凡调"有副作用且返回成败"的辅助函数,必须消费返回值;丢弃返回值是缺陷信号(可在 review 时查 `await _xxx` 无 `ok =` 赋值)
- 状态机分支应有退出/恢复路径,无 `continue` 前不查任何成败的分支是隐患 — 加 linter/review 守则:分支内每条 `continue` 必须"要么消费了某返回值,要么有等待/重试上限"
- `_recover_from_navigation_error` 这类双级恢复能力应推广到所有"卡加载"类分支,不只 chrome-error;本次先修 select-account,passkey 分支已达稳,consent 分支频次低风险小,暂不动

## Related

- 历史相关 bug 报告:`specs/bugfixes/password-field-never-appeared/report.md`(同类"等待失败需重试"模式,密码字段多次检测递增等待)
- v1.0.0 tag 注释「当前已知约束」未涵盖本场景,本修复补强阶段 C 健壮性
- commit(待提交):`fix(captcha): select-account 卡加载时 reload/重进恢复,不再死循环到阶段C超时`
