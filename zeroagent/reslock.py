# -*- coding: utf-8 -*-
"""按资源分片的本地文件锁（带持有方心跳 + 陈旧接管）。

要解决什么
----------
同一台机器上多个自动化任务并发跑时，如果它们会碰**同一份共享资源**，就必须互斥；
但**不相干的资源不应该互相排队**。于是锁按 **资源名分片**：
``resource="downloads"`` 用 ``<prefix>-downloads.lock``，与 ``resource="report"`` 互不阻塞；
不传 ``resource`` 则退化为**全局锁**（行为同最简单的实现）。

三个不显然的设计点
------------------
1. **持有方心跳**。
   只靠「pid 活着 + 未超 TTL」不够：任务其实早干完了、进程却还留着（会话复用 /
   进程池），锁会被**白占满一个 TTL**——这就是「假生效」。所以持有方定期刷新
   ``beat``；等待方见 ``beat`` 过期即可接管，**不必等 pid 死、也不必等 TTL**。
   真还在跑时 ``beat`` 一直是新的，绝不会被误抢。

2. **安全的进程判活（跨平台坑）**。
   POSIX 上 ``os.kill(pid, 0)`` 是「只探测存在性」；但 **Windows 上 Python 的
   ``os.kill`` 会把非 ``CTRL_*`` 信号一律交给 ``TerminateProcess``** —— 传 0 等于
   真的把那个进程**杀掉**。所以 Windows 必须走
   ``OpenProcess + GetExitCodeProcess``（见 :func:`pid_alive`）。

3. **退出即释放**。
   注册 :func:`atexit` 钩子，异常路径也不留死锁。

用法::

    from zeroagent.reslock import ResourceLock, LockBusy

    try:
        with ResourceLock("~/.myapp/locks", resource="downloads", task="导出") as lk:
            ...  # 独占 downloads 资源
    except LockBusy as e:
        print(e)      # 如实报出「谁在占」，而不是硬闯
"""

import atexit
import json
import os
import threading
import time

_DEFAULT_TTL = 45 * 60
_DEFAULT_STALE = 90
_DEFAULT_WAIT = 2 * 60
_DEFAULT_POLL = 3
_DEFAULT_HEARTBEAT = 15

_HELD = []


def _release_all():
    for lk in list(_HELD):
        try:
            lk.release()
        except Exception:
            pass


atexit.register(_release_all)


class LockBusy(Exception):
    """抢锁失败：被别的任务占着（调用方应**如实报出**，而不是硬闯）。"""


