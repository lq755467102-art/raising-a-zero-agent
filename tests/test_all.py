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
from failclosed.contextbudget import Item, check as check_budget
from failclosed.approvalgate import (ApprovalError, ApprovalStore,
                                     SelfApprovalError, UnknownRequest)
from failclosed.checkpoint import Progress, run_steps, signature

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


class TestContextbudget(unittest.TestCase):
    SIZES = {"a.md": 100, "b.md": 900, "c.md": 1000, "d.md": 1001}

    def _read(self, path):
        return self.SIZES.get(path)          # 未知文件 → None（缺失）

    def test_states(self):
        rep = check_budget(
            [Item("a", "a.md", 1000), Item("b", "b.md", 1000),
             Item("c", "c.md", 1000), Item("d", "d.md", 1000),
             Item("g", "ghost.md", 1000)], _chars=self._read)
        st = {r.name: r.state for r in rep.readings}
        self.assertEqual(st["a"], "ok")
        self.assertEqual(st["b"], "near")          # 恰好到接近线
        self.assertEqual(st["c"], "near")          # 恰好等于上限，还没超
        self.assertEqual(st["d"], "over")
        self.assertEqual(st["g"], "missing")

    def test_over_fails_but_missing_does_not(self):
        over = check_budget([Item("d", "d.md", 1000)], _chars=self._read)
        self.assertFalse(over.ok)
        ghost = check_budget([Item("g", "ghost.md", 1000)], _chars=self._read)
        self.assertTrue(ghost.ok)                  # 配置问题 ≠ 超线
        self.assertEqual(len(ghost.missing), 1)

    def test_all_within_budget_passes(self):
        rep = check_budget([Item("a", "a.md", 1000)], _chars=self._read)
        self.assertTrue(rep.ok)


class TestApprovalgate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(os.path.join(self._tmp.name, "approvals.json"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_not_allowed_until_approved(self):
        req = self.store.submit("publish", {"t": 1}, requester="alice")
        self.assertFalse(self.store.is_allowed(req["id"]))
        self.store.decide(req["id"], approver="bob", approve=True)
        self.assertTrue(self.store.is_allowed(req["id"]))

    def test_self_approval_denied(self):
        req = self.store.submit("publish", {"t": 1}, requester="alice")
        with self.assertRaises(SelfApprovalError):
            self.store.decide(req["id"], approver="alice", approve=True)
        self.assertFalse(self.store.is_allowed(req["id"]))          # 状态没变
        self.assertEqual(self.store.get(req["id"])["status"], "pending")

    def test_idempotent_submit(self):
        a = self.store.submit("publish", {"t": 1}, requester="alice")
        b = self.store.submit("publish", {"t": 1}, requester="alice")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.store.list_pending()), 1)

    def test_reject_does_not_allow(self):
        req = self.store.submit("publish", {"t": 2}, requester="carol")
        self.store.decide(req["id"], approver="bob", approve=False)
        self.assertFalse(self.store.is_allowed(req["id"]))

    def test_double_decide_raises(self):
        req = self.store.submit("publish", {"t": 3}, requester="carol")
        self.store.decide(req["id"], approver="bob", approve=True)
        with self.assertRaises(ApprovalError):
            self.store.decide(req["id"], approver="bob", approve=False)

    def test_unknown_request(self):
        self.assertFalse(self.store.is_allowed("nope"))
        with self.assertRaises(UnknownRequest):
            self.store.decide("nope", approver="bob", approve=True)

    def test_audit_records_self_approval_denial(self):
        req = self.store.submit("publish", {"t": 4}, requester="alice")
        with self.assertRaises(SelfApprovalError):
            self.store.decide(req["id"], approver="alice", approve=True)
        actions = [a["action"] for a in self.store.audit_log()]
        self.assertIn("submit", actions)
        self.assertIn("self-approval-denied", actions)


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "sub", "progress.json")
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def _step(self, tag):
        def fn():
            self.calls.append(tag)
            return tag + "-result"
        return fn

    def _steps(self):
        return [("a", self._step("a")), ("b", self._step("b")), ("c", self._step("c"))]

    def test_first_run_executes_all(self):
        res = run_steps(self._steps(), Progress(self.path), echo=_quiet)
        self.assertEqual(self.calls, ["a", "b", "c"])
        self.assertEqual(res["b"], "b-result")

    def test_rerun_is_idempotent(self):
        prog = Progress(self.path)
        run_steps(self._steps(), prog, echo=_quiet)
        self.calls.clear()
        run_steps(self._steps(), prog, echo=_quiet)
        self.assertEqual(self.calls, [])            # 一步都不重跑

    def test_changed_inputs_rerun(self):
        prog = Progress(self.path)
        run_steps([("a", self._step("a"))], prog, echo=_quiet)
        self.calls.clear()
        run_steps([("a", self._step("a"), {"v": 2})], prog, echo=_quiet)
        self.assertEqual(self.calls, ["a"])         # 输入变了 ⇒ 重新执行

    def test_corrupt_progress_is_fail_closed(self):
        """进度文件坏掉 ⇒ 一切视为未完成（宁可重跑，不可跳错）。"""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        run_steps(self._steps(), Progress(self.path), echo=_quiet)
        self.assertEqual(self.calls, ["a", "b", "c"])

    def test_atomic_write_leaves_no_tmp(self):
        run_steps(self._steps(), Progress(self.path), echo=_quiet)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_signature_stability(self):
        self.assertEqual(signature("k", {"x": 1}), signature("k", {"x": 1}))
        self.assertNotEqual(signature("k", {"x": 1}), signature("k", {"x": 2}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
