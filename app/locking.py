"""进程内写锁：补采与词表换表互斥。

两类写操作都会在一个 SQLite 事务内同时改动书目/计数，因此共用同一把锁，
避免“换表和补采撞在一起”。拿不到锁的请求立即得到 409，不排队阻塞。
"""
from __future__ import annotations

import threading

# 补采与换表共用：同一时刻只允许一个写事务
WRITE_LOCK = threading.Lock()


class WriteConflictError(RuntimeError):
    """已有写操作（补采或换表）正在执行。"""
