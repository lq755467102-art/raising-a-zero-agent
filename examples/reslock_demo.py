# -*- coding: utf-8 -*-
"""演示：按资源分片的锁 —— 同资源互斥、异资源并行、心跳防「假占用」。

    python examples/reslock_demo.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zeroagent.reslock import LockBusy, ResourceLock

locks_dir = tempfile.mkdtemp(prefix="reslock-demo-")
print("锁目录：", locks_dir)
print()

with ResourceLock(locks_dir, resource="downloads", task="导出报表") as lk:
    print("① 拿到 downloads 锁：", lk.describe())

    # 同一资源：应被拦下
    try:
        with ResourceLock(locks_dir, resource="downloads", wait=0, task="另一个导出"):
            print("② （不该出现）同资源竟然也拿到了")
    except LockBusy as e:
        print("② 同资源被正确拦下：")
        print("   " + str(e).replace("\n", "\n   "))

    # 不同资源：不应互相阻塞
    with ResourceLock(locks_dir, resource="report", wait=0, task="生成报告") as lk2:
        print("③ 不同资源（report）不受影响：", lk2.describe())

print()
print("④ 退出上下文后锁文件已释放：",
      not any(f.endswith(".lock") for f in os.listdir(locks_dir)))
