# -*- coding: utf-8 -*-
"""演示：幂等 + 断点续跑。

    python examples/checkpoint_demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from failclosed.checkpoint import Progress, run_steps

prog = Progress(os.path.join(tempfile.mkdtemp(prefix="checkpoint-demo-"),
                             "progress.json"))
calls = []


def mk(tag):
    def f():
        calls.append(tag)
        return "%s 的产出" % tag
    return f


steps = [("采集", mk("采集"), {"day": "2026-10-15"}),
         ("清洗", mk("清洗")),
         ("导出", mk("导出"), {"格式": "md"})]

print("① 首跑")
run_steps(steps, prog)
print("   实际执行了：", calls)

print()
print("② 再跑一次（模拟「中断后重跑」）")
calls.clear()
run_steps(steps, prog)
print("   实际执行了：", calls, "← 空 = 幂等，一步都没重跑")

print()
print("③ 把「导出」的输入改掉，再跑")
calls.clear()
steps2 = [("采集", mk("采集"), {"day": "2026-10-15"}),
          ("清洗", mk("清洗")),
          ("导出", mk("导出"), {"格式": "json"})]     # ← 格式变了
run_steps(steps2, prog)
print("   实际执行了：", calls, "← 只有输入变的那步重跑")

print()
print("④ 把进度文件弄坏，再跑（fail-closed）")
with open(prog.path, "w", encoding="utf-8") as f:
    f.write("{ 坏掉的 JSON")
calls.clear()
run_steps(steps, prog)
print("   实际执行了：", calls, "← 全部重跑：宁可多跑，绝不跳错")
