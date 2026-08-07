# coding: utf-8
"""内嵌 MCP 服务：把调试器五模块能力以工具形式暴露给 AI 客户端。

- 传输：Streamable HTTP（FastMCP 1.x + uvicorn），仅监听 127.0.0.1；
- 线程：uvicorn 在独立线程跑自己的 asyncio 事件循环，不占 Qt 主循环；
- 设备操作全部经 WorkerBridge 转发到各 worker 线程，与 GUI 共享连接；
- 可选 Bearer Token 鉴权（纯 ASGI 中间件实现）。
"""
import functools
import threading

from app.mcp_bridge import BridgeError, parse_hex, to_hex

INSTRUCTIONS = (
    "all-in-debugger 的调试能力集合：串口、USB HID、ADB、DAP-Link RTT、Modbus、SSH、"
    "TCP/IP 网络（UDP/TCP Server/TCP Client）。"
    "典型流程：先 *_status / *_enumerate 查询，再 open/connect/start，"
    "然后 send/write/read。HEX 数据用空格分隔的十六进制字节表示。"
)


def _fmt_rx(data: bytes) -> dict:
    return {"length": len(data),
            "hex": to_hex(data),
            "ascii": bytes(data).decode("utf-8", "replace")}


def _guard(fn):
    """统一错误出口：BridgeError/异常 → 工具文本错误。"""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except BridgeError as e:
            return f"错误：{e}"
        except Exception as e:  # noqa: BLE001 - 工具层兜底
            return f"内部错误：{type(e).__name__}: {e}"
    return wrapper


