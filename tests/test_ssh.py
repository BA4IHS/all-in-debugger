# coding: utf-8
"""SSH 模块测试：fake paramiko 客户端验证 worker 信号流 + 会话纯函数 + MCP 注册。"""
import asyncio
import socket

import pytest

from app.mcp_bridge import BridgeError, WorkerBridge
from app.ssh_worker import SshWorker
from app.ui.ssh_page import (SESSION_FIELDS, merge_sessions,
                             session_record)


# ── fake paramiko ─────────────────────────────────────────────


class _FakeChannel:

    def __init__(self):
        self.sent = bytearray()
        self.closed = False
        self._exit = False
        self.width = 80
        self.height = 24

    def settimeout(self, t):
        pass

    def send(self, data):
        self.sent.extend(data)
        return len(data)

    def resize_pty(self, width=80, height=24):
        self.width = width
        self.height = height

    def close(self):
        self.closed = True

    def exit_status_ready(self):
        return self._exit

    def recv(self, n):
        raise socket.timeout()


class _FakeTransport:

    def set_keepalive(self, s):
        pass


class _FakeClient:

    def __init__(self):
        self.kwargs = None
        self.chan = _FakeChannel()
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kw):
        self.kwargs = kw

    def get_transport(self):
        return _FakeTransport()

    def invoke_shell(self, term="xterm", width=80, height=24):
        self.chan.width = width
        self.chan.height = height
        return self.chan

    def close(self):
        self.closed = True


def _make_worker():
    """返回 (worker, factory 最近产出的 fake client 容器)。"""
    box = {}

    def factory():
        box["client"] = _FakeClient()
        return box["client"]

    return SshWorker(client_factory=factory), box


def _capture(sig):
    out = []
    sig.connect(lambda *a: out.append(a))
    return out


CFG = {"host": "127.0.0.1", "port": 2222, "username": "root",
       "password": "pw", "cols": 100, "rows": 30}


# ── worker 连接/写/断线 ──────────────────────────────────────


def test_connect_success():
    w, box = _make_worker()
    got = _capture(w.connected)
    w.requestConnect(dict(CFG))
    assert len(got) == 1
    info = got[0][0]
    assert info == {"host": "127.0.0.1", "port": 2222, "username": "root"}
    kw = box["client"].kwargs
    assert kw["hostname"] == "127.0.0.1" and kw["port"] == 2222
    assert kw["password"] == "pw"
    # 初始 PTY 尺寸取自连接参数
    assert box["client"].chan.width == 100
    assert box["client"].chan.height == 30


def test_connect_requires_host_and_user():
    w, _ = _make_worker()
    fail = _capture(w.connectFailed)
    w.requestConnect({"host": "", "username": ""})
    assert fail and "主机" in fail[0][0]
    assert not w._connected


def test_key_auth_takes_priority_over_password():
    w, box = _make_worker()
    cfg = dict(CFG, key_path="/tmp/id_rsa")
    w.requestConnect(cfg)
    kw = box["client"].kwargs
    assert kw.get("key_filename") == "/tmp/id_rsa"
    assert "password" not in kw


def test_write_and_resize():
    w, box = _make_worker()
    w.requestConnect(dict(CFG))
    w.requestWrite(b"ls -l\r")
    assert bytes(box["client"].chan.sent) == b"ls -l\r"
    w.requestResize(120, 40)
    assert box["client"].chan.width == 120
    assert box["client"].chan.height == 40


def test_close_emits_closed_and_cleanup():
    w, box = _make_worker()
    closed = _capture(w.closed)
    w.requestConnect(dict(CFG))
    w.requestClose()
    assert closed and box["client"].closed
    assert not w._connected
    # 未连接时再关不再发 closed
    n = len(closed)
    w.requestClose()
    assert len(closed) == n


def test_poll_rx_detects_remote_exit():
    w, box = _make_worker()
    closed = _capture(w.closed)
    w.requestConnect(dict(CFG))
    box["client"].chan._exit = True
    w._poll_rx()
    assert closed and not w._connected


def test_sftp_list_parses_attributes_objects():
    """listdir_attr 返回 SFTPAttributes 对象列表（回归：曾误当元组解包）。"""

    class _Attr:
        def __init__(self, filename, mode, size):
            self.filename = filename
            self.st_mode = mode
            self.st_size = size

    class _FakeSftp:
        def listdir_attr(self, path):
            return [_Attr("beta", 0o40755, 0),
                    _Attr("a.txt", 0o100644, 5)]

    w, _ = _make_worker()
    w._sftp = _FakeSftp()
    out = w._sftp_list("/x")
    assert out[0] == {"name": "beta", "size": 0, "type": "dir"}  # 目录优先
    assert out[1] == {"name": "a.txt", "size": 5, "type": "file"}


# ── 会话记录（密码绝不落盘）──────────────────────────────────


def test_session_record_excludes_password():
    rec = session_record("dev", "10.0.0.1", 22, "root", "密码", "")
    assert set(rec.keys()) == set(SESSION_FIELDS)
    assert "password" not in rec


def test_merge_sessions_overwrite_by_name():
    old = [{"name": "a", "host": "h1"}, {"name": "b", "host": "h2"}]
    rec = session_record("a", "newhost", 2222, "u", "密码", "")
    out = merge_sessions(old, rec)
    assert len(out) == 2
    by_name = {s["name"]: s for s in out}
    assert by_name["a"]["host"] == "newhost"
    assert by_name["a"]["port"] == 2222
    assert by_name["b"]["host"] == "h2"


# ── MCP 桥与工具注册 ─────────────────────────────────────────


def test_bridge_ssh_requires_module():
    box = WorkerBridge(None, None, None, None)   # sht 缺省 None
    with pytest.raises(BridgeError, match="SSH"):
        box.ssh_status()
    with pytest.raises(BridgeError, match="SSH"):
        box.ssh_connect("h")


def test_mcp_tools_registered():
    from app.mcp_server import build_mcp

    mcp = build_mcp(object())
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    for expected in ("ssh_status", "ssh_connect", "ssh_disconnect",
                     "ssh_exec", "ssh_file_list"):
        assert expected in names
    assert len(names) == 37      # 原 32 + SSH 5
