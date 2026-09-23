# coding: utf-8
"""CH347 工作线程：唯一持有 CH347DLL 设备索引（原生句柄）的地方。

请求-应答模式：UI / MCP 桥 emit Ch347Thread.sigRequest({op, id, ...})，
worker 线程内执行对应操作，结果经 sigOpResult({op, id, ok, data|error})
返回；长操作（Flash/LCD 传输）经 sigProgress({id, done, total, label})
汇报进度。所有 DLL 调用都被约束在本线程内（AGENTS 架构约定）。
"""
import logging
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot

from app import ch347_core as cc

log = logging.getLogger(__name__)


class Ch347Worker(QObject):
    """op 分发器：设备操作全部在此线程执行，异常收敛为 error 应答。"""

    sigOpResult = pyqtSignal(dict)      # {op, id, ok, data|error}
    sigProgress = pyqtSignal(dict)      # {id, done, total, label}
    sigOpened = pyqtSignal(dict)        # 连接成功（UI 便捷信号）
    sigClosed = pyqtSignal()            # 连接关闭（UI 便捷信号）
    finished = pyqtSignal()

    def __init__(self, device_factory=None):
        super().__init__()
        # device_factory 仅供测试注入 fake Ch347Device
        self._device_factory = device_factory
        self._dev = None
        self._quit = threading.Event()

    # ── 基础 ────────────────────────────────────────────────────

    @property
    def _device(self):
        return self._dev

    def _need_dev(self):
        if self._dev is None:
            raise cc.Ch347Error("设备未打开")
        return self._dev

    @pyqtSlot(dict)
    def request(self, req: dict):
        req = dict(req or {})
        op = str(req.get("op", ""))
        rid = req.get("id")
        handler = getattr(self, "_op_" + op, None) if op else None
        if handler is None:
            self.sigOpResult.emit({"op": op, "id": rid, "ok": False,
                                   "error": f"未知操作 {op or '(空)'}"})
            return
        try:
            data = handler(req)
            self.sigOpResult.emit({"op": op, "id": rid, "ok": True,
                                   "data": data if data is not None else {}})
        except cc.Ch347Error as e:
            self.sigOpResult.emit({"op": op, "id": rid, "ok": False,
                                   "error": str(e)})
        except ValueError as e:          # 参数校验错误
            self.sigOpResult.emit({"op": op, "id": rid, "ok": False,
                                   "error": str(e)})
        except Exception as e:           # noqa: BLE001 - 兜底出口
            log.warning("CH347 操作失败 op=%s：%s", op, e)
            self.sigOpResult.emit({"op": op, "id": rid, "ok": False,
                                   "error": f"{type(e).__name__}: {e}"})

    # ── 枚举 / 连接 ─────────────────────────────────────────────

    def _op_snapshot(self, req):
        data = {"opened": self._dev is not None,
                "library": cc.dll_info()}
        if self._dev is not None:
            data["index"] = self._dev.index
        return data

    def _op_scan(self, req):
        # 已打开设备时绝不再枚举：scan_devices 会对索引 0~15 逐个
        # Open/Close，若与当前已持有的索引重叠，等于对同一句柄重复
        # 打开/关闭，会破坏 CH347 驱动内部状态（底层是内核态驱动，
        # 异常时可导致蓝屏）。设备已打开时直接返回当前设备信息即可，
        # 完全不触碰驱动。
        if self._dev is not None:
            try:
                info = self._dev.info()
            except Exception:  # noqa: BLE001 - 枚举失败不应影响已打开设备
                info = {"index": self._dev.index}
            return {"devices": [info], "library": cc.dll_info()}
        return {"devices": cc.scan_devices(), "library": cc.dll_info()}

    def _op_open(self, req):
        if self._dev is not None:
            raise cc.Ch347Error("设备已打开，请先关闭")
        index = int(req.get("index", 0))
        if self._device_factory is not None:
            dev = self._device_factory(index)
        else:
            dev = cc.Ch347Device(index)
        self._dev = dev
        info = dev.info()
        log.info("CH347 已打开：index=%s", index)
        self.sigOpened.emit(info)
        return info

    def _op_close(self, req):
        if self._dev is not None:
            self._dev.close()
            self._dev = None
            log.info("CH347 已关闭")
            self.sigClosed.emit()
        return {}

    # ── SPI ─────────────────────────────────────────────────────

    def _op_spi_init(self, req):
        d = self._need_dev()
        d.spi_init(
            mode=int(req.get("mode", 0)), clk=int(req.get("clk", 0)),
            msb_first=bool(req.get("msb_first", True)),
            cs_index=int(req.get("cs_index", 0)),
            cs1_polarity=int(req.get("cs1_polarity", 0)),
            cs2_polarity=int(req.get("cs2_polarity", 0)),
            cs_enable=bool(req.get("cs_enable", True)),
            auto_deactive_cs=bool(req.get("auto_deactive_cs", True)),
            active_delay_us=int(req.get("active_delay_us", 0)),
            delay_deactive_us=int(req.get("delay_deactive_us", 0)),
            interval_us=int(req.get("interval_us", 0)),
            out_default=int(req.get("out_default", 0xFF)),
            data_bits=int(req.get("data_bits", 0)),
            frequency_hz=int(req.get("frequency_hz", 0)))
        return {}

    def _op_spi_xfer(self, req):
        d = self._need_dev()
        tx = cc.parse_hex(req.get("tx_hex", "")) \
            if str(req.get("tx_hex", "")).strip() else b""
        rx_len = int(req.get("rx_len", 0))
        if not tx and rx_len <= 0:
            raise ValueError("发送数据与读取长度不能同时为空")
        rx = d.spi_xfer(tx, rx_len)
        return {"tx_len": len(tx), "rx_len": len(rx), "rx_hex": rx.hex()}

    # ── I2C ─────────────────────────────────────────────────────

    def _op_i2c_init(self, req):
        d = self._need_dev()
        d.i2c_init(speed=int(req.get("speed", 1)),
                   stretch=bool(req.get("stretch", False)),
                   delay_ms=int(req.get("delay_ms", 0)))
        return {}

    def _op_i2c_xfer(self, req):
        d = self._need_dev()
        d.ensure_i2c_ready()      # 原始传输同样需先初始化 I2C
        write = cc.parse_hex(req.get("write_hex", ""))
        if not write:
            raise ValueError("写入数据为空（首字节须为 8bit 设备地址）")
        data, ack = d.i2c_xfer(write, int(req.get("read_len", 0)))
        return {"read_hex": data.hex(), "ack_miss": ack}

    def _op_i2c_scan(self, req):
        d = self._need_dev()
        rid = req.get("id")

        def prog(done, total):
            self.sigProgress.emit({"id": rid, "done": done,
                                   "total": total, "label": "I2C 地址扫描"})

        # 扫描前确保接口已初始化（否则总线事务全部立即失败，
        # 表现为“秒扫完但一个器件都没有”）
        addrs = d.i2c_scan(progress=prog)
        return {"addrs": [f"0x{a:02X}" for a in addrs]}

    def _op_i2c_reg_read(self, req):
        """读 count 个寄存器：每个寄存器地址读 width 字节（Repeated Start）。"""
        d = self._need_dev()
        d.ensure_i2c_ready()      # 寄存器工具同样需先初始化 I2C
        addr = int(req["addr"]) & 0x7F
        reg = int(req.get("reg", 0))
        reg_wide = int(req.get("reg_wide", 8))
        width = int(req.get("width", 8))
        count = max(1, min(256, int(req.get("count", 1))))
        if reg_wide not in (8, 16) or width not in (8, 16):
            raise ValueError("寄存器/数据宽度只支持 8/16 位")
        rows = []
        for i in range(count):
            a = reg + i
            head = bytes([addr << 1])
            if reg_wide == 16:
                head += bytes([(a >> 8) & 0xFF, a & 0xFF])
            else:
                head += bytes([a & 0xFF])
            data, ack = d.i2c_xfer(head, width // 8)
            rows.append({"reg": a, "hex": data.hex(), "ack": ack == 0})
        return {"rows": rows}

    def _op_i2c_reg_write(self, req):
        d = self._need_dev()
        d.ensure_i2c_ready()      # 寄存器工具同样需先初始化 I2C
        addr = int(req["addr"]) & 0x7F
        reg = int(req.get("reg", 0))
        reg_wide = int(req.get("reg_wide", 8))
        value = cc.parse_hex(req.get("value_hex", ""))
        if not value or len(value) > 8:
            raise ValueError("写入值须为 1~8 字节")
        head = bytes([addr << 1])
        if reg_wide == 16:
            head += bytes([(reg >> 8) & 0xFF, reg & 0xFF])
        else:
            head += bytes([reg & 0xFF])
        _, ack = d.i2c_xfer(head + value, 0)
        if ack:
            raise cc.Ch347Error(f"设备 0x{addr:02X} 未应答（NACK）")
        return {}

    def _op_i2c_script(self, req):
        steps = [cc.I2cScriptStep.from_dict(s) for s in req.get("steps", [])]
        if not steps:
            raise ValueError("脚本为空")
        for st in steps:
            if not st.write_bytes and st.read_len:
                raise ValueError("读步的 write_hex 至少须含设备读地址字节")
        loop = max(1, min(1000, int(req.get("loop", 1))))
        rid = req.get("id")
        d = self._need_dev()
        d.ensure_i2c_ready()      # 脚本执行同样需先初始化 I2C
        reads = []
        done = 0
        total = len(steps) * loop
        for _ in range(loop):
            for st in steps:
                data, _ack = d.i2c_xfer(st.write_bytes, st.read_len)
                if st.read_len:
                    reads.append(data.hex())
                if st.delay_ms:
                    time.sleep(st.delay_ms / 1000.0)
                done += 1
                self.sigProgress.emit({"id": rid, "done": done,
                                       "total": total, "label": "I2C 脚本"})
        return {"ran_steps": done, "reads": reads[:64]}

    # ── GPIO ────────────────────────────────────────────────────

    def _op_gpio_get(self, req):
        d = self._need_dev()
        dr, data = d.gpio_get()
        return {"dir": dr, "data": data}

    def _op_gpio_set(self, req):
        d = self._need_dev()
        d.gpio_set(int(req.get("enable", 0)), int(req.get("dir", 0)),
                   int(req.get("data", 0)))
        return {}

    def _op_gpio_macro(self, req):
        steps = [cc.GpioMacroStep.from_dict(s) for s in req.get("steps", [])]
        if not steps:
            raise ValueError("宏为空")
        d = self._need_dev()
        for st in steps:
            d.gpio_write_pin(st.pin, st.level)
            if st.delay_ms:
                time.sleep(st.delay_ms / 1000.0)
        return {"ran": len(steps)}

    # ── SPI Flash ───────────────────────────────────────────────

    def _prog(self, rid, label):
        def cb(done, total):
            self.sigProgress.emit({"id": rid, "done": done, "total": total,
                                   "label": label})
        return cb

    def _op_flash_identify(self, req):
        return self._need_dev().flash_identify()

    def _op_flash_status(self, req):
        sr = self._need_dev().flash_status()
        return {"sr": sr, "busy": bool(sr & 0x01), "wel": bool(sr & 0x02)}

    def _op_flash_write_status(self, req):
        d = self._need_dev()
        d.flash_cmd([cc.CMD_FLASH_EWSR])
        d.flash_cmd([cc.CMD_FLASH_WRSR, int(req.get("value", 0)) & 0xFF])
        d.flash_wait_busy(2000)
        return {}

    def _op_flash_read(self, req):
        d = self._need_dev()
        addr = int(req.get("addr", 0))
        length = int(req.get("len", 0))
        file = str(req.get("file") or "").strip()
        if not file and length > 65536:
            raise ValueError("界面读取上限 64KB，请改用“保存到文件”")
        data = d.flash_read(addr, length, fast=bool(req.get("fast", False)),
                            progress=self._prog(req.get("id"), "Flash 读"))
        if file:
            with open(file, "wb") as f:
                f.write(bytes(data))
            return {"file": file, "length": len(data)}
        return {"hex": bytes(data).hex(), "length": len(data)}

    def _op_flash_erase(self, req):
        d = self._need_dev()
        gran = int(req.get("gran", 4096))
        n = d.flash_erase(int(req.get("addr", 0)), int(req.get("len", 1)),
                          gran, progress=self._prog(req.get("id"),
                                                    "Flash 擦除"))
        return {"blocks": n}

    def _op_flash_write(self, req):
        d = self._need_dev()
        addr = int(req.get("addr", 0))
        file = str(req.get("file") or "").strip()
        if file:
            with open(file, "rb") as f:
                data = f.read(cc.FLASH_CAP_LIMIT + 1)
            if len(data) > cc.FLASH_CAP_LIMIT:
                raise ValueError("固件文件超过 32MB 上限")
        else:
            data = cc.parse_hex(req.get("data_hex", ""))
        n = d.flash_write(addr, data,
                          progress=self._prog(req.get("id"), "Flash 写"))
        return {"written": n}

    def _op_flash_blank(self, req):
        d = self._need_dev()
        ok, first = d.flash_blank_check(int(req.get("addr", 0)),
                                        int(req.get("len", 0)),
                                        progress=self._prog(req.get("id"),
                                                            "空白校验"))
        return {"blank": ok, "first": first}

    def _op_flash_verify(self, req):
        d = self._need_dev()
        path = str(req.get("file") or "").strip()
        if not path:
            raise ValueError("请选择待校验文件")
        with open(path, "rb") as f:
            data = f.read(cc.FLASH_CAP_LIMIT + 1)
        if len(data) > cc.FLASH_CAP_LIMIT:
            raise ValueError("文件超过 32MB 上限")
        rid = req.get("id")
        addr = int(req.get("addr", 0))
        pos = 0
        while pos < len(data):
            n = min(4096, len(data) - pos)
            chunk = d.flash_read(addr + pos, n)
            for i in range(n):
                if chunk[i] != data[pos + i]:
                    return {"ok": False, "first_diff": addr + pos + i}
            pos += n
            self.sigProgress.emit({"id": rid, "done": pos,
                                   "total": len(data), "label": "Flash 校验"})
        return {"ok": True, "first_diff": None, "length": len(data)}

    # ── EEPROM ──────────────────────────────────────────────────

    def _op_eeprom_read(self, req):
        d = self._need_dev()
        data = d.eeprom_read(str(req.get("model", "24C02")),
                             int(req.get("addr", 0)), int(req.get("len", 16)))
        return {"hex": data.hex(), "length": len(data)}

    def _op_eeprom_write(self, req):
        d = self._need_dev()
        n = d.eeprom_write(str(req.get("model", "24C02")),
                           int(req.get("addr", 0)),
                           cc.parse_hex(req.get("data_hex", "")))
        return {"written": n}

    # ── SPI 屏幕（LCD）──────────────────────────────────────────

    def _lcd_send(self, d, prof, cmd, payload=b""):
        """4 线 SPI：DC=0 发 1 字节命令，DC=1 发数据（分块 ≤4096）。"""
        d.gpio_write_pin(prof.dc_pin, 0)
        d.spi_xfer(bytes([cmd]))
        if payload:
            d.gpio_write_pin(prof.dc_pin, 1)
            pos = 0
            while pos < len(payload):
                n = min(cc.MAX_STREAM, len(payload) - pos)
                d.spi_xfer(payload[pos:pos + n])
                pos += n

    def _lcd_window(self, d, prof, x, y, w, h):
        for cmd, a, b in ((cc.LCD_CMD_CASET, x, x + w - 1),
                          (cc.LCD_CMD_RASET, y, y + h - 1)):
            self._lcd_send(d, prof, cmd,
                           bytes([(a >> 8) & 0xFF, a & 0xFF,
                                  (b >> 8) & 0xFF, b & 0xFF]))

    def _op_lcd_init(self, req):
        prof = cc.LcdProfile.from_dict(req.get("profile") or {})
        d = self._need_dev()
        d.spi_init(mode=prof.spi_mode, clk=prof.spi_clk, msb_first=True,
                   cs_enable=True, auto_deactive_cs=True)
        if prof.blk_pin >= 0:
            d.gpio_write_pin(prof.blk_pin, 1)
        if prof.res_pin >= 0:
            d.gpio_write_pin(prof.res_pin, 0)
            time.sleep(0.01)
            d.gpio_write_pin(prof.res_pin, 1)
            time.sleep(0.12)
        for st in prof.steps:
            if st.kind == "cmd":
                self._lcd_send(d, prof, st.payload[0])
            elif st.kind == "data":
                d.gpio_write_pin(prof.dc_pin, 1)
                d.spi_xfer(st.payload)
            else:
                time.sleep(st.ms / 1000.0)
        # 以 profile 的几何/色序开关为准，覆盖模板中的 MADCTL
        self._lcd_send(d, prof, cc.LCD_CMD_MADCTL, bytes([prof.madctl]))
        return {"steps": len(prof.steps)}

    def _op_lcd_fill(self, req):
        prof = cc.LcdProfile.from_dict(req.get("profile") or {})
        d = self._need_dev()
        x, y = int(req.get("x", 0)), int(req.get("y", 0))
        w = int(req.get("w", prof.width)) or prof.width
        h = int(req.get("h", prof.height)) or prof.height
        color = int(req.get("color565", 0)) & 0xFFFF
        rid = req.get("id")
        self._lcd_window(d, prof, x, y, w, h)
        self._lcd_send(d, prof, cc.LCD_CMD_RAMWR)
        d.gpio_write_pin(prof.dc_pin, 1)
        px = bytes([color >> 8, color & 0xFF])
        line = px * w
        total = w * h
        done = 0
        for _ in range(h):
            d.spi_xfer(line)
            done += w
            self.sigProgress.emit({"id": rid, "done": done, "total": total,
                                   "label": "屏幕填充"})
        return {"pixels": total}

    def _op_lcd_image(self, req):
        from PyQt6.QtGui import QImage
        prof = cc.LcdProfile.from_dict(req.get("profile") or {})
        d = self._need_dev()
        x, y = int(req.get("x", 0)), int(req.get("y", 0))
        path = str(req.get("file") or "").strip()
        if not path:
            raise ValueError("请选择图片文件")
        img = QImage(path)
        if img.isNull():
            raise ValueError(f"无法读取图片：{path}")
        w = int(req.get("w") or prof.width)
        h = int(req.get("h") or prof.height)
        img = img.scaled(w, h).convertedToFormat(QImage.Format.Format_RGB888)
        # RGB888 按行 4 字节对齐，须用 bytesPerLine 逐行去填充
        bpl = img.bytesPerLine()
        mv = img.constBits()
        rows = [bytes(mv[r * bpl:r * bpl + w * 3]) for r in range(h)]
        data = cc.rgb888_to_rgb565(b"".join(rows),
                                   swap=bool(req.get("swap", False)))
        rid = req.get("id")
        self._lcd_window(d, prof, x, y, w, h)
        self._lcd_send(d, prof, cc.LCD_CMD_RAMWR)
        d.gpio_write_pin(prof.dc_pin, 1)
        pos = 0
        while pos < len(data):
            n = min(cc.MAX_STREAM // 2 * 2, len(data) - pos)
            d.spi_xfer(data[pos:pos + n])
            pos += n
            self.sigProgress.emit({"id": rid, "done": pos,
                                   "total": len(data), "label": "图片发送"})
        return {"bytes": len(data)}

    # ── 退出 ────────────────────────────────────────────────────

    @pyqtSlot()
    def requestQuit(self):
        self._quit.set()

    def run(self):
        from PyQt6.QtCore import QCoreApplication
        while not self._quit.is_set():
            QCoreApplication.processEvents()
            time.sleep(0.02)
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:         # noqa: BLE001
                pass
            self._dev = None
        self.finished.emit()


class _Ch347WorkerThread(QThread):

    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self._worker = worker

    def run(self):
        self._worker.run()


class Ch347Thread(QObject):
    """QThread 启停辅助，用法与 SshThread 一致。"""

    sigRequest = pyqtSignal(dict)

    def __init__(self, parent=None, device_factory=None):
        super().__init__(parent)
        self.worker = Ch347Worker(device_factory=device_factory)
        self.thread = _Ch347WorkerThread(self.worker, self)
        self.worker.moveToThread(self.thread)
        self.sigRequest.connect(self.worker.request,
                                Qt.ConnectionType.QueuedConnection)

    def start(self):
        self.thread.start()

    def stop(self, timeout_ms: int = 3000):
        self.worker.requestQuit()
        if not self.thread.wait(timeout_ms):
            self.thread.wait(500)

    @property
    def isRunning(self) -> bool:
        return self.thread.isRunning()
