# 原生依赖存放目录

- hidapi.dll            HID 调试 + DAP-link RTT 调试共用（官方 libusb/hidapi 发布版，
                        位数需与 Python 解释器一致；x86/x64 子目录也支持）
- adb/                  程序自带的 ADB 三件套（adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll，
                        官方 platform-tools；find_adb 在 未配置/PATH 找不到 时自动使用）

说明：
- ADB 调试走 adb.exe 子进程（AdbWinApi.dll 为 32 位，仅供 adb.exe 自身使用，
  Python 不做 ctypes 直连）
- DAP-link RTT 经 hidapi.dll 直连调试器 USB HID（CMSIS-DAP 协议由 Python 实现，
  不需要 Keil CMSIS_DAP.dll —— 其为 32 位私有插件，无公开接口）
- 可用环境变量 HIDAPI_DLL 指定其它位置的 hidapi.dll
