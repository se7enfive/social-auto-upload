# 更新日志

项目使用 GitHub Release 发布可复用版本；日常合并先记录在这里，发布时把对应版本的内容复制到 Release notes。

## [Unreleased]

### 结构与 CLI

- 将 `sau_cli.py` 从单体入口拆为 `sau_commands/` 下的按平台命令模块，入口只负责注册和分发。
- README、CLI 文档和 Skill 按平台及视频、文章、图文三类整理发布入口。
- 移除知乎没有实际用途的 `--expected-name` 参数；登录时直接读取当前页面身份并保存账号绑定信息。
- 知乎文章正式 URL 校验排除 `/edit` 草稿地址，避免把自动保存草稿误报为已发布。

### 知乎文章

- 支持 HTML、Markdown、TXT 原稿，正文图片按原稿中的位置上传，不再统一追加到文末。
- 支持知乎真实编辑器中的 h2、加粗、编号列表、引用和外链校验。
- PNG 图片使用剪贴板粘贴路径，发布前核对文字、图片顺序、格式和链接。

### 知乎视频

- CLI 提供完整领域和视频声明可选值，支持实时菜单核对、编号选择和 `--show-options` 查询。
- 上传前拒绝小于或等于 100KB 的视频。
- 修正发布成功判断，读取成功弹层打开的 `/pin/<作品ID>` 详情链接；链接读取失败时保留已发表状态。
- 作品查询兼容新版视频链接；定时发布标为实验性功能。

### 发布安全（抖音 / 小红书）

- 新增共享保护层 `utils/publish_safety.py`：账号级跨进程会话锁、原子 JSON journal、有界 deadline、内容指纹和单次提交控制。
- 抖音和小红书的视频／图文上传新增 `--prepare-only`、`--result`、`--request-id`、`--timeout`；旧命令默认仍会尝试发表。
- 最终发表点击前先持久化提交意图，点击最多一次；点击后结果不明保持 `submission_unknown`，不会自动重发。
- 存在未解决的 `submission_unknown` / `needs_human` 时阻止该账号继续自动发表；新增本地命令 `sau safety status` 和 `sau safety resolve`，不读取 cookie、不访问平台。
- 抖音短信验证码改为账号作用域文件优先、最多自动请求一次、日志不打印明文；等待超过 10 分钟转人工处理。
- 四条路径统一有界清理浏览器资源；context 关闭失败仍会关闭 browser。
- 抖音／小红书 Skill 改为两阶段确认：先 `--prepare-only` 展示内容与副作用，等待用户新的明确确认后再正式提交；prepare／submit 使用可关联但不同的 request-id。
- 新增 `tests/test_publish_safety.py`（43 项）及 CLI 包装层测试；验证仅使用 mock 页面和临时状态目录，真实平台发布仍待单独授权验收。
- 机制说明、恢复流程和许可边界见 [docs/publish-safety.md](./docs/publish-safety.md)。

## 发布流程

1. 在 `Unreleased` 中记录用户可感知的变更和验证结果。
2. 更新 `pyproject.toml` 的 `project.version`，遵循 `MAJOR.MINOR.PATCH`。
3. 运行专项测试、`git diff --check` 和构建检查。
4. 创建版本提交和 `v<version>` 标签，推送到 GitHub。
5. 在 GitHub 创建同名 Release，把本版本日志粘贴到 Release notes，并保留验证范围和已知限制。

版本发布前，未完成真实平台验收的能力必须明确写成“待验收”，不能写成已支持。
