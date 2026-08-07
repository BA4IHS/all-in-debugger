# 临时诊断：逐命令 dump 原始响应字节（跑完即删）
import sys
sys.path.insert(0, '.')
from app import hid_binding, winusb_binding, dap_core

def hx(b):
    return " ".join(f"{x:02X}" for x in b) if b else "(空)"

probes = dap_core.enum_probes(verify=True)
hid_path = next((p["path"] for p in probes if p["transport"] == "hid"), None)
wu_path = next((p["path"] for p in probes if p["transport"] == "winusb"), None)

print("=" * 60)
print("HID 路径原始字节测试")
if hid_path:
    h = hid_binding.HidDevice()
    h.open_path(hid_path)
    # drain
    for _ in range(8):
        d = h.read(65, timeout_ms=50)
        if not d:
            break
        print("  drain:", hx(d[:16]))

    def hid_xchg(req, tag):
        payload = b"\x00" + req + b"\x00" * (64 - len(req))
        h.write(payload)
        d = h.read(65, timeout_ms=500)
        print(f"  {tag}: 发={hx(req)}  收({len(d) if d else 0})={hx(d[:20] if d else b'')}")
        return d

    hid_xchg(bytes([0x00, 0x04]), "Info FW版本")
    hid_xchg(bytes([0x02, 0x01]), "Connect SWD")
    hid_xchg(bytes([0x11]) + (1000000).to_bytes(4, "little"), "SWJ_Clock")
    hid_xchg(bytes([0x12, 64]) + b"\xFF" * 8, "SWJ_Sequence 复位")
    # DAP_Transfer: index=0, count=1, 读 DP IDCODE (req=0xA5: APnDP=0 RnW=1 A=0 偶校验)
    hid_xchg(bytes([0x05, 0x00, 0x01, 0xA5]), "Transfer 读IDCODE")
    h.close()
else:
    print("  无 HID 调试器")

print("=" * 60)
print("WinUSB 路径原始字节测试")
if wu_path:
    w = winusb_binding.WinUsbDevice()
    w.open_path(wu_path, timeout_ms=500)

    def wu_xchg(req, tag):
        payload = req + b"\x00" * (64 - len(req))
        w.write(payload)
        d = w.read(64)
        print(f"  {tag}: 发={hx(req)}  收({len(d) if d else 0})={hx(d[:20] if d else b'')}")
        return d

    wu_xchg(bytes([0x00, 0x04]), "Info FW版本")
    wu_xchg(bytes([0x02, 0x01]), "Connect SWD")
    wu_xchg(bytes([0x11]) + (1000000).to_bytes(4, "little"), "SWJ_Clock")
    wu_xchg(bytes([0x12, 64]) + b"\xFF" * 8, "SWJ_Sequence 复位")
    wu_xchg(bytes([0x05, 0x00, 0x01, 0xA5]), "Transfer 读IDCODE")
    # 再读一次 RDBUFF 取上一次读结果
    wu_xchg(bytes([0x05, 0x00, 0x01, 0x8F]), "Transfer 读RDBUFF")
    w.close()
else:
    print("  无 WinUSB 调试器")
