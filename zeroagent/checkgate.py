# -*- coding: utf-8 -*-
"""检查器注册表 + 运行闸门（fail-closed）。

契约
----
- 一个「检查器」= 一个可执行脚本，**退出码 0 = 通过，非 0 = 未通过**。
- 注册表（JSON）声明：有哪几项检查、每项失败算「阻塞（fail）」还是「警告（warn）」、
  成功/失败各显示什么文案。
- 本模块只负责**按注册表跑一遍并汇总**，不关心检查器内部逻辑。

为什么要有「注册表」
--------------------
把「有哪几项检查 / 该红该黄 / 显示什么」收口到**一个文件**，加一项检查只改一处。
否则常见坏法：判据散落在运行脚本和健康看板两处，改一处漏一处，**静默少跑**。

fail-closed 的四条（都宁可拦下，也不放过）
----------------------------------------
1. **注册表读不出来** ⇒ 整闸失败。否则会「一项都没跑」却看着绿。
2. **某项没声明 level** ⇒ 按阻塞处理（不凭默认值静默降级）。
3. **某项超时** ⇒ 按阻塞处理 ——「判不了」绝不允许冒充「通过」。
4. **检查脚本不存在** ⇒ 明说「该项没检查」，不当作通过。

用法::

    from zeroagent.checkgate import run_gate
    result = run_gate("registry.json", "checks/", timeout=120)
    raise SystemExit(0 if result.ok else 1)

注册表格式::

    {
      "levels": {
        "check_docs":  {"level": "fail", "ok": "文档索引一致",  "fail": "文档索引漂移"},
        "check_env":   {"level": "warn", "ok": "环境齐备",      "fail": "环境有缺口"}
      }
    }
"""

import json
import os
import subprocess
import sys

DEFAULT_TIMEOUT = 120


class RegistryError(Exception):
    """注册表缺失 / 坏 JSON / 结构不对。"""


def load_registry(path):
    """读注册表并校验；结构不对直接抛 :class:`RegistryError`。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:                      # 缺文件 / 坏 JSON 都归这里
        raise RegistryError("注册表读不出来：%s" % e)
    levels = data.get("levels")
    if not isinstance(levels, dict) or not levels:
        raise RegistryError("注册表缺 levels 或为空：%s" % path)
    return levels


class GateResult:
    """一次闸门运行的结果。``ok`` 只取决于有没有**阻塞**项（警告不阻塞）。"""

    def __init__(self):
        self.passed = []
        self.warned = []
        self.failed = []

    @property
    def ok(self):
        return not self.failed

    def summary(self):
        return "通过 %d / 警告 %d / 阻塞 %d" % (
            len(self.passed), len(self.warned), len(self.failed))


def run_gate(registry_path, check_dir, python_bin=None, timeout=DEFAULT_TIMEOUT,
             undeclared_level="fail", echo=print):
    """按注册表跑一遍。

    :param registry_path: 注册表 JSON 路径
    :param check_dir:     检查器脚本所在目录（`<名字>.py`，或注册表里的 ``script``）
    :param timeout:       单个检查器的秒级上限；超时按**阻塞**处理
    :param undeclared_level: 注册表里漏写 level 时按哪一档处理（默认阻塞）
    """
    python_bin = python_bin or sys.executable
    result = GateResult()

    try:
        levels = load_registry(registry_path)
    except RegistryError as e:
        echo("✗ %s ⇒ 一项都没跑，按阻塞处理" % e)
        result.failed.append("<registry>")
        return result

    for name, spec in levels.items():
        spec = spec or {}
        lvl = spec.get("level")
        note = ""
        if lvl not in ("fail", "warn"):
            lvl = undeclared_level
            note = "（未声明 level ⇒ 按阻塞处理）"

        script = spec.get("script") or (name + ".py")
        spath = os.path.join(check_dir, script)
        if not os.path.isfile(spath):
            result.warned.append(name)
            echo("⚠ %s：缺 %s —— 该项**没检查**，不算通过" % (name, script))
            continue

        try:
            proc = subprocess.run([python_bin, spath], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            result.failed.append(name)
            echo("✗ %s：超时·判不了（>%ss）⇒ 按阻塞处理，绝不当作通过" % (name, timeout))
            continue
        except Exception as e:                  # 跑不起来也算判不了
            result.failed.append(name)
            echo("✗ %s：跑不起来（%s）⇒ 按阻塞处理" % (name, e))
            continue

        if proc.returncode == 0:
            result.passed.append(name)
            echo("✓ %s%s" % (spec.get("ok") or name, note))
        else:
            (result.failed if lvl == "fail" else result.warned).append(name)
            mark = "✗" if lvl == "fail" else "⚠"
            echo("%s %s%s" % (mark, spec.get("fail") or name, note))
            detail = spec.get("detailLines", 0) or 0
            if detail:
                out = (proc.stdout or "") + (proc.stderr or "")
                for line in out.splitlines()[:detail]:
                    echo("    " + line)

    return result


def _selftest():
    """自测：临时目录里造几种检查器，验证四类 fail-closed。"""
    import tempfile

    bad = []
    def eq(name, got, want):
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    with tempfile.TemporaryDirectory() as d:
        cdir = os.path.join(d, "checks")
        os.makedirs(cdir)
        def mk(name, code):
            with open(os.path.join(cdir, name + ".py"), "w", encoding="utf-8") as f:
                f.write(code)
        mk("check_pass", "import sys; sys.exit(0)")
        mk("check_fail", "import sys; sys.exit(1)")
        mk("check_slow", "import time; time.sleep(5)")
        # 故意失败、且注册表里**不声明 level** —— 用来验证「未声明 ⇒ 按阻塞」
        mk("check_nodecl", "import sys; sys.exit(1)")

        reg = os.path.join(d, "registry.json")
        with open(reg, "w", encoding="utf-8") as f:
            json.dump({"levels": {
                "check_pass": {"level": "fail", "ok": "PASS-OK"},
                "check_fail": {"level": "fail", "ok": "x", "fail": "FAIL-OK"},
                "check_warn": {"level": "warn", "ok": "x", "fail": "WARN-OK"},
                "check_slow": {"level": "fail", "ok": "x"},
                "check_nodecl": {"ok": "x"},          # 未声明 level
                "check_missing": {"level": "fail", "ok": "x"},  # 脚本不存在
            }}, f)

        quiet = lambda *_a, **_k: None
        r = run_gate(reg, cdir, timeout=1, echo=quiet)
        eq("通过项", "check_pass" in r.passed, True)
        eq("失败项", "check_fail" in r.failed, True)
        eq("警告项", "check_warn" in r.warned, True)
        eq("超时按阻塞", "check_slow" in r.failed, True)
        eq("未声明按阻塞", "check_nodecl" in r.failed, True)
        eq("缺脚本只告警", "check_missing" in r.warned, True)
        eq("整闸不通过", r.ok, False)

    # 注册表读不出来 ⇒ 整闸阻塞
    r2 = run_gate("/nonexistent/registry.json", "/nonexistent", echo=lambda *_a, **_k: None)
    eq("坏注册表 ⇒ 阻塞", r2.ok, False)

    for b in bad:
        print("  ✗ %s" % b)
    print("checkgate selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", 8))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest())
