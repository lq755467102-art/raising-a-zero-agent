# -*- coding: utf-8 -*-
"""演示：词典脱敏 + 残留自检。

    python examples/redact_demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zeroagent.redact import ContextRule, LeakError, Redactor

# 词典来自「已知的真值」（配置里的公司名 / 账号），不靠正则猜
r = Redactor(
    pairs=[("Acme Corporation", "<公司>"), ("ACME", "<公司>"), ("10000001", "<账号>")],
    rules=[ContextRule(r"\d{8}", ["账号", "account"], "<账号>")],
)

doc = "Acme Corporation 的账号 10000001 已完成；另见 account 20880000。"
print("原文   ：", doc)
print("脱敏后 ：", r.redact(doc))

print()
print("长值优先（避免短值吃掉长值子串）")
r2 = Redactor(pairs=[("ACME", "<缩写>"), ("Acme Corporation", "<全称>")])
print("  ", r2.redact("Acme Corporation"))

print()
print("残留自检（fail-closed 的判据）")
print("  原文残留：", r.residual(doc), "→ 有残留，说明还没脱敏 OK")
print("  产物残留：", r.residual(r.redact(doc)), "→ 空，说明干净")

print()
print("严格模式：干净输入正常产出，有残留则抛错")
try:
    print("  ", r.redact_strict(doc))
except LeakError as e:
    print("  抛错：", e)
