# -*- coding: utf-8 -*-
"""演示：审批闸门 —— 危险操作必须有人批，且不能自己批自己。

    python examples/approvalgate_demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from failclosed.approvalgate import ApprovalStore, SelfApprovalError

store = ApprovalStore(os.path.join(tempfile.mkdtemp(prefix="approval-demo-"),
                                   "approvals.json"))

print("① 提交一个「有副作用」的申请（发布）")
req = store.submit("publish", {"target": "demo-site"}, requester="alice")
print("   申请 id:", req["id"], "状态:", req["status"])
print("   现在能放行吗？", store.is_allowed(req["id"]), "← 没批，不放行（fail-closed）")

print()
print("② 重复提交同一个人、同样的内容")
again = store.submit("publish", {"target": "demo-site"}, requester="alice")
print("   返回同一条 id 吗？", again["id"] == req["id"], "← 幂等，不重复排队")

print()
print("③ 让申请人自己批自己")
try:
    store.decide(req["id"], approver="alice", approve=True)
    print("   （不该到这里）")
except SelfApprovalError as e:
    print("   被拒绝：", e)
print("   状态仍是：", store.get(req["id"])["status"], "← 形式上有审批，实质等于没审批，必须拦下")

print()
print("④ 换一个审批人批准")
store.decide(req["id"], approver="bob", approve=True, note="看起来没问题")
print("   现在能放行吗？", store.is_allowed(req["id"]))

print()
print("⑤ 审计留痕")
for a in store.audit_log():
    print("   -", a["action"], "|", a["actor"], "|", a["id"])
