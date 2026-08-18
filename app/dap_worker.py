# coding: utf-8
"""DAP-link RTT 工作线程：唯一持有 DapProbe 的地方。

流程：打开调试器 → SWD 连接 → （可选复位）→ 定位/解析 RTT 控制块 →
周期轮询 UP 通道上行数据；下行通过 queued slot 写入 DOWN 通道。
"""
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app import dap_core, dap_rtt
from app.dap_core import DapError

# MCP 只读查询用的每通道 RX 环形缓冲上限
RX_CAP = 65536

# RTT 轮询失败自动恢复（目标复位/瞬态故障）：
# - 每 _POLL_RECOVER_EVERY 次连续失败尝试一次 read_idcode 重新初始化
#   SWD（swd_activate + 清 STICKYERR + 重新调试电源上电），成功后无缝续传；
# - 连续失败达 _POLL_FAIL_LIMIT 次（约 3 秒）才判定目标断开，停止并报错。
_POLL_RECOVER_EVERY = 5
_POLL_FAIL_LIMIT = 60


class DapWorker(QObject):
    # ── worker → UI ────────────────────────────────────────────
    probeOpened = pyqtSignal(str)
    openFailed = pyqtSignal(str)
    probeClosed = pyqtSignal()
    connected = pyqtSignal(dict)        # {idcode, channels...}
    rttFound = pyqtSignal(dict)         # 控制块解析结果
    dataReceived = pyqtSignal(str, bytes, float)  # 通道名, 数据, ts
    dataWritten = pyqtSignal(str, int)
    errorOccurred = pyqtSignal(str)
    resetDone = pyqtSignal()
    resetWarned = pyqtSignal(str)   # 复位部分失败/异常提示（如无 RESET 线）
    finished = pyqtSignal()
    mcpReply = pyqtSignal(dict)         # MCP 只读查询应答 {op, data|error}

    def __init__(self):
        super().__init__()
        self._probe = dap_core.DapProbe()
        self._target = None
        self._rtt = None
        self._rttActive = False
        self._pollMs = 50
        self._quit = threading.Event()
        self._rxBufs = {}   # 通道名 → bytearray
        self._pollFails = 0  # 连续轮询失败计数（自动重连用）

    # ── UI → worker ────────────────────────────────────────────

    @pyqtSlot(dict)
    def requestOpen(self, cfg: dict):
        """cfg: {path, clock, ram_start, ram_size, cb_addr, reset, kernel}"""
        if self._probe.opened:
            self.openFailed.emit("调试器已打开")
            return
        cfg = dict(cfg or {})
        try:
            self._probe.open(cfg.get("path") or b"")
            port = self._probe.connect(dap_core.DAP_PORT_SWD)
            clock = int(cfg.get("clock") or 1_000_000)
            self._probe.set_clock(clock)
            self._probe.transfer_configure()
            target = dap_core.SwdTarget(self._probe)
            idcode = target.read_idcode()
            if cfg.get("reset"):
                # 连接时复位：双重复位（软复位 AIRCR + 硬复位 nRESET），
                # 与复位按钮共用 _do_reset；失败静默忽略（无 RESET 线/
                # 软复位不可用时仍继续连接），复位后重读 IDCODE
                self._do_reset(self._probe, target)
                try:
                    time.sleep(0.1)
                    idcode = target.read_idcode()
                except DapError:
                    pass  # 目标 boot 中读不到 IDCODE 时继续，稍后 RTT 扫描重试
            self._target = target
        except DapError as e:
            self._cleanup_probe()
            self.openFailed.emit(str(e))
            return
        self.probeOpened.emit(f"IDCODE={idcode:#010x}")
        # RTT 控制块：指定地址优先，否则按芯片包/内核预设解析区间
        try:
            cb_addr = int(cfg.get("cb_addr") or 0)
            if not cb_addr:
                kernel = dap_rtt.get_kernel(cfg.get("kernel"))
                regions = self._resolve_regions(cfg, kernel)
                if regions is None:
                    # Cortex-A 无通用 RAM 布局（SEGGER 官方文档），不自动扫描
                    self.errorOccurred.emit(
                        "Cortex-A 无通用 RAM 布局：请手动指定控制块地址"
                        "或 RAM 区间（不自动扫描）")
                    return
                cb_addr = dap_rtt.find_control_block(
                    target, regions,
                    try_vtor=(kernel["family"] == "m"))
            if not cb_addr:
                self.errorOccurred.emit(
                    "未找到 RTT 控制块：确认固件已初始化 SEGGER RTT，"
                    "或手动指定 RAM 区间/控制块地址")
                return
            rtt = dap_rtt.parse_control_block(target, cb_addr)
        except DapError as e:
            self.errorOccurred.emit(f"RTT 控制块解析失败：{e}")
            return
        self._rtt = rtt
        self._rxBufs.clear()
        self.rttFound.emit(rtt)

    @staticmethod
    def _resolve_regions(cfg: dict, kernel: dict):
        """解析 RTT 扫描区间；Cortex-A 无区间时返回 None（须手动指定）。

        优先级：cfg.regions（芯片包/UI 传入）> cfg.ram_start/ram_size
                > 内核预设 ram_regions > 内置默认区间。
        """
        regions = cfg.get("regions")
        if isinstance(regions, list) and regions:
            norm = []
            for r in regions:
                if (isinstance(r, (list, tuple)) and len(r) == 2
                        and all(isinstance(v, int) for v in r)):
                    norm.append((int(r[0]), int(r[1])))
            if norm:
                return norm
        ram_start = int(cfg.get("ram_start") or 0)
        ram_size = int(cfg.get("ram_size") or 0)
        if ram_start and ram_size:
            return [(ram_start, ram_start + ram_size)]
        if kernel["family"] == "a":
            return None
        return kernel.get("ram_regions") or list(dap_rtt.DEFAULT_RAM_REGIONS)

    @pyqtSlot()
    def requestStartRtt(self):
        if self._rtt is not None:
            self._rttActive = True

    @pyqtSlot()
    def requestStopRtt(self):
        self._rttActive = False

    @pyqtSlot(str, bytes)
    def requestWrite(self, channel: str, data: bytes):
        """channel 为固定槽位编号 "0"~"15"；未配置通道拒绝写入。"""
        if self._rtt is None:
            self.errorOccurred.emit("RTT 未就绪")
            return
        try:
            idx = int(channel)
        except (TypeError, ValueError):
            self.errorOccurred.emit(f"通道编号无效：{channel}")
            return
        ch = dap_rtt.channel_by_index(self._rtt, "DOWN", idx)
        if ch is None:
            self.errorOccurred.emit(
                f"DOWN 通道 {idx} 未配置"
                f"（固件仅 {self._rtt['max_down']} 个下行通道）")
            return
        try:
            n = dap_rtt.write_channel(self._target, ch, bytes(data))
        except DapError as e:
            self.errorOccurred.emit(f"RTT 写入失败：{e}")
            return
        self.dataWritten.emit(str(idx), n)

    @pyqtSlot()
    def requestReset(self):
        """双重复位目标：软复位（AIRCR SYSRESETREQ）+ 硬复位（nRESET）。

        软复位：经调试口写 AIRCR（0xE000ED0C = 0x05FA0004 SYSRESETREQ）
        触发系统复位，不依赖 RESET 线；写前先设 DHCSR C_DEBUGEN 使能
        调试，避免复位后调试访问失效。
        硬复位：nRESET 引脚拉低再释放（需连接 RESET 线），完整复位
        包括调试电源域。
        芯片复位重启会丢失调试电源上电状态：不清 STICKYERR/不重新
        power_up 的话，复位后 AP 访问立即 FAULT（实测报
        "AP 写 0x0 ACK=0x4"）。故复位后必须 read_idcode() 重新初始化
        （swd_activate + 清 ABORT + power_up），并重试覆盖芯片 boot 时间。
        两种复位任一成功即可——无 RESET 线时软复位生效，软复位不可用
        时硬复位兜底。复位是否真的发生用 DHCSR.S_RESET_ST（bit25，
        复位后置位）验证，避免"目标本来就连着"时误报复位成功。
        """
        if not self._probe.opened or self._target is None:
            self.errorOccurred.emit("调试器未连接")
            return
        # 复位前恢复 AP 访问：轮询期间 RTT 读瞬时失败会残留 STICKYERR，
        # 不清除则复位时首次 AP 写即 FAULT（AP 写 ACK=0x4 连锁失败）。
        # 只清 STICKYERR + 确认调试电源上电，不重做 swd_activate
        # （避免打断已建立的 SWD 会话）
        try:
            self._target.dp_write(0x00, dap_core._ABORT_CLEAR_STICKY)
            self._target.power_up()
            self._target.dp_write(0x00, dap_core._ABORT_CLEAR_STICKY)
        except DapError as e:
            self.errorOccurred.emit(f"复位前恢复 AP 访问失败：{e}")
            return
        errs, warns = self._do_reset(self._probe, self._target)
        # 重新初始化 SWD（复位会丢失调试电源上电状态），重试覆盖 boot 时间
        last_err = None
        for i in range(4):
            time.sleep(0.15 if i else 0.1)
            try:
                self._target.read_idcode()
                break
            except DapError as e:
                last_err = e
        else:
            detail = "；".join(errs + warns) if (errs or warns) \
                else "SWD 重连失败"
            if last_err is not None:
                detail += f"；SWD 重连失败：{last_err}"
            self.errorOccurred.emit(f"硬件复位失败：{detail}")
            return
        # 复位有硬失败时验证是否真的发生：读 DHCSR 的 S_RESET_ST（bit25），
        # 复位后应置位（_do_reset 已先清）。未置位说明目标没复位——
        # 避免"目标本来就连着、软硬复位都失败"时误报"已复位"。
        # 读不到（AP 访问异常）时按恢复模式恢复后重读；仍失败则如实
        # 报错（复位有硬失败 + 无法确认 = 大概率没复位），不静默通过。
        if errs:
            dhcsr = None
            try:
                dhcsr = self._target.read_mem32(0xE000EDF0)
            except DapError:
                try:
                    self._target.dp_write(
                        0x00, dap_core._ABORT_CLEAR_STICKY)
                    self._target.power_up()
                    self._target.dp_write(
                        0x00, dap_core._ABORT_CLEAR_STICKY)
                    dhcsr = self._target.read_mem32(0xE000EDF0)
                except DapError:
                    dhcsr = None
            if dhcsr is None:
                self.errorOccurred.emit(
                    "硬件复位失败：" + "；".join(errs + warns)
                    + "（复位后无法读取 DHCSR，未能确认复位）")
                return
            if not (dhcsr & (1 << 25)):
                self.errorOccurred.emit(
                    "硬件复位失败："
                    + "；".join(errs + warns) + "（目标未检测到复位）")
                return
        # 复位部分失败/写入异常提示（不阻断成功状态）
        for msg in errs + warns:
            self.resetWarned.emit(msg)
        # 复位期间可能残留的轮询失败计数清零，避免误判断开
        self._pollFails = 0
        self.resetDone.emit()

    @staticmethod
    def _do_reset(probe, target) -> tuple:
        """执行复位：halt（尽力而为）→ 软复位（AIRCR SYSRESETREQ）→ 硬复位。

        返回 (errs, warns)：
        - errs：硬失败列表（复位确定未执行，目前仅硬复位失败——无 RESET
          线时 nRESET 拉不动；软复位写入异常按 warns，因触发瞬间目标
          复位导致传输响应丢失属正常）；
        - warns：提示列表（halt 失败、AIRCR 写入异常等，均不阻断）。
        连接时复位与复位按钮共用此逻辑。

        软复位标准流程（OpenOCD/ST-Link 同款）：
        1) halt 目标（尽力而为，失败不阻断软复位）：写 DHCSR =
           C_DEBUGEN|C_HALT 并读回确认 S_HALT。STM32F1 的 PPB 调试
           寄存器（DHCSR/AIRCR 0xE000EDF0 区域）须特权 AHB-AP 访问
           （CSW.HPROT=0x23000052；真机实测 SPROT bit8 被目标忽略），
           非特权访问被目标拒绝（AP 写 0xC ACK=0x4）。失败则恢复 AP
           后重试（清 STICKYERR + power_up），4 次后仍无法 halt 仅
           提示——软复位不依赖 halt，继续尝试 AIRCR。
        2) halt 成功时清 S_RESET_ST（DHCSR bit25 写 1 清除）供复位后
           验证；DBGKEY 必须保持 0xA05F，值 = 0xA05F0001 | (1<<25)。
        3) 写 AIRCR = SYSRESETREQ 触发系统复位（不依赖 RESET 线）；
           触发瞬间目标复位导致传输响应丢失/FAULT 属正常，仅提示。
        4) 硬复位兜底：nRESET 引脚拉低再释放（需接 RESET 线）。
        """
        errs, warns = [], []

        def _recover():
            """清 STICKYERR + 重新请求调试电源上电（幂等），恢复 AP 访问。"""
            try:
                target.dp_write(0x00, dap_core._ABORT_CLEAR_STICKY)
                target.power_up()
                target.dp_write(0x00, dap_core._ABORT_CLEAR_STICKY)
            except DapError:
                pass

        # 1) halt 目标（尽力而为：C_DEBUGEN|C_HALT = 0xA05F0003）
        halted = False
        for _ in range(4):
            try:
                target.write_mem32(0xE000EDF0, 0xA05F0003)
            except DapError:
                _recover()
                time.sleep(0.05)
                continue
            try:
                if target.read_mem32(0xE000EDF0) & (1 << 17):  # S_HALT
                    halted = True
                    break
            except DapError:
                pass
            time.sleep(0.05)
        if not halted:
            # 软复位不依赖 halt：PPB 访问被拒时 halt 必然失败，但 AIRCR
            # 写入仍要尝试（同为 PPB 访问，失败仅提示，不阻断）
            warns.append("无法 halt 目标（PPB 调试访问被拒或目标不可调试）"
                         "，已直接尝试软复位")
        else:
            # 2) 清 S_RESET_ST（bit25 写 1 清除）供复位后验证；
            #    DBGKEY 必须保持 0xA05F，值 = 0xA05F0001 | (1<<25)
            try:
                target.write_mem32(0xE000EDF0, 0xA25F0001)
            except DapError:
                pass   # 清不了不强求（验证阶段按实际值判断）
        # 3) AIRCR SYSRESETREQ：触发瞬间目标复位，传输响应可能丢失/
        #    FAULT——复位大概率已触发，写入异常仅提示不判失败
        try:
            target.write_mem32(0xE000ED0C, 0x05FA0004)
            time.sleep(0.05)
        except DapError as e:
            warns.append(f"软复位写入异常（复位可能已触发）：{e}")
            time.sleep(0.05)
        # 3.5) 解除 halt 兜底：Cortex-M3 在 debug halt 状态下 SYSRESETREQ
        #      被挂起（pending），直到 unhalt 才真正执行复位。若 halt
        #      成功但第 2 步清 S_RESET_ST 写失败（目标仍 halt），必须
        #      显式 unhalt（0xA05F0001 = DBGKEY|C_DEBUGEN，清 C_HALT）
        #      触发挂起的复位；目标已在运行则此写幂等无害，复位瞬间
        #      写失败也忽略（复位已触发）。
        if halted:
            try:
                target.write_mem32(0xE000EDF0, 0xA05F0001)
                time.sleep(0.05)
            except DapError:
                pass
        # 4) 硬复位：nRESET 引脚拉低再释放（无 RESET 线时仅软复位生效）
        try:
            probe.reset_target()
        except DapError as e:
            errs.append(f"硬复位失败：{e}")
        return errs, warns

    @pyqtSlot()
    def requestClose(self):
        self._rttActive = False
        self._rtt = None
        self._target = None
        self._cleanup_probe()
        self.probeClosed.emit()

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    # ── MCP 只读查询（sigMcpQuery → mcpReply）─────────────────

    @pyqtSlot(dict)
    def requestMcpQuery(self, q: dict):
        """q: {op:'snapshot'} 或 {op:'rx', channel, n}；只读。"""
        op = str(q.get("op", "snapshot"))
        rid = q.get("id")
        if op == "snapshot":
            self.mcpReply.emit({"op": op, "id": rid,
                                "data": self._snapshot()})
        elif op == "rx":
            self.mcpReply.emit({
                "op": op, "id": rid,
                "data": self._recentRx(str(q.get("channel", "")),
                                       int(q.get("n", 0)))})
        else:
            self.mcpReply.emit({"op": op, "id": rid,
                                "error": f"未知查询 {op}"})

    def _snapshot(self):
        """调试器/RTT 状态。"""
        info = {"opened": self._probe.opened,
                "rtt_active": self._rttActive,
                "channels": []}
        if self._rtt:
            info["cb_addr"] = self._rtt.get("addr", 0)
            info["channels"] = [
                {"index": c["index"], "name": c["name"],
                 "direction": c["direction"]}
                for c in self._rtt["channels"]]
        return info

    def _recentRx(self, channel_name: str, n: int) -> bytes:
        """指定 UP 通道最近 n 字节（n<=0 返回全部缓冲）。"""
        buf = self._rxBufs.get(channel_name, bytearray())
        if n <= 0:
            return bytes(buf)
        return bytes(buf[-n:])

    def _cleanup_probe(self):
        try:
            if self._probe.opened:
                self._probe.close()
        except Exception:
            pass

    # ── 轮询循环（worker 线程）────────────────────────────────

    def run(self):
        from PyQt6.QtCore import QCoreApplication

        while not self._quit.is_set():
            QCoreApplication.processEvents()
            if self._quit.is_set():
                break
            if self._rttActive and self._rtt is not None:
                self._poll_once()
                # 轮询节流：原实现无 sleep（_pollMs 定义了却从未使用），
                # RTT 激活后循环紧转占满一个 CPU 核
                time.sleep(self._pollMs / 1000.0)
            else:
                time.sleep(0.02)
        self._cleanup_probe()
        self.finished.emit()

    def _poll_once(self):
        try:
            for ch in self._rtt["channels"]:
                if ch["direction"] != "UP":
                    continue
                data = dap_rtt.read_channel(self._target, ch)
                if data:
                    key = str(ch["index"])   # 通道编号作标识
                    buf = self._rxBufs.setdefault(key, bytearray())
                    buf.extend(data)
                    if len(buf) > RX_CAP:
                        del buf[:len(buf) - RX_CAP]
                    self.dataReceived.emit(key, data, time.time())
        except DapError as e:
            self._pollFails += 1
            # 每 N 次失败尝试重连 SWD：覆盖目标复位瞬间/瞬态故障，
            # 避免一次失败就永久停止 RTT（须重启软件才能恢复）
            if self._pollFails % _POLL_RECOVER_EVERY == 0:
                try:
                    self._target.read_idcode()
                    self._pollFails = 0   # 重连成功，无缝续传
                    return
                except DapError:
                    pass
            if self._pollFails >= _POLL_FAIL_LIMIT:
                self._rttActive = False
                self._pollFails = 0
                self.errorOccurred.emit(f"RTT 轮询失败（目标断开？）：{e}")


class _DapWorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class DapThread(QObject):
    """QThread 启停辅助，用法与 SerialThread / HidThread 一致。"""

    sigOpen = pyqtSignal(dict)
    sigClose = pyqtSignal()
    sigStartRtt = pyqtSignal()
    sigStopRtt = pyqtSignal()
    sigWrite = pyqtSignal(str, bytes)
    sigReset = pyqtSignal()
    sigMcpQuery = pyqtSignal(dict)      # MCP 只读查询请求

    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = DapWorker()
        self.thread = _DapWorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)

        queued = Qt.ConnectionType.QueuedConnection
        self.sigOpen.connect(self.worker.requestOpen, queued)
        self.sigClose.connect(self.worker.requestClose, queued)
        self.sigStartRtt.connect(self.worker.requestStartRtt, queued)
        self.sigStopRtt.connect(self.worker.requestStopRtt, queued)
        self.sigWrite.connect(self.worker.requestWrite, queued)
        self.sigReset.connect(self.worker.requestReset, queued)
        self.sigMcpQuery.connect(self.worker.requestMcpQuery, queued)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 1500):
        self.worker.requestQuit()
        if not self.thread.wait(timeout_ms):
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
