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
```

如果无效，就重新登录：

```bash
sau douyin login --account <account>
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
