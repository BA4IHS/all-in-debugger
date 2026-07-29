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
    path, err = ar.find_adb("adb")
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
