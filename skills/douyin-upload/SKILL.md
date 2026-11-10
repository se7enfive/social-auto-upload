---
name: douyin-upload
description: 当 agent 需要通过已安装的 `sau` CLI 完成抖音登录、cookie 校验、视频上传或图文发布时使用这个 skill。该 skill 适用于已经安装 `social-auto-upload` 且可调用 `sau` 命令的环境。优先使用这个 skill 进行稳定的命令式抖音工作流，而不是一开始就阅读 uploader 源码。
license: MIT-0
slug: social-auto-upload-douyin-upload
displayName: 抖音视频与图文发布
version: 1.1.0
summary: 使用 sau 登录抖音并发布视频或图文，可选浏览器或 HTTP 引擎。
---

# 抖音上传 Skill

优先把 `sau` 作为主接口。

不要假设当前环境一定能读取仓库源码。
不要一开始就去读 `uploader/`。
只有在命令不可用或 CLI 执行失败时，才回退到故障排查说明。

## 功能概览

| 功能 | 命令入口 | 说明 |
| --- | --- | --- |
| HTTP 运行时安装（可选） | `sau douyin setup --accept-third-party` | 安装第三方 `DouYin_Spider`，用于纯 HTTP 发布 |
| 抖音登录 | `sau douyin login --account <name> [--engine ...]` | 浏览器登录，或 HTTP 扫码登录 |
| 会话校验 | `sau douyin check --account <name> [--engine ...]` | 检查浏览器登录态或 HTTP 票据 |
| 视频上传 | `sau douyin upload-video ...` | 浏览器或 HTTP 引擎发布视频 |
| 图文上传 | `sau douyin upload-note ...` | 仅浏览器引擎 |

元数据约定：

- 视频使用 `title + desc + tags`
- 图文使用 `title + note + tags`

## 两种发布引擎

| | 浏览器引擎（默认） | HTTP 引擎（可选） |
| --- | --- | --- |
| 登录态 | 本机 Chrome 专用 profile | `cookies/douyin_http_<account>.json` |
| 草稿预演 | `--prepare-only` | **不支持** |
| AI 内容声明 | `--declaration` | **不支持**，必须 `--skip-ai-declaration` |
| 商品 / 合集 | 支持 | 不支持 |
| 发布结果 | 页面信号 | `create_v2` 响应 |

`--engine` 可选 `auto`（默认）、`browser`、`http`。`auto` 在传了 `--skip-ai-declaration` 时选择 HTTP；否则仅在 HTTP 运行时已安装且 HTTP 凭据存在时选择 HTTP，其余情况走浏览器。HTTP 失败不会自动回退浏览器。

## 本机 Chrome 运行形态（浏览器引擎）

- 登录、校验、上传全过程都在**本机 Google Chrome 的可见窗口**中完成；抖音浏览器命令默认有头，显式传 `--headless` 会被 argparse 直接拒绝。
- 每个 `--account` 使用独立的专用 Chrome profile，默认位于 `.local/chrome-profiles/douyin/`（可用 `SAU_STATE_DIR` 或 `conf.LOCAL_CHROME_PROFILE_ROOT` 改写）。不同账号的 profile 相互隔离。
- 已有的 `cookies/douyin_<account>.json` 只在首次导入 profile 和之后导出备份时使用；登录后以专用 profile 中的登录态为准。
- 同一账号 profile 同一时间只能被一个进程使用。启动前先结束同账号的其他自动化 Chrome 窗口；否则命令会提示 profile 被占用。
- 用户明确要求无头模式时，说明浏览器引擎已不支持无头，不能传 `--headless`，也不要改用 Playwright Chromium 绕过。若用户接受无 AI 声明，可考虑 HTTP 引擎。
- 首次登录或二维码失效时，需要用户在打开的本机 Chrome 窗口中扫码或完成验证。

## 默认工作流（浏览器引擎）

