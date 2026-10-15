# -*- coding: utf-8 -*-
"""failclosed 单元测试。

    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from failclosed.redact import ContextRule, LeakError, Redactor
from failclosed.checkgate import run_gate
from failclosed.reslock import LockBusy, ResourceLock, pid_alive

def _quiet(*_a, **_k):   # 静音 echo（Python 3.8 起可用；staticmethod 对象要到 3.10 才可调用）
    return None


class TestRedact(unittest.TestCase):
    def test_replaces_all_pairs(self):
        r = Redactor(pairs=[("Acme", "<ORG>"), ("1001", "<ID>")])
        out = r.redact("Acme / 1001")
        self.assertNotIn("Acme", out)
        self.assertNotIn("1001", out)

    def test_longest_first(self):
        """短值不能先吃掉长值的子串。"""
        r = Redactor(pairs=[("A", "<A>"), ("AB", "<B>")])
        self.assertEqual(r.redact("AB"), "<B>")

    def test_residual_detects_unredacted(self):
        r = Redactor(pairs=[("secret", "<S>")])
        self.assertNotEqual(r.residual("这里还有 secret"), [])
        self.assertEqual(r.residual("<S>"), [])

    def test_strict_raises_when_residual_left(self):
        """fail-closed：残留检出时，严格模式必须抛错、不产出。"""
        class Leaky(Redactor):
            def residual(self, text):
                return ["forced"]

        with self.assertRaises(LeakError):
            Leaky().redact_strict("anything")

    def test_context_rule_only_near_keyword(self):
        rule = ContextRule(r"\d{8}", ["account", "账号"], "<ACC>")
        self.assertEqual(rule.findall("账号 10000001"), ["10000001"])
        self.assertEqual(rule.findall("版本 98765432 发布"), [])

    def test_none_input_is_safe(self):
        self.assertEqual(Redactor().redact(None), "")


class _GateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.checks = os.path.join(self.dir, "checks")
        os.makedirs(self.checks)

    def tearDown(self):
        self._tmp.cleanup()

    def mk_check(self, name, body):
        with open(os.path.join(self.checks, name + ".py"), "w", encoding="utf-8") as f:
            f.write(body)

    def write_registry(self, levels):
        p = os.path.join(self.dir, "registry.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"levels": levels}, f)
        return p


class TestCheckgate(_GateCase):
    def test_unreadable_registry_blocks(self):
        res = run_gate(os.path.join(self.dir, "nope.json"), self.checks, echo=_quiet)
        self.assertFalse(res.ok)
        self.assertIn("<registry>", res.failed)

    def test_pass_fail_and_warn_levels(self):
        self.mk_check("ok", "import sys; sys.exit(0)")
        self.mk_check("bad", "import sys; sys.exit(1)")
        self.mk_check("env", "import sys; sys.exit(1)")
        reg = self.write_registry({
            "ok": {"level": "fail", "ok": "全好"},
            "bad": {"level": "fail", "ok": "x", "fail": "内容有问题"},
            "env": {"level": "warn", "ok": "x", "fail": "环境有缺口"},
        })
        res = run_gate(reg, self.checks, echo=_quiet)
        self.assertIn("ok", res.passed)
        self.assertIn("bad", res.failed)
        self.assertIn("env", res.warned)
        self.assertFalse(res.ok)

    def test_timeout_blocks_not_passes(self):
        self.mk_check("slow", "import time; time.sleep(5)")
        reg = self.write_registry({"slow": {"level": "fail", "ok": "x"}})
        res = run_gate(reg, self.checks, timeout=1, echo=_quiet)
        self.assertIn("slow", res.failed)      # 判不了 ⇒ 阻塞，绝不冒充通过

    def test_undeclared_level_blocks(self):
        self.mk_check("nd", "import sys; sys.exit(1)")
        reg = self.write_registry({"nd": {"ok": "x"}})   # 没写 level
        res = run_gate(reg, self.checks, echo=_quiet)
        self.assertIn("nd", res.failed)

    def test_missing_script_warns_not_passes(self):
        reg = self.write_registry({"ghost": {"level": "fail", "ok": "x"}})
        res = run_gate(reg, self.checks, echo=_quiet)
        self.assertIn("ghost", res.warned)
        self.assertNotIn("ghost", res.passed)


class TestReslock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def lock(self, **kw):
        kw.setdefault("echo", _quiet)
        return ResourceLock(self.dir, **kw)

    def test_sharding_isolated(self):
        a, b = self.lock(resource="alpha"), self.lock(resource="beta")
        self.assertTrue(a.acquire().ok)
        self.assertTrue(b.acquire().ok)        # 不同资源互不阻塞
        a.release(); b.release()

    def test_same_resource_is_busy(self):
        held = self.lock(resource="same")
        held.acquire()
        self.assertFalse(self.lock(resource="same", wait=0).acquire().ok)
        held.release()

    def test_stale_heartbeat_takeover(self):
        old = time.time() - 10_000
        with open(os.path.join(self.dir, "lock-beat.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "beat": old, "startedAt": old}, f)
        self.assertTrue(self.lock(resource="beat", stale=1, wait=0).acquire().ok)

    def test_dead_pid_takeover(self):
        with open(os.path.join(self.dir, "lock-dead.lock"), "w", encoding="utf-8") as f:
            json.dump({"pid": 99999999, "beat": time.time(),
                       "startedAt": time.time()}, f)
        self.assertTrue(self.lock(resource="dead", wait=0).acquire().ok)

    def test_release_removes_lockfile(self):
        lk = self.lock(resource="rel")
        lk.acquire()
        path = lk.path
        lk.release()
        self.assertFalse(os.path.exists(path))

    def test_context_manager_raises_when_busy(self):
        held = self.lock(resource="ctx")
        held.acquire()
        with self.assertRaises(LockBusy):
            with self.lock(resource="ctx", wait=0):
                pass
        held.release()

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(99999999))


if __name__ == "__main__":
    unittest.main(verbosity=2)
