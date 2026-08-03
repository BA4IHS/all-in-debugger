# coding: utf-8
"""MCP 工具层 <-> Qt worker 线程桥。

设备句柄全部在各 worker 线程内，本桥负责：
- 命令类操作：emit 现有 sig* 信号（queued）→ 一次性信号监听 +
  threading.Event 收集结果，带超时；
- 只读查询：sigMcpQuery → mcpReply（worker 侧 requestMcpQuery 槽）。

所有对外方法抛 BridgeError 表示可传达给 AI 调用方的错误。
"""
import subprocess
import sys
import threading
import uuid

from PyQt6.QtCore import Qt


class BridgeError(RuntimeError):
    """桥层错误：消息可直接作为 MCP 工具错误返回。"""


DEFAULT_TIMEOUT = 5.0
ADB_TIMEOUT_CAP = 300.0


def to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in bytes(data or b""))


def parse_hex(text: str) -> bytes:
    """解析 HEX 字符串：支持空格/逗号分隔、0x 前缀、连续十六进制串。"""
    t = str(text).replace(",", " ").replace("0x", " ").replace("0X", " ")
    toks = [x for x in t.split() if x]
    if not toks:
        raise BridgeError("HEX 数据为空")
    if len(toks) == 1 and len(toks[0]) > 2 and len(toks[0]) % 2 == 0:
        try:
            return bytes.fromhex(toks[0])
        except ValueError:
            pass
    try:
        vals = [int(x, 16) for x in toks]
    except ValueError:
        raise BridgeError(f"无法解析 HEX 数据：{text}") from None
    if any(v < 0 or v > 0xFF for v in vals):
        raise BridgeError(f"存在超出 0x00~0xFF 的字节：{text}")
    return bytes(vals)


