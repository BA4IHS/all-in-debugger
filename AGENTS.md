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

## 开发约定

- 编码 UTF-8，注释/文档中文。
- UI 用 qfluentwidgets，Fluent 深色主题（`console_style.py` / `scrollbar_style.py` 做主题适配）。
- 串口原始数据保存为 `.bin` 日志，位于 `config.json` 的 `logDir`。
- 打包脚本 `build.py`（Nuitka standalone + 7z，入口 `build.bat`，见 README「打包发布」）；`dist/` 下旧文件是历史产物，以新打包为准。