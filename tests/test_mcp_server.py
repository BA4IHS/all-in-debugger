# coding: utf-8
"""MCP 服务层测试：工具注册完整性、工具调用、鉴权中间件、服务启停。"""
import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

mcp_mod = pytest.importorskip("mcp")

from app.mcp_server import McpService, _TokenMiddleware, build_mcp  # noqa: E402


class FakeBridge:
    """最小桩桥：仅实现 debugger_status。"""

    def debugger_status(self):
        return {"serial": {"opened": False}, "ok": True}


@pytest.fixture(scope="module")
def mcp_app():
    return build_mcp(FakeBridge())


EXPECTED_TOOLS = {
    "debugger_status",
    "serial_list_ports", "serial_status", "serial_open", "serial_close",
    "serial_send", "serial_read_recent",
    "hid_enumerate", "hid_status", "hid_open", "hid_close", "hid_write",
    "hid_feature_get", "hid_feature_set", "hid_read_recent",
    "adb_devices", "adb_shell", "adb_list_dir", "adb_push", "adb_pull",
    "dap_list_probes", "dap_status", "dap_open", "dap_close",
    "dap_rtt_write", "dap_rtt_read_recent",
    "modbus_status", "modbus_connect_rtu", "modbus_connect_tcp",
    "modbus_disconnect", "modbus_read", "modbus_write",
}


def test_all_tools_registered(mcp_app):
    tools = asyncio.run(mcp_app.list_tools())
    names = {t.name for t in tools}
    assert EXPECTED_TOOLS <= names, f"缺少工具：{EXPECTED_TOOLS - names}"
    for t in tools:
        assert t.description, f"工具 {t.name} 缺少描述"


def test_tool_schemas_have_types(mcp_app):
    tools = asyncio.run(mcp_app.list_tools())
    by_name = {t.name: t for t in tools}
    props = by_name["serial_open"].inputSchema.get("properties", {})
    assert "port" in props and "baudrate" in props
    assert by_name["serial_open"].inputSchema.get(
        "required") and "port" in by_name["serial_open"].inputSchema["required"]


def test_call_debugger_status(mcp_app):
    result = asyncio.run(mcp_app.call_tool("debugger_status", {}))
    # FastMCP 返回 (content_list, structured)
    content = result[0] if isinstance(result, tuple) else result
    text = content[0].text
    data = json.loads(text)
    assert data["ok"] is True


def test_token_middleware_rejects():
    sent = []

    async def app(scope, receive, send):
        sent.append("app")

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request"}

    mw = _TokenMiddleware(app, "secret")
    scope = {"type": "http", "headers": []}
    asyncio.run(mw(scope, receive, send))
    assert "app" not in sent  # 未鉴权不应进入真实应用
    assert sent[0]["status"] == 401


def test_token_middleware_accepts():
    entered = []

    async def app(scope, receive, send):
        entered.append(True)

    async def send(msg):
        pass

    async def receive():
        return {"type": "http.request"}

    mw = _TokenMiddleware(app, "secret")
    scope = {"type": "http",
             "headers": [(b"authorization", b"Bearer secret")]}
    asyncio.run(mw(scope, receive, send))
    assert entered == [True]


def test_service_http_smoke():
    """真实起服务：无 Token 时匿名请求被拒（401）。"""
    svc = McpService(FakeBridge(), port=18642, token="tk123")
    try:
        svc.start()
        deadline = time.time() + 5
        status = None
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    svc.url, data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    status = r.status
                break
            except urllib.error.HTTPError as e:
                status = e.code
                break
            except OSError:
                time.sleep(0.1)
        assert status == 401
    finally:
        svc.stop()
