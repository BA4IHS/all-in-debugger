# coding: utf-8
"""Nuitka 打包脚本（Windows x64 / Win10-11）。

用法：
    python build.py            # 完整打包（standalone + 压缩归档）
    python build.py --dry-run  # 只打印将要执行的命令，不实际编译
    python build.py --no-archive  # 只编译，不压缩

产物：
    dist/main.dist/                      standalone 目录（可直接运行）
    dist/all-in-debugger-x64-<版本>.7z  压缩归档（无 7z 时回退 .zip）

安全与体积：
    - config.json / data.json 绝不打包（含 MCP 密钥）：Nuitka 只打包
      --include-data-* 指定的文件，编译后另有防御检查兜底
    - 无终端模式：--windows-console-mode=disable（不弹控制台），
      编译后自动校验 exe 的 PE 子系统为 WINDOWS_GUI（Subsystem=2）
    - 体积优化：--lto=yes + 排除 Qt tls 插件/翻译 + 裁剪无用图片格式插件

前置要求：
    - Python 3.10+（本机 C:\\Users\\admin\\AppData\\Local\\Programs\\Python\\Python310）
    - C 编译器：MSVC（Visual Studio Build Tools）或 MinGW64；
      Nuitka 未检测到编译器时会按 --assume-yes-for-downloads 自动下载 MinGW64
    - 首次打包需联网（下载插件/依赖）
"""
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 下经管道（如 Tee-Object）重定向 stdout 时，Python 会按 GBK 编码输出，
# 打印 ✓ 等非 GBK 字符会抛 UnicodeEncodeError 中断打包。强制 UTF-8 输出。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # 旧版 Python 无 reconfigure，忽略

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "main.py"
VERSION = "1.2.0"

# 输出目录名 = 入口模块名 + ".dist"（与既有 dist/main.dist 结构一致）
DIST_DIR = ROOT / "dist"
BUNDLE_DIR = DIST_DIR / "main.dist"
# 归档文件名带架构标识（x64），与发布包命名约定一致
ARCHIVE_BASE = DIST_DIR / f"all-in-debugger-x64-{VERSION}"


def build_args() -> list:
    """组装 Nuitka 命令行（参数名已对照本机 Nuitka 4.1.3 核实）。"""
    return [
        sys.executable, "-m", "nuitka",
        "--standalone",
        f"--output-dir={DIST_DIR}",
        "--output-filename=all-in-debugger.exe",
        # Qt6 平台插件（qwindows.dll 等），qfluentwidgets 依赖
        "--enable-plugin=pyqt6",
        # pydantic v2 的动态插件/序列化机制，必须整包跟随
        "--include-package=pydantic",
        # mcp 可选依赖分支较多（stdio 传输依赖 anyio/httpx 等），显式整包跟随
        "--include-package=mcp",
        # qfluentwidgets 的图标等资源文件（FluentIcon 渲染依赖）
        "--include-package-data=qfluentwidgets",
        # ---- 业务数据（frozen 模式下由 sys.executable / __file__ 定位）----
        # hidapi.dll + 官方 adb 三件套（app/native.py 的 LIBS_DIR）
        "--include-data-dir=app/libs=app/libs",
        # 侧栏 Android 图标（app/ui/main_window.py 的 ANDROID_ICON_PATH）
        "--include-data-dir=app/assets=app/assets",
        # 型号指令集 profile（app/adb_runner.py 的 list_profiles）
        "--include-data-dir=app/adb_profiles=app/adb_profiles",
        # HID 发送模板（app/ui/hid_page.py 的 TEMPLATE_FILE）
        "--include-data-files=app/hid_templates.json=app/hid_templates.json",
        # ---- 排除项 ----
        "--nofollow-import-to=tests",
        # qfluentwidgets/common/image_utils.py 的 Acrylic 窗口/主色提取功能
        # （numpy/scipy/PIL/colorthief 幽灵依赖）：应用从未使用，运行时 try/except
        # 自动降级，与现状行为一致，省 ~117.6MB。已核实库内仅此一处引用。
        "--nofollow-import-to=numpy,scipy,PIL,colorthief",
        # ---- Windows 形态与版本信息 ----
        "--windows-console-mode=disable",   # GUI 程序，不弹控制台（编译后另有 PE 子系统校验）
        # ---- 体积优化（Nuitka 4.1.3 参数已核实）----
        "--lto=yes",                        # 链接期优化；编译器不支持时会报错，可改回 auto
        "--noinclude-qt-plugins=tls",       # 无 Qt TLS 用途（SSH/TCP 走 socket/paramiko），省 ~0.7MB
        "--noinclude-qt-translations",      # UI 文案中文内置，不需要 Qt 官方翻译文件
        "--company-name=BA4IHS",
        "--product-name=all-in-debugger",
        f"--file-version={VERSION}",
        "--file-description=一站式硬件调试工具集（串口/ADB/HID/DAP-RTT/Modbus/SSH/TCP-IP，内嵌 MCP 服务）",
        "--copyright=Copyright (C) 2026 BA4IHS (GPLv3)",
        # ---- 构建行为 ----
        "--remove-output",                  # 打包完成后清理 .build 中间目录
        "--assume-yes-for-downloads",       # 缺少编译器/依赖时自动下载
        str(ENTRY),
    ]


