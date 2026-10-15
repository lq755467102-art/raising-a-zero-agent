# -*- coding: utf-8 -*-
"""演示：按注册表跑一遍检查闸门。

    python examples/checkgate_demo/run.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from failclosed.checkgate import run_gate

print("═══ 检查闸门（fail-closed）═══")
result = run_gate(os.path.join(HERE, "registry.json"),
                  os.path.join(HERE, "checks"),
                  timeout=30)
print()
print("判定：" + result.summary())
print("0 阻塞才算通过 —— 警告会显形，但不阻塞。" if result.ok
      else "有阻塞项 ⇒ 不应放行。")
sys.exit(0 if result.ok else 1)
