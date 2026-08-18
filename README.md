# nvidia-register

自动注册 NVIDIA BUILD 账号并创建 api key。

## 功能特点

- **全自动流程**：创建临时邮箱 → 注册 → 过验证码 → 创建组织 → 建 Key → 记录 CSV，全流程自动化
- **批量注册**：支持单次注册多个账号，交互式询问或 `-n` 参数直接指定
- **多验证码模式**：`manual` 手动过验证 / `yescaptcha` / `captcharun` 三方打码平台 / `local` 本地 camoufox-turnstile 真机求解 / `classify` 本地纯 LLM 判图（客户端取图回填，不开浏览器）
- **hCaptcha 全类型覆盖**：classify 模式支持 `grid`（九宫格）、`point`（整图点击）、`drag`（拖拽）三类挑战，多轮挑战循环对齐 hcaptcha-challenger 库 `for cid` 模型
- **多邮箱服务**：`cloudflare_temp_email`（自部署）、`duckmail`（DuckMail API）、`outlook_email`（OutlookEmail 账号池，按分组流转 A→B/C）
- **浏览器内核可选**：`camoufox`（默认，产生 isTrusted 鼠标事件过 hCaptcha 自动化检测）/ `chromium`（降级）
- **随机密码**：每次注册自动生成 12 位密码（大小写 + 数字）
- **自动跳过手机验证**：利用组织名注册跳过手机号要求，并创建长效 API Key
- **CSV 记录**：每次注册成功立即追加 `email,password,apikey` 到 CSV 文件
- **日志落盘**：统一 logging，按天滚动保留 30 天，每行带时间戳，便于事后按账号/按天对账（见 [日志](#日志)）
- **优雅退出**：`Ctrl+C` 完成当前账号后安全退出

## 项目结构

```
├── main.py              # 主入口 + 流程编排
├── config.py            # 配置加载（config.toml）
├── email_providers.py   # 临时邮箱服务抽象层
├── captcha.py           # 验证码处理（5 种 solver）
├── passwords.py         # 随机密码生成
├── records.py           # CSV 记录写入
├── logging_setup.py     # 日志落盘配置（按天滚动 + 控制台镜像）
├── config.toml          # 配置文件（gitignore，不随仓库分发）
├── config.toml.example  # 配置示例
├── tests/               # 离线单元测试（mock/fixtures，不依赖外网）
│   ├── test_classify_solver.py   # ClassifySolver 各分支
│   ├── test_email_providers.py   # 邮箱 provider + 配置
│   ├── test_local_solver.py      # LocalSolver 协议
│   └── test_password_field_wait.py
├── prototype/           # 阶段验证脚本（step1-4 分级真机诊断）
└── specs/               # bug 修复调查报告
    └── bugfixes/
        ├── classify-fallback-opens-browser/
        ├── local-solver-service-unreachable/
        └── password-field-never-appeared/
```


## 前置条件

- Python 3.11+
- Playwright 浏览器：chromium（`playwright install chromium`）；若用 camoufox 内核还需安装 camoufox
- **邮箱服务**（三选一）：`cloudflare_temp_email` 自部署 / `duckmail` / `outlook_email` 账号池
- **验证码**（按 mode 选其一）：
  - `manual`：人工
  - `yescaptcha` / `captcharun`：三方平台密钥
  - `local` / `classify`：本地 [camoufox-turnstile](https://github.com/antflyliu/camoufox-turnstile) 服务（默认 `http://127.0.0.1:5072`）


## 安装

```bash
pip install -r requirements.txt
playwright install chromium
# 若用 camoufox 内核（默认 browser.engine=camoufox）：
#   pip install camoufox && camoufox fetch
```


## 配置

```bash
# 生成配置文件模板
python main.py --init
```

编辑生成的 `config.toml`（完整字段见 `config.toml.example`）：

```toml
email_provider = "outlook_email" # cloudflare_temp_email | duckmail | outlook_email

[cloudflare_temp_email]
api_url = "https://cftmp.example.com"
admin_auth = "your_admin_key"
domain = "example.com"

[duckmail]
api_url = "https://api.duckmail.sbs"
domain = "duckmail.sbs"
api_key = ""

[outlook_email]
api_url = "http://127.0.0.1:5000"
api_key = "your_outlook_api_key"
source_group_id = 207   # 分组 A：未使用账号池
success_group_id = 208  # 分组 B：注册成功后移入
failed_group_id = 209   # 分组 C：注册失败后移入
skip_disabled = false
from_contains = "nvidia.com"  # 按发件人过滤，空收所有
subject_contains = "NVIDIA Account"  # 按主题过滤，空收所有
folder = "all"  # inbox | junkemail | deleteditems | all（同时查收件箱+垃圾箱）

[captcha]
mode = "classify" # manual | yescaptcha | captcharun | local | classify
yescaptcha_client_key = ""
yescaptcha_api_url = "https://api.yescaptcha.com"
captcharun_token = ""
captcharun_api_url = "https://api.captcha-run.com"
local_solver_url = "http://127.0.0.1:5072"   # local/classify fallback 兜底走 YesCaptcha 兼容协议
classify_solver_url = "http://127.0.0.1:5072" # classify 模式：POST /v1/classify 纯判图
classify_humanize = false   # camoufox 内核已 humanize，贝塞尔多余；chromium 需 true
classify_fallback_local = false  # 判图失败不 fallback 开浏览器（保持 classify 无浏览器语义）
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
engine = "camoufox" # camoufox | chromium；camoufox 过 hCaptcha isTrusted 检测，chromium 降级
```


### 配置项说明

| 配置项 | 说明 |
| --- | --- |
| `email_provider` | 邮箱服务类型（`cloudflare_temp_email` / `duckmail` / `outlook_email`） |
| `cloudflare_temp_email.*` | 自部署临时邮箱 API 地址、admin 密钥、域名 |
| `duckmail.*` | DuckMail API 地址、域名、私有域 API Key（公共域可空） |
| `outlook_email.*` | OutlookEmail 对外 API、API Key、分组 A/B/C id、跳过已禁用账号、发件人/主题过滤、查询文件夹 |
| `captcha.mode` | `manual` 手动 / `yescaptcha` YesCaptcha API / `captcharun` CaptchaRun API / `local` 本地 camoufox-turnstile 真机求解 / `classify` 本地纯 LLM 判图 |
| `captcha.yescaptcha_*` | YesCaptcha 密钥与 API 地址（mode=yescaptcha 必填） |
| `captcha.captcharun_*` | CaptchaRun Token 与 API 地址（mode=captcharun 必填） |
| `captcha.local_solver_url` | 本地 camoufox-turnstile 服务地址（mode=local 用；classify fallback 也用此） |
| `captcha.classify_solver_url` | classify 模式 POST `/v1/classify` 的服务地址（通常同 local_solver_url） |
| `captcha.classify_humanize` | classify checkbox 点击是否加贝塞尔真人轨迹（camoufox 关、chromium 开） |
| `captcha.classify_fallback_local` | classify 模式判图/回填失败时是否 fallback 到 LocalSolver（开浏览器）。默认 `false` 保持无浏览器语义；`true` 恢复旧真机兜底 |
| `captcha.poll_interval_seconds` | 三方平台结果轮询间隔 |
| `captcha.timeout_seconds` | 验证码/判图等待超时 |
| `nvidia.output_csv` | 输出 CSV 路径 |
| `nvidia.key_name` | API Key 名称 |
| `nvidia.account_name` | 创建组织账户时填入的名称（用于跳过手机验证） |
| `nvidia.key_expiry_date` | API Key 过期时间（默认 ~100 年） |
| `browser.headless` | 是否无头模式 |
| `browser.close_delay_seconds` | 完成后浏览器关闭延迟秒数 |
| `browser.engine` | `camoufox`（推荐，过 hCaptcha）或 `chromium`（降级） |



## 使用

```bash
# 交互式询问注册数量
python main.py

# 直接指定注册数量
python main.py -n 5
python main.py --count 3
```

批量注册时每个账号使用独立的浏览器会话，间隔 5 秒。`Ctrl+C` 优雅退出：完成当前正在注册的账号后停止，显示成功/失败汇总。第二次 `Ctrl+C` 强制退出。

每次注册成功追加记录到 `accounts.csv`：

```csv
email,password,apikey
nv12345678@example.com,aB3dE5fG7hI9,nvapi-xxxx...
```


## 注册流程

```
build.nvidia.com (填邮箱) → login.nvgs.nvidia.com (填密码 + hCaptcha)
→ 验证码页 (键盘输入) → 同意页 (提交) → 创建组织 (跳过手机验证)
→ NGC API (建 Key) → CSV 记录
```


## 验证码模式

### manual
人工完成 hCaptcha。

### yescaptcha / captcharun
三方打码平台：客户端提交 sitekey → 平台云端求解返回 token → 客户端注入 token 触发 onSuccess。

### local
本地 [camoufox-turnstile](https://github.com/antflyliu/camoufox-turnstile) 服务（`http://127.0.0.1:5072`）真机求解。nvidia-register 已走到密码环节（`login.nvgs.nvidia.com`，hCaptcha 已出现），把 `page.url` 连同会话 cookie + userAgent 交给服务；服务开独立 Camoufox 浏览器去该页求解并返回 token，nvidia-register 再注入回页面。协议与 YesCaptcha 兼容（`createTask`/`getTaskResult`），服务端不校验 `clientKey`、忽略 `websiteKey`（sitekey 由服务从页面 DOM 推导）。

### classify（推荐）
本地纯 LLM 判图，**客户端不向服务端开浏览器**。流程：客户端浏览器内点 checkbox 弹挑战框 → 截挑战图（base64）→ POST 服务端 `/v1/classify` 纯判图 → 按类型回填点击/拖拽 → 提交。服务端 `/v1/classify` 不开浏览器，只做 LLM 图片分类。

- **类型识别三层信号**：网络监听捕获 challenge.js URL 子路径映射（`image_label_binary`→grid / `image_label_area_select`→point / `image_drag_drop`→drag，列表化去重逆序取最新）；challenge.js 未捕获时用 DOM 兜底（9 个 task-image→grid，`.challenge-example` 存在→point 否则→drag）；DOM 查询失败拒绝猜测、由 refresh 换题重试。
- **多轮挑战**：对齐 hcaptcha-challenger 库 `for cid in range(crumb_count)` 模型，所有轮提交后统一查 pass token（监听 `/getcaptcha/` 或 `/checkcaptcha/` 响应 `{pass:true, generated_pass_UUID}`）。
- **回填坐标映射**：服务端返回挑战截图内像素坐标，客户端 `+bbox_x/+bbox_y` 映射到视口（路 B）。
- **默认不 fallback 开浏览器**：`classify_fallback_local=false` 时判图/回填失败直接让当前账号失败，不退化到 LocalSolver（避免违背 classify 无浏览器初衷）。置 `true` 可恢复真机兜底。

前置条件：启动 camoufox-turnstile 服务，并确保其 `solver_hcaptcha` 配置为 `camoufox`（真实求解）而非 `mock`。 classify 与 local 共用同一服务端口，按 mode 选不同协议端点。


## 日志

nvidia-register 统一用 Python `logging`（`logging_setup.py`），不再用 `print`：

- **格式**：`%(asctime)s [%(levelname)s] %(name)s: %(message)s`，每行开头带 `%Y-%m-%d %H:%M:%S` 时间戳。
- **滚动策略**：按天滚动（`TimedRotatingFileHandler` when=midnight），保留 **30 天**。选型理由：本项目是单进程批处理，核心价值是「按账号/按天对账」——同名文件即同一天日志，天然契合按天切分；按大小滚动会让跨日长跑同一天横跨多文件、违背对账用途。
- **双 handler**：控制台走 INFO（运行时可见性，避免 DEBUG 刷屏）；文件走 DEBUG（含逐格点击坐标、screenshot 失败诊断、iframe DOM snapshot 等细节，用于事故回溯）。
- **落盘位置**：`logs/nvidia-register.log`（当天，写入中）+ `logs/nvidia-register.log.YYYY-MM-DD`（历史 30 天）。
- **子 logger**：`nvidia-register.main` / `.captcha` / `.email` / `.config`，便于按模块过滤（`grep captcha logs/...`）。
- 目录建不出时静默降级到只走控制台，绝不因日志初始化阻断注册流程。

查看示例：

```bash
# 当天日志
tail -f logs/nvidia-register.log
# 某账号相关的所有日志（按 email 片段过滤）
grep "nv12345678" logs/nvidia-register.log*
# 只看错误与告警
grep -E "\[(ERROR|WARNING)\]" logs/nvidia-register.log*
```


## 扩展邮箱服务

实现 `TempEmailProvider` 协议并在 `email_providers.py` 注册到 `build_email_provider()`：

```python
class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox: ...
    def poll_verification_code(self, inbox: TempEmailInbox, timeout_seconds: int = 180) -> str | None: ...
    # 可选：finalize_inbox 注册成功/失败后流转（OutlookEmail 用此做分组迁移）
```


## 测试

离线单元测试（mock/fixtures，不依赖外网）：

```bash
python -m pytest tests/ -q
```


## 注意事项

- hCaptcha **手动模式**必须人工完成验证
- 注册包含验证码轮询（最长 3 分钟，可由 `captcha.timeout_seconds` 配置）
- 浏览器窗口会在完成后自动关闭（可配置延迟）
- 批量注册时每个账号独立浏览器会话，互不影响
- `classify` 模式默认不开浏览器兜底；判图/回填失败会让该账号失败而非退化真机（见 `classify_fallback_local`）
