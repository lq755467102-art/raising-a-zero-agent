# -*- coding: utf-8 -*-
"""幂等 + 断点续跑 —— 让任务「跑一次」和「跑很多次」结果一样。

问题
----
多步任务中途失败后重跑，两种朴素做法**都不安全**：

- **从头再来** ⇒ 浪费，而且会**重复执行有副作用的步骤**（静默重复写入）；
- **跳过"看起来做过"的步** ⇒ **可能跳错**（输入其实已经变了）。

做法
----
1. 每步有 **key + 输入指纹**，两者合起来决定「这步做过没有」；
2. 做完把结果记进**进度文件**；重跑时直接取结果，**不重新执行**；
3. ★ **输入变了 ⇒ 指纹变了 ⇒ 视为未完成**（防止「改了东西却跳过」）；
4. 进度写入用**原子替换**（临时文件 → rename），避免读到半截 JSON；
5. ★ **fail-closed：进度文件读不出来 ⇒ 一律视为全部未完成** ——
   宁可多跑一遍（幂等步不会有害），也**绝不跳错**。

用法::

    from failclosed.checkpoint import Progress, run_steps

    prog = Progress("run/progress.json")
    steps = [
        ("fetch",  fetch,  {"day": "2026-10-15"}),
        ("clean",  clean,  None),
        ("export", export, {"format": "md"}),
    ]
    results = run_steps(steps, prog)   # 中断后重跑：已完成的自动跳过

注意：``result`` 需要能被 JSON 序列化（默认用 ``str()`` 兜底）。
"""

import hashlib
import json
import os
import time


def signature(key, inputs=None):
    """``(key, 输入)`` → 指纹。**输入变了，指纹就变** ⇒ 该步视为未完成。"""
    blob = json.dumps({"k": key, "i": inputs}, ensure_ascii=False,
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class Progress:
    """进度存储（一个 JSON 文件）。"""

    def __init__(self, path):
        self.path = path

    def _load(self):
        """★ fail-closed：读不出 / 坏 JSON ⇒ 返回空 ⇒ **一切视为未完成**。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
                return {"entries": {}}
            return data
        except Exception:
            return {"entries": {}}

    def _save(self, data):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)          # ★ 原子替换

    def done(self, key, inputs=None):
        return signature(key, inputs) in self._load()["entries"]

    def result(self, key, inputs=None):
        entry = self._load()["entries"].get(signature(key, inputs))
        return entry.get("result") if entry else None

    def record(self, key, inputs, result):
        data = self._load()
        data["entries"][signature(key, inputs)] = {
            "key": key, "at": time.time(), "result": result}
        self._save(data)

    def clear(self):
        self._save({"entries": {}})

    def snapshot(self):
        return self._load()["entries"]


def run_steps(steps, progress, echo=None):
    """按顺序执行 steps；**已完成的直接取结果，不重新执行**。

    :param steps: ``[(key, fn, inputs), ...]``（``inputs`` 可省略）
    :return: ``{key: result}``
    """
    echo = echo or (lambda *_a: None)
    out = {}
    for item in steps:
        key, fn = item[0], item[1]
        inputs = item[2] if len(item) > 2 else None
        if progress.done(key, inputs):
            out[key] = progress.result(key, inputs)
            echo("[checkpoint] 跳过已完成：%s" % key)
            continue
        res = fn()
        progress.record(key, inputs, res)
        out[key] = res
        echo("[checkpoint] 完成：%s" % key)
    return out


def _selftest():
    import tempfile

    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    quiet = lambda *_a: None

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "progress.json")
        calls = []

        def mk(tag):
            def f():
                calls.append(tag)
                return "%s-result" % tag
            return f

        steps = [("a", mk("a")), ("b", mk("b")), ("c", mk("c"))]

        prog = Progress(path)
        r1 = run_steps(steps, prog, echo=quiet)
        eq("首跑执行了 3 步", calls, ["a", "b", "c"])
        eq("结果正确", r1, {"a": "a-result", "b": "b-result", "c": "c-result"})

        # ★ 幂等：重跑一步都不执行
        calls.clear()
        r2 = run_steps(steps, prog, echo=quiet)
        eq("重跑零执行（幂等）", calls, [])
        eq("重跑结果一致", r2, r1)

        # ★ 输入变了 ⇒ 重跑
        calls.clear()
        run_steps([("a", mk("a"), {"v": 2})], prog, echo=quiet)
        eq("输入变了 ⇒ 重新执行", calls, ["a"])
        eq("原输入的记录还在", prog.done("a"), True)
        eq("新输入是独立记录", prog.done("a", {"v": 2}), True)

        # ★ fail-closed：进度文件坏掉 ⇒ 一切视为未完成
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        calls.clear()
        run_steps(steps, prog, echo=quiet)
        eq("进度坏掉 ⇒ 全部重跑（fail-closed）", calls, ["a", "b", "c"])

        # 原子写：不该留 .tmp
        eq("无残留 .tmp", os.path.exists(path + ".tmp"), False)

        # 指纹稳定性
        eq("同输入同指纹", signature("k", {"x": 1}), signature("k", {"x": 1}))
        eq("异输入异指纹", signature("k", {"x": 1}) != signature("k", {"x": 2}), True)

    for b in bad:
        print("  ✗ %s" % b)
    print("checkpoint selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
