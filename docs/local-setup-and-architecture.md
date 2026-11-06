# 本机安装与架构导览

记录日期：2026-11-06。对应仓库快照：`3847fff`。

## 一、本机安装

项目目录：`C:\Users\A\Desktop\project\post-to-all`。

| 项目 | 本次配置 |
| --- | --- |
| Python | 项目 `.venv` 使用 3.12.12，不改系统默认 Python 3.14 |
| 环境工具 | 已有 `uv 0.9.22` |
| 项目安装方式 | `uv pip install -e .`，源码修改直接对本地安装生效 |
| 项目版本 | `social-auto-upload 0.1.0` |
| 浏览器驱动 | `patchright 1.58.2` |
| 本地配置 | 根目录 `conf.py`，基于 `conf.example.py`；`LOCAL_CHROME_PATH` 指向本机 Chrome |
| 主线配套浏览器 | Chromium 145.0.7632.6（revision 1208），`channel="chromium"` 的无头与有头本地交互均通过 |
| 本机 Chrome | 156.0.8078.81，保留给实际支持 `LOCAL_CHROME_PATH` 的平台流程，不能代替所有配套 Chromium 调用 |
| Node.js | 已有 22.21.1；不是基础视频／图文 CLI 的必需依赖 |

`conf.py` 和 `.venv` 均已被仓库现有 `.gitignore` 排除；本次未更改全局 Python 或权限配置。

### 重建环境

以下命令在仓库根目录的 PowerShell 执行。已存在的 `.venv` 不要无故重建，已有的 `conf.py` 也不要覆盖。

```powershell
# 仅在尚未创建虚拟环境时执行。
uv venv --python 3.12
uv pip install -e .

# 主线抖音／小红书的 channel="chromium" 需要配套浏览器。
# --no-shell 对应本次已验证的新无头／有头启动方式。
.\.venv\Scripts\patchright.exe install chromium --no-shell

# 不需要激活虚拟环境，直接调用项目自己的入口。
.\.venv\Scripts\sau.exe --help
```

如果需要创建本地配置，请参照 `conf.example.py` 新建 `conf.py`。本次设置为：

```python
LOCAL_CHROME_PATH = "C:/Program Files/Google/Chrome/Application/chrome.exe"
```

**安装过程与结论更正：** 首次官方 Chromium 下载很慢；默认 npmmirror 的 Playwright 路径返回 404。曾先验证系统 Chrome，但底层核对发现，抖音和小红书主要登录／上传路径硬编码 `channel="chromium"`，不会完整采用 `LOCAL_CHROME_PATH`，所以仅凭系统 Chrome 不能宣布这些路径已就绪。现已从 Chrome for Testing 的可用镜像补齐 **145.0.7632.6 / revision 1208**：归档大小与 Google 官方 HTTPS 元数据一致（181196793 字节），MD5 为 `d2889c5c2fadbd9d2271d512a063cac9`，ZIP CRC 校验通过；再由标准官方安装器完成检查。

已使用与主线相同的 `channel="chromium"`、不指定 `executable_path`，分别跑通无头与有头的本地页面点击验证。配套浏览器位于 `C:/Users/A/AppData/Local/ms-playwright/chromium-1208/chrome-win64/chrome.exe`。参见 `uploader/douyin_uploader/main.py` 中 `DouYinVideo.upload`／`DouYinNote.upload` 和 `uploader/xiaohongshu_uploader/main.py` 中 `XiaoHongShuVideo.upload`／`XiaoHongShuNote.upload` 的启动调用。本次没有额外安装 `chromium-headless-shell`；如果外部脚本使用不指定 channel 的默认无头启动方式，可按需完整执行 `patchright install chromium`。

当前未配置 YouTube 代理，也未保存任何平台凭证。`LOCAL_CHROME_PATH` 只影响实际读取它的流程，不是统一的浏览器覆盖开关；非空配置与配套 Chromium 可以并存。

### 验证范围

已通过：

