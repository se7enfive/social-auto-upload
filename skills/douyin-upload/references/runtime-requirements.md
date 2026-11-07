# 运行前提

这个 skill 默认假设当前环境已经具备：

- 已安装 `social-auto-upload`
- 可以调用 `sau` 命令，或至少有等效调用方式
- 已安装本机 Google Chrome（不是 Playwright 自带的 Chromium）

## 推荐安装方式

在项目根目录执行：

```bash
uv pip install -e .
```

## 准备本机 Google Chrome

抖音的登录、校验和上传都通过本机 Chrome 的专用 profile 运行，必须使用可见浏览器窗口。

- 默认使用系统安装的 Google Chrome。
- 如果 Chrome 不在常规位置，在 `conf.py` 中设置 `LOCAL_CHROME_PATH` 为实际可执行文件路径。
- 不需要为抖音执行 `patchright install chromium`；该命令仍供其他未迁移平台使用。
- 专用 profile 默认保存在 `.local/chrome-profiles/douyin/`；同一账号的 profile 不能同时被两个进程打开。

## 登录态与二维码

- 首次登录需要用户在打开的本机 Chrome 窗口中扫码或完成验证。
- `cookies/douyin_<account>.json` 只作为首次导入和后续备份；日常以专用 Chrome profile 中的登录态为准。
- 如果登录过程中已经生成了本地二维码图片，agent 应优先直接把图片展示/发送给用户扫码，不要只告诉用户图片路径。
- 不要传 `--headless`；抖音命令会直接拒绝该参数。

## 常见调用方式

### 如果 `sau` 已经在 PATH 中

```bash
sau douyin --help
```

### 如果虚拟环境存在，但还没有激活

PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
sau douyin --help
```

### 如果你想直接调用可执行文件

PowerShell：

```powershell
.\.venv\Scripts\sau.exe douyin --help
```

### 如果你更倾向于使用 uv

```bash
uv run sau douyin --help
```

## 可见窗口与账号隔离

- 抖音命令始终使用有头模式；`--headless` 会被拒绝，`--headed` 仅为兼容旧脚本保留。
- 每个 `--account` 对应一个独立的本机 Chrome profile，互不共享登录态。
- 同一 profile 同一时间只允许一个进程操作；遇到“profile 被占用”时先关闭该账号残留的自动化 Chrome，再重试。
- 如果登录过程中已经生成了本地二维码图片，agent 应优先直接把图片展示/发送给用户扫码，不要只告诉用户图片路径。
