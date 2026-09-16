"""Pytest-only socket guard for isolated public-source regression checks."""
import socket

def _deny(*args, **kwargs):
    raise RuntimeError("Network is disabled during public-source validation")

socket.socket.connect = _deny
socket.socket.connect_ex = _deny
socket.socket.sendto = _deny
socket.create_connection = _deny