- `uv pip check`：19 个已安装包的依赖关系一致。
- Python、OpenCV、NumPy 和 patchright 的加载检查。
- patchright 驱动本机 Chrome 156.0.8078.81，在独立临时浏览器会话中加载本地页面、点击按钮并读取 DOM；未访问发布平台，未读取既有 Chrome 登录状态。
- 配套 Chromium 145.0.7632.6（revision 1208）归档已校验并安装；按主线 `channel="chromium"` 且不指定 `executable_path`，无头和有头两种模式的本地页面加载、点击及 DOM 读取均通过。
- `sau --help`，以及 `douyin`、`kuaishou`、`xiaohongshu`、`bilibili`、`article` 的帮助入口。
- 实际执行 `sau article render`：将临时 HTML 原稿转换为本地 HTML，确认中文正文和加粗保留；不需要 Node.js 渲染依赖、不登录、不发布。
- 10 个浏览器平台 CLI 参数解析测试。
- 5 个模拟依赖的 Bilibili CLI 测试。

以上不是完整测试套件，也不代表任意平台当前都能成功发布。本次不执行平台登录、会话检查、真实上传或发表。

可复跑的无真实上传测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_sau_browser_cli.py -k BrowserCliParserTests -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_sau_bilibili_cli.py -v
```

预期分别通过 10 个和 5 个测试。负向参数用例可能输出 `usage` 和 `error`；以最后的 `OK` 和进程退出码判断测试结果，不要把预期的参数拒绝误判为安装失败。

### 尚未按需初始化的部分

- **Bilibili 的 `biliup`**：由真实业务命令按需准备，本次没有为验证帮助入口而下载它。
- **Markdown 文章渲染**：Node.js 版本已满足要求，但 npm 渲染依赖未初始化。需要时先执行 `.\.venv\Scripts\sau.exe article setup`。
- **旧 Web 服务**：没有安装 Flask 可选依赖，也没有安装或启动历史前端；参见 `docs/legacy-web.md`。

### 下一步：由用户选择平台并登录

例如抖音，先执行登录与会话检查：

```powershell
.\.venv\Scripts\sau.exe douyin login --account creator
.\.venv\Scripts\sau.exe douyin check --account creator
```

`creator` 是自定义的本地账号别名。登录后生成的会话文件相当于登录凭证，不要提交、分享或写入知识库。发布命令会对外发表内容，执行前应再次确认素材、账号和发布时间。

## 二、先把项目分成三层

当前主线入口是 CLI，而不是 Web 网站。先按职责理解这些目录：

| 位置 | 职责 |
| --- | --- |
| `pyproject.toml` | 声明 Python 版本、依赖和 `sau` 可执行入口；其中 `sau = "sau_cli:main"` |
| `sau_cli.py` | CLI 主入口 |
| `sau_commands/` | 按平台组织命令、参数和业务调度 |
| `uploader/` | 各平台真正的登录、会话校验和发布实现 |
| `utils/`、`myUtils/` | 浏览器、文章及其他共享工具 |
| `skills/` | 给 Agent 的操作说明、约束与参考资料；不是另一套发布服务 |
| `sau_backend.py`、`sau_backend/`、`sau_frontend/` | 历史 Web 实现，不是这次安装的主入口 |
| `examples/` | 既有当前 CLI 包装示例，也有历史直连 uploader 示例；不能把所有示例都当成当前推荐路径 |

概括来说，CLI 负责把用户输入变成平台调用，uploader 负责真正操作平台，Skill 负责教 Agent 怎样使用它们。主线定位见 `README.md:9`、`README.md:13` 和 `docs/legacy-web.md:20`。

### 总体调用关系

下图按模块职责展开；浏览器分支代表抖音、小红书等网页自动化平台，而不是所有平台共用同一种上传实现。

```text
人，或阅读了 skills/ 操作说明的 Agent
  │
  ▼
sau <platform> <action> [参数]
  │  pyproject.toml 注册 sau_cli:main
  ▼
sau_cli.py
  │
  ▼