def pid_alive(pid):
    """进程是否存活 —— 跨平台安全实现。

    ★ **不要**用 ``os.kill(pid, 0)`` 判活：在 Windows 上它会真的把进程
      ``TerminateProcess`` 掉（Python 文档明确：非 ``CTRL_*`` 信号一律如此）。
    """
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return False
    if n <= 0:
        return False

    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, n)
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)

    try:
        os.kill(n, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class LockInfo:
    """一次 :meth:`ResourceLock.acquire` 的结果。"""

    __slots__ = ("ok", "reason", "holder", "path")

    def __init__(self, ok, reason="", holder=None, path=""):
        self.ok = bool(ok)
        self.reason = reason
        self.holder = holder or {}
        self.path = path

    def __bool__(self):
        return self.ok

    def describe(self):
        if self.ok:
            return "[reslock] 已获得锁：%s" % (self.holder.get("resource") or "(global)")
        h = self.holder
        started = h.get("startedAt")
        age = "未知"
        if isinstance(started, (int, float)):
            age = "%.0f 秒" % max(0, time.time() - started)
        return ("[reslock] 资源被占用，本次未启动：\n"
                "  任务：%s\n  进程：pid %s\n  已运行：%s\n"
                "  处理：等它跑完再试，或加大 wait 秒数。"
                % (h.get("task") or "(未标注)", h.get("pid", "?"), age))


class ResourceLock:
    """按资源分片的文件锁。既可作为上下文管理器，也可手动 acquire/release。"""

    def __init__(self, base_dir, resource=None, task="", owner="",
                 ttl=_DEFAULT_TTL, stale=_DEFAULT_STALE, wait=_DEFAULT_WAIT,
                 poll=_DEFAULT_POLL, heartbeat=_DEFAULT_HEARTBEAT,
                 prefix="lock", echo=None):
        self.base_dir = os.path.expanduser(base_dir)
        safe = "".join(c for c in str(resource or "") if c.isalnum() or c in "._-")
        self.resource = safe or None
        self.task = task
        self.owner = owner
        self.ttl = ttl
        self.stale = stale
        self.wait = wait
        self.poll = poll
        self.heartbeat = heartbeat
        fname = ("%s-%s.lock" % (prefix, safe)) if safe else ("%s.lock" % prefix)
        self.path = os.path.join(self.base_dir, fname)
        self._echo = echo or (lambda *_a: None)
        self._held = False
        self._stop = None

    # ---------- 文件读写 ----------
    def read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def _write(self, obj):
        os.makedirs(self.base_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, self.path)      # 原子替换，避免读到半截 JSON
        return True

    # ---------- 抢锁 ----------
    def acquire(self):
        deadline = time.time() + max(0, self.wait)
        while True:
            cur = self.read()
            if not cur:
                break
            started = cur.get("startedAt")
            too_old = (isinstance(started, (int, float)) and self.ttl > 0
                       and (time.time() - started) > self.ttl)
            dead = not pid_alive(cur.get("pid"))
            beat = cur.get("beat") or 0
            stale_beat = bool(self.stale > 0 and beat and (time.time() - beat) > self.stale)

            if dead or stale_beat or too_old:
                why = ("持有进程已不存在" if dead
                       else ("心跳停了 %.0fs" % (time.time() - beat) if stale_beat
                             else "超过 TTL"))
                self._echo("[reslock] 接管失效锁（%s）" % why)
                break

            if time.time() >= deadline:
                return LockInfo(False, "busy", cur, self.path)
            time.sleep(self.poll)

        wrote = True
        try:
            self._write({"pid": os.getpid(), "task": self.task, "owner": self.owner,
                         "resource": self.resource or "(global)",
                         "startedAt": time.time(), "beat": time.time()})
        except Exception as e:
            wrote = False
            self._echo("[reslock] ⚠ 锁文件写不进去（%s）—— 并发保护未生效，继续执行" % e)

        self._held = True
        _HELD.append(self)
        self._start_heartbeat()
        return LockInfo(True, "acquired" if wrote else "acquired-no-write",
                        {"resource": self.resource or "(global)"}, self.path)

    def _start_heartbeat(self):
        if self.heartbeat <= 0:
            return
        stop = threading.Event()
        self._stop = stop

        def loop():
            while not stop.wait(self.heartbeat):
                cur = self.read()
                if cur and int(cur.get("pid", -1)) == os.getpid():
                    cur["beat"] = time.time()
                    try:
                        self._write(cur)
                    except Exception:
                        pass

        t = threading.Thread(target=loop, daemon=True, name="reslock-heartbeat")
        t.start()

    def release(self):
        if self._stop is not None:
            self._stop.set()
            self._stop = None
        if not self._held:
            return
        self._held = False
        try:
            _HELD.remove(self)
        except ValueError:
            pass
        cur = self.read()
        if cur and int(cur.get("pid", -1)) == os.getpid():
            try:
                os.unlink(self.path)
            except OSError:
                pass

    # ---------- 上下文管理 ----------
    def __enter__(self):
        info = self.acquire()
        if not info.ok:
            raise LockBusy(info.describe())
        return info

    def __exit__(self, *_exc):
        self.release()
        return False


def _selftest():
    """自测：分片隔离 / 持锁阻塞 / 心跳陈旧接管 / 死进程接管 / 释放。"""
    import tempfile

    bad = []
    def eq(name, got, want):
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    quiet = lambda *_a: None
    with tempfile.TemporaryDirectory() as d:
        # 分片隔离：两个不同资源可同时持有
        a = ResourceLock(d, resource="alpha", echo=quiet)
        b = ResourceLock(d, resource="beta", echo=quiet)
        eq("alpha 拿到", a.acquire().ok, True)
        eq("beta 不互相阻塞", b.acquire().ok, True)
        a.release(); b.release()

        # 同一资源互斥：自身持有时，另一个实例 wait=0 应 busy
        c = ResourceLock(d, resource="same", echo=quiet)
        c.acquire()
        other = ResourceLock(d, resource="same", wait=0, echo=quiet)
        eq("同资源被阻塞", other.acquire().ok, False)
        c.release()

        # 心跳陈旧 ⇒ 可接管
        old = time.time() - 10_000
        with open(os.path.join(d, "lock-beat.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "task": "stale", "beat": old,
                       "startedAt": old}, f)
        eq("心跳陈旧可接管",
           ResourceLock(d, resource="beat", stale=1, wait=0, echo=quiet).acquire().ok, True)

        # 死进程 ⇒ 可接管
        with open(os.path.join(d, "lock-dead.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": 99999999, "task": "dead", "beat": time.time(),
                       "startedAt": time.time()}, f)
        eq("死进程可接管",
           ResourceLock(d, resource="dead", wait=0, echo=quiet).acquire().ok, True)

        # 释放后锁文件应消失
        e = ResourceLock(d, resource="rel", echo=quiet)
        e.acquire(); p = e.path; e.release()
        eq("释放后锁文件消失", os.path.exists(p), False)

    # 活进程判据：当前进程必然活着，离谱 pid 必然死了
    eq("自身 pid 存活", pid_alive(os.getpid()), True)
    eq("离谱 pid 不存活", pid_alive(99999999), False)

    for x in bad:
        print("  ✗ %s" % x)
    print("reslock selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", 9))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
