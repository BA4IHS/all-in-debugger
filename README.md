# All-in Debugger（QFluentWidgets）

基于 PyQt6 + qfluentwidgets 1.11.2 的全功能调试工具集：串口、ADB、HID、DAP-link RTT、Modbus、SSH，Fluent 风格，支持浅色/深色/跟随系统主题，并内嵌 MCP 服务供 AI 客户端调用。

## 功能

- 串口参数：端口自动检测（2s 轮询热插拔）、波特率（常用 11 档 + 自定义）、数据位/停止位/校验位/流控、DTR/RTS
- 接收：文本/HEX 显示、UTF-8/GBK/ASCII 解码（增量解码，跨包半个汉字不出错）、时间戳、暂停显示、自动滚动（手动上滚自动暂停）、清屏、RX/TX 计数、容量上限自动截断（防卡顿）
- 发送：文本/HEX（HEX 输入容忍空格/逗号/0x 前缀，非法有提示）、换行符追加（None/CR/LF/CRLF）、Enter 发送、周期发送（间隔 ≥10ms）、发送历史（50 条，持久化）
- 预设命令页：多行命令表格，每行独立 启用/HEX/周期/间隔/单发，支持 JSON 导入导出，退出自动保存
- 日志：原始字节日志（worker 线程写入），目录可在设置页配置，默认 `程序目录/logs/`
- 设置：主题切换、接收区容量上限，持久化到 `config.json`
- 界面：原生 Qt 与 Fluent 控件统一使用半透明白色滚动条
- **终端模式**：调试页"终端模式"开关，切到交互式 VT100/ANSI 终端（pyte 仿真 + Qt 自绘网格），键盘直发串口，支持彩色/光标/清屏/滚屏、右侧回滚条、拖选越界自动滚动、鼠标框选复制、本地回显、回车符可选
- **内容查找**：串口日志、串口终端和 ADB 终端均可按 `Ctrl+F` 或右键“查找”，支持匹配计数、循环跳转、大小写匹配与结果高亮
- **ADB 文件管理**：独立可缩放窗口，支持设备目录浏览、复选/框选、上传文件或文件夹、批量下载与二次确认删除；全部 ADB 操作异步执行并显示进度
- **HID 调试**：集成官方 `hidapi.dll`（已随程序交付于 `app/libs/`），设备枚举（VID/PID 过滤）、打开/关闭、HEX/文本发送（报告 ID 自动补 0x00）、周期发送、特征报告读/写、异步接收显示
- **ADB 自带三件套**：`app/libs/adb/` 内置官方 platform-tools（adb.exe + AdbWinApi.dll + AdbWinUsbApi.dll），未配置且 PATH 找不到 adb 时自动使用；ADB 调试始终走 adb.exe 子进程
- **DAP-link RTT**：经 `hidapi.dll` 直连调试器 USB HID（CMSIS-DAP 协议纯 Python 实现，不依赖厂商 DLL；Keil CMSIS_DAP.dll 为 32 位私有插件，无公开接口，故不采用），SWD 连接、IDCODE 读取、可选硬件复位、SEGGER RTT 控制块自动扫描/手动指定、多通道上行实时显示与下行发送
- **Modbus 调试**：基于 `pymodbus`，支持 RTU（串口）与 TCP 客户端，FC01–FC06/FC15/FC16 读写；Modbus Poll 式网格数据表（每寄存器一格，固定 10 格一列，任意起始地址/数量），逐格数据类型（U16/I16/HEX/Float/ASCII，右键设置），周期轮询与帧日志
- **SSH 调试**：基于 `paramiko`，交互式终端（复用 pyte 仿真，自动 resize_pty）、密码/私钥认证、会话保存（密码不落盘）、SFTP 目录浏览/下载/上传/删除
- **MCP 服务**：内嵌 streamable HTTP MCP 服务（FastMCP + uvicorn，仅监听 127.0.0.1，Bearer Token 鉴权），向 AI 客户端暴露 37 个调试工具（串口/HID/DAP-RTT/Modbus/SSH 的状态、读写与文件操作），设置页可开关端口与复制接入配置

> 原生依赖统一放 `app/libs/`（位数需与 Python 一致，支持 x86/x64 子目录与 `HIDAPI_DLL` 环境变量）；缺失时相应页面优雅降级并提示。

## 终端模式

像 MobaXterm / ssh 那样的交互式串口 shell：