sau_commands/：命令与参数、平台业务调度
  │
  ├─ 浏览器平台 ──► uploader/ 下对应实现
  │                  └─► patchright + Chromium
  │                        └─► 平台创作者后台
  │
  ├─ Bilibili ────► uploader/bilibili_uploader/
  │                  └─► biliup 运行时
  │
  └─ 本地 article ─► utils/article_render.py
                     ├─ HTML/TXT：本地处理
                     └─ Markdown 主题：Node.js 渲染
                           └─► 本地 HTML 输出，不发表
```

历史 Web 代码是另一组保留入口，不要把它当作上述 CLI 正在依赖的必经服务。

### CLI 的真实分发机制

```text
pyproject.toml:21 注册 sau_cli:main
  → main() 解析参数
  → asyncio.run(dispatch(args))
  → PLATFORMS[args.platform].run(args)
  → 对应平台的请求对象与业务函数
  → uploader 中的真实实现
```

`build_parser()` 遍历平台注册表，调用各平台模块的 `register(platform_parsers)`；平台模块自己的 `run(args)` 再区分 login/check/upload-video 等动作。注册、参数和业务分发不是在 Web 后端中完成的。

关键位置：

| 源码位置 | 关注点 |
| --- | --- |
| `sau_cli.py:16` | `PLATFORMS` 平台注册表 |
| `sau_cli.py:37` | 构建 argparse 参数与注册平台 |
| `sau_cli.py:46` | 异步 dispatch，调用平台模块的 `run` |
| `sau_cli.py:53` | 顶层入口与异常处理；异常写 stderr，返回退出码 1 |
| `sau_commands/arguments.py:16` | 账号路径、文件、标签、时间及运行参数等共用处理 |
| `uploader/base_video.py:7` | `BaseVideoUploader`，主要负责文件和定时时间校验 |

`uv` 到安装和环境管理这一层就结束了，不参与上传调度或浏览器操作。未激活环境时直接执行 `.venv/Scripts/sau.exe` 即可。

### 抖音：登录、校验与发布

命令层为 `sau_commands/douyin.py`，实现层为 `uploader/douyin_uploader/main.py`。

**登录与校验：**

```text
sau douyin login --account <别名>
  → login_douyin_account()
  → resolve_account_file("douyin", 别名)
  → douyin_setup(handle=True, return_detail=True)
  → 先校验已有会话；没有有效会话则进入二维码登录
  → context.storage_state(path=账号文件)

sau douyin check --account <别名>
  → check_douyin_account()
  → cookie_auth()
  → 浏览器恢复 storage_state 并访问上传页
  → 根据 URL 与登录元素判断 valid / invalid
```

登录入口见 `sau_commands/douyin.py:103`，检查见同文件 `:108`；底层 `cookie_auth`、`douyin_setup`、二维码登录及会话保存分别见 `uploader/douyin_uploader/main.py:112`、`:137`、`:265`、`:301`。

**视频发布：**

```text
sau douyin upload-video
  → run() 构造 DouyinVideoUploadRequest
  → upload_video() → douyin_setup(handle=False)
  → DouYinVideo.douyin_upload_video() → upload()
  → 恢复账号会话、上传文件、填写表单
  → 处理声明／合集／封面／定时等平台字段
  → 点击发布，等待跳转到 content/manage
  → 刷新 storage_state，命令层输出 upload submitted
```

命令层见 `sau_commands/douyin.py:115`、`:188`；实现见 `uploader/douyin_uploader/main.py:971`、`:1099`、`:1112`、`:1118`。

**图文发布：**

```text
sau douyin upload-note
  → DouyinNoteUploadRequest → upload_note()
  → DouYinNote.douyin_upload_note() → upload()
  → upload_note_content()
  → 发布图文表单：图片、标题、正文、话题、BGM、定时
  → 发布并等待作品管理页，刷新会话
```

命令层见 `sau_commands/douyin.py:148`；实现见 `uploader/douyin_uploader/main.py:1179`、`:1230`、`:1259`。

### 小红书：当前 CLI 使用浏览器实现

当前 CLI 对接 `uploader/xiaohongshu_uploader/main.py`，命令层为 `sau_commands/xiaohongshu.py`。

```text
login
  → login_xiaohongshu_account() → xiaohongshu_setup(handle=True)
  → 二维码登录 → 保存 storage_state → cookie_auth 再核验

