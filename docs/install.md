# 安装说明

这个文档分成两部分：

- `For Humans`：给正常使用仓库的开发者、创作者、CLI 用户看
- `For AI Agents`：给 OpenClaw、Codex、Claude Code 一类 agent 看

如果你是“正在使用 agent 客户端的人”，想先给 agent 一段启动提示词，而不是直接阅读下面的执行细节，先看：

- [Agent Bootstrap Prompt](./agent-bootstrap.md)

## For Humans

### 1. 克隆项目

```bash
git clone https://github.com/dreammis/social-auto-upload.git
cd social-auto-upload
```

### 2. 创建虚拟环境

推荐使用 `uv`：

Windows PowerShell：

```powershell
uv venv
.venv\Scripts\activate
```

Linux / macOS：

```bash
uv venv
source .venv/bin/activate
```

### 3. 安装主线依赖

当前主线依赖已经放到 `pyproject.toml`，推荐直接执行：

```bash
uv pip install -e .
```

安装完成后，会注册 `sau` 命令。

### 4. 准备浏览器

抖音、小红书、视频号直接使用**本机 Google Chrome**，登录、校验和发布都会打开可见窗口。请先安装正式版 Google Chrome；Chrome 不在常规位置时，在 `conf.py` 设置 `LOCAL_CHROME_PATH`。这三个平台不需要 `patchright install chromium`，也不接受 `--headless`。抖音视频另有一条可选的纯 HTTP 引擎，不启动 Chrome，安装方式见下文「抖音 HTTP 发布引擎（可选）」。

其他仍使用 patchright 的平台需要安装 Chromium。国内用户推荐先指定镜像：

