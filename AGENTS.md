# AGENTS.md

## 项目简介

全功能硬件调试工具集（串口/ADB/HID/DAP-RTT/Modbus/SSH），内嵌 MCP 服务（仅 127.0.0.1 + Bearer）暴露模块能力给 AI 客户端。  
技术栈：Python 3.10+ / PyQt6 / qfluentwidgets==1.11.2 / pyserial / pyte / pymodbus / paramiko / FastMCP。

## 架构约定（核心，别打破）

**每个功能模块 = worker 线程（唯一持有原生句柄）+ UI 页面 + MCP 桥接。**

- 硬件句柄（串口/HID/DAP/Modbus/SSH）必须在 worker 线程内构造和销毁，禁止跨线程传递。
- UI 层只通过 worker 的 `sig*` 信号/槽通信，禁止直接触碰句柄。
- **例外：ADB 不走 worker**。`adb_runner.py` 与 `mcp_bridge.adb_*` 均直接 `subprocess` 调 `adb.exe`（`app/libs/adb/`），无任何持有句柄的线程。
- 原生 DLL（`hidapi.dll`）经 `app/native.py` 统一加载，位于 `app/libs/`（与 adb.exe 一起被 git 跟踪，勿删）。

## MCP 新增/改动工具的完整链路

1. worker 线程实现能力（`app/*_worker.py`）；
2. `app/mcp_bridge.py` 的 `WorkerBridge` 加同步方法——用「emit 既有 `sig*` 信号 + `threading.Event` 等待」或「`sigMcpQuery`→`mcpReply`」返回结果；
3. `app/mcp_server.py` 的 `build_mcp()` 里加 `@tool()`（工具层定义全部在这里，不在 bridge）。

三处改一处漏，AI 客户端就拿不到能力。bridge 方法抛 `BridgeError`，工具层统一 `@_guard` 转换成错误文本。

## 安全约定（别退化）

- **MCP 高危能力按开关门控**：`build_mcp(bridge, allow_exec, allow_file)` 中，命令执行类
  （`ssh_exec` / `adb_shell` / `phoenix_burn`）包在 `if allow_exec:`，文件读写类
  （`adb_push` / `adb_pull` / `adb_list_dir` / `ssh_file_list`）包在 `if allow_file:`。
  开关是 `cfg.mcpAllowExec` / `cfg.mcpAllowFile`，**默认全关**；关闭时工具根本不注册
  （`tools/list` 里看不到），不是注册后再拒绝。新增同类高危工具请放进对应 `if` 块。
- **无 Bearer 密钥拒绝启动 MCP**：`McpService.start()` 在 `token` 为空时记 `last_error`
  并返回空串，绝不静默降级为无鉴权服务；`_serve()` 里 `_TokenMiddleware` 必须挂上。
- **SSH 主机密钥走 TOFU**：`ssh_worker.make_host_key_policy` 首次连接记录 SHA256 指纹到
  `data.json` 的 `ssh_host_keys`（键名同 known_hosts 风格，见 `host_key_id`），指纹变更
  即抛 `HostKeyMismatchError` → 发 `hostKeyMismatch` + `connectFailed` 两个信号
  （UI 弹框 / MCP 桥有失败出口）。禁止改回 `AutoAddPolicy`；策略内也不要写
  `client._host_keys`，否则同一客户端二次连接会跳过校验。
- **全局异常兜底**：`app/crash_guard.py` 在 `main.py:main()` 开头 `installCrashGuard()`，
  接管 `sys.excepthook` 与 `threading.excepthook`，把槽内逃逸异常转成
  `logs/crash.log` + 非模态提示框。PyQt 默认钩子会 `qFatal()` 直接杀进程
  （打包版无控制台，表现为“点一下程序就消失”），新增页面务必别依赖它。
  弹框只能在 GUI 线程建，跨线程经 `_notifier.crashed` 信号排队。
- `data.json` 由 UI 线程与 worker 线程并发读写，必须走 `config.py` 的
  `loadData()` / `saveData()`（内部有 `_DATA_LOCK`），不要自己 `open()`。

## 运行与测试

```bash
# 启动（入口负责 loadConfig / 主题 / 白滚动条）
python main.py

# 测试（仓库根目录运行；无 conftest.py，各测试文件自带 sys.path 处理）
python -m pytest tests/ -q
```

- 测试不需要硬件：用 pyserial `loop://` 回环、fake paramiko 客户端、fake 信号线程。
- `config.json`（qconfig 运行时配置）与 `data.json`（发送历史/预设）**运行期自动生成且被 gitignore**，干净检出后不存在；改配置逻辑后本地删掉旧文件再跑。
- MCP 密钥在 `config.py:_ensureMcpToken` 首次启动自动生成（`uuid4().hex[:16]`），已有密钥绝不覆盖；`mcpEnabled` 默认关闭。
- 测试会读写 `data.json` 的地方（如 SSH 主机密钥）必须 monkeypatch `app.ssh_worker.loadData/saveData`
  成内存表，别污染仓库根目录。

## 开发约定

- 编码 UTF-8，注释/文档中文。
- UI 用 qfluentwidgets，Fluent 深色主题（`console_style.py` / `scrollbar_style.py` 做主题适配）。
- 串口原始数据保存为 `.bin` 日志，位于 `config.json` 的 `logDir`。
- 打包脚本 `build.py`（Nuitka standalone + 7z，入口 `build.bat`，见 README「打包发布」）；`dist/` 下旧文件是历史产物，以新打包为准。