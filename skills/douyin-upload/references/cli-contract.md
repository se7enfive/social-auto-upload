# 抖音 CLI 契约

这个 skill 默认假设当前环境已经安装并可调用 `sau` 命令。

## 命令列表

### 安装 HTTP 运行时（可选）

```bash
sau douyin setup --accept-third-party \
  [--source <local-checkout>] \
  [--force] \
  [--timeout <seconds>]
```

- `--accept-third-party` 必填：`DouYin_Spider` 是第三方代码且没有 LICENSE。
- `--source`：使用本地 git checkout，`HEAD` 必须等于固定 commit。
- `--force`：删除并重建运行时。
- `--timeout`：安装超时，默认 900 秒。

### 登录

```bash
sau douyin login --account <account> [--engine auto|browser|http] [--force]
```

- 必填参数:
  - `--account`
- 浏览器路径（默认）:
  - 在专用本机 Chrome profile 中启动抖音登录流程，并把登录态导出为 `cookies/douyin_<account>.json` 备份
  - 如果专用 profile 已经登录，会直接复用，不重复生成二维码
- HTTP 路径（`--engine http`）:
  - 必须在交互式终端执行；非 TTY 会直接报错
  - 在终端打印二维码，并保存 `.local/qrcodes/douyin-http-<account>.png`
  - 凭据写入 `cookies/douyin_http_<account>.json`，与浏览器账号文件分开
  - 已有凭据且校验通过时直接复用；`--force` 强制重新扫码
- 如果登录过程中生成本地二维码图片，agent 应优先直接把图片展示/发送给用户扫码，而不是只回传路径
- 账号说明:
  - `--account` 传的是用户自定义的 `account_name`，不是固定只能叫 `creator`
  - 一个 `account_name` 对应一个专用 Chrome profile（或一份 HTTP 凭据），可用于多账号隔离
  - 同一账号同一时间只能有一个进程使用

### 校验会话

```bash
sau douyin check --account <account> [--engine auto|browser|http]
```

- 必填参数:
  - `--account`
- 预期输出:
  - `valid`：会话可用
  - `invalid`：会话缺失或已失效
- HTTP 路径只读校验创作者接口和凭据字段，不保证 bd-ticket 服务端仍有效；发布遇到 403 时重新扫码

### 上传视频

```bash
sau douyin upload-video \
  --account <account> \
  --file <video-path> \
  --title "<title>" \
  [--desc "<description>"] \
  [--tags tag1,tag2] \
  [--schedule "YYYY-MM-DD HH:MM"] \
  [--thumbnail <image-path>] \
  [--product-link <url>] \
  [--product-title "<title>"] \
  [--prepare-only] \
  [--result <json-path>] \
  [--request-id <stable-id>] \
  [--timeout <seconds>] \
  [--engine auto|browser|http] \
  [--skip-ai-declaration] \
  [--visibility public|private|friends] \
  [--debug] \
  [--headed]
```

- 必填参数:
  - `--account`
  - `--file`
  - `--title`
- 可选参数:
  - `--desc`
  - `--tags`
  - `--schedule`
  - `--thumbnail`
  - `--product-link`
  - `--product-title`
  - `--prepare-only`
  - `--result`
  - `--request-id`
  - `--timeout`
  - `--engine`
  - `--skip-ai-declaration`
  - `--visibility`
  - `--debug`
  - `--headed`
- HTTP 引擎额外要求:
  - 必须提供 `--thumbnail`、`--thumbnail-portrait` 或 `--thumbnail-landscape`
  - 必须传 `--skip-ai-declaration`
  - 不支持 `--prepare-only`、`--declaration`、`--product-link`、`--product-title`、`--collection`
  - 标题按 UTF-16 不超过 20；`--schedule` 必须是未来时间
  - `--tags` 会以 `#tag` 文本拼进描述，不是结构化话题

### 上传图文

```bash
sau douyin upload-note \
  --account <account> \
  --images <image-1> [image-2 ...] \
  --title "<title>" \
  [--note "<content>"] \
  [--tags tag1,tag2] \
  [--schedule "YYYY-MM-DD HH:MM"] \
  [--prepare-only] \
  [--result <json-path>] \
  [--request-id <stable-id>] \
  [--timeout <seconds>] \
  [--debug] \
  [--headed]
```

- 必填参数:
  - `--account`
  - `--images`
  - `--title`
- 可选参数:
  - `--note`
  - `--tags`
  - `--schedule`
  - `--prepare-only`
  - `--result`
  - `--request-id`
  - `--timeout`
  - `--debug`
  - `--headed`

