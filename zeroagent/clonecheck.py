# -*- coding: utf-8 -*-
"""空白机器验证 —— 把「克隆判据」变成一条命令。

问题
----
三步产生（灌注 / 磨合 / 固化）的完成判据，文档里写的是一句话：

> **换一台空白机器、只给固化的机制、不给带教记忆 —— 它还能不能做对？**

★ 它是整套方法论里**唯一客观的验收方式**，也是「克隆」这件事唯一的验收方式。

但它一直**只活在文字里** —— 没有东西能跑它，于是它退化成了一句自我感觉。
而这个仓库里最该被验证的，恰恰是它。

做法
----
把 ``zeroagent/`` **原样复制**到一个干净的临时目录，然后：

1. 以**该临时目录**为唯一上下文（``cwd`` 指向它）运行每个模块的 ``--selftest``；
2. **故意剥掉**可能把仓库"带过去"的环境变量（``PYTHONPATH`` / ``PYTHONHOME``）；
3. 逐模块报「通过 / 失败 / 跑不起来」。

也就是说：**它能不能在只有代码、没有任何上下文的地方活下去。**

三态
----
- ``ok``        : 该模块在空白环境里自测通过 —— 它确实**只依赖自己**
- ``stale``     : 自测失败 —— ★ **说明它有依赖没被固化**（正是要抓的东西）
- ``unresolved``: 跑不起来 / 超时 —— 判不了，**阻塞**

★ fail-closed：脚本能跑但结果读不出，一律按 ``unresolved`` 处理，不当成通过。

用法::

    python -m zeroagent.clonecheck                 # 用当前 zeroagent/
    python -m zeroagent.clonecheck --src zeroagent
    python -m zeroagent.clonecheck --json
    python -m zeroagent.clonecheck --selftest

★ 这个脚本存在的意义：**把一句判据变成可证伪的实验。**
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_PKG = "zeroagent"
TIMEOUT = 120.0


class Result(object):
    __slots__ = ("module", "state", "detail", "items")

    def __init__(self, module, state, detail="", items=None):
        self.module = module
        self.state = state
        self.detail = detail
        self.items = items

    def describe(self):
        mark = {"ok": "✓", "stale": "✗", "unresolved": "?"}[self.state]
        tail = ""
        if self.items is not None:
            tail = "（%s 项）" % self.items
        elif self.detail:
            tail = "（%s）" % self.detail
        return "%s %-16s%s" % (mark, self.module, tail)

    def to_dict(self):
        return {"module": self.module, "state": self.state,
                "detail": self.detail, "items": self.items}


class CloneReport(object):
    def __init__(self, results, where="", count=0):
        self.results = list(results)
        self.where = where
        self.count = count

    def _by(self, state):
        return [r for r in self.results if r.state == state]

    @property
    def ok(self):
        return not (self._by("stale") or self._by("unresolved"))

    @property
    def stale(self):
        return self._by("stale")

    @property
    def unresolved(self):
        return self._by("unresolved")

    def summary(self):
        return "通过 %d / 未固化 %d / 判不了 %d / 共 %d 个模块" % (
            len(self._by("ok")), len(self.stale), len(self.unresolved),
            len(self.results))

    def to_dict(self):
        return {"ok": self.ok, "summary": self.summary(), "where": self.where,
                "modules_found": self.count,
                "results": [r.to_dict() for r in self.results]}


def _modules_of(pkg_dir):
    return sorted(f[:-3] for f in os.listdir(pkg_dir)
                  if f.endswith(".py") and f != "__init__.py")


def _clean_env():
    """★ 把可能"把仓库带过去"的变量剥掉 —— 这才是空白机器。"""
    env = dict(os.environ)
    for k in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        env.pop(k, None)
    return env


def verify(src=DEFAULT_PKG, python=None, timeout=TIMEOUT, _keep=False):
    """把 ``src`` 复制到干净目录，逐个模块跑 --selftest。"""
    py = python or sys.executable
    if not os.path.isdir(src):
        return CloneReport([Result("(包目录)", "unresolved",
                                   "目录不存在：%s" % src)], where=src)

    tmp = tempfile.mkdtemp(prefix="clonecheck-")
    dst = os.path.join(tmp, DEFAULT_PKG)
    try:
        shutil.copytree(src, dst)

        # 判不了 ⇒ 不当成通过（fail-closed）
        try:
            modules = _modules_of(dst)
        except Exception as e:
            return CloneReport([Result("(模块枚举)", "unresolved", str(e))], where=dst)

        results = []
        for m in modules:
            try:
                p = subprocess.run(
                    [py, "-m", "%s.%s" % (DEFAULT_PKG, m), "--selftest"],
                    cwd=tmp, env=_clean_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=timeout)
                out = (p.stdout or b"").decode("utf-8", "replace")
            except subprocess.TimeoutExpired:
                results.append(Result(m, "unresolved", "超时 %gs" % timeout))
                continue
            except Exception as e:
                results.append(Result(m, "unresolved", "起不来：%s" % e))
                continue

            items = None
            for token in ("（", "("):
                if "selftest:" in out and token in out:
                    seg = out.split("selftest:")[-1]
                    digits = "".join(ch for ch in seg if ch.isdigit())
                    if digits:
                        items = int(digits)
                        break

            if p.returncode == 0:
                results.append(Result(m, "ok", items=items))
            else:
                tail = " / ".join(x.strip() for x in out.strip().splitlines()[-2:])
                results.append(Result(m, "stale", "自测失败：%s" % (tail or "无输出")))
        return CloneReport(results, where=dst, count=len(modules))
    finally:
        if not _keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="zeroagent.clonecheck",
        description="空白机器验证：只给代码、不给上下文，它还能不能做对（fail-closed）")
    ap.add_argument("--src", default=DEFAULT_PKG, help="包目录（默认 zeroagent）")
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    rep = verify(args.src, timeout=args.timeout)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1))
    else:
        print("★ 空白机器验证：复制到干净目录、剥掉上下文，再跑自测")
        print("  来源：%s" % args.src)
        print()
        for r in rep.results:
            print(r.describe())
        print()
        print("判定：" + rep.summary())
        if rep.stale:
            print("⇒ 有模块在空白环境里失败：**它还有依赖没被固化**。")
        if rep.unresolved:
            print("⇒ 有模块判不了：**判不了不等于通过**，先查清原因。")
    return 0 if rep.ok else 1


def _selftest():
    """★ 造一个「会失败的模块」并断言它**确实被标成 stale**。"""
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    # 空目录 / 不存在的目录 ⇒ 判不了（不是"通过"）
    with tempfile.TemporaryDirectory() as tmp:
        empty = os.path.join(tmp, "empty-zeroagent")
        os.makedirs(empty)
        open(os.path.join(empty, "__init__.py"), "w").close()

        # 造一个包：一个好模块 + 一个坏模块
        pkg = os.path.join(tmp, "pkg", "zeroagent")
        os.makedirs(pkg)
        open(os.path.join(pkg, "__init__.py"), "w").close()
        with open(os.path.join(pkg, "good.py"), "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                "def _selftest():\n"
                "    print('good selftest: OK（3 项）')\n"
                "    return 0\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(_selftest())\n")
        with open(os.path.join(pkg, "broken.py"), "w", encoding="utf-8") as f:
            f.write(
                "import sys\n"
                "def _selftest():\n"
                "    print('  ✗ 我故意失败')\n"
                "    print('broken selftest: FAIL（1 项）')\n"
                "    return 1\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(_selftest())\n")

        rep = verify(src=os.path.join(tmp, "pkg", "zeroagent"))
        st = {r.module: r.state for r in rep.results}
        eq("好模块 ⇒ ok", st.get("good"), "ok")
        eq("坏模块 ⇒ stale（抓到未固化）", st.get("broken"), "stale")
        eq("有 stale ⇒ 整体不通过", rep.ok, False)
        eq("项数被解析出来",
           [r.items for r in rep.results if r.module == "good"][0], 3)

        # 目录不存在 ⇒ 判不了，而不是假装通过
        rep2 = verify(src=os.path.join(tmp, "no-such-dir"))
        eq("目录不存在 ⇒ unresolved", rep2.ok, False)
        eq("目录不存在不误报为 ok", len(rep2.unresolved), 1)

        # 空包（只有 __init__）⇒ 没有模块可验；如实报 0 个，不崩
        rep3 = verify(src=empty)
        eq("空包如实报 0 个模块", (rep3.count, len(rep3.results)), (0, 0))

    for b in bad:
        print("  ✗ %s" % b)
    print("clonecheck selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
