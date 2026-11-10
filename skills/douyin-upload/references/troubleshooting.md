# 故障排查

## 找不到 `sau` 命令

可以尝试以下方式：

```powershell
.\.venv\Scripts\Activate.ps1
sau douyin --help
```

```powershell
.\.venv\Scripts\sau.exe douyin --help
```

```bash
uv run sau douyin --help
```

如果当前环境还没有安装项目：

```bash
uv pip install -e .
```

## cookie 无效或已过期

先检查 cookie 状态：

```bash
sau douyin check --account <account>
# HTTP 引擎：
sau douyin check --account <account> --engine http
```

如果无效，就重新登录：

```bash
sau douyin login --account <account>
# HTTP 引擎（需要交互式终端）：
sau douyin login --account <account> --engine http --force
```

## 登录二维码处理

登录会在本机打开可见的 Google Chrome 窗口，用户通常可以直接在窗口中扫码。若 CLI 仍生成了临时二维码图片：

- 查找 CLI 打印出来的图片路径
- agent 不要只把图片路径回给用户
- agent 应优先直接把本地二维码图片展示/发送给用户扫码

不要为绕过扫码而传 `--headless`；该参数会被直接拒绝。

## 本机 Chrome 无法启动

- 确认已安装 Google Chrome，而不是只有 Playwright Chromium。
- Chrome 不在常规位置时，在 `conf.py` 设置 `LOCAL_CHROME_PATH` 为 `chrome.exe` 的实际路径。
- 首次使用会创建专用 profile；若目录不可写，检查 `SAU_STATE_DIR` 或 `conf.LOCAL_CHROME_PROFILE_ROOT` 的权限。

## profile 被占用

同一账号的专用 Chrome profile 不能被两个进程同时打开。

- 关闭该账号残留的自动化 Chrome 窗口后重试。
- 不要删除整个 profile 来绕过占用；删除会丢失该账号的本机登录态。

## HTTP 引擎

### 运行时未安装或完整性校验失败

现象：提示未找到有效运行时、manifest 版本不兼容、commit 不匹配，或关键源文件哈希不一致。

处理：

```bash
sau douyin setup --accept-third-party
```

仍然失败时重建：

```bash
sau douyin setup --accept-third-party --force
```

不要手动修改运行时目录里的源文件或 `manifest.json`；下次发布会再次拒绝。

### 安装失败（网络、pip 或超时）

- 确认能访问 GitHub；不能访问时，让用户提供固定 commit 的本地 checkout，再执行：

  ```bash
  sau douyin setup --accept-third-party --source <checkout-dir> --timeout 1800
  ```

- `--source` 目录必须是 git 仓库，且 `HEAD` 等于运行时固定 commit。
- 完整 `requirements.txt` 安装失败时，安装器会自动退回最小发布依赖集；如果两者都失败，查看错误里的 pip 输出尾部。
- 安装失败会在运行时目录留下 `.failed`，内容为错误摘要；修复后加 `--force` 重建。

### 登录被拒绝：需要交互式终端

HTTP 扫码登录要求 stdin 和 stdout 都是 TTY。请让用户在本地终端直接执行：

```bash
sau douyin login --account <account> --engine http
```

agent 不要在非交互子进程里代跑登录。二维码图片位于 `.local/qrcodes/douyin-http-<account>.png`，优先直接展示给用户。

### 发布 403 或 check 失效

`check` 只做只读校验，不验证 bd-ticket 的服务端有效性。若发布返回 403：

```bash
sau douyin login --account <account> --engine http --force
sau douyin check --account <account> --engine http
```

### 预检失败：HTTP 不支持的选项

以下参数在 HTTP 引擎下会直接报错，状态为 `failed_before_submit`，不会上传：

- `--prepare-only`：HTTP 没有草稿接口。需要草稿时改为 `--engine browser --prepare-only`。
- `--declaration`：HTTP 无法写入自主声明。
- `--product-link` / `--product-title` / `--collection`：不支持。
- 缺少 `--skip-ai-declaration`：必须显式接受无声明发布。
- 缺少封面：必须传 `--thumbnail`、`--thumbnail-portrait` 或 `--thumbnail-landscape`。
- 标题超过 20 个 UTF-16 code unit，或定时时间已过去。

### 发布结果处理

- `submit_rejected`：平台明确拒绝。修正内容后可用同一 `--request-id` 重试。
- `submission_unknown`：请求可能已经发出。**不要重发**；先 `sau safety status`，到抖音创作中心核对原作品，再 `sau safety resolve`。
- `needs_human`：出现二次验证或风险信号，交给用户人工完成验证。
- `failed_before_submit`：未提交，修复后重试。

HTTP 引擎不会自动重试，也不会回退浏览器。

## 上传参数缺失

### 视频上传

最少需要：

- `--account`
- `--file`
- `--title`

### 图文上传

最少需要：

- `--account`
- `--images`
- `--title`

`--note` 当前是可选图文正文。

## 图片限制

对 `upload-note` 来说：

- 不支持 GIF
- 最多 35 张图片

如果超出这些限制，先减少图片数量或替换文件格式，再重试。

## 定时发布

时间格式使用：

```text
YYYY-MM-DD HH:MM
```

如果不需要定时发布，去掉 `--schedule` 即可改为立即发布。