def find_7z():
    """定位 7z 可执行文件（PATH 或常见安装目录）；找不到返回 None。"""
    for name in ("7z", "7za"):
        p = shutil.which(name)
        if p:
            return p
    for cand in (r"C:\Program Files\7-Zip\7z.exe",
                 r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(cand).is_file():
            return cand
    return None


# 本项目用不到的 Qt 图片格式插件（qfluentwidgets 图标全为 SVG；
# 保留 qsvg/qjpeg/qico）。Nuitka 只能按 family 目录整体取舍，
# 单个文件需编译后手动裁剪。
PRUNE_QT_PLUGINS = [
    "imageformats/qgif.dll",
    "imageformats/qicns.dll",
    "imageformats/qpdf.dll",
    "imageformats/qtga.dll",
    "imageformats/qtiff.dll",
    "imageformats/qwbmp.dll",
    "imageformats/qwebp.dll",
    "platforms/qminimal.dll",
]


def prune_qt_plugins():
    """裁剪无用 Qt 图片格式插件，缩小体积。"""
    plugins = BUNDLE_DIR / "PyQt6" / "Qt6" / "plugins"
    if not plugins.is_dir():
        print(f"[build] 未找到 Qt 插件目录，跳过裁剪：{plugins}", file=sys.stderr)
        return
    removed = 0
    for rel in PRUNE_QT_PLUGINS:
        p = plugins / rel
        if p.is_file():
            kb = p.stat().st_size // 1024
            p.unlink()
            removed += 1
            print(f"[build] 裁剪 Qt 插件：{rel} ({kb} KB)")
    print(f"[build] Qt 插件裁剪完成，共移除 {removed} 个文件")


# qt6pdf.dll（4.4MB）与 qt6network.dll（1.7MB）为死重：
# pefile 扫描全部 dll/pyd 及 exe 导入表，qt6pdf 无任何引用者，
# qt6network 亦无引用者（qfluentwidgets 仅用 Core/Gui/Widgets/Svg/Xml）。
# 需在编译后手动删除（Nuitka 无法按单 DLL 裁剪 Qt 库）。
PRUNE_QT_DLLS = [
    "qt6pdf.dll",
    "qt6network.dll",
]


def prune_qt_dlls():
    """删除无引用者的 Qt 库 DLL（qt6pdf/qt6network），缩小体积。"""
    removed = 0
    for name in PRUNE_QT_DLLS:
        p = BUNDLE_DIR / name
        if p.is_file():
            kb = p.stat().st_size // 1024
            p.unlink()
            removed += 1
            print(f"[build] 裁剪 Qt 库：{name} ({kb} KB)")
    if removed:
        print(f"[build] Qt 库裁剪完成，共移除 {removed} 个文件")
    else:
        print("[build] Qt 库裁剪：未找到目标文件（可能已在依赖分析中排除）")


def check_no_config_leak():
    """防御检查：确认 config.json/data.json（含 MCP 密钥）未进入产物。"""
    leaked = []
    for name in ("config.json", "data.json"):
        p = BUNDLE_DIR / name
        if p.is_file():
            leaked.append(p)
    if leaked:
        for p in leaked:
            print(f"[build] 警告：产物中发现残留 {p.name}（含 MCP 密钥），已删除！",
                  file=sys.stderr)
            p.unlink()
        return
    print("[build] 配置检查：产物中无 config.json/data.json（MCP 密钥不打包）✓")


# HID/ADB 运行必需的原生文件（app/libs 由 --include-data-dir 递归复制）。
# 若缺失，HID 页无法加载 hidapi.dll、ADB 页无法调用 adb.exe。
REQUIRED_LIBS = [
    "libs/hidapi.dll",
    "libs/README.txt",
    "libs/adb/adb.exe",
    "libs/adb/AdbWinApi.dll",
    "libs/adb/AdbWinUsbApi.dll",
]


def ensure_libs_copied():
    """把 app/libs 整目录强制复制进产物。

    Nuitka 的 --include-data-dir 对含子目录/二进制的 libs 目录复制不完整
    （实测只带出 README.txt，丢 hidapi.dll 和 adb\ 子目录），这里编译后
    直接从源码目录整体复制兜底，保证 HID/ADB 原生依赖一定进产物。
    """
    src = ROOT / "app" / "libs"
    dst = BUNDLE_DIR / "app" / "libs"
    if not src.is_dir():
        print(f"[build] 警告：源码目录 {src} 不存在，无法复制 libs", file=sys.stderr)
        return False
    shutil.copytree(src, dst, dirs_exist_ok=True)
    n = sum(1 for _ in dst.rglob("*"))
    print(f"[build] libs 完整性：已从源码复制 app/libs 到产物（{n} 项）")
    return True


def check_libs_complete():
    """防御检查：确认 HID/ADB 原生依赖已进入产物（缺失会静默失败，必须显式暴露）。"""
    missing = []
    for rel in REQUIRED_LIBS:
        p = BUNDLE_DIR / "app" / rel
        if not p.is_file():
            missing.append(rel)
    if missing:
        print("[build] 警告：产物中缺少 HID/ADB 原生依赖！", file=sys.stderr)
        for rel in missing:
            print(f"[build]   缺失：app\\{rel}", file=sys.stderr)
        print("[build] 请检查 app/libs 目录完整性后重新打包（HID/ADB 功能将不可用）",
              file=sys.stderr)
        return False
    print(f"[build] 依赖检查：HID/ADB 原生依赖齐全（{len(REQUIRED_LIBS)} 项）✓")
    return True


def check_gui_subsystem():
    """校验 exe 为 GUI 子系统（无终端）。读 PE Optional Header 的 Subsystem 字段：
    2=WINDOWS_GUI（无控制台），3=WINDOWS_CUI（有控制台）。返回 True 表示无终端。"""
    exe = BUNDLE_DIR / "all-in-debugger.exe"
    if not exe.is_file():
        print(f"[build] 警告：未找到 {exe}，跳过子系统检查", file=sys.stderr)
        return False
    data = exe.read_bytes()
    e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        print("[build] 警告：exe 不是有效 PE 文件，跳过子系统检查", file=sys.stderr)
        return False
    opt_off = e_lfanew + 24
    subsystem = int.from_bytes(data[opt_off + 68:opt_off + 70], "little")
    ok = subsystem == 2
    print(f"[build] 子系统检查：Subsystem={subsystem} "
          f"({'WINDOWS_GUI（无终端）✓' if ok else '非 GUI 子系统，会有控制台窗口！'})"
          f"（2=GUI，3=Console）")
    return ok


def make_archive():
    """把 main.dist 压缩归档：优先 7z（最高压缩），失败回退 zip。"""
    if not BUNDLE_DIR.is_dir():
        print(f"[build] 未找到 {BUNDLE_DIR}，跳过归档", file=sys.stderr)
        return
    sevenzip = find_7z()
    if sevenzip:
        target = f"{ARCHIVE_BASE}.7z"
        cmd = [sevenzip, "a", "-t7z", "-mx=9", "-bso0", "-bsp0",
               target, str(BUNDLE_DIR / "*")]
        print(f"[build] 7z 压缩 -> {target}")
        subprocess.run(cmd, check=True)
    else:
        target = str(ARCHIVE_BASE) + ".zip"
        print(f"[build] 未找到 7z，回退 zip 压缩 -> {target}")
        shutil.make_archive(str(ARCHIVE_BASE), "zip", root_dir=BUNDLE_DIR)
    print(f"[build] 归档完成：{target}")


def main():
    if "--dry-run" in sys.argv:
        print("[build] 将要执行的命令（--dry-run）：")
        print("  " + " ".join(build_args()))
        print("[build] 编译后将自动执行：Qt 插件裁剪 → 配置文件泄漏检查 → exe 子系统检查（无终端）")
        print(f"[build] 归档目标：{ARCHIVE_BASE}.7z / .zip")
        return

    # 清理旧产物，避免上次构建残留混淆
    if BUNDLE_DIR.is_dir():
        print(f"[build] 清理旧产物 {BUNDLE_DIR}")
        shutil.rmtree(BUNDLE_DIR, ignore_errors=True)

    print("[build] 开始 Nuitka 打包（首次约 10-30 分钟，请耐心等待）...")
    subprocess.run(build_args(), check=True)
    print(f"[build] 编译完成：{BUNDLE_DIR}")

    # 编译后处理：裁剪体积 → 密钥安全 → 无终端确认 → HID/ADB 依赖完整
    prune_qt_plugins()
    prune_qt_dlls()
    check_no_config_leak()
    if not check_gui_subsystem():
        print("[build] 警告：exe 存在控制台窗口风险，请检查 --windows-console-mode",
              file=sys.stderr)
    ensure_libs_copied()
    if not check_libs_complete():
        print("[build] 警告：产物缺少 HID/ADB 原生依赖，归档将不完整！",
              file=sys.stderr)

    if "--no-archive" not in sys.argv:
        make_archive()


if __name__ == "__main__":
    main()