- 引擎：`pyte`（VT100/ANSI 仿真，含 16 色 + 256/24bit 真彩色），渲染用 Qt 自绘等宽字符网格
- 输入：可打印字符、回车（CR/CRLF/LF 可选）、退格、Tab、Esc、方向键、Home/End、Insert/Delete、F1–F12、Ctrl+字母（控制字符）
- 回滚：滚轮 / PgUp / PgDn 查看历史输出
- 选择复制：鼠标框选，`Ctrl+Shift+C` 或右键复制；`Ctrl+Shift+V` / 右键粘贴
- 工具条：本地回显（设备不回显时开启）、回车符、清屏
- 终端模式下底部"发送框"自动隐藏（改为键盘直输），切回日志模式恢复

> 注意：终端遵循真实终端语义——单独的 `\n`(LF) 不会回到行首，会出现"阶梯"。绝大多数串口 shell 输出 `\r\n`，不受影响；若你的设备只发 `\n`，可在设备端开启换行模式，或后续按需加"LF→CRLF"选项。
> 依赖：`pip install pyte`（已写入 requirements.txt）。

## 运行

```bash
pip install -r requirements.txt
python main.py
```

> 注意：本项目的 qfluentwidgets 为 **PyQt6** 版本，代码全部从 PyQt6 导入。不要安装 `PyQt5-Fluent-Widgets`（PyQt5/PyQt6 不能混用）。
> ADB 交互终端建议使用 **ADB 1.0.40 或更高版本**；部分刷机工具捆绑的 1.0.39 客户端会产生数秒级输入回显延迟，程序检测到后会提示更换。

## 测试

```bash
python -m pytest tests/ -q
```

测试包含纯函数单测（HEX 解析、增量解码、换行符等）与 **无需硬件** 的 worker 端到端测试（基于 pyserial 内置 `loop://` 回环协议：打开 → 写入 → 收回相同字节 → 关闭 → 线程退出）。

## 无硬件验证 GUI 收发

- Windows：安装 [com0com](https://com0com.sourceforge.net/) 建立虚拟串口对（如 COM10↔COM11），本工具连一端，另一实例或 sscom/PuTTY 连另一端
- 或物理回环：USB-TTL 模块 TX/RX 短接自发自收

## 项目结构

```
com/
├── main.py                 # 入口
├── requirements.txt
├── app/
│   ├── config.py           # qconfig 配置 + data.json（历史/预设）
│   ├── serial_utils.py     # 纯函数：HEX/解码/换行/端口枚举
│   ├── serial_worker.py    # SerialWorker（QThread 内阻塞读循环）
│   ├── native.py           # 统一 DLL 加载器（app/libs/ 目录）
│   ├── hid_binding.py      # hidapi.dll 绑定（HID 与 DAP 共用）
│   ├── hid_worker.py       # HID 收发线程
│   ├── dap_core.py         # CMSIS-DAP/SWD 协议（HID 直连）
│   ├── dap_rtt.py          # SEGGER RTT 控制块扫描/通道读写
│   ├── dap_worker.py       # DAP/RTT 轮询线程
│   ├── modbus_core.py      # pymodbus 异步客户端封装 + Modbus 收发线程
│   ├── ssh_worker.py       # paramiko SSH/SFTP 线程
│   ├── mcp_bridge.py       # MCP 桥接（跨线程信号转发）
│   ├── mcp_server.py       # 内嵌 MCP 服务（37 工具）
│   ├── libs/               # hidapi.dll + adb 三件套（随程序交付）
│   └── ui/
│       ├── main_window.py  # SplitFluentWindow 八页面
│       ├── console_page.py # 主调试页（左配置 + 右收发）
│       ├── connect_panel.py
│       ├── receive_panel.py
│       ├── terminal_widget.py  # pyte 仿真 + 自绘终端
│       ├── send_panel.py
│       ├── preset_page.py
│       ├── adb_page.py
│       ├── adb_file_manager.py
│       ├── hid_page.py     # HID 调试页
│       ├── dap_page.py     # DAP-link RTT 调试页
│       ├── modbus_page.py  # Modbus 调试页（网格数据表）
│       ├── ssh_page.py     # SSH 调试页（终端 + SFTP）
│       ├── console_style.py # 日志/终端深色主题适配
│       └── setting_page.py
└── tests/                  # 纯函数单测 + worker 端到端测试
```

## 已知限制

- 周期发送基于 UI 线程 QTimer，最小间隔 10ms，受 Windows 定时器合并（~15ms）影响，不适合 <10ms 的高频场景
- Mark/Space 校验位在部分平台驱动不支持，打开失败会有错误提示
