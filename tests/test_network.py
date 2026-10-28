"""Tests for recorder.utils.network (no real network access needed)."""
from __future__ import annotations

import socket

from recorder.utils import network
from recorder.utils.network import (LOOPBACK_HOST, default_labrecorder_host,
                                    get_local_ip)


class _FakeUdpSocket:
    def __init__(self, ip=None, connect_error=None):
        self._ip = ip
        self._connect_error = connect_error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, addr):
        if self._connect_error:
            raise self._connect_error

    def getsockname(self):
        return (self._ip, 12345)


def _patch_socket(monkeypatch, **kw):
    monkeypatch.setattr(network.socket, "socket", lambda *a, **k: _FakeUdpSocket(**kw))


def test_get_local_ip_returns_outgoing_interface_address(monkeypatch):
    _patch_socket(monkeypatch, ip="192.168.1.42")
    assert get_local_ip() == "192.168.1.42"


def test_get_local_ip_none_when_no_route(monkeypatch):
    _patch_socket(monkeypatch, connect_error=OSError("Network is unreachable"))
    assert get_local_ip() is None


def test_get_local_ip_ignores_loopback_and_wildcard(monkeypatch):
    _patch_socket(monkeypatch, ip="127.0.1.1")
    assert get_local_ip() is None
    _patch_socket(monkeypatch, ip="0.0.0.0")
    assert get_local_ip() is None


def test_default_host_on_linux_is_the_machines_ip(monkeypatch):
    monkeypatch.setattr(network, "get_local_ip", lambda: "192.168.1.42")
    assert default_labrecorder_host("linux") == "192.168.1.42"


def test_default_host_on_linux_falls_back_to_loopback_without_network(monkeypatch):
    monkeypatch.setattr(network, "get_local_ip", lambda: None)
    assert default_labrecorder_host("linux") == LOOPBACK_HOST


def test_default_host_on_other_platforms_stays_loopback(monkeypatch):
    monkeypatch.setattr(network, "get_local_ip", lambda: "192.168.1.42")
    assert default_labrecorder_host("darwin") == LOOPBACK_HOST
    assert default_labrecorder_host("win32") == LOOPBACK_HOST


def test_real_get_local_ip_returns_ipv4_string_or_none():
    ip = get_local_ip()
    assert ip is None or len(socket.inet_aton(ip)) == 4