def build_mcp(bridge):
    """构建 FastMCP 实例并注册全部工具。"""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("all-in-debugger", instructions=INSTRUCTIONS)
    tool = mcp.tool

    # ── 通用 ───────────────────────────────────────────────────

    @tool()
    @_guard
    async def debugger_status() -> dict:
        """查询各调试模块（串口/HID/DAP/Modbus）的当前连接状态。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.debugger_status)

    # ── 串口 ───────────────────────────────────────────────────

    @tool()
    @_guard
    async def serial_list_ports() -> list:
        """枚举本机可用串口（含描述与硬件 ID）。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.serial_list_ports)

    @tool()
    @_guard
    async def serial_status() -> dict:
        """查询串口连接状态与参数。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.serial_status)

    @tool()
    @_guard
    async def serial_open(port: str, baudrate: int = 115200,
                          bytesize: int = 8, parity: str = "N",
                          stopbits: float = 1) -> dict:
        """打开串口。parity 取 N/E/O/M，stopbits 取 1/1.5/2。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.serial_open(port, baudrate, bytesize,
                                       parity, stopbits))

    @tool()
    @_guard
    async def serial_close() -> dict:
        """关闭当前串口。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.serial_close)

    @tool()
    @_guard
    async def serial_send(data: str, as_text: bool = False) -> dict:
        """向已打开的串口发送数据。as_text=False 时 data 为 HEX 串（如 'AA BB CC'），否则按 UTF-8 文本发送。"""
        import anyio

        def _do():
            payload = data.encode("utf-8") if as_text else parse_hex(data)
            return bridge.serial_send(payload)
        return await anyio.to_thread.run_sync(_do)

    @tool()
    @_guard
    async def serial_read_recent(limit: int = 512) -> dict:
        """读取串口最近接收的数据（最多 limit 字节），返回 HEX 与 ASCII。"""
        import anyio
        data = await anyio.to_thread.run_sync(
            lambda: bridge.serial_read_recent(limit))
        return _fmt_rx(data)

    # ── HID ────────────────────────────────────────────────────

    @tool()
    @_guard
    async def hid_enumerate(vid: int = 0, pid: int = 0) -> list:
        """枚举 USB HID 设备；vid/pid 为 0 表示不过滤。返回带 index 的列表。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.hid_enumerate(vid, pid))

    @tool()
    @_guard
    async def hid_status() -> dict:
        """查询 HID 设备打开状态。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.hid_status)

    @tool()
    @_guard
    async def hid_open(index: int = -1, vid: int = 0, pid: int = 0,
                       serial: str = "") -> dict:
        """打开 HID 设备：index>=0 按 hid_enumerate 的序号打开，否则按 vid/pid/serial 打开。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.hid_open(index, vid, pid, serial))

    @tool()
    @_guard
    async def hid_close() -> dict:
        """关闭当前 HID 设备。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.hid_close)

    @tool()
    @_guard
    async def hid_write(data: str) -> dict:
        """向 HID 设备写中断输出报告，data 为 HEX 串（首字节为报告 ID，可用 00 占位）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.hid_write(parse_hex(data)))

    @tool()
    @_guard
    async def hid_feature_get(report_id: int = 0, size: int = 64) -> dict:
        """读取 HID 特征报告（控制传输 GET_FEATURE）。"""
        import anyio
        data = await anyio.to_thread.run_sync(
            lambda: bridge.hid_feature_get(report_id, size))
        return _fmt_rx(data)

    @tool()
    @_guard
    async def hid_feature_set(data: str) -> dict:
        """写 HID 特征报告（SET_FEATURE），data 为 HEX 串，首字节为报告 ID。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.hid_feature_set(parse_hex(data)))

    @tool()
    @_guard
    async def hid_read_recent(limit: int = 512) -> dict:
        """读取 HID 最近接收的报告（最多 limit 字节）。"""
        import anyio
        data = await anyio.to_thread.run_sync(
            lambda: bridge.hid_read_recent(limit))
        return _fmt_rx(data)

    # ── ADB ────────────────────────────────────────────────────

    @tool()
    @_guard
    async def adb_devices() -> list:
        """列出 ADB 已连接设备（serial/state/info）。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.adb_devices)

    @tool()
    @_guard
    async def adb_shell(serial: str, command: str, timeout: float = 15.0) -> str:
        """在指定设备上执行 adb shell 命令并返回输出。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.adb_shell(serial, command, timeout))

    @tool()
    @_guard
    async def adb_list_dir(serial: str, path: str = "/") -> dict:
        """列出设备目录内容（名称/大小/时间/类型）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.adb_list_dir(serial, path))

    @tool()
    @_guard
    async def adb_push(serial: str, local: str, remote: str,
                       timeout: float = 120.0) -> dict:
        """把本机文件 push 到设备。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.adb_push(serial, local, remote, timeout))

    @tool()
    @_guard
    async def adb_pull(serial: str, remote: str, local: str,
                       timeout: float = 120.0) -> dict:
        """把设备文件 pull 到本机。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.adb_pull(serial, remote, local, timeout))

    # ── DAP-RTT ────────────────────────────────────────────────

    @tool()
    @_guard
    async def dap_list_probes() -> list:
        """枚举已连接的 CMSIS-DAP 调试器。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.dap_list_probes)

    @tool()
    @_guard
    async def dap_status() -> dict:
        """查询 DAP 调试器与 RTT 通道状态。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.dap_status)

    @tool()
    @_guard
    async def dap_open(path: str = "", speed_khz: int = 0,
                       reset: bool = False, cb_addr: int = 0,
                       ram_start: int = 0, ram_size: int = 0,
                       kernel: str = "auto", chip: str = "") -> dict:
        """打开调试器并经 SWD 连接目标、定位 RTT 控制块。chip 为芯片包名（app/chip_profiles/，用 dap_chip_profiles 查看，优先于 kernel）；kernel 选择目标 ARM 内核（auto/m0/m3/m4/m7/m23/m33/a7/a53）；cb_addr=0 时自动检测，可给 ram_start/ram_size 限定扫描区间；Cortex-A 不自动扫描须手动指定地址；speed_khz=0 时用芯片包默认（否则 4000kHz）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.dap_open(path, speed_khz, reset, cb_addr,
                                    ram_start, ram_size, kernel, chip))

    @tool()
    @_guard
    async def dap_chip_profiles() -> list:
        """列出可用芯片包（名称/内核/RAM 区间/SWD 速度），供 dap_open 的 chip 参数使用。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.dap_chip_profiles)

    @tool()
    @_guard
    async def dap_close() -> dict:
        """关闭 DAP 调试器连接。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.dap_close)

    @tool()
    @_guard
    async def dap_rtt_write(channel: str, text: str) -> dict:
        """向 RTT 下行通道写入文本（channel 为固定槽位编号 "0"~"15"，
        未配置通道拒绝写入）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.dap_write(channel, text))

    @tool()
    @_guard
    async def dap_rtt_read_recent(channel: str = "0", limit: int = 2048) -> dict:
        """读取指定 RTT 上行通道最近数据（channel 为固定槽位编号 "0"~"15"，
        未配置通道无数据；UTF-8 文本与 HEX）。"""
        import anyio
        data = await anyio.to_thread.run_sync(
            lambda: bridge.dap_read_recent(channel, limit))
        return _fmt_rx(data)

    # ── Modbus ─────────────────────────────────────────────────

    @tool()
    @_guard
    async def modbus_status() -> dict:
        """查询 Modbus 连接状态与最近一次读取结果。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.modbus_status)

    @tool()
    @_guard
    async def modbus_connect_rtu(port: str, baudrate: int = 9600,
                                 parity: str = "N",
                                 stopbits: float = 1) -> dict:
        """以 Modbus RTU（串口）方式连接。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.modbus_connect_rtu(port, baudrate, parity,
                                              stopbits))

    @tool()
    @_guard
    async def modbus_connect_tcp(host: str, tcp_port: int = 502) -> dict:
        """以 Modbus TCP 方式连接。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.modbus_connect_tcp(host, tcp_port))

    @tool()
    @_guard
    async def modbus_disconnect() -> dict:
        """断开 Modbus 连接。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.modbus_disconnect)

    @tool()
    @_guard
    async def modbus_read(fc: int, addr: int, count: int,
                          slave: int = 1) -> dict:
        """读寄存器/线圈。fc 取 1 线圈/2 离散输入/3 保持寄存器/4 输入寄存器。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.modbus_read(fc, addr, count, slave))

    @tool()
    @_guard
    async def modbus_write(fc: int, addr: int, values: list,
                           slave: int = 1) -> dict:
        """写寄存器/线圈。fc=5 单线圈(values=[0/1])，fc=6 单寄存器，fc=15 多线圈，fc=16 多寄存器。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.modbus_write(fc, addr, values, slave))

    # ── SSH ─────────────────────────────────────────────

    @tool()
    @_guard
    async def ssh_status() -> dict:
        """查询 SSH 连接状态。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.ssh_status)

    @tool()
    @_guard
    async def ssh_connect(host: str, port: int = 22,
                          username: str = "root", password: str = "",
                          key_path: str = "", timeout: float = 10.0) -> dict:
        """连接 SSH 服务器。key_path 非空时用私钥认证，否则用密码。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.ssh_connect(host, port, username, password,
                                       key_path, timeout))

    @tool()
    @_guard
    async def ssh_disconnect() -> dict:
        """断开当前 SSH 连接。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.ssh_disconnect)

    @tool()
    @_guard
    async def ssh_exec(command: str, timeout: float = 15.0) -> dict:
        """在已连接的 SSH 会话上执行命令，返回 exit/stdout/stderr。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.ssh_exec(command, timeout))

    @tool()
    @_guard
    async def ssh_file_list(path: str = ".") -> dict:
        """列出远端目录内容（名称/大小/类型，经 SFTP）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.ssh_file_list(path))

    # ── TCP/IP（UDP / TCP Server / TCP Client）────────────

    @tool()
    @_guard
    async def tcpip_status() -> dict:
        """查询网络调试（TCP/UDP）连接状态与客户端列表。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.tcpip_status)

    @tool()
    @_guard
    async def tcpip_start(mode: str, local_host: str = "",
                          local_port: int = 0, remote_host: str = "",
                          remote_port: int = 0) -> dict:
        """启动网络连接。mode: 'tcp_server'（监听本地端口）| 'tcp_client'
        （连接远程主机）| 'udp'（绑定本地并向目标主机发送，支持广播）。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.tcpip_start(mode, local_host, local_port,
                                       remote_host, remote_port))

    @tool()
    @_guard
    async def tcpip_stop() -> dict:
        """断开当前网络连接。"""
        import anyio
        return await anyio.to_thread.run_sync(bridge.tcpip_stop)

    @tool()
    @_guard
    async def tcpip_send(data: str, target: str = "") -> dict:
        """发送 HEX 数据。TCP Server 模式 target 填 'ALL' 广播或具体
        客户端地址（用 tcpip_list_clients 查询）；其余模式忽略。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.tcpip_send(data, target))

    @tool()
    @_guard
    async def tcpip_list_clients() -> list:
        """列出 TCP Server 当前已连接的客户端地址。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.tcpip_list_clients())

    @tool()
    @_guard
    async def tcpip_close_client(addr: str) -> dict:
        """断开指定 TCP Server 客户端；addr 为空串 = 全部断开。"""
        import anyio
        return await anyio.to_thread.run_sync(
            lambda: bridge.tcpip_close_client(addr))

    return mcp


class _TokenMiddleware:
    """纯 ASGI 鉴权中间件：校验 Authorization: Bearer <token>。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token or ""

    async def __call__(self, scope, receive, send):
        if self.token and scope.get("type") == "http":
            headers = {k.decode("latin-1"): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if headers.get("authorization", "") != f"Bearer {self.token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body",
                            "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


class McpService:
    """内嵌 MCP 服务的启停封装（独立线程跑 uvicorn）。"""

    def __init__(self, bridge, port: int = 8642, token: str = ""):
        self.bridge = bridge
        self.port = int(port)
        self.token = token or ""
        self._server = None
        self._thread = None
        self._error = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self):
        """后台线程启动失败时的异常（依赖缺失/端口占用等）。"""
        return self._error

    def start(self) -> str:
        """启动服务；返回接入 URL。

        MCP 应用构建（注册全部工具 + 生成 ASGI 应用，约 0.6s）与
        uvicorn 运行均放在后台线程，避免阻塞 GUI 启动。
        失败（依赖缺失/端口占用等）记录到 last_error，GUI 不崩溃。
        """
        if self.running:
            return self.url
        self._error = None
        self._thread = threading.Thread(
            target=self._serve, name="mcp-http", daemon=True)
        self._thread.start()
        return self.url

    def _serve(self):
        """后台线程：构建 MCP 应用并运行 uvicorn 直到 stop()。"""
        try:
            import uvicorn

            app = build_mcp(self.bridge).streamable_http_app()
            if self.token:
                app = _TokenMiddleware(app, self.token)
            config = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                                    log_level="warning", lifespan="on",
                                    access_log=False)
            self._server = uvicorn.Server(config)
            self._server.run()
        except Exception as e:  # 依赖缺失/端口占用等：记录错误，不崩溃
            self._error = e
            self._server = None

    def stop(self, timeout: float = 3.0):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
        self._server = None
        self._thread = None
