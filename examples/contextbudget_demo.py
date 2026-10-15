# -*- coding: utf-8 -*-
"""演示：上下文预算守卫（水位）。

    python examples/contextbudget_demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from failclosed.contextbudget import Item, check

d = tempfile.mkdtemp(prefix="ctxbudget-demo-")


def write(name, chars):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write("x" * chars)
    return p


# 造三个「笔记文件」：一个很空闲、一个接近线、一个超线
p_ok = write("notes.md", 1200)          # 1200 / 4000
p_near = write("memory.md", 7700)       # 7700 / 8000 = 96% ⇒ near
p_over = write("changelog.md", 9500)    # 9500 / 9000 ⇒ over

items = [
    Item("notes", p_ok, 4000),
    Item("memory", p_near, 8000),
    Item("changelog", p_over, 9000),
    Item("ghost", os.path.join(d, "nope.md"), 1000),   # 读不到
]

report = check(items)
for r in report.readings:
    print(r.describe())
print()
print("判定：" + report.summary())
print("结论：", "有文件超线 ⇒ 尾部正在被静默截断" if not report.ok else "全部在预算内")
sys.exit(0 if report.ok else 1)