class WorkerBridge:
    """把同步调用转发到四个 worker 线程，与 GUI 共享同一连接。"""

    def __init__(self, st, ht, dt, mt, sht=None):
        self.st = st   # SerialThread
        self.ht = ht   # HidThread
        self.dt = dt   # DapThread
        self.mt = mt   # ModbusThread
        self.sht = sht  # SshThread（可选）
        self._hid_cache = []

    # ── 通用机制 ───────────────────────────────────────────────

    def _query(self, thread_obj, q: dict, timeout: float = 3.0):
        """只读查询：sigMcpQuery → mcpReply（带请求 id 防串扰）。"""
        q = dict(q)
        rid = q.setdefault("id", uuid.uuid4().hex)
        worker = thread_obj.worker
        box = {}
        ev = threading.Event()

        def cb(reply):
            if reply.get("id") != rid:
                return
            if not ev.is_set():
                box.update(reply)
                ev.set()

        worker.mcpReply.connect(cb, Qt.ConnectionType.DirectConnection)
        try:
            thread_obj.sigMcpQuery.emit(q)
            if not ev.wait(timeout):
                raise BridgeError("查询超时（worker 未运行或正忙）")
        finally:
            try:
                worker.mcpReply.disconnect(cb)
            except TypeError:
                pass
        if "error" in box:
            raise BridgeError(str(box["error"]))
        return box.get("data")

    def _emit_wait(self, ok_signals, err_signals, emit, timeout: float,
                   label: str):
        """emit() 发命令，等待 ok/err 任一信号，返回 ok 信号的参数元组。"""
        box = {}
        ev = threading.Event()
        conns = []

        def make_cb(kind):
            def cb(*args):
                if not ev.is_set():
                    box["kind"] = kind
                    box["args"] = args
                    ev.set()
            return cb

        for sig in ok_signals:
            cb = make_cb("ok")
            conns.append((sig, cb))
            sig.connect(cb, Qt.ConnectionType.DirectConnection)
        for sig in err_signals:
            cb = make_cb("err")
            conns.append((sig, cb))
            sig.connect(cb, Qt.ConnectionType.DirectConnection)
        try:
            emit()
            if not ev.wait(timeout):
                raise BridgeError(f"{label}：等待结果超时（{timeout:g}s）")
        finally:
            for sig, cb in conns:
                try:
                    sig.disconnect(cb)
                except TypeError:
                    pass
        if box.get("kind") == "err":
            msg = " ".join(str(a) for a in box.get("args", ())) or f"{label}失败"
            raise BridgeError(msg)
        return box.get("args", ())

    # ── 状态总览 ───────────────────────────────────────────────

    def debugger_status(self) -> dict:
        out = {}
        for name, th, q in (("serial", self.st, {"op": "snapshot"}),
                            ("hid", self.ht, {"op": "snapshot"}),
                            ("dap", self.dt, {"op": "snapshot"}),
                            ("modbus", self.mt, {"op": "snapshot"}),
                            ("ssh", self.sht, {"op": "snapshot"})):
            if th is None:
                out[name] = {"error": "模块未启用"}
                continue
            try:
                out[name] = self._query(th, q, timeout=2.0)
            except Exception as e:  # noqa: BLE001 - 状态聚合需容错
                out[name] = {"error": str(e)}
        return out

    # ── 串口 ───────────────────────────────────────────────────

    def serial_list_ports(self):
        from serial.tools import list_ports
        return [{"device": p.device,
                 "description": p.description or "",
                 "hwid": p.hwid or ""}
                for p in list_ports.comports()]

    def serial_status(self):
        return self._query(self.st, {"op": "snapshot"})

    def serial_open(self, port: str, baudrate: int = 115200, bytesize: int = 8,
                    parity: str = "N", stopbits: float = 1):
        cfg = {"port": str(port), "baudrate": int(baudrate),
               "bytesize": int(bytesize), "parity": str(parity)[:1].upper(),
               "stopbits": float(stopbits), "timeout": 0.05}
        args = self._emit_wait(
            [self.st.worker.portOpened], [self.st.worker.openFailed],
            lambda: self.st.sigOpen.emit(cfg), DEFAULT_TIMEOUT, "打开串口")
        return {"port": args[0]}

    def serial_close(self):
        self.st.sigClose.emit()
        return {"ok": True}

    def serial_send(self, data: bytes):
        args = self._emit_wait(
            [self.st.worker.dataWritten], [self.st.worker.errorOccurred],
            lambda: self.st.sigWrite.emit(bytes(data)),
            DEFAULT_TIMEOUT, "串口发送")
        return {"written": int(args[0])}

    def serial_read_recent(self, limit: int) -> bytes:
        return bytes(self._query(self.st, {"op": "rx", "n": int(limit)}))

    # ── HID ────────────────────────────────────────────────────

    def hid_enumerate(self, vid: int = 0, pid: int = 0):
        from app import hid_binding
        devs = hid_binding.enumerate_devices(int(vid), int(pid))
        self._hid_cache = devs
        return [{"index": i,
                 "vid": d.get("vid", 0), "pid": d.get("pid", 0),
                 "product": d.get("product", ""),
                 "manufacturer": d.get("manufacturer", ""),
                 "serial": d.get("serial", ""),
                 "usage_page": d.get("usage_page", 0),
                 "interface": d.get("interface", 0)}
                for i, d in enumerate(devs)]

    def hid_status(self):
        return self._query(self.ht, {"op": "snapshot"})

    def hid_open(self, index: int = -1, vid: int = 0, pid: int = 0,
                 serial: str = ""):
        index = int(index)
        if index >= 0:
            if not self._hid_cache:
                self.hid_enumerate()
            if index >= len(self._hid_cache):
                raise BridgeError(f"设备索引 {index} 超出范围"
                                  f"（当前 {len(self._hid_cache)} 个）")
            entry = self._hid_cache[index]
            cfg = {"path": entry.get("path", b""),
                   "vid": int(entry.get("vid", 0)),
                   "pid": int(entry.get("pid", 0)),
                   "product": entry.get("product", ""),
                   "serial": entry.get("serial", "")}
        else:
            cfg = {"vid": int(vid), "pid": int(pid), "serial": str(serial)}
        args = self._emit_wait(
            [self.ht.worker.deviceOpened], [self.ht.worker.openFailed],
            lambda: self.ht.sigOpen.emit(cfg), DEFAULT_TIMEOUT, "打开 HID 设备")
        return args[0]

    def hid_close(self):
        self.ht.sigClose.emit()
        return {"ok": True}

    def hid_write(self, data: bytes):
        args = self._emit_wait(
            [self.ht.worker.dataWritten], [self.ht.worker.errorOccurred],
            lambda: self.ht.sigWrite.emit(bytes(data)),
            DEFAULT_TIMEOUT, "HID 写入")
        return {"written": int(args[0])}

    def hid_feature_get(self, report_id: int, size: int = 64) -> bytes:
        args = self._emit_wait(
            [self.ht.worker.featureData], [self.ht.worker.errorOccurred],
            lambda: self.ht.sigFeatureGet.emit(int(report_id), int(size)),
            DEFAULT_TIMEOUT, "获取特征报告")
        return bytes(args[0])

    def hid_feature_set(self, data: bytes):
        args = self._emit_wait(
            [self.ht.worker.dataWritten], [self.ht.worker.errorOccurred],
            lambda: self.ht.sigFeatureSend.emit(bytes(data)),
            DEFAULT_TIMEOUT, "发送特征报告")
        return {"written": int(args[0])}

    def hid_read_recent(self, limit: int) -> bytes:
        return bytes(self._query(self.ht, {"op": "rx", "n": int(limit)}))

    # ── DAP-RTT ────────────────────────────────────────────────

    def dap_list_probes(self):
        from app import dap_core
        # verify=True：逐个发 DAP_Info 在线验证，排除触摸屏等
        # 冒充 0xFF00 厂商页的系统 HID 设备
        probes = dap_core.enum_probes(verify=True)
        out = []
        for i, p in enumerate(probes):
            item = {"index": i}
            for k, v in (p or {}).items():
                item[k] = (v.decode("utf-8", "replace")
                           if isinstance(v, bytes) else v)
            out.append(item)
        return out

    def dap_status(self):
        return self._query(self.dt, {"op": "snapshot"})

    def dap_open(self, path: str = "", speed_khz: int = 4000,
                 reset: bool = False, cb_addr: int = 0,
                 ram_start: int = 0, ram_size: int = 0):
        cfg = {"path": str(path or ""), "clock": int(speed_khz) * 1000,
               "reset": bool(reset), "cb_addr": int(cb_addr),
               "ram_start": int(ram_start), "ram_size": int(ram_size)}
        args = self._emit_wait(
            [self.dt.worker.rttFound],
            [self.dt.worker.openFailed, self.dt.worker.errorOccurred],
            lambda: self.dt.sigOpen.emit(cfg), 15.0, "DAP 连接")
        self.dt.sigStartRtt.emit()
        rtt = args[0]
        return {"cb_addr": rtt.get("addr", 0),
                "channels": [{"name": c["name"], "direction": c["direction"]}
                             for c in rtt.get("channels", [])]}

    def dap_close(self):
        self.dt.sigClose.emit()
        return {"ok": True}

    def dap_write(self, channel: str, text: str):
        data = str(text).encode("utf-8")
        args = self._emit_wait(
            [self.dt.worker.dataWritten], [self.dt.worker.errorOccurred],
            lambda: self.dt.sigWrite.emit(str(channel), data),
            DEFAULT_TIMEOUT, "RTT 写入")
        return {"written": int(args[1])}

    def dap_read_recent(self, channel: str, limit: int) -> bytes:
        return bytes(self._query(
            self.dt, {"op": "rx", "channel": str(channel), "n": int(limit)}))

    # ── Modbus ─────────────────────────────────────────────────

    def modbus_status(self):
        return self._query(self.mt, {"op": "snapshot"})

    def _modbus_connect(self, cfg: dict):
        args = self._emit_wait(
            [self.mt.worker.connected], [self.mt.worker.connectFailed],
            lambda: self.mt.sigConnect.emit(cfg), 10.0, "Modbus 连接")
        return {"message": args[0]}

    def modbus_connect_rtu(self, port: str, baudrate: int = 9600,
                           parity: str = "N", stopbits: float = 1):
        return self._modbus_connect({
            "transport": "rtu", "port": str(port),
            "baudrate": int(baudrate), "parity": str(parity)[:1].upper(),
            "stopbits": float(stopbits), "timeout": 1.0})

    def modbus_connect_tcp(self, host: str, tcp_port: int = 502):
        return self._modbus_connect({
            "transport": "tcp", "host": str(host),
            "tcp_port": int(tcp_port), "timeout": 1.0})

    def modbus_disconnect(self):
        self.mt.sigClose.emit()
        return {"ok": True}

    def modbus_read(self, fc: int, addr: int, count: int, slave: int = 1):
        req = {"fc": int(fc), "addr": int(addr),
               "count": int(count), "slave": int(slave)}
        args = self._emit_wait(
            [self.mt.worker.readResult], [self.mt.worker.errorOccurred],
            lambda: self.mt.sigRead.emit(req),
            DEFAULT_TIMEOUT + 3.0, "Modbus 读取")
        r = args[0]
        return {"fc": r.get("fc"), "addr": r.get("addr"),
                "values": r.get("values"), "ms": r.get("ms")}

    def modbus_write(self, fc: int, addr: int, values, slave: int = 1):
        if not isinstance(values, (list, tuple)):
            values = [values]
        req = {"fc": int(fc), "addr": int(addr),
               "values": list(values), "slave": int(slave)}
        args = self._emit_wait(
            [self.mt.worker.writeResult], [self.mt.worker.errorOccurred],
            lambda: self.mt.sigWrite.emit(req),
            DEFAULT_TIMEOUT + 3.0, "Modbus 写入")
        w = args[0]
        return {"fc": w.get("fc"), "addr": w.get("addr"),
                "count": w.get("count")}

    # ── SSH ────────────────────────────────────────────────────

    def _ssh_required(self):
        if self.sht is None:
            raise BridgeError("SSH 模块未启用")

    def ssh_status(self):
        self._ssh_required()
        return self._query(self.sht, {"op": "snapshot"})

    def ssh_connect(self, host: str, port: int = 22,
                    username: str = "root", password: str = "",
                    key_path: str = "", timeout: float = 10.0):
        self._ssh_required()
        cfg = {"host": str(host), "port": int(port),
               "username": str(username), "password": str(password or ""),
               "key_path": str(key_path or ""), "timeout": float(timeout),
               "cols": 80, "rows": 24}
        args = self._emit_wait(
            [self.sht.worker.connected],
            [self.sht.worker.connectFailed, self.sht.worker.errorOccurred],
            lambda: self.sht.sigConnect.emit(cfg),
            max(15.0, float(timeout) + 5.0), "SSH 连接")
        return args[0]

    def ssh_disconnect(self):
        self._ssh_required()
        self.sht.sigClose.emit()
        return {"ok": True}

    def ssh_exec(self, command: str, timeout: float = 15.0):
        self._ssh_required()
        return self._query(
            self.sht, {"op": "exec", "cmd": str(command),
                       "timeout": float(timeout)},
            timeout=max(20.0, float(timeout) + 5.0))

    def ssh_file_list(self, path: str = "."):
        self._ssh_required()
        return self._query(self.sht, {"op": "list", "path": str(path)},
                           timeout=DEFAULT_TIMEOUT + 3.0)

    # ── ADB（subprocess 直连，不经 worker）────────────────────

    def _adb_path(self) -> str:
        from app import adb_runner
        from app.config import cfg, qconfig
        path, err = adb_runner.find_adb(qconfig.get(cfg.adbPath))
        if not path:
            raise BridgeError(f"未找到 adb：{err}")
        return path

    def _run_adb(self, args, timeout: float) -> str:
        timeout = max(1.0, min(float(timeout), ADB_TIMEOUT_CAP))
        kwargs = dict(capture_output=True, text=True, timeout=timeout)
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            r = subprocess.run([self._adb_path(), *args], **kwargs)
        except subprocess.TimeoutExpired:
            raise BridgeError(f"adb 命令超时（{timeout:g}s）") from None
        except Exception as e:
            raise BridgeError(f"adb 执行失败：{e}") from None
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            raise BridgeError(
                f"adb 退出码 {r.returncode}：{out.strip() or '无输出'}")
        return out

    def adb_devices(self):
        from app import adb_runner
        devs, err = adb_runner.list_adb_devices(self._adb_path())
        if err:
            raise BridgeError(f"枚举设备失败：{err}")
        return devs

    def adb_shell(self, serial: str, command: str, timeout: float = 15.0):
        args = []
        if serial:
            args += ["-s", str(serial)]
        args += ["shell", str(command)]
        return self._run_adb(args, timeout)

    def adb_list_dir(self, serial: str, path: str):
        import shlex
        from app.ui.adb_file_manager import parse_directory_listing
        cmd = ("LC_ALL=C TERM=dumb LS_COLORS= "
               f"ls -lAn {shlex.quote(str(path) or '/')}")
        out = self.adb_shell(serial, cmd, timeout=10.0)
        entries, errors = parse_directory_listing(out.encode("utf-8"))
        return {"path": str(path), "entries": entries, "errors": errors}

    def adb_push(self, serial: str, local: str, remote: str,
                 timeout: float = 120.0):
        args = []
        if serial:
            args += ["-s", str(serial)]
        args += ["push", str(local), str(remote)]
        return {"output": self._run_adb(args, timeout)}

    def adb_pull(self, serial: str, remote: str, local: str,
                 timeout: float = 120.0):
        args = []
        if serial:
            args += ["-s", str(serial)]
        args += ["pull", str(remote), str(local)]
        return {"output": self._run_adb(args, timeout)}