1. 先确认 `references/runtime-requirements.md` 里的运行前提。
2. 再确认 `references/cli-contract.md` 里的命令契约。
3. 默认先加 `--prepare-only` 执行匹配的 `sau douyin ...` 命令，完成素材上传和表单填写但不点最终发表。
4. 向用户报告账号、标题、正文、标签、封面、排期和 `--prepare-only` 可能留下的素材／草稿，取得明确确认。
5. 确认后执行不带 `--prepare-only` 的同一命令完成发布；准备与正式提交使用不同但可关联的 `--request-id`（例如 `<业务ID>-prepare` / `<业务ID>-submit`），正式提交的 ID 在重试间保持稳定。
6. 如果命令失败，再看 `references/troubleshooting.md`；结果未知时先运行 `sau safety status`，核对平台原作品后再决定下一步。

用户在最初请求中说“帮我发布”不构成对最终内容的确认。必须在 prepare 结果展示后停止，等待用户对账号、标题、正文、素材和排期的**新的明确确认**，再执行正式提交。

## HTTP 引擎工作流（可选）

只有在用户明确要求改用 HTTP，或用户知道浏览器路径反复失败并同意尝试时才使用。HTTP 发布**没有草稿、没有 AI 声明、不能预览**，因此确认环节不能省。

1. **取得第三方代码同意**：说明 `DouYin_Spider` 是第三方、无 LICENSE 的私有接口实现，会安装到用户目录并在运行时调用。用户同意后才执行：

   ```bash
   sau douyin setup --accept-third-party
   ```

   网络不可达时，可让用户提供固定 commit 的本地 checkout，再加 `--source <dir>`。

2. **扫码登录**：必须在交互式终端中执行；非 TTY 会直接报错。

   ```bash
   sau douyin login --account <account> --engine http
   ```

   终端会打印二维码并保存为 `.local/qrcodes/douyin-http-<account>.png`。agent 不要只回传路径，优先直接把二维码图片展示/发送给用户扫码。已有凭据且校验通过时会直接复用；需要换号或票据过期时加 `--force`。

3. **校验**：

   ```bash
   sau douyin check --account <account> --engine http
   ```

   必须得到 `valid` 才能发布。该校验只验证只读链路和凭据字段，不保证 bd-ticket 服务端仍有效；发布遇到 403 时重新扫码。

4. **准备发布参数**。HTTP 的硬性要求：
   - 必须提供封面：`--thumbnail`、`--thumbnail-portrait` 或 `--thumbnail-landscape`。
   - 必须传 `--skip-ai-declaration`，表示接受平台上看不到“内容由AI生成”声明。
   - 标题按 UTF-16 计算不超过 20；视频文件非空；定时时间必须是未来。
   - `--visibility` 明确选择 `public`、`private` 或 `friends`，默认 `public`。
   - 标签会以 `#tag` 文本拼进描述，不是结构化话题，发布后建议核对。

5. **发布前向用户展示并取得明确确认**：账号、标题、正文、标签、封面路径、排期、可见范围，以及**“本次发布不会写入 AI 内容声明”**。用户最初说“帮我发布”不能替代这次确认。

6. **直接发布**（没有 prepare 预演）：

   ```bash
   sau douyin upload-video \
     --account <account> \
     --file <video> \
     --title "<title>" \
     --desc "<description>" \
     --tags tag1,tag2 \
     --thumbnail <cover> \
     --engine http \
     --skip-ai-declaration \
     --visibility public \
     --request-id <stable-id> \
     --result <json-path>
   ```

7. **按结果处理**：
   - `submitted`：向用户报告成功，并记录返回的 `item_id`。不要补写“已声明 AI 生成”。
   - `submit_rejected`：平台明确拒绝。可修正标题、简介、封面或视频后，用同一 `--request-id` 重试。
   - `submission_unknown`：**不要重发**。先运行 `sau safety status`，到抖音创作中心核对原作品，再用 `sau safety resolve` 记录结论。
   - `needs_human`：出现二次验证或风险信号，交给用户人工处理。
   - `failed_before_submit`：预检或上传前失败，修复后重试。
   - HTTP 引擎不重试、不自动回退浏览器。需要草稿或 AI 声明时，改为 `--engine browser --prepare-only`。

