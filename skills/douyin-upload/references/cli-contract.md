# 抖音 CLI 契约

这个 skill 默认假设当前环境已经安装并可调用 `sau` 命令。

## 命令列表

### 登录

```bash
sau douyin login --account <account>
```

- 必填参数:
  - `--account`
- 作用:
  - 启动抖音登录流程，为指定账号生成或刷新 cookie 文件
  - 如果登录过程中生成本地二维码图片，agent 应优先直接把图片展示/发送给用户扫码，而不是只回传路径
- 账号说明:
  - `--account` 传的是用户自定义的 `account_name`，不是固定只能叫 `creator`
  - 一个 `account_name` 对应一个账号文件，可用于多账号隔离和并发任务

### 校验 cookie

```bash
sau douyin check --account <account>
```

- 必填参数:
  - `--account`
- 预期输出:
  - `valid`：cookie 可用
  - `invalid`：cookie 缺失或已失效

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
  [--debug] \
  [--headless | --headed]
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
  - `--debug`
  - `--headless`
  - `--headed`

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
  [--headless | --headed]
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
  - `--headless`
  - `--headed`

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

- `--prepare-only`：完成素材上传和表单填写后停在发布页，不点击最终发表；素材仍会上传，平台可能保存草稿。
- `--result PATH`：导出本次发布记录 JSON，成功、失败或结果未知时都会尽力写出。
- `--request-id TEXT`：稳定业务请求键；相同键且内容相同、已提交过时跳过（退出码 0），内容不同则拒绝。
- `--timeout SECONDS`：总执行预算，默认 1800 秒。

状态含义：

- `prepared`：预演完成，未提交
- `failed_before_submit`：点击前失败，可重试
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

发布节奏（可选）：`.local/publish_pacing.json` 可配置 `min_interval_seconds`、`max_submits_per_24h`、`cooldown_after_risk_seconds`，默认不限制。被限制时命令在启动浏览器前失败，不创建记录、不点击发表；`--prepare-only` 不受影响。`sau safety status` 的 `pacing` 字段给出 `allowed_now`、`next_allowed_at` 和原因。不要通过修改配置、换 `--request-id` 或调整系统时间绕过用户设置的节奏。