Windows PowerShell：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"; patchright install chromium
```

Linux / macOS：

```bash
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" patchright install chromium
```

### 5. 配置 conf.py

复制一份配置：

```bash
cp conf.example.py conf.py
```

Windows 也可以直接手动复制并重命名。

当前通常还会用到这些配置项：

- `LOCAL_CHROME_PATH`：本机 Chrome 可执行文件路径；留空时按系统 Chrome 通道查找
- `LOCAL_CHROME_PROFILE_ROOT`：专用 Chrome profile 根目录；留空时使用 `<状态目录>/chrome-profiles`
- `LOCAL_CHROME_IMPORT_STORAGE_STATE`：首次是否从 `cookies/*.json` 导入登录态，默认 `True`
- `LOCAL_CHROME_HEADLESS`：只影响尚未迁移的旧平台，抖音／小红书／视频号始终有头
- `DEBUG_MODE`

`XHS_SERVER` 目前只和小红书旧流程相关。

### 6. 验证 CLI 是否可用

```bash
sau --help
sau douyin --help
sau kuaishou --help
sau xiaohongshu --help
sau bilibili --help
```

如果命令找不到，优先确认：

- 当前虚拟环境是否已激活
- 是否执行过 `uv pip install -e .`

### 7. 抖音主线示例

```bash
sau douyin login --account <account_name>
sau douyin check --account <account_name>
sau douyin upload-video --account <account_name> --file videos/demo.mp4 --title "示例标题" --desc "示例简介"
图文正文1:
$noteText = @"图文正文"@    
sau douyin upload-note --account <account_name> --images videos/demo1.png videos/demo2.png --title "图文标题" --note $noteText --tags 'tag1,tag2'
图文正文2:
sau douyin upload-note --account <account_name> --images videos/demo1.png videos/demo2.png --title "图文标题" --notef '图文文件路径' --tags 'tag1,tag2'
添加 BGM（可选）:
sau douyin upload-note --account <account_name> --images videos/demo1.png videos/demo2.png --title "图文标题" --note $noteText --tags 'tag1,tag2' --bgm '音乐名称'
``` 

抖音短信验证码补充说明：

- 视频发布过程中如果触发短信二次验证，程序会优先读取项目根目录下的 `verify_code.txt`
- 如果当前是你手动运行的交互式终端，没提供 `verify_code.txt` 时，CLI 会直接提示你在终端输入验证码
- 如果是 agent 或自动化桥接场景，仍然可以继续通过写入 `verify_code.txt` 来提供验证码
- 验证通过后，程序会自动删除 `verify_code.txt`

抖音卡login手动获取cookie:

- 目标服务器使用vnc
- 浏览器登录抖音创作者中心https://creator.douyin.com/
- 执行`bash export_douyin_cookie.sh --account <account_name>`
- 检查cookie可用性,执行`sau douyin check --account <account_name>`

### 8. 抖音 HTTP 发布引擎（可选）

如果浏览器发布路径不稳定，可以安装外部 `DouYin_Spider` 运行时，改用纯 HTTP 发布视频。该仓库是第三方代码且**没有 LICENSE**，本项目只把它安装到用户目录，不复制进仓库；安装前必须显式传 `--accept-third-party`。

```bash
sau douyin setup --accept-third-party
```

默认从 GitHub 下载固定 commit 的归档并校验 SHA-256。网络不可达时，可指定本地 checkout：

```bash
sau douyin setup --accept-third-party --source C:/path/to/DouYin_Spider
```

`--source` 目录的 git `HEAD` 必须等于运行时固定的 commit。安装会创建独立 venv 并安装依赖，首次可能较慢；默认超时 900 秒，可用 `--timeout` 调整，`--force` 强制重建。

安装位置按 `SAU_DOUYIN_HTTP_ROOT` → `conf.DOUYIN_HTTP_ROOT` → `~/.social-auto-upload/tools/douyin-http` 解析，实际目录为 `<root>/<commit>/`。运行时包含 `manifest.json` 和 `requirements.lock`，每次发布前都会校验关键源文件与依赖锁哈希；不一致时拒绝运行并提示重新 setup。

安装后扫码登录并校验：

```bash
sau douyin login --account <account_name> --engine http
sau douyin check --account <account_name> --engine http
```

HTTP 登录需要交互式终端。凭据写入 `cookies/douyin_http_<account_name>.json`，与浏览器的 `cookies/douyin_<account_name>.json` 分开。

发布示例：

```bash
sau douyin upload-video \
  --account <account_name> \
  --file videos/demo.mp4 \
  --title "示例标题" \
  --desc "示例简介" \
  --tags tag1,tag2 \
  --thumbnail cover.jpg \
  --engine http \
  --skip-ai-declaration
```

HTTP 引擎不支持草稿（`--prepare-only`）、AI 声明（`--declaration`）、商品和合集，必须提供封面，并显式传 `--skip-ai-declaration` 接受无声明发布。完整说明、失败语义和限制见 [抖音 HTTP 发布引擎](./douyin-http.md)。

### 9. 快手主线示例

```bash
sau kuaishou login --account <account_name>
sau kuaishou check --account <account_name>
sau kuaishou upload-video --account <account_name> --file videos/demo.mp4 --title "示例标题" --desc "示例简介"
sau kuaishou upload-note --account <account_name> --images videos/demo1.png videos/demo2.png videos/demo.png --title "图文标题" --note "图文正文"
```

### 10. 小红书主线示例

```bash
sau xiaohongshu login --account <account_name>
sau xiaohongshu check --account <account_name>
sau xiaohongshu upload-video --account <account_name> --file videos/demo.mp4 --title "示例标题" --desc "示例简介"
sau xiaohongshu upload-note --account <account_name> --images videos/demo1.png videos/demo2.png videos/demo.png --title "图文标题" --note "图文正文"
```

### 11. Bilibili 主线示例

```bash
sau bilibili login --account <account_name>
sau bilibili check --account <account_name>
sau bilibili upload-video --account <account_name> --file videos/demo.mp4 --title "示例标题" --desc "示例简介" --tid 249
```

补充说明：

- `creator` 之类的名字只是示例值，真正传的是用户自定义的 `account_name`
- 一个 `account_name` 对应一个账号文件，可以准备多个账号并发使用
- 浏览器平台统一元数据约定：
- 视频使用 `title + desc + tags`
- 图文使用 `title + note + tags`
- 用户不需要手动安装 `biliup`
- 首次运行 Bilibili 相关命令时，程序会自动下载 `biliup`
- 后续运行会自动检查上游 release 并自动更新
- Bilibili 登录建议由用户自己在本地真实终端里执行；如果终端里的二维码显示不完整，可直接打开当前目录下的 `qrcode.png` 扫码
- 如果国内网络访问 GitHub Release 较慢，可先用 `https://gh-proxy.com/` 或 `https://gh-proxy.org/` 辅助访问对应 release 地址排障
- 示例：
  - `https://gh-proxy.org/https://github.com/biliup/biliup/releases/download/v1.1.29/biliupR-v1.1.29-aarch64-linux.tar.xz`

## For AI Agents

如果你是一个可执行命令的 agent，请优先按下面顺序处理：

1. 先假设仓库根目录就是当前工作目录
2. 优先使用 `uv` 管理环境，不要默认回退到旧的 `requirements.txt`
3. 安装命令优先使用：

```bash
uv pip install -e .
```

4. 准备浏览器：抖音、小红书、视频号需要本机 Google Chrome；其他 patchright 平台需要 Chromium。国内镜像安装方式：

Windows PowerShell：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"; patchright install chromium
```

Linux / macOS：

```bash
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" patchright install chromium
```

5. 抖音、小红书、视频号的登录、校验和发布默认打开可见的本机 Chrome 窗口。不要传 `--headless`，也不要尝试用 Playwright Chromium 代替本机 Chrome；首次登录需要用户扫码或完成验证。例外：抖音视频可选 HTTP 引擎（`sau douyin setup --accept-third-party` 后使用 `--engine http`），它不启动浏览器，但需要外部运行时和独立扫码凭据；安装步骤见上文「抖音 HTTP 发布引擎（可选）」。

6. 安装完成后，优先检查：

```bash
sau --help
sau douyin --help
sau kuaishou --help
sau xiaohongshu --help
sau bilibili --help
```

7. 如果用户的目标是抖音或快手的登录、cookie 校验、视频上传、图文上传，优先走 CLI：

```bash
sau douyin setup --accept-third-party   # 可选：安装 HTTP 发布运行时
sau douyin login
sau douyin check
sau douyin upload-video
sau douyin upload-note

sau kuaishou login
sau kuaishou check
sau kuaishou upload-video
sau kuaishou upload-note

sau xiaohongshu login
sau xiaohongshu check
sau xiaohongshu upload-video
sau xiaohongshu upload-note

sau bilibili login
sau bilibili check
sau bilibili upload-video
```

8. 如果用户明确在使用 skill 系统，再引导其阅读：

- `skills/douyin-upload/SKILL.md`
- `skills/douyin-upload/references/cli-contract.md`
- `skills/kuaishou-upload/SKILL.md`
- `skills/kuaishou-upload/references/cli-contract.md`
- `skills/xiaohongshu-upload/SKILL.md`
- `skills/xiaohongshu-upload/references/cli-contract.md`
- `skills/bilibili-upload/SKILL.md`
- `skills/bilibili-upload/references/cli-contract.md`

### 对 agent 的额外说明

- 当登录流程生成本地二维码图片时，不要只把图片路径发给用户
- 这类二维码图片本身就是给用户扫码的，agent 应优先直接展示/发送本地图片给用户扫码
- 如果环境支持查看本地图片，优先用查看图片能力把二维码展示出来；路径只作为补充信息
- 抖音、小红书、视频号默认使用本机 Chrome 专用 profile，默认位于 `.local/chrome-profiles/`；登录态以 profile 为准，`cookies/*.json` 仅作首次导入和备份
- 抖音 HTTP 引擎不使用 Chrome profile，凭据位于 `cookies/douyin_http_<account>.json`；`setup` 安装的是第三方、无 LICENSE 代码，必须得到用户对 `--accept-third-party` 的明确同意
- HTTP 发布没有草稿接口，不能传 `--prepare-only`；必须显式传 `--skip-ai-declaration`，并在发布前向用户说明无法写入“内容由AI生成”声明
- 同一账号的 Chrome profile 不能并发打开；不要删除 profile 或 cookie 文件来绕过登录问题
- 抖音、小红书、视频号不接受 `--headless`；用户要求无头时应说明该限制，不要改用 Playwright Chromium
- Bilibili 登录当前不建议 agent 在非交互环境里直接代跑
- 正确做法是让用户自己在本地终端执行 `sau bilibili login --account <name>`；如果二维码显示不完整，再提示用户打开 `qrcode.png`
- `requirements.txt` 目前是历史兼容文件，不是主安装入口
- `uploader/` 是核心实现目录
- `sau_cli.py` 是当前 CLI 主入口
- `docs/legacy-web.md` 是历史 Web 版本说明，不保证当前可用
- Bilibili 首次运行时可能自动下载 `biliup`