## HTTP 引擎

HTTP 引擎调用外部 `DouYin_Spider` 运行时，不启动浏览器。它只支持视频发布；图文仍走浏览器。

```bash
sau douyin setup --accept-third-party
sau douyin login --account <account> --engine http
sau douyin check --account <account> --engine http
sau douyin upload-video \
  --account <account> \
  --file <video> \
  --title "<title>" \
  --desc "<description>" \
  --tags tag1,tag2 \
  --thumbnail <cover> \
  --engine http \
  --skip-ai-declaration \
  --visibility public
```

- 运行时默认位于 `~/.social-auto-upload/tools/douyin-http/<commit>/`，可用 `SAU_DOUYIN_HTTP_ROOT` 或 `conf.DOUYIN_HTTP_ROOT` 改写。
- 凭据默认位于 `cookies/douyin_http_<account>.json`，可用 `SAU_COOKIES_DIR` 改写根目录。
- `--engine auto`（CLI 默认）：传了 `--skip-ai-declaration` 时选择 HTTP；否则仅在运行时已安装且 HTTP 凭据存在时选择 HTTP。
- HTTP 没有草稿接口，不能 `--prepare-only`；也没有 AI 声明、商品和合集。
- HTTP 失败不重试、不自动回退浏览器。结果状态见下文「发布安全与恢复」。

## 浏览器运行形态（默认引擎）

- 登录、校验和上传都使用本机 Google Chrome 的专用 profile，并始终打开可见窗口。
- `--headless` 不被支持，传入会在参数解析阶段以退出码 2 失败；不要尝试用其他方式绕过。
- `--headed` 仅为兼容旧脚本保留，传入不会改变行为。
- 同一账号 profile 同一时间只允许一个进程操作；遇到占用错误时先关闭该账号残留的自动化 Chrome。
- `cookies/douyin_<account>.json` 只用于首次导入和备份；日常以专用 profile 中的登录态为准。

## 发布策略

- 如果不传 `--schedule`，CLI 使用立即发布
- 如果传了 `--schedule`，CLI 自动切换为定时发布
- 时间格式为:

```text
YYYY-MM-DD HH:MM
```

## 额外说明

- `upload-video` 每次命令只支持一个视频文件
- `upload-note` 每次命令支持多张图片
- 视频描述字段统一使用 `--desc`
- 图文正文统一使用 `--note`
- `upload-note` 当前不支持 GIF
- `upload-note` 当前最多支持 35 张图片

## 发布安全与恢复

- `--prepare-only`（仅浏览器引擎）：完成素材上传和表单填写后停在发布页，不点击最终发表；素材仍会上传，平台可能保存草稿。HTTP 引擎传入该参数会被预检拒绝。
- `--result PATH`：导出本次发布记录 JSON，成功、失败或结果未知时都会尽力写出。
- `--request-id TEXT`：稳定业务请求键；相同键且内容相同、已提交过时跳过（退出码 0），内容不同则拒绝。
- `--timeout SECONDS`：总执行预算，默认 1800 秒。

状态含义：

- `prepared`：预演完成，未提交
- `failed_before_submit`：点击前失败，可重试
- `submit_rejected`：平台明确拒绝（如 HTTP `create_v2` 返回非零 `status_code`）；修正内容后可重试，不阻塞
- `submission_unknown`：已尝试提交但结果不明，**先核对原作品，不要重发**
- `needs_human`：点击前遇到验证或风险信号，需人工处理
- `submitted`：已确认提交成功
- `not_submitted`：人工核对确认未发布

```bash
sau safety status --platform douyin --account <account>
sau safety resolve --platform douyin --account <account> \
  --run-id <ID> --outcome not-submitted --reason "创作中心未找到该作品"
```

存在未解决的 `submission_unknown` / `needs_human` 时，后续自动发布会失败；换 `--request-id` 不能绕过。`safety status` 只读本地记录，不访问平台；`safety resolve` 不会登录或重发。本机制不保证 exactly-once，也不承诺不触发平台风控。

发布节奏（可选）：`.local/publish_pacing.json` 可配置 `min_interval_seconds`、`max_submits_per_24h`、`cooldown_after_risk_seconds`，默认不限制。被限制时命令在启动提交前失败（浏览器路径不启动浏览器，HTTP 路径不启动 worker），不创建记录、不提交；`--prepare-only` 不受影响。`sau safety status` 的 `pacing` 字段给出 `allowed_now`、`next_allowed_at` 和原因。不要通过修改配置、换 `--request-id` 或调整系统时间绕过用户设置的节奏。
