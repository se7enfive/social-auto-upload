# 发布节奏与账号冷却

本文说明可选的发布节奏限制：发布间隔、近 24 小时提交上限，以及遇到验证或风险记录后的冷却期。CLI 参数速查见 [CLI 使用说明](./CLI.md#发布安全与恢复抖音--小红书)。

## 目标与范围

第一期只覆盖通过 `sau` CLI 执行的抖音／小红书四条发布路径：

- `sau douyin upload-video`
- `sau douyin upload-note`
- `sau xiaohongshu upload-video`
- `sau xiaohongshu upload-note`

这些限制是**运营层面的确定性规则**：只读取本地发布 journal 和本地 JSON 配置，不访问平台、不读取 cookie。默认关闭，不配置时行为与旧命令完全一致。

## 明确不包含

本模块**不**模拟人类操作，也**不**提供反检测能力：

- 不插入随机延迟、随机请求间隔或随机暂停。
- 不随机化 User-Agent、viewport 或设备参数。
- 不模拟打字节奏、键盘抖动、鼠标贝塞尔曲线、鼠标抖动或拖拽人为化。
- 不模拟人为滚动，不提供账号“养号”或预热流程。
- 不伪造浏览器指纹，不绕过验证码，不规避平台风控。

这些能力的目标是让自动化行为难以被平台识别，属于反检测／规避封禁范畴，与本项目“遇到验证交人工处理、遵守平台规则”的边界冲突；随机化也不能证明账号不会触发风控。因此本模块只提供可审计、确定性的运营上限，不承诺“不封号”。

## 配置文件

默认路径为 `.local/publish_pacing.json`；使用 `SAU_STATE_DIR` 时位于 `$SAU_STATE_DIR/publish_pacing.json`。

```json
{
  "douyin": {
    "min_interval_seconds": 3600,
    "max_submits_per_24h": 3,
    "cooldown_after_risk_seconds": 86400
  },
  "xiaohongshu": {
    "min_interval_seconds": 7200,
    "max_submits_per_24h": 2,
    "cooldown_after_risk_seconds": 86400
  }
}
```

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `min_interval_seconds` | 同一账号两次正式提交尝试之间的最小间隔 | `0`–`2592000`（30 天），`0` 表示关闭 |
| `max_submits_per_24h` | 滚动 24 小时内允许的正式提交尝试次数 | `0`–`1000`，`0` 表示关闭 |
| `cooldown_after_risk_seconds` | 最近一次验证／风险记录后的冷却时长 | `0`–`2592000`（30 天），`0` 表示关闭 |

- 配置文件不存在：不做任何限制。
- 只写一个平台时，另一个平台保持不限制。
- 顶层只能是平台名对象；未知平台、未知字段、非整数、布尔值、负数或越界值都会使整份配置无效。
- 配置无效时**拒绝自动发表**（fail closed），避免拼写错误静默放宽限制；`--prepare-only` 不受影响。

## 判定规则

每个账号独立判定（按 `--account` 的本地名称），只统计本地 journal 中进入过最终点击边界的记录：

- 只统计 `submit_attempted=true` 的记录；`prepared`、`failed_before_submit` 等点击前结束的记录不占名额。
- 提交时间取 `submit_attempted_at`；旧记录没有该字段时回退到 `updated_at`。
- 24 小时窗口是滚动的：只有最早一次提交滑出窗口，才会空出一个名额。
- 风险记录包括当前或解析前状态为 `needs_human` 的记录，以及带有 `evidence.risk_signal` 的点击后未知记录；冷却从记录的 `updated_at` 起算（人工 `safety resolve` 后即从核对时间起算）。
- 有记录损坏或缺少可用时间时，无法可靠判断节奏，同样拒绝自动发表，并提示先人工检查发布记录目录。

所有限制随时间单调解除，实际可再次发布的时刻取各项限制中最晚的一个。被拦截时命令会在**启动浏览器之前**失败，不创建新的 run 记录，也不会点击最终发表。

`--prepare-only` 准备模式永远不受节奏限制，因为它不触发最终发表。

## 查看当前状态

`sau safety status` 的返回值包含 `pacing` 快照：

```json
{
  "config_path": ".../.local/publish_pacing.json",
  "enabled": true,
  "rules": {
    "min_interval_seconds": 3600,
    "max_submits_per_24h": 3,
    "cooldown_after_risk_seconds": 86400
  },
  "allowed_now": false,
  "next_allowed_at": "2026-11-07T16:24:56+08:00",
  "reasons": ["近 24 小时内已有 3 次正式提交尝试，达到上限 3，最早可在 2026-11-07 16:24:56 再次正式发布"]
}
```

配置无效时返回 `config_path` 和 `error`，不会抛出异常。该命令只读本地文件，不访问平台。

## 与上游同步

节奏逻辑全部封装在 `utils/publish_pacing.py`，接入点刻意保持为少量单行钩子，便于和上游 `dreammis/social-auto-upload` 同步：

| 文件 | 本项目的改动 | 合并上游时的处理 |
| --- | --- | --- |
| `utils/publish_pacing.py` | 新增模块，全部规则、配置解析和判定逻辑 | 上游通常不会冲突；冲突时优先保留本文件 |
| `tests/test_publish_pacing.py` | 新增测试 | 同上 |
| `sau_commands/douyin.py` | 一行 import；`upload_video` / `upload_note` 在 `attempt.ensure_can_start()` 后各加一行 `enforce_publish_pacing(attempt)` | 保留上游上传逻辑，只补回 import 和两处调用 |
| `sau_commands/xiaohongshu.py` | 同上，两处调用 | 同上 |
| `sau_commands/safety.py` | status 返回值增加 `pacing` 字段 | 保留上游改动，补回字段与 import |
| `utils/publish_safety.py` | `state_root()`、`submit_attempted_at` 字段、`risk_signal` 证据 | 属于发布安全层；上游若改动该文件，按下面说明逐项核对 |

建议流程：

```bash
git fetch origin
git merge origin/main
# 若命令模块冲突：保留上游的上传流程，只重新应用每个 upload 函数中的两行钩子
.venv/Scripts/python.exe -m unittest tests.test_publish_pacing tests.test_publish_safety
```

注意：此前的发布安全层（`d1c9095`）本身修改了 `uploader/douyin_uploader/main.py`、`uploader/xiaohongshu_uploader/main.py` 等上游文件，因此真正的合并成本主要来自安全层，而不是节奏模块。节奏模块只依赖 `publish_safety` 中的 `state_root()`、`submit_attempted_at` 和 `risk_signal`；上游若重写发布流程，需要确认这三点仍然存在。

## 已知边界

- 只覆盖通过 `sau` CLI 的四条路径（抖音视频的浏览器引擎与 HTTP 引擎共用同一入口）。Web 控制台路径 `sau_backend.py` → `myUtils/postVideo.py` 直接构造上传器，不经过 `sau_commands` 包装，因此既不受发布安全层保护，也不受节奏限制；详见 [发布安全与可靠性机制](./publish-safety.md#已知边界与未覆盖路径)。
- 按本地 `--account` 名称隔离，不做远端账号身份去重；不同别名指向同一远端账号时无法共享限额。
- 只依据本地记录判断，不核验平台侧实际发布数量；手工在平台发布的内容不计入。
- 配置文件是本地可信输入；有写权限的人可以修改。本模块不防篡改，也不用于强制执行平台政策。
- 不保证账号安全，不替代平台自身的频率限制和人工判断。

## 测试

`tests/test_publish_pacing.py` 覆盖配置缺失／按平台加载／非法配置 fail closed、间隔与滚动窗口边界、准备和点击前失败不计数、风险冷却（含人工解析后）、损坏记录 fail closed、journal 集成、prepare-only 豁免、两个平台在启动浏览器前被阻断、`sau safety status` 的 pacing 快照。测试只使用临时状态目录和 mock，不访问真实账号。
