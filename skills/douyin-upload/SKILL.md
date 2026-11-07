---
name: douyin-upload
description: 当 agent 需要通过已安装的 `sau` CLI 完成抖音登录、cookie 校验、视频上传或图文发布时使用这个 skill。该 skill 适用于已经安装 `social-auto-upload` 且可调用 `sau` 命令的环境。优先使用这个 skill 进行稳定的命令式抖音工作流，而不是一开始就阅读 uploader 源码。
license: MIT-0
slug: social-auto-upload-douyin-upload
displayName: 抖音视频与图文发布
version: 1.0.0
summary: 使用 sau 登录抖音并发布视频或图文。
---

# 抖音上传 Skill

优先把 `sau` 作为主接口。

不要假设当前环境一定能读取仓库源码。
不要一开始就去读 `uploader/`。
只有在命令不可用或 CLI 执行失败时，才回退到故障排查说明。

## 功能概览

| 功能 | 命令入口 | 说明 |
| --- | --- | --- |
| 抖音登录 | `sau douyin login --account <name>` | 在专用本机 Chrome profile 中登录，并导出 cookie 备份 |
| cookie 校验 | `sau douyin check --account <name>` | 检查指定账号 cookie 是否有效 |
| 视频上传 | `sau douyin upload-video ...` | 上传并发布抖音视频 |
| 图文上传 | `sau douyin upload-note ...` | 上传并发布抖音图文 |

元数据约定：

- 视频使用 `title + desc + tags`
- 图文使用 `title + note + tags`

## 本机 Chrome 运行形态

- 登录、校验、上传全过程都在**本机 Google Chrome 的可见窗口**中完成；抖音命令默认有头，显式传 `--headless` 会被 argparse 直接拒绝。
- 每个 `--account` 使用独立的专用 Chrome profile，默认位于 `.local/chrome-profiles/douyin/`（可用 `SAU_STATE_DIR` 或 `conf.LOCAL_CHROME_PROFILE_ROOT` 改写）。不同账号的 profile 相互隔离。
- 已有的 `cookies/douyin_<account>.json` 只在首次导入 profile 和之后导出备份时使用；登录后以专用 profile 中的登录态为准。
- 同一账号 profile 同一时间只能被一个进程使用。启动前先结束同账号的其他自动化 Chrome 窗口；否则命令会提示 profile 被占用。
- 用户明确要求无头模式时，说明该平台已不支持无头，不能传 `--headless`，也不要改用 Playwright Chromium 绕过。
- 首次登录或二维码失效时，需要用户在打开的本机 Chrome 窗口中扫码或完成验证。

## 默认工作流

1. 先确认 `references/runtime-requirements.md` 里的运行前提。
2. 再确认 `references/cli-contract.md` 里的命令契约。
3. 默认先加 `--prepare-only` 执行匹配的 `sau douyin ...` 命令，完成素材上传和表单填写但不点最终发表。
4. 向用户报告账号、标题、正文、标签、封面、排期和 `--prepare-only` 可能留下的素材／草稿，取得明确确认。
5. 确认后执行不带 `--prepare-only` 的同一命令完成发布；准备与正式提交使用不同但可关联的 `--request-id`（例如 `<业务ID>-prepare` / `<业务ID>-submit`），正式提交的 ID 在重试间保持稳定。
6. 如果命令失败，再看 `references/troubleshooting.md`；结果未知时先运行 `sau safety status`，核对平台原作品后再决定下一步。

用户在最初请求中说“帮我发布”不构成对最终内容的确认。必须在 prepare 结果展示后停止，等待用户对账号、标题、正文、素材和排期的**新的明确确认**，再执行正式提交。

## 支持动作

- 使用 `sau douyin login --account <name>` 在专用本机 Chrome profile 中登录抖音
- 使用 `sau douyin check --account <name>` 校验本机 Chrome 登录态是否有效
- 使用 `sau douyin upload-video ...` 上传抖音视频
- 使用 `sau douyin upload-note ...` 上传抖音图文
- 使用 `sau safety status --platform douyin --account <name>` 查看本地未解决记录
- 使用 `sau safety resolve ...` 在人工核对后记录结论（不登录、不重发）

## 命令选择建议

- 当用户需要新的登录态，或现有登录态已失效时，使用 `login`
- 当用户只需要确认 cookie 状态时，使用 `check`
- 当用户要发布视频时，使用 `upload-video`
- 当用户要发布图文时，使用 `upload-note`
- 除非用户已明确确认内容并授权立即发布，否则先使用 `--prepare-only` 预演

## 发布安全约定

- `--prepare-only` 不是离线操作：素材仍会上传，平台可能保存草稿。
- 最终发表只点击一次；点击后结果不明时状态为 `submission_unknown`，此时**不要重发**，先到抖音创作中心核对原作品。
- 存在 `submission_unknown` 或 `needs_human` 时，后续自动发布会被阻止；必须人工核对后用 `sau safety resolve` 记录结论。
- 用户可能配置了发布节奏（最小间隔、24 小时提交上限、风险后冷却）。被限制时命令会在点击前失败；不要通过修改 `.local/publish_pacing.json`、换 `--request-id` 或调整系统时间绕过，确需调整时先征得用户同意。`--prepare-only` 不受节奏限制。
- 不要尝试绕过验证码、伪造指纹或规避平台风控；遇到验证时交给用户人工处理。
- 不要声称“保证不封号”或“完全 exactly-once”；本机制只降低误发和重复提交风险。

## 执行前检查

- 先确认当前 shell 里是否可以调用 `sau`，且本机已安装 Google Chrome（必要时在 `conf.py` 设置 `LOCAL_CHROME_PATH`）
- 如果 `sau` 不可用，按 `references/runtime-requirements.md` 里的回退方式处理
- 不要传 `--headless`；登录、校验和发布都会打开可见的本机 Chrome 窗口
- `--headed` 仅为兼容旧脚本保留，传入不会改变行为
- 只有用户明确要求定时发布时，才使用 `--schedule`
- 非交互场景建议传 `--request-id` 和 `--result`，便于幂等判断和事后核对
- 如果登录流程生成了本地二维码图片，不要只把图片路径告诉用户
- 二维码图片本身就是给用户扫码的，优先直接把本地图片展示/发送给用户

## 参考文档

- 运行前提：`references/runtime-requirements.md`
- CLI 契约：`references/cli-contract.md`
- 故障排查：`references/troubleshooting.md`
