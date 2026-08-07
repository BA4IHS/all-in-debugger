# 临时验证脚本：用项目 dap_core 完整链路测试所有调试器（跑完即删）
import sys
sys.path.insert(0, '.')
from app import dap_core

probes = dap_core.enum_probes(verify=True)
print(f"在线验证后共 {len(probes)} 个调试器")
for info in probes:
    print("=" * 60)
    print(f"[{info['transport']}] {info.get('product')}  path={info['path']!r}")
    p = dap_core.DapProbe()
    try:
        p.open(info["path"])
        print(f"  打开成功，packet_size={p.packet_size}")
        port = p.connect(dap_core.DAP_PORT_SWD)
        print(f"  DAP_Connect 成功，端口={port}")
        p.set_clock(1000000)
        t = dap_core.SwdTarget(p)
        idcode = t.read_idcode()
        print(f"  IDCODE = 0x{idcode:08X}")
        ctrl = t.dp_read(0x04)
        print(f"  CTRL/STAT = 0x{ctrl:08X}")
        p.close()
        print("  关闭成功")
    except Exception as e:
        print(f"  失败：{e}")
        try:
            p.close()
        except Exception:
            pass