check
  → check_xiaohongshu_account() → cookie_auth()
  → 恢复会话，访问创作者视频页，判断登录路径和登录框

upload-video
  → upload_xiaohongshu_video() → XiaoHongShuVideo.main()
  → xiaohongshu_upload_video() → upload() → upload_video_content()
  → 上传、填写、发布 → 等待 /publish/success → 刷新会话

upload-note
  → upload_xiaohongshu_note() → XiaoHongShuNote.main()
  → xiaohongshu_upload_note() → upload() → upload_note_content()
  → target=image 表单 → 上传、填写、发布 → 等待成功页
```

命令层入口见 `sau_commands/xiaohongshu.py:85`、`:90`、`:97`、`:121`；底层登录与校验见 `uploader/xiaohongshu_uploader/main.py:177`、`:219`、`:242`、`:273`；视频流程见 `:623`、`:701`、`:721`、`:725`；图文流程见 `:771`、`:824`、`:844`、`:848`。

### 统一到什么程度，没有统一什么

这个项目适合概括为 **“统一 CLI 外观 + 逐平台适配器”**，不能当作已经具备统一状态机和全平台事务的发布引擎。

- `BaseVideoUploader` 共用文件、图片扩展名和时间校验，并没有统一登录协议、浏览器生命周期或发布结果类型；文章和 Bilibili 等流程也不都依赖该基类。
- 浏览器的 launch/context 等控制仍分散在平台实现中；同一个 CLI 并不意味着相同的页面步骤和成功判断。
- 抖音、小红书等旧路径上传后通常返回账号文件路径，而不是统一的作品 ID；成功主要根据管理页／成功页跳转判断。
- `upload submitted` 不能解读为“平台审核通过、作品最终公开可见”。部分发布等待循环也没有统一的全局期限，外层做批量调度时需要独立管理超时和状态。
- 大鱼号、雪球、知乎等较新的实现提供了更细的身份绑定、结果持久化和 verify，但不能据此假设所有平台都具备相同能力。

### 安装验证的边界

`--help` 在 argparse 解析阶段退出，不会调用平台 `run()`，所以不会启动浏览器、联网验证账号或下载 biliup。其导入过程仍可能创建本地 `cookies/` 子目录和 `logs/`，不应把“无网络／无发布”写成“完全不写磁盘”。参见 `uploader/__init__.py:5` 与 `utils/log.py:42`。

反过来，`check` 并不是纯本地安装检查：浏览器平台会实际访问平台，Bilibili 的 `check` 会准备 biliup 并执行 renew。较新平台的 `--prepare-only` 也可能恢复会话、上传素材和留下草稿，不能当作无副作用测试。

本机另做了系统 Chrome 和配套 Chromium 的独立本地点击验证，其中配套 Chromium 使用与主线一致的 `channel="chromium"`，有头和无头均通过。因此，结论不只是来自 `--help`；但仍不代表账号有效、网站可达、选择器兼容或真实发布成功。

### 账号隔离、提交状态与定时发布

主线 CLI 的会话文件规则为 `BASE_DIR/cookies/{platform}_{account}.json`，例如 `cookies/douyin_creator.json`；见 `sau_commands/arguments.py:16`。这和部分包导入时自动创建的历史子目录不是一回事。Bilibili 使用同一命名规则，但文件内容由 biliup 管理，不是所有平台都保存相同格式。

`--account` 是本地别名，不是远端用户名。两个别名可以绑定同一个远端账号；仅靠不同文件名不能保证真实账号互不相同。抖音、小红书旧路径没有额外的主体绑定文件；大鱼号、雪球、知乎会另存 `.identity.json` 并比对主体，见 `uploader/dayu_uploader/session.py:32`、`uploader/xueqiu_uploader/session.py:29`、`uploader/zhihu_uploader/session.py:35`。

较新实现已提供可借鉴的提交状态：知乎文章记录 `prepared/published/failed_before_submit/submission_unknown`（`uploader/zhihu_uploader/article.py:175`），大鱼号保存 `content_id` 与 `scheduled/submitted`（`uploader/dayu_uploader/publication.py:82`）。若结果未知，应先核对已提交作品，不能直接把 unknown 当作未发表并再次发布。

平台定时不是本机常驻调度器：程序提前上传并把排期提交给平台，由平台到点发表。对应抖音 `set_schedule_time_douyin`（`uploader/douyin_uploader/main.py:363`）、小红书 `set_schedule_time_xiaohongshu`（`uploader/xiaohongshu_uploader/main.py:343`）、Bilibili 的 `--dtime`，以及各平台自己的时间校验；不能将一种平台的限制套到全部平台。

### Bilibili：独立的 biliup 运行时

Bilibili 的 CLI 并不使用浏览器上传链路，而是由 `sau_commands/bilibili.py` 调用 `uploader/bilibili_uploader/runtime.py` 中的 `run_biliup_command()`，再启动 biliup 子进程。

```text
sau bilibili login / check / upload-video
  → 命令层构造 biliup 参数
  → run_biliup_command()
  → ensure_biliup_binary(force_check=False)
  → 本地有二进制：直接复用
  → 本地没有：查询 GitHub Release，按系统与架构下载
  → 执行 biliup login / renew / upload 等对应操作