## 支持动作

- 使用 `sau douyin setup --accept-third-party` 安装可选 HTTP 运行时
- 使用 `sau douyin login --account <name> [--engine http]` 登录
- 使用 `sau douyin check --account <name> [--engine http]` 校验会话
- 使用 `sau douyin upload-video ...` 上传抖音视频（浏览器或 HTTP）
- 使用 `sau douyin upload-note ...` 上传抖音图文（仅浏览器）
- 使用 `sau safety status --platform douyin --account <name>` 查看本地未解决记录
- 使用 `sau safety resolve ...` 在人工核对后记录结论（不登录、不重发）

## 命令选择建议

- 当用户需要新的登录态，或现有登录态已失效时，使用 `login`
- 当用户只需要确认会话状态时，使用 `check`
- 当用户要发布视频时，使用 `upload-video`
- 当用户要发布图文时，使用 `upload-note`
- 除非用户已明确确认内容并授权立即发布，否则浏览器路径先使用 `--prepare-only` 预演
- 用户接受无 AI 声明、且需要绕过不稳定的浏览器路径时，才使用 `--engine http`

## 发布安全约定

- 浏览器路径的 `--prepare-only` 不是离线操作：素材仍会上传，平台可能保存草稿。
- HTTP 路径没有草稿接口：`--prepare-only`、`--declaration`、商品和合集都会在预检阶段被拒绝。
- 最终发表只执行一次；结果不明时状态为 `submission_unknown`，此时**不要重发**，先到抖音创作中心核对原作品。
- `submit_rejected` 是平台明确拒绝，不阻塞；修正内容后可用同一 request-id 重试。
- 存在 `submission_unknown` 或 `needs_human` 时，后续自动发布会被阻止；必须人工核对后用 `sau safety resolve` 记录结论。
- 用户可能配置了发布节奏（最小间隔、24 小时提交上限、风险后冷却）。被限制时命令会在提交前失败；不要通过修改 `.local/publish_pacing.json`、换 `--request-id` 或调整系统时间绕过，确需调整时先征得用户同意。`--prepare-only` 不受节奏限制。
- 不要尝试绕过验证码、伪造指纹或规避平台风控；遇到验证时交给用户人工处理。
- 不要声称“保证不封号”或“完全 exactly-once”；本机制只降低误发和重复提交风险。

## 执行前检查

- 先确认当前 shell 里是否可以调用 `sau`
- 浏览器路径：确认本机已安装 Google Chrome（必要时在 `conf.py` 设置 `LOCAL_CHROME_PATH`）
- HTTP 路径：确认已获得第三方代码安装同意、运行时已安装、当前是交互式终端；凭据位于 `cookies/douyin_http_<account>.json`
- 如果 `sau` 不可用，按 `references/runtime-requirements.md` 里的回退方式处理
- 浏览器命令不要传 `--headless`；登录、校验和发布都会打开可见的本机 Chrome 窗口
- `--headed` 仅为兼容旧脚本保留，传入不会改变行为
- 只有用户明确要求定时发布时，才使用 `--schedule`
- 非交互场景建议传 `--request-id` 和 `--result`，便于幂等判断和事后核对
- 如果登录流程生成了本地二维码图片，不要只把图片路径告诉用户
- 二维码图片本身就是给用户扫码的，优先直接把本地图片展示/发送给用户

## 参考文档

- 运行前提：`references/runtime-requirements.md`
- CLI 契约：`references/cli-contract.md`
- 故障排查：`references/troubleshooting.md`
- 仓库内完整说明（若可读取源码）：`../../docs/douyin-http.md`
