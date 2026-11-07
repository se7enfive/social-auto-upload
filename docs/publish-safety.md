# 发布安全与可靠性机制

本文说明抖音／小红书发布路径中的本地保护机制、状态语义、恢复流程和设计边界。CLI 参数速查见 [CLI 使用说明](./CLI.md#发布安全与恢复抖音--小红书)。

## 目标与范围

第一期只覆盖抖音和小红书的四条路径：

- 抖音视频 `sau douyin upload-video`
- 抖音图文 `sau douyin upload-note`
- 小红书视频 `sau xiaohongshu upload-video`
- 小红书图文 `sau xiaohongshu upload-note`

目标是降低四类风险：

1. 上传或表单未完成就点击最终发表。
2. 点击后结果不明时自动重发，造成重复作品。
3. 等待页面跳转或验证码时无限挂起。
4. 同一账号被多个本地进程同时操作。

不包含验证码绕过、浏览器指纹伪造、封禁规避或“保证不封号”的承诺。

## 机制来源与许可边界

方案参考了两个公开项目的**设计思路**，没有复制其源码：

- `se7enfive/recruiting-copilot`（MIT）：风险 URL 锁存、人工解除、CDP 超时和占用检查。本项目独立实现了本地会话锁、风险停手和人工恢复命令。
- `DaBaoAgent/PostFlow`（仓库快照未发现清晰 LICENSE）：视频填表后不点发表的预演、阶段 deadline。本项目只吸收“先准备、后提交”和“分阶段预算”的概念；其图文上下文作用域和发布轮询重复点击的问题没有被照搬。

现有仓库中腾讯、大鱼号、知乎的“单次提交／prepared／unknown”模式也被复用为状态语义参考。实现集中在新增的共享保护层，不把平台专属逻辑抽成通用任务引擎。

## 核心机制

### 1. 本地会话锁

- 锁键由账号文件的规范化绝对路径派生，状态放在 `.local/locks/`。
- Windows 使用 `msvcrt.locking(LK_NBLCK)`，POSIX 使用 `fcntl.flock(LOCK_EX | LOCK_NB)`。
- 非阻塞获取：冲突立即报错并提示持有者 PID，不轮询抢占。
- 进程退出时由操作系统释放，不依赖删除 PID 文件。
- 同一进程内拒绝重入；不同账号文件可并行。

限制：不同 `--account` 别名可能绑定同一个远端账号，本机制只保证同一本地会话文件不并发，不做远端身份去重；仓库内历史直连脚本也不在这把锁的保护范围内。

抖音、小红书、视频号还使用本机 Chrome 的专用 profile；Chrome 自身的 profile 锁会阻止同一账号目录被两个浏览器实例打开。它与 CLI 的 `session_lock` 互补：前者防止浏览器层面的并发，后者防止命令层重复进入发布流程。

### 2. 发布 journal

每次发布在以下位置创建独立记录：

```text
.local/publications/<platform>/<account>/runs/<run-id>.json
```

记录包含 run-id、可选 request-id、内容指纹、状态、提交尝试标记、摘要和证据位置。**不保存** cookie、验证码、storage_state 或可复用认证 URL。

写入使用同目录临时文件 + `flush` + `fsync` + `os.replace`，避免读到半截 JSON。提交意图必须在点击前成功落盘；写入失败时不会点击。

### 3. 内容指纹与 request-id

内容指纹由标题、正文、标签、排期、媒体文件路径／大小／修改时间等字段生成，不哈希大文件本身。

- 相同 `--request-id` + 相同指纹 + 已提交：跳过，退出码 0。
- 相同 `--request-id` + 不同指纹：拒绝执行。
- 没有 `--request-id`：保留旧的“每次调用都尝试发布”语义，不做无依据的内容去重。

### 4. 有界执行

- `--timeout` 是总预算，默认 1800 秒，必须是有限正数。
- 上传、表单填写和等待循环检查剩余预算，超时抛出 `PublishDeadlineExceeded`。
- 最终提交后的结果观察最多 120 秒，且不超过剩余预算。
- 浏览器清理使用独立的短暂宽限，不占用提交观察预算。

### 5. 准备模式

`--prepare-only` 完成素材上传、表单填写和排期设置后，把状态记为 `prepared` 并停止，不点击最终发表。

注意：这不是离线操作。素材已经上传到平台，平台可能自动保存草稿；需要用户自行清理。

Agent 工作流中，准备与正式提交应使用不同但可关联的 `--request-id`（例如 `<业务ID>-prepare` / `<业务ID>-submit`），正式提交的 ID 在重试间保持稳定。用户在最初请求中的“帮我发布”不能替代准备结果展示后的再次确认；必须在用户核对账号、标题、正文、素材和排期后，再执行正式提交。

### 6. 单次提交

最终发表按钮移出重试循环，`submit_once()` 保证：

1. 先持久化提交意图（状态 `submission_unknown`，`submit_attempted=true`）。
2. 持久化成功后才点击，且只点击一次。
3. 点击异常、任务取消、观察超时或风险信号都保持“可能已提交”的事实。
4. 后续只观察结果，不为等待跳转重复点击。
5. 只有页面出现既有成功证据时才记 `submitted`。

不把管理页跳转等同于平台审核通过，不捏造作品 ID。

### 7. 风险停手

只识别已知的独立验证页、挑战弹层或明确账号限制信号，不全文匹配正文中的“验证”字样。

- 点击前发现明确风险：记 `needs_human`，不点击。
- 点击后发现风险：保持 `submission_unknown`，并记录风险原因。
- 普通网络错误、登录过期和 DOM 变化不会被一概永久冻结账号。

### 8. 短信验证码

- 优先读取账号作用域文件 `.local/verify_codes/<platform>-<hash>.txt`；等待日志会打印本次路径。
- 兼容项目根目录 `verify_code.txt` 作为串行 fallback。
- 每次发布最多自动点击一次「获取验证码」。
- 验证码不以明文写入日志；验证通过后删除对应文件。
- 等待超过 10 分钟或总预算耗尽时记 `needs_human`，不点击最终发表。

### 9. 资源清理

`close_browser_resources()` 分别有界关闭 context 和 browser。抖音、小红书、视频号使用持久化本机 Chrome context，没有独立 browser 对象，因此传入 `browser=None` 只关闭 context。context 关闭失败仍会尝试关闭 browser；清理错误只记录日志，不覆盖原始失败或已确认的提交状态。

### 10. 发布节奏（可选）

独立的 `utils/publish_pacing.py` 提供可选的发布间隔、滚动 24 小时提交上限和风险后冷却，默认关闭。它复用本 journal 的 `submit_attempted_at`，判定发生在 `ensure_can_start()` 之后、创建新记录和启动浏览器之前；`--prepare-only` 不受限制。该模块只做确定性运营限制，不做拟人化、随机化或反检测。配置、规则和上游同步方式见 [发布节奏与账号冷却](./publish-pacing.md)。

## 状态机

```text
preparing ──> prepared                 （--prepare-only）
    │
    ├──> failed_before_submit          （点击前失败，可重试）
    ├──> needs_human                   （点击前验证/风险）
    └──> submission_unknown            （提交意图已落盘）
             ├──> submitted            （页面成功证据）
             └──> submission_unknown   （点击异常/取消/超时/风险）
```

`submission_unknown` 和 `needs_human` 是阻塞状态。存在未解决记录时，该账号后续自动发布会失败；换 `--request-id` 不能绕过。

## 恢复流程

1. 运行 `sau safety status --platform <platform> --account <account>` 查看未解决记录和 run-id。该命令只读本地 journal，不读取 cookie、不访问平台。
2. 到平台创作中心人工核对原作品是否已经发布。
3. 根据核对结果运行：

```bash
sau safety resolve \
  --platform <platform> \
  --account <account> \
  --run-id <run-id> \
  --outcome submitted|not-submitted \
  --reason "<人工核对依据>"
```

4. `submitted` 表示确认已发布，后续同 request-id 不再重发；`not-submitted` 表示确认未发布，允许重新尝试。
5. `safety resolve` 不会登录、刷新、重传或发表，也不会自动修改平台作品。

## 失败时的状态判定

| 场景 | 状态 |
| --- | --- |
| cookie 缺失/失效、参数校验失败、上传失败 | `failed_before_submit` |
| 点击前检测到验证/风险 | `needs_human` |
| 提交意图写入失败 | 不点击；按点击前失败处理 |
| 点击抛出异常、任务取消、观察超时 | `submission_unknown` |
| 页面出现成功跳转 | `submitted` |
| 写回 cookie、导出结果、截图或清理失败 | 不改变已有提交状态 |

截图只在 `--debug` 且结果未知时尽力保存到 journal 目录的 `screenshots/` 下；截图失败被忽略。

## 已知边界与未覆盖路径

- 本保护层和发布节奏只覆盖 `sau` CLI 的四条路径。Web 控制台路径 `sau_backend.py`（Flask）→ `myUtils/postVideo.py`（`post_video_DouYin` / `post_video_xhs`）直接构造 `DouYinVideo` / `XiaoHongShuVideo`，绕过 `sau_commands` 包装，因此会话锁、journal、单次提交、风险停手和节奏限制都不生效。如需覆盖，应把 Web 路径迁移到 `sau_commands` 的上传函数，而不是复制保护逻辑。
- 仓库内其他历史直连脚本（例如直接实例化上传器的示例）也不在这把锁的保护范围内。
- 锁按本地账号文件路径隔离，不识别指向同一远端账号的不同别名；不做远端身份去重。
- 抖音、小红书、视频号的专用 Chrome profile 由 `utils/local_chrome.py` 管理，journal 不保存 profile 内容或其中的 cookie。删除 profile 不是本保护层的恢复动作；需要重新登录时使用各平台 `login`。
- 状态判定依赖平台页面已有的可证明信号，平台改版可能导致状态退化为 `submission_unknown`；此时按人工核对流程处理。

## 测试与验证

新增 `tests/test_publish_safety.py` 覆盖：

- 原子写入成功/失败、deadline、锁重入、跨进程冲突与进程退出释放。
- 状态迁移、阻塞、request-id 幂等与冲突、损坏记录。
- `submit_once` 成功、点击失败、等待失败、落盘失败阻止点击、取消。
- 四条上传路径的准备模式零点击、正常发布恰好一次点击。
- 提交超时后截图失败仍保持 `submission_unknown`。
- `sau safety status/resolve` 通过 CLI 分发执行。
- CLI 包装层：阻塞账号不启动浏览器、失败时导出结果、同 request-id 已提交时退出码 0。

`tests/test_publish_pacing.py` 另行覆盖发布节奏的配置校验、窗口边界、prepare-only 豁免、启动浏览器前阻断和 `sau safety status` 快照。

验证只使用 mock 页面和本地临时状态目录，不使用真实账号登录、上传或发表。真实平台验收仍需单独授权和人工核对。
