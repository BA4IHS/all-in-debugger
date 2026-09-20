# coding: utf-8
"""应用日志初始化：让 `logging.getLogger(__name__).xxx(...)` 真正落盘。

背景：仓库长期使用 Python 标准库 ``logging``，但从未调用 ``basicConfig``
或挂 handler，root logger 只能靠 ``lastResort`` 把 WARNING 以上打到
``sys.stderr``。打包版关闭了控制台（``--windows-console-mode=disable``），
stderr 为 None → 业务日志彻底静默，只剩 ``crash_guard`` 写的
``logs/crash.log`` 里有崩溃栈，非崩溃异常/警告全部丢失。

本模块提供 :func:`setupLogging`：

- 挂 ``RotatingFileHandler`` 到 ``<APP_DIR>/logs/app.log``（与 ``crash.log``
  同目录，已在 ``.gitignore`` 中排除），单文件 1 MB，保留 3 份历史；
- 源码运行时（``sys.stderr`` 可用）额外挂 ``StreamHandler``，便于终端调试；
- 抑制已知噪音第三方 logger（``pymodbus`` / ``paramiko`` / ``uvicorn`` /
  ``asyncio`` / ``mcp``）到 WARNING 及以上，避免重连刷屏淹没业务日志；
- pytest 环境（``PYTEST_CURRENT_TEST`` 存在）自动跳过落盘，防止测试
  污染仓库根 ``logs/``；
- 幂等：重复调用只挂一次 handler。

调用位置：``main.py:main()`` 中 ``installCrashGuard()`` 之后、
``QApplication`` 构造之前——保证后续任何模块的 ``getLogger(__name__)``
都能直接落盘。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

# 单文件上限与保留份数：与 crash.log 的 256 KB 相比略宽，业务日志
# 通常量更大，但仍控制在总共 4 MB 内避免膨胀
_MAX_BYTES = 1 * 1024 * 1024
_BACKUP_COUNT = 3

# 已知会刷屏的第三方 logger，压到 WARNING（业务只需知道出错，不需
# 要看每次重连/心跳的 INFO）
_NOISY_LOGGERS = (
    "pymodbus",
    "paramiko",
    "paramiko.transport",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "asyncio",
    "mcp",
    "mcp.server",
)

# 幂等标记：main.py 只调一次，测试/子进程二次 import 也不重复挂
_installed = False


def _logDir():
    """日志目录：<APP_DIR>/logs（与 crash.log 同目录）。"""
    from app.config import APP_DIR
    return APP_DIR / "logs"


def _isPytest() -> bool:
    """pytest 运行时不落盘，避免测试污染仓库根 logs/。"""
    return "PYTEST_CURRENT_TEST" in os.environ


def setupLogging(level: int = logging.INFO) -> bool:
    """初始化根 logger；返回是否成功挂载文件 handler。

    - 幂等：重复调用不会重复挂 handler；
    - 无 stderr（打包版）时只挂文件；有 stderr 时同时挂 stderr 便于调试；
    - 文件 handler 挂载失败（磁盘只读/权限等）不影响程序启动，只降级为
      仅 stderr 输出，返回 False。
    """
    global _installed
    if _installed:
        return True

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fileOk = False
    if not _isPytest():
        try:
            path = _logDir()
            path.mkdir(parents=True, exist_ok=True)
            fileHandler = logging.handlers.RotatingFileHandler(
                path / "app.log",
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            fileHandler.setFormatter(fmt)
            root.addHandler(fileHandler)
            fileOk = True
        except OSError:
            # 磁盘不可写：降级为仅 stderr，不阻塞程序启动
            fileOk = False

    # 源码运行 / 有终端时并挂 stderr；打包版 stderr 为 None 时跳过
    if sys.stderr is not None:
        streamHandler = logging.StreamHandler(sys.stderr)
        streamHandler.setFormatter(fmt)
        root.addHandler(streamHandler)

    # 抑制第三方噪音（业务日志与这些库的 INFO 混在一起几乎无法阅读）
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _installed = True
    return fileOk


def setLogLevel(name) -> bool:
    """运行时调整根 logger 等级（设置页切换即时生效）。

    接受等级名（``"DEBUG"`` / ``"INFO"`` / ``"WARNING"`` / ``"ERROR"``，
    大小写不敏感）或整数；非法输入返回 False 且不改当前等级。
    只动 root level，不动第三方噪音 logger 的抑制级别。
    """
    if isinstance(name, int):
        level = name
    else:
        level = getattr(logging, str(name).strip().upper(), None)
        if not isinstance(level, int):
            return False
    logging.getLogger().setLevel(level)
    return True


def resetLogging() -> None:
    """测试收尾用：卸载本模块挂的 handler 并复位幂等标记。"""
    global _installed
    root = logging.getLogger()
    for h in list(root.handlers):
        # 只清掉自己挂的（RotatingFileHandler + StreamHandler-to-stderr），
        # pytest / 其它库预先挂的 handler 保持原样，避免影响它们
        if isinstance(h, logging.handlers.RotatingFileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001 - 收尾路径不得再抛
                pass
        elif isinstance(h, logging.StreamHandler) and h.stream is sys.stderr:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass
    _installed = False
