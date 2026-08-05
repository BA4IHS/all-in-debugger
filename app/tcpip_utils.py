# coding: utf-8
"""TCP/IP 调试纯函数：地址/端口校验、来源串格式化。

全部为无副作用函数，可独立单测；socket 一律在 tcpip_worker.py 内创建。
"""
import re

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-_:\]\[%]+$")


def validate_host(host: str) -> str:
    """校验主机名 / IPv4 / IPv6；空串表示通配（本地监听），原样返回。"""
    host = (host or "").strip()
    if host and not _HOST_RE.match(host):
        raise ValueError(f"非法主机名：{host}")
    return host


def validate_port(port, allow_zero: bool = False) -> int:
    """校验端口，返回 int。allow_zero=True 时允许 0（系统自动分配）。"""
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"非法端口：{port}") from None
    if allow_zero and p == 0:
        return 0
    if not 1 <= p <= 65535:
        raise ValueError(f"端口超出 1~65535：{p}")
    return p


def format_source(host: str, port: int) -> str:
    """'192.168.1.10:8080' / '[::1]:8080'，host 为空时显示 '?'。"""
    host = str(host or "?")
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def is_broadcast_host(host: str) -> bool:
    """是否为广播地址（255.255.255.255 或 xxx.255 尾段）。"""
    host = (host or "").strip()
    return host == "255.255.255.255" or host.endswith(".255")
