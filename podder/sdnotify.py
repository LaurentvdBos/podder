"""
Pure Python implementation of sd_notify, which can be used to signal systemd
that the unit started correctly (among others). If systemd is not present (i.e.,
NOTIFY_SOCKET is not set), sd_notify does nothing.
"""

import os
import socket
from typing import Optional


_socket: Optional[socket.socket] = None
if "NOTIFY_SOCKET" in os.environ.keys():
    addr = os.environ["NOTIFY_SOCKET"]
    if addr[0] == "/" or addr[0] == "@":
        _socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _socket.connect(addr)
    else:
        raise NotImplementedError("Only AF_UNIX socket is implemented")

def sd_notify(state: bytes):
    global _socket
    
    if _socket is not None:
        _socket.send(state)