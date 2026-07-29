# coding: utf-8
"""ADB 核心测试：纯解析/profile + 真实设备端到端（无设备自动跳过）。"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import adb_runner as ar


@pytest.fixture(scope="session")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(pred, qapp, ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(scope="session")
def adb_dev():
    from app.config import cfg, loadConfig, qconfig
    loadConfig()
    path, err = ar.find_adb(qconfig.get(cfg.adbPath))
    if not path:
        pytest.skip(f"adb 不可用：{err}")
    devs, e = ar.list_adb_devices(path)
    serial = next((d["serial"] for d in devs if d["state"] == "device"), None)
    if not serial:
        pytest.skip("无已连接的 adb device（state=device）")
    return path, serial


# ---------------------------------------------------------------------------
# 纯解析
# ---------------------------------------------------------------------------

def test_parse_devices_text():
    out = ("List of devices attached\n"
           "0402101560             device transport_id:2\n"
           "DEADBEEF               offline\n"
           "\n")
    devs = ar._parse_devices_text(out)
    assert devs[0]["serial"] == "0402101560" and devs[0]["state"] == "device"
    assert devs[1]["serial"] == "DEADBEEF" and devs[1]["state"] == "offline"
    assert "transport_id:2" in devs[0]["info"]


def test_parse_adb_version_ignores_server_messages():
    out = (
        "adb server version (39) doesn't match this client (40); killing...\n"
        "* daemon started successfully\n"
        "Android Debug Bridge version 1.0.40\n"
        "Version 4797878\n"
    )
    assert ar.parse_adb_version(out) == (1, 0, 40)
    assert ar.adb_version_line(out) == "Android Debug Bridge version 1.0.40"
    assert ar.is_legacy_adb_version((1, 0, 39))
    assert not ar.is_legacy_adb_version((1, 0, 40))


def test_load_profile_ok_and_bad(tmp_path):
    good = tmp_path / "g.json"
    good.write_text(json.dumps({
        "model": "M1", "commands": [
            {"name": "a", "cmd": "uname -a"},
            {"cmd": "hostname"},          # 无名 -> 自动命名
        ]}, ensure_ascii=False), encoding="utf-8")
    d = ar.load_profile(good)
    assert d["commands"][0]["name"] == "a"
    assert d["commands"][1]["name"].startswith("命令")

    bad = tmp_path / "b.json"
    bad.write_text(json.dumps({"commands": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        ar.load_profile(bad)


def test_list_profiles_skips_invalid(tmp_path):
    (tmp_path / "alpha.json").write_text(json.dumps({
        "commands": [{"name": "x", "cmd": "id"}]}), encoding="utf-8")
    (tmp_path / "beta.json").write_text(json.dumps({
        "model": "乙型", "commands": [{"name": "y", "cmd": "w"}]}),
        encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    res = ar.list_profiles(tmp_path)
    models = [m for _, m, _ in res]
    assert models == ["alpha", "乙型"]   # 按型号名 Unicode 排序；坏文件被跳过
    assert all(len(d["commands"]) == 1 for _, _, d in res)


def test_runner_error_handler_no_crash(qapp):
    """errorOccurred 传来的 ProcessError 在 PyQt6 下不可 int()，处理不得崩。"""
    r = ar.AdbCommandRunner()
    r._cur_name = "x"

    class FakeEnum:           # 模拟 PyQt6 枚举：有 .value，但 int() 抛错
        value = 3

        def __int__(self):
            raise TypeError("ProcessError not int-convertible")

    r._on_error(FakeEnum())     # 不得抛
    r._on_error(object())       # 无 .value 也不得抛


def test_shipped_default_profile_loads():
    d = ar.load_profile(ar.profile_dir() / "generic_linux.json")
    assert d["commands"] and all(c["cmd"] for c in d["commands"])


def test_async_probe_timeout_keeps_ui_responsive(qapp):
    """挂死的 adb 探测必须超时退出，期间 Qt 事件循环仍能运行。"""
    from PyQt6.QtCore import QTimer

    probe = ar.AdbProbe()
    done, ticks = [], []
    probe.finished.connect(
        lambda data, code, error: done.append((data, code, error)))
    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(lambda: ticks.append(1))
    ticker.start()

    started = time.perf_counter()
    probe.start(
        sys.executable,
        ["-c", "import time; time.sleep(5)"],
        timeout_ms=120,
    )
    assert time.perf_counter() - started < 0.1
    assert _pump(lambda: done, qapp, 1500)
    ticker.stop()

    assert ticks, "探测期间 UI 事件循环没有继续运行"
    assert done[0][1] == -1
    assert "超时" in done[0][2]
    assert not probe.is_running()
    probe.shutdown()


def test_adb_page_refresh_does_not_call_sync_subprocess(qapp, monkeypatch):
    """页面初始化/刷新只能启动异步 QProcess，禁止走同步 subprocess.run。"""
    from PyQt6.QtCore import QPoint, Qt

    calls = []

    monkeypatch.setattr(ar, "find_adb", lambda _configured: ("fake-adb", ""))
    monkeypatch.setattr(
        ar, "adb_version",
        lambda *_args, **_kwargs: pytest.fail("UI 调用了同步 adb_version"))
    monkeypatch.setattr(
        ar, "list_adb_devices",
        lambda *_args, **_kwargs: pytest.fail("UI 调用了同步 list_adb_devices"))
    monkeypatch.setattr(
        ar.AdbProbe, "start",
        lambda self, program, args, timeout_ms=6000:
            calls.append((program, tuple(args), timeout_ms)))

    from app.ui.adb_page import AdbPage
    page = AdbPage()
    try:
        assert ("fake-adb", ("version",), 6000) in calls
        assert ("fake-adb", ("devices", "-l"), 6000) in calls
        page.shell.dataReceived.emit(b"queued output")
        assert page.terminal.queued_byte_count() == len(b"queued output")
        page._on_devices_refreshed(
            b"List of devices attached\nSER123 device product:x\n", 0, "")
        assert page._current_serial() == "SER123"

        commands = [
            {"name": "设备信息", "cmd": "getprop"},
            {"name": "内存状态", "cmd": "cat /proc/meminfo"},
            {"name": "设备日志", "cmd": "logcat -d"},
        ]
        page._rebuild_cmd_list(commands)
        page.commandSearch.setText("设备")
        assert [c["name"] for c in page._filtered_commands] == [
            "设备信息", "设备日志"]
        page.commandSearch.setText("不存在")
        assert page._filtered_commands == []
        page.commandSearch.clear()
        assert page._filtered_commands == commands

        # 搜索框必须是页面内普通控件；Qt.Popup 会抢占 Windows 中文输入法。
        assert page.commandSearch.parent() is page
        assert not (
            page.commandSearch.windowFlags() & Qt.WindowType.Popup)
        assert page.commandSearch.testAttribute(
            Qt.WidgetAttribute.WA_InputMethodEnabled)
        page.resize(1000, 700)
        page.show()
        qapp.processEvents()
        page._toggle_command_search()
        assert page.commandSearch.isVisible()
        start_rect = page._commandSearchAnimation.startValue()
        end_rect = page._commandSearchAnimation.endValue()
        assert start_rect.right() == end_rect.right()
        assert start_rect.left() > end_rect.left()
        title_left = page.mapFromGlobal(
            page.modelTitle.mapToGlobal(QPoint(0, 0))).x()
        assert end_rect.left() == title_left
        page.serialCombo.setFocus()
        qapp.processEvents()
        assert not page.commandSearch.isVisible()
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        qapp.processEvents()


def test_file_manager_listing_parser_keeps_chinese_and_spaces():
    from app.ui.adb_file_manager import parse_directory_listing

    raw = (
        b"total 8\r\n"
        b"drwxr-xr-x 2 0 0 4096 Jul 30 12:34 "
        + "中文 文件夹".encode("utf-8") + b"\r\n"
        b"-rw-r--r-- 1 0 0 1536 Jul 29 2026 "
        + "长 文件名.txt".encode("utf-8") + b"\r\n"
        b"lrwxrwxrwx 1 0 0 4 Jul 29 2026 link name -> /tmp\r\n"
    )
    entries, error = parse_directory_listing(raw)
    assert not error
    assert [item["name"] for item in entries] == [
        "中文 文件夹", "link name", "长 文件名.txt"]
    assert entries[0]["kind"] == "directory"
    assert entries[2]["size"] == 1536


def test_file_manager_remote_paths_and_safe_delete_command():
    from app.ui.adb_file_manager import (
        build_delete_command, normalize_remote_path, remote_child_path,
    )

    assert normalize_remote_path("../中文", "/sdcard/Download") \
        == "/sdcard/中文"
    assert remote_child_path("/sdcard", "a b.txt") == "/sdcard/a b.txt"
    command = build_delete_command(["/sdcard/中文 文件.txt", "/tmp/a"])
    assert "rm -rf --" in command
    assert "'/sdcard/中文 文件.txt'" in command
    assert "__ADB_FILE_DELETE_OK__" in command
    with pytest.raises(ValueError, match="根目录"):
        build_delete_command(["/"])


def test_file_manager_is_independent_window_with_selection_actions(qapp):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from app.ui.adb_file_manager import AdbFileManagerWindow

    window = AdbFileManagerWindow("adb", "SER123", auto_load=False)
    try:
        assert window.parent() is None
        assert window.isWindow()
        assert window.addressEdit.text() == "/"
        assert not window.downloadBtn.isEnabled()
        assert not window.deleteBtn.isEnabled()

        window._current_path = "/sdcard"
        window.addressEdit.setText("/sdcard")
        window._populate([
            {
                "name": "Download", "size": 0,
                "modified": "2026-07-30 10:00",
                "kind": "directory", "mode": "drwxr-xr-x",
            },
            {
                "name": "中文.txt", "size": 12,
                "modified": "2026-07-30 10:01",
                "kind": "file", "mode": "-rw-r--r--",
            },
        ])
        assert window.tree.topLevelItemCount() == 2
        file_item = window.tree.topLevelItem(1)
        file_item.setCheckState(0, Qt.CheckState.Checked)
        assert window.downloadBtn.isEnabled()
        assert window.deleteBtn.isEnabled()
        assert file_item.data(0, Qt.ItemDataRole.UserRole) \
            == "/sdcard/中文.txt"
        window.tree.selectAll()
        assert len(window._target_items()) == 2

        # 从列表空白处拖出框选区域，不得因 QTreeWidgetItem 不可哈希而崩溃。
        window.resize(900, 600)
        window.show()
        qapp.processEvents()
        viewport = window.tree.viewport()
        start = QPoint(
            viewport.width() - 5, viewport.height() - 5)
        end = QPoint(5, 5)
        QTest.mousePress(
            viewport, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier, start)
        QTest.mouseMove(viewport, end)
        QTest.mouseRelease(
            viewport, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.ControlModifier, end)
        qapp.processEvents()
        assert window.tree._rubber_origin is None
    finally:
        window.close()
        qapp.processEvents()


def test_shell_stop_never_waits_in_ui_thread(qapp):
    """关闭 Shell 只发 terminate；不得调用阻塞式 waitForFinished。"""
    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def waitForFinished(self, _timeout):
            raise AssertionError("stop() 不得阻塞 UI 线程")

    shell = ar.AdbShellProcess()
    fake = FakeProcess()
    shell._proc = fake
    shell.stop()
    assert fake.terminated
    shell._proc = None


def test_adb_process_shutdown_reaps_children(qapp):
    """应用退出时，活动及已取消的 QProcess 都必须先进入 NotRunning。"""
    from PyQt6.QtCore import QProcess

    def start_sleep(parent):
        p = QProcess(parent)
        p.start(
            sys.executable,
            ["-c", "import time; time.sleep(5)"],
        )
        assert p.waitForStarted(1500)
        return p

    # 当前活动进程
    for owner in (ar.AdbProbe(), ar.AdbShellProcess(), ar.AdbCommandRunner()):
        p = start_sleep(owner)
        owner._proc = p
        owner.shutdown()
        assert p.state() == QProcess.ProcessState.NotRunning

    # 普通 cancel 是异步的；紧接着关闭时还要回收 retired 进程。
    probe = ar.AdbProbe()
    probe._proc = start_sleep(probe)
    retired = probe._proc
    probe.cancel()
    probe.shutdown()
    assert retired.state() == QProcess.ProcessState.NotRunning
    qapp.processEvents()


# ---------------------------------------------------------------------------
# 真实设备端到端
# ---------------------------------------------------------------------------

def test_real_list_devices_state(adb_dev):
    path, serial = adb_dev
    devs, _ = ar.list_adb_devices(path)
    assert any(d["serial"] == serial and d["state"] == "device" for d in devs)


def test_real_one_shot_command(qapp, adb_dev):
    path, serial = adb_dev
    runner = ar.AdbCommandRunner()
    buf = bytearray()
    done = []
    runner.dataReceived.connect(lambda b: buf.extend(b))
    runner.commandFinished.connect(lambda n, c: done.append((n, c)))
    runner.run_one(path, serial, "回环", "echo ADBRUN_OK_42")
    assert _pump(lambda: done, qapp, 20000)
    assert done[0][1] == 0
    assert b"ADBRUN_OK_42" in bytes(buf)
    # 分隔标题也应出现（含命令名）
    assert "回环".encode("utf-8") in bytes(buf)


def test_real_interactive_shell(qapp, adb_dev):
    path, serial = adb_dev
    sh = ar.AdbShellProcess()
    buf = bytearray()
    started, stopped = [], []
    sh.dataReceived.connect(lambda b: buf.extend(b))
    sh.started.connect(lambda: started.append(1))
    sh.stopped.connect(lambda c, m: stopped.append(c))
    sh.start(path, serial)
    assert _pump(lambda: started, qapp, 15000), "shell 未启动"
    sh.write(b"echo SHELL_OK_7\r")
    assert _pump(lambda: b"SHELL_OK_7" in bytes(buf), qapp, 15000), \
        f"未收到回显，buf={bytes(buf)[:200]!r}"
    sh.stop()
    _pump(lambda: stopped, qapp, 5000)
    assert not sh.is_running()
