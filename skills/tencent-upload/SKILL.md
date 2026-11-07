---
name: tencent-upload
description: 使用 social-auto-upload 的 `sau tencent` 登录视频号并发布、定时发布或保存草稿。Use when 用户要向微信视频号发布视频或检查视频号账号状态。
license: MIT-0
slug: social-auto-upload-tencent-upload
displayName: 微信视频号发布
version: 1.0.0
summary: 登录视频号并发布、定时发布或保存视频草稿。
---

# 视频号发布

调用现有 `sau tencent` CLI，不另写网页上传流程。先确认 `sau tencent --help`；账号名是本地会话别名。

```powershell
sau tencent login --account creator
sau tencent check --account creator
sau tencent upload-video --account creator --file videos/demo.mp4 --title "标题" --desc "简介" --tags 视频号,测试
```

视频号仅支持视频发布。可选参数包括 `--schedule "YYYY-MM-DD HH:MM"`、`--draft`、`--collection`、`--short-title`、`--category`、`--thumbnail-landscape` 和 `--thumbnail-portrait`。`--thumbnail` 是竖版封面的兼容参数。合集必须已存在。

## 本机 Chrome 运行形态

- 登录、校验、上传全过程都在**本机 Google Chrome 的可见窗口**中完成；视频号命令默认有头，显式传 `--headless` 会被 argparse 直接拒绝。
- 每个 `--account` 使用独立的专用 Chrome profile，默认位于 `.local/chrome-profiles/tencent/`（可用 `SAU_STATE_DIR` 或 `conf.LOCAL_CHROME_PROFILE_ROOT` 改写）。
- 已有的 `cookies/tencent_<account>.json` 只在首次导入 profile 和之后导出备份时使用；登录后以专用 profile 中的登录态为准。
- 同一账号 profile 同一时间只能被一个进程使用；命令还会获取账号级本地会话锁。遇到占用错误时先关闭该账号残留的自动化 Chrome 或等待另一进程结束。
- 不要用 Playwright 自带 Chromium 替代本机 Chrome；也不需要为视频号执行 `patchright install chromium`。
- Chrome 不在常规位置时，在 `conf.py` 设置 `LOCAL_CHROME_PATH`。

## 登录与发布约定

- 如果专用 profile 已经登录，`login` 会直接复用，不重复生成二维码；否则需要用户在打开的本机 Chrome 窗口中扫码。
- 登录若生成二维码图片，直接展示图片供用户扫码，不要只回传路径。
- 定时仅在用户给出明确时间时使用。`--draft` 只保存草稿，不代表发布。
- 发布前确认目标账号、视频、标题、简介、标签及立即发布/定时/草稿意图。只依据 CLI 输出和平台页面确认结果，不把命令启动成功当成发布成功。
- 用户要求无头模式时，说明视频号已不支持无头，不能传 `--headless`。
- 完整参数以 `sau tencent upload-video --help` 为准。
