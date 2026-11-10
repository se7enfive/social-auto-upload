# 抖音 HTTP 发布引擎

`sau douyin` 除默认的本机 Chrome 路径外，还提供一条纯 HTTP 发布链路。它固定使用 [`cv-cat/DouYin_Spider`](https://github.com/cv-cat/DouYin_Spider) 的创作者中心接口实现，通过扫码票据直接完成视频上传与发表，不启动浏览器。

这条链路是可选能力，浏览器路径保持原样。两者的差别：

| | 浏览器引擎 | HTTP 引擎 |
| --- | --- | --- |
| 运行方式 | 可见的本机 Google Chrome | 外部 venv 中的 Python worker |
| 草稿预演 | `--prepare-only` | 不支持，只能直接发布 |
| AI 内容声明 | `--declaration` | 不支持，必须显式 `--skip-ai-declaration` |
| 商品 / 合集 | 支持 | 不支持 |
| 登录态 | 专用 Chrome profile | `cookies/douyin_http_<account>.json` 票据 |
| 失败处理 | 页面信号判定 | `create_v2` 响应判定，不重试、不回退浏览器 |

## 第三方代码边界

- `DouYin_Spider` 仓库**没有 LICENSE**。本项目不复制、不 vendoring 其源码，只把固定 commit 安装到用户目录，并在运行时调用。
- 固定 commit：`d151f9c2980abc304c1ae4918c2272675cd9e4c2`。升级需要修改 `uploader/douyin_uploader/http_runtime.py` 中的常量、归档哈希和源文件哈希，并重新跑测试。
- 安装前必须传 `--accept-third-party`，表示你已自行确认该第三方代码的使用条款与风险。
- worker 运行在独立 venv、临时工作目录，并清空 `PYTHONPATH`、`PYTHONHOME`、`VIRTUAL_ENV` 和所有 `DY_*` 环境变量；不会加载外部仓库的 `.env`。

## 安装运行时

```bash
sau douyin setup --accept-third-party
```

默认从 GitHub 下载固定 commit 的归档，校验 SHA-256 后解压。网络不可达或需要离线安装时，可指定本地 checkout：

```bash
sau douyin setup --accept-third-party --source C:/path/to/DouYin_Spider
```

`--source` 目录必须是 git checkout，且 `HEAD` 等于上面的固定 commit；复制时会排除 `.git`、`.env`、`__pycache__`、`.venv`、`venv`、`node_modules` 和 `*.pyc`。

其他参数：

- `--force`：删除并重建运行时。
- `--timeout SECONDS`：安装超时，默认 900 秒，同时约束下载与每次 pip 安装。

安装位置按以下顺序解析：

1. 环境变量 `SAU_DOUYIN_HTTP_ROOT`
2. `conf.py` 的 `DOUYIN_HTTP_ROOT`
3. 默认 `~/.social-auto-upload/tools/douyin-http`

实际目录为 `<root>/<commit>/`，包含：

```text
src/                  DouYin_Spider 源码（固定 commit）
venv/                 独立虚拟环境
manifest.json         commit、源文件哈希、依赖锁哈希、Python 版本
requirements.lock     pip freeze 结果
.failed               仅安装失败时存在，内容为错误摘要
```

安装过程：

1. 复制或下载源码；
2. 用当前 Python 创建独立 venv；
3. 安装完整 `requirements.txt`；失败时退回最小发布依赖集（`curl_cffi`、`cryptography`、`ecdsa`、`loguru`、`python-dotenv`、`lxml`、`protobuf`、`pillow`、`qrcode` 等），并重新验证；
4. 写 `requirements.lock`；
5. 运行一次 worker self-test，确认 `DouyinAuth`、`DouyinCreatorAPI.post_video` 和 `douyin_creator_api._request` 仍存在；
6. 最后写 `manifest.json`。

每次发布、登录和校验前都会重新校验 manifest、venv Python、关键源文件哈希和依赖锁哈希。任何不一致都直接拒绝运行，并提示重新 setup；不会自动更新第三方代码。

## 登录与校验

HTTP 登录是扫码流程，必须在交互式终端中执行（stdin 和 stdout 都是 TTY）：

```bash
sau douyin login --account <account> --engine http
```

- 若已存在 HTTP 凭据且 `check` 通过，会直接复用，不重复扫码。
- `--force` 强制重新扫码。
- `login --engine auto`（默认）在运行时已安装时选择 HTTP；如需浏览器登录，显式传 `--engine browser`。`check --engine auto` 仅在运行时已安装且 HTTP 凭据存在时选择 HTTP。
- CLI 在终端打印二维码，同时保存到 `.local/qrcodes/douyin-http-<account>.png`（可用 `SAU_STATE_DIR` 改写状态根），流程结束后删除临时图。
- 成功后写入 `cookies/douyin_http_<account>.json`（可用 `SAU_COOKIES_DIR` 改写根目录），权限尽力设为 `600`。

凭据包含 `cookie`、`ticket`、`ts_sign`、`private_key`，以及可选的 `client_cert`、`dtrait_blob`。它与浏览器路径的 `cookies/douyin_<account>.json` 分开保存，互不覆盖。

校验：

```bash
sau douyin check --account <account> --engine http
```

输出 `valid` / `invalid`，退出码 0/1。该校验只调用只读的 `get_preview_video_list`，能证明 cookie 和创作者读链路可用，但**不校验 bd-ticket 的服务端有效性**。如果发布时遇到 403，请用 `login --engine http --force` 重新扫码。

## 发布视频

最小命令：

```bash
sau douyin upload-video \
  --account <account> \
  --file videos/demo.mp4 \
  --title "示例标题" \
  --desc "示例简介" \
  --tags tag1,tag2 \
  --thumbnail cover.jpg \
  --engine http \
  --skip-ai-declaration
```

HTTP 引擎的硬性要求：

- 必须提供封面：`--thumbnail`、`--thumbnail-portrait` 或 `--thumbnail-landscape` 三选一。优先级为 portrait → `--thumbnail` → landscape。显式封面可跳过第三方默认的 AI 封面流程。
- 必须传 `--skip-ai-declaration`。当前固定 commit 无法写入“内容由AI生成”声明；传该参数表示你确认接受无声明发布。该风险会记录在发布 journal 的 evidence 中。
- 标题不能为空，且按 UTF-16 code unit 计算不超过 20。含 emoji 时不要按字符数估算。
- 视频文件必须存在且非空。
- `--schedule` 必须是未来时间，格式仍为 `YYYY-MM-DD HH:MM`。
- `--visibility` 可选 `public`（默认）、`private`、`friends`。非 `public` 会强制走 HTTP；显式 `--engine browser`，或与 `--prepare-only` / `--declaration` / 商品 / 合集同时使用，会直接报错，避免被浏览器引擎静默发布为公开。

标签不会作为结构化 challenge 提交，而是由父进程拼进描述：正文 + ` #tag1 #tag2`。首次发布后建议在创作中心核对话题是否被平台识别。

以下参数在 HTTP 引擎下会被预检拒绝，请改用 `--engine browser`：

- `--prepare-only`：HTTP 没有草稿接口。
- `--declaration`：无法写入自主声明。
- `--product-link` / `--product-title`：不支持商品。
- `--collection`：不支持合集。

预检发生在发布记录创建之后、worker 启动之前，因此失败仍会导出 `--result`，状态为 `failed_before_submit`，不会上传任何内容。

### 引擎选择

`--engine` 可选 `auto`（默认）、`browser`、`http`。`auto` 规则按顺序如下：

1. 传了非 `public` 的 `--visibility` → HTTP；如果同时带浏览器专属选项（`--prepare-only`、`--declaration`、商品、合集），直接报错，不回退浏览器。
2. 传了 `--prepare-only`、`--declaration`、商品链接或合集 → 浏览器。
3. 传了 `--skip-ai-declaration` → HTTP。此时若运行时或凭据缺失，会在预检阶段报错，**不会回退浏览器**。
4. HTTP 运行时已安装且 HTTP 凭据文件存在 → HTTP。
5. 否则 → 浏览器。

也可以在 `conf.py` 设置默认值：

```python
DOUYIN_UPLOAD_ENGINE = "auto"  # auto | browser | http
```

`upload-note` 仍只支持浏览器引擎，不接受 `--engine`。

## 提交与失败语义

worker 在上传视频、获取 CSRF、准备封面等步骤完成后，会拦截 `POST /web/api/media/aweme/create_v2/`，先向父进程发 `ready_to_submit`，收到 `submit` 确认后才真正发送请求。父子进程共用现有的 `PublicationAttempt` / `submit_once` 安全层：提交意图先落盘，再发一次请求，不重试。

| 场景 | journal 状态 | 建议动作 |
| --- | --- | --- |
| 运行时、凭据、选项预检失败 | `failed_before_submit` | 修复后重试 |
| worker 上传 / CSRF / 封面等提交前失败 | `failed_before_submit` | 修复后重试 |
| 写提交意图失败，未发请求 | `failed_before_submit` | 修复后重试 |
| 发送 `submit` 命令失败 | `submission_unknown` | 先核对原作品 |
| 请求发出后 worker 死亡、超时或 EOF | `submission_unknown` | 先核对原作品 |
| `create_v2` 成功且返回 `item_id` | `submitted` | 无需重试 |
| `create_v2` 明确拒绝（JSON `status_code != 0`、非 JSON 4xx） | `submit_rejected` | 修正内容后可重试 |
| 响应头出现二次验证（`x-tt-verify-passport-decision`） | `needs_human` | 人工完成验证 |
| 成功后再跑相同 request-id | 跳过，退出码 0 | 无需动作 |

`submit_rejected` 是终态但不阻塞：同一 request-id 在内容不变时可以再次尝试；`submission_unknown` 和 `needs_human` 仍会阻止该账号后续自动发布，必须先用 `sau safety status` 核对，再用 `sau safety resolve` 记录结论。

HTTP 引擎**不重试、不自动回退浏览器**。失败时请按上表处理；需要草稿或声明时，显式改用 `sau douyin upload-video --engine browser ... --prepare-only`。

## 进程与凭据安全

- 父进程把 `http_worker.py` 复制到私有临时目录，以外部 venv 的 Python 启动；`cwd` 为临时目录，避免加载第三方仓库的 `.env`。
- 子进程的 `PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`、`VIRTUAL_ENV` 和所有 `DY_*` 变量会被清空；`PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。
- 凭据只通过临时任务文件的路径传递，不出现在 argv、日志或 `--result` JSON 中。
- worker 的 stdout 只用于 JSON-lines 协议；外部代码的 print 和日志全部重定向到 stderr。
- 失败时只输出 redact 后的 stderr 尾部（`cookie`、`ticket`、`ts_sign`、`private_key`、`client_cert` 的值替换为 `***`）。
- 结束或取消时按进程树清理：Windows 使用 `taskkill /T /F`，POSIX 使用 `killpg`。

## 已知限制

- **私有协议**：`create_v2`、bd-ticket、dtrait 都是抖音未公开接口，平台改版可能导致失败。固定 commit + manifest 哈希 + self-test 让失效快速暴露，而不是静默发错。
- **无草稿、无 AI 声明、无商品、无合集**：这些能力只在浏览器引擎可用。
- **票据过期**：`check` 不验证服务端票据；发布 403 时重新扫码。
- **依赖较重**：完整依赖包含 `curl_cffi`、`protobuf`、opencv、av、rocksdict 等，首次安装慢；失败时可用 `--source` 和更长的 `--timeout` 重试。
- **发布节奏与账号锁不变**：HTTP 路径同样受 `utils/publish_pacing.py` 和账号级 `session_lock` 约束；锁锚点仍是浏览器账号文件 `cookies/douyin_<account>.json`，因此同一 checkout 内浏览器和 HTTP 不会并发。

## 相关文档

- [CLI 使用说明](./CLI.md#抖音-cli-子命令)
- [发布安全与可靠性机制](./publish-safety.md)
- [安装说明](./install.md)
- [抖音上传 Skill](../skills/douyin-upload/SKILL.md)