```

工具缓存位于 `~/.social-auto-upload/tools/biliup/<OS-arch>/`，不是项目 `.venv` 中的 Python 包。命令层调用点见 `sau_commands/bilibili.py:63`、`:76`、`:106`。

**注意文档与实现的差异：** `docs/install.md:163` 关于后续自动检查更新的表述比较宽泛；实际常规调用传入 `force_check=False`（`uploader/bilibili_uploader/runtime.py:178`），已有二进制时直接返回（同文件 `:157`），不是每次启动都联网检查更新。本次只验证帮助入口和模拟测试，没有触发下载。

### 文章、Skill 与旧 Web

- 本地 `sau article render` 和平台 `upload-article` 是两件事：前者产出 HTML，后者才有真实发表动作。
- Markdown 主题渲染按需使用 Node.js 22+ 和 `utils/article_renderer/` 的渲染组件；`sau article setup` 显式执行 npm 依赖安装。HTML/TXT 路径不需要该 Node 运行时，参见 `utils/article_render.py:36`。
- Skill 是供 Agent 使用的操作规范和参数参考，不会因为克隆仓库就自动成为本会话的已安装命令，也不是独立的发布引擎。
- 历史 Web 的 `/postVideoBatch` 是旧的循环分发入口，不能据此认定当前 CLI 已有全平台统一编排。旧 Web 与当前 uploader 的参数和能力可能不同步，定位以 `docs/legacy-web.md:9`、`:20` 为准。

### 阅读与二次开发的切入点

1. 先看 `pyproject.toml` 的入口和依赖，再看 `sau_cli.py`，避免从历史 Web 的启动脚本入手。
2. 选择一个目标平台，成对阅读 `sau_commands/` 的对应模块和 `uploader/` 的对应目录，而不是一次性读完所有平台。
3. 修改参数、账号选择或 CLI 输出时，先看命令层；修改网页定位、上传、表单填写或发表成功判断时，重点看 uploader 层。
4. 文章流程要区分“本地转换”和“对外发表”：`sau article render` 只输出本地文件，`sau dayu upload-article` 等平台命令才会实际发表。
5. 新增平台时，除了实现，也要补 CLI 能力表、平台 Skill 和分发资料；仓库在 `README.md:342` 明确要求它们随同一 PR 更新。
6. 当前 CLI 是 `sau <platform> <action>` 的逐平台入口。如果目标是自己的“一键多平台分发”产品，建议在这些能力外增加任务编排、发布确认和结果汇总，而不是默认不同平台的内容限制、账号状态和发布结果完全相同。

### 平台数量更正

先前对话中“19 个平台”的表述不准确。当前 `README.md:79` 的表格列出 **17 个平台入口**（Facebook 与 Facebook Page 分开计算），其中 TikTok 没有 `sau` CLI；`skills/` 有 **18 个产品 Skill**，包括跨平台总入口。不要把 Skill 数量、CLI 命令数量和平台数量混为一谈。
