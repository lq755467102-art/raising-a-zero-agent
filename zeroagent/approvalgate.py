# -*- coding: utf-8 -*-
"""审批闸门 —— 有副作用的操作必须有人批，且**不能自己批自己**。

问题
----
自动化里那些「有副作用」的动作（改配置、发布、花钱、动别人的数据），如果没有闸门，
就会被**静默放行** —— 这又是一类「没报错但错了」。

最隐蔽的一种绕过是：**申请人 = 审批人**。形式上"审过了"，实际上等于没审。

fail-closed 三条
----------------
1. **只有 ``approved`` 才放行**；未知状态、不存在、已驳回 —— 一律不放行。
2. **申请人 == 审批人 ⇒ 拒绝**（:class:`SelfApprovalError`），且**记入审计**。
3. 同一人 + 同内容 + 仍在待审 ⇒ **幂等**，不重复排队
   （指纹 = kind + 内容 + 申请人）。

存储是**一个 JSON 文件**（单机演示用）。接口不绑定存储，
换成数据库 / KV 时上层不变。每次状态变更都写审计记录。

用法::

    from zeroagent.approvalgate import ApprovalStore, SelfApprovalError

    store = ApprovalStore("approvals.json")
    req = store.submit("publish", {"target": "site"}, requester="alice")
    assert not store.is_allowed(req["id"])            # 还没批 ⇒ 不放行

    store.decide(req["id"], approver="bob", approve=True)
    assert store.is_allowed(req["id"])                # 批了 ⇒ 放行

    store.decide(req["id"], approver="alice", approve=True)   # ← 抛 SelfApprovalError
"""

import hashlib
import json
import os
import time

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"


class ApprovalError(Exception):
    """审批相关的通用错误。"""


class SelfApprovalError(ApprovalError):
    """申请人试图审批自己的申请 —— 形式上审了，等于没审。"""


class UnknownRequest(ApprovalError):
    """没有这个申请。"""


def fingerprint(kind, payload, requester):
    """同一个人 + 同样内容 ⇒ 同一个指纹（用于幂等去重）。"""
    blob = json.dumps({"k": kind, "p": payload, "r": requester},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class ApprovalStore:
    """一个最小可用的审批存储（JSON 文件）。"""

    def __init__(self, path):
        self.path = path

    # ---------- 存储 ----------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("结构不对")
            data.setdefault("requests", {})
            data.setdefault("audit", [])
            return data
        except Exception:
            return {"requests": {}, "audit": []}

    def _save(self, data):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)      # 原子替换，避免读到半截 JSON

    def _audit(self, data, action, req_id, actor, extra=None):
        data["audit"].append({"at": time.time(), "action": action,
                              "id": req_id, "actor": actor, **(extra or {})})

    # ---------- 提交（幂等） ----------
    def submit(self, kind, payload, requester, req_id=None):
        data = self._load()
        fp = fingerprint(kind, payload, requester)
        for rid, r in data["requests"].items():
            if r.get("fingerprint") == fp and r.get("status") == PENDING:
                return r                            # ★ 幂等：不重复排队
        rid = req_id or fp
        req = {"id": rid, "kind": kind, "payload": payload, "requester": requester,
               "status": PENDING, "fingerprint": fp, "createdAt": time.time(),
               "decidedBy": None, "decidedAt": None, "note": ""}
        data["requests"][rid] = req
        self._audit(data, "submit", rid, requester)
        self._save(data)
        return req

    # ---------- 裁决 ----------
    def decide(self, req_id, approver, approve, note=""):
        data = self._load()
        r = data["requests"].get(req_id)
        if not r:
            raise UnknownRequest("没有这个申请：%s" % req_id)
        if r.get("status") != PENDING:
            raise ApprovalError("申请 %s 已经是 %s，不能重复裁决"
                                % (req_id, r.get("status")))

        if approver == r.get("requester"):
            # ★ 防自批：形式上有审批，实质上等于没审批。
            self._audit(data, "self-approval-denied", req_id, approver)
            self._save(data)
            raise SelfApprovalError("申请人 %s 不能审批自己的申请（%s）"
                                    % (approver, req_id))

        r["status"] = APPROVED if approve else REJECTED
        r["decidedBy"] = approver
        r["decidedAt"] = time.time()
        r["note"] = note
        self._audit(data, "decide", req_id, approver, {"to": r["status"]})
        self._save(data)
        return r

    # ---------- 查询 ----------
    def get(self, req_id):
        return self._load()["requests"].get(req_id)

    def is_allowed(self, req_id):
        """★ fail-closed：只有 ``approved`` 才放行；其余一切都不放行。"""
        r = self.get(req_id)
        return bool(r) and r.get("status") == APPROVED

    def list_pending(self):
        return [r for r in self._load()["requests"].values()
                if r.get("status") == PENDING]

    def audit_log(self):
        return self._load()["audit"]


def _selftest():
    import tempfile

    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    with tempfile.TemporaryDirectory() as d:
        store = ApprovalStore(os.path.join(d, "approvals.json"))
        req = store.submit("publish", {"target": "x"}, requester="alice")
        eq("提交后是待审", req["status"], PENDING)
        eq("未批 ⇒ 不放行", store.is_allowed(req["id"]), False)

        # 幂等：同人同内容再提交 ⇒ 同一条
        again = store.submit("publish", {"target": "x"}, requester="alice")
        eq("幂等去重", again["id"], req["id"])
        eq("待审只有 1 条", len(store.list_pending()), 1)

        # 自批 ⇒ 拒绝
        try:
            store.decide(req["id"], approver="alice", approve=True)
            eq("自批必须被拒", False, True)
        except SelfApprovalError:
            eq("自批必须被拒", True, True)
        eq("自批后仍是待审", store.get(req["id"])["status"], PENDING)
        eq("自批后仍不放行", store.is_allowed(req["id"]), False)

        # 换人批 ⇒ 放行
        store.decide(req["id"], approver="bob", approve=True)
        eq("他人批 ⇒ 放行", store.is_allowed(req["id"]), True)
        eq("裁决人记录", store.get(req["id"])["decidedBy"], "bob")

        # 重复裁决 ⇒ 报错
        try:
            store.decide(req["id"], approver="bob", approve=False)
            eq("重复裁决应报错", False, True)
        except ApprovalError:
            eq("重复裁决应报错", True, True)

        # 驳回 ⇒ 不放行
        r2 = store.submit("publish", {"target": "y"}, requester="carol")
        store.decide(r2["id"], approver="bob", approve=False)
        eq("驳回 ⇒ 不放行", store.is_allowed(r2["id"]), False)
        eq("驳回状态", store.get(r2["id"])["status"], REJECTED)

        # 不存在的申请
        eq("不存在的申请不放行", store.is_allowed("nope"), False)
        try:
            store.decide("nope", approver="bob", approve=True)
            eq("不存在的申请应报错", False, True)
        except UnknownRequest:
            eq("不存在的申请应报错", True, True)

        # 审计留痕（提交 2 + 自批被拒 1 + 裁决 2 = 5）
        eq("审计有记录", len(store.audit_log()) >= 5, True)
        eq("自批被拒已留痕",
           any(a["action"] == "self-approval-denied" for a in store.audit_log()), True)

    for b in bad:
        print("  ✗ %s" % b)
    print("approvalgate selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
