# 串口调试工具（QFluentWidgets）

基于 PyQt6 + qfluentwidgets 1.11.2 + pyserial 的全功能串口调试工具，Fluent 风格，支持浅色/深色/跟随系统主题。

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
│   └── ui/
│       ├── main_window.py  # SplitFluentWindow 三页面
│       ├── console_page.py # 主调试页（左配置 + 右收发）
│       ├── connect_panel.py
│       ├── receive_panel.py
│       ├── terminal_widget.py  # pyte 仿真 + 自绘终端
│       ├── send_panel.py
│       ├── preset_page.py
│       ├── adb_page.py
│       ├── adb_file_manager.py
│       └── setting_page.py
└── tests/test_serial_utils.py
```

## 已知限制

- 周期发送基于 UI 线程 QTimer，最小间隔 10ms，受 Windows 定时器合并（~15ms）影响，不适合 <10ms 的高频场景
- Mark/Space 校验位在部分平台驱动不支持，打开失败会有错误提示
