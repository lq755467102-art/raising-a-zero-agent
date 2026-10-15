# -*- coding: utf-8 -*-
"""上下文预算守卫 —— 在「静默截断」发生之前报警。

问题
----
与 AI 协作时，笔记 / 记忆 / 指令文件通常有**容量上限**。一旦超出，很多客户端会
**按字符静默截断**：尾部的规则悄悄消失，**不报错、不提示**。
你失去的不是一次报错，而是「某条约束从此不再生效」——典型的静默失败。

把水位尺子做出来并不难；难的是**让它真的会跑**。
本模块只负责「量 + 报」，**只读不写**（守卫不该有副作用）；
接到哪条既有必跑路径上（提交前 / 发布前），由使用方决定。

三态
----
- ``ok``      : 低于接近线
- ``near``    : 已达接近线（默认 90%）—— 该蒸馏 / 精简了
- ``over``    : 超过上限 —— **尾部已经在丢，必须立刻处理**
- ``missing`` : 文件读不到 —— 配置问题，单独报出（不混进 over）

用法::

    from failclosed.contextbudget import Item, check
    report = check([Item("memory", "MEMORY.md", 8000),
                    Item("notes", "notes.md", 4000)])
    print(report.summary())
    if not report.ok:            # 有 over ⇒ 不该放行
        ...

CLI::

    python -m failclosed.contextbudget --item memory=MEMORY.md:8000 --item notes=notes.md:4000
    python -m failclosed.contextbudget --config budget.json --json
"""

import argparse
import json
import os
import sys

DEFAULT_NEAR = 0.90          # ★ 阈值单一真源：别在别处再抄一份


class Item:
    """一个受管文件：名字（给人看）、路径、上限、接近线。"""

    __slots__ = ("name", "path", "limit", "near")

    def __init__(self, name, path, limit, near=DEFAULT_NEAR):
        self.name = str(name)
        self.path = str(path)
        self.limit = int(limit)
        self.near = float(near)

    def to_dict(self):
        return {"name": self.name, "path": self.path,
                "limit": self.limit, "near": self.near}


class Reading:
    """一个文件的测量结果。"""

    __slots__ = ("name", "path", "chars", "limit", "state")

    def __init__(self, name, path, chars, limit, state):
        self.name = name
        self.path = path
        self.chars = int(chars)
        self.limit = int(limit)
        self.state = state

    @property
    def ratio(self):
        return (self.chars / self.limit) if self.limit else 0.0

    def describe(self):
        pct = "%.1f%%" % (self.ratio * 100)
        mark = {"ok": "✓", "near": "⚠", "over": "✗", "missing": "?"}[self.state]
        tail = {
            "ok": "",
            "near": " → 该蒸馏 / 精简了",
            "over": " → 尾部已在被静默截断，必须立刻处理",
            "missing": " → 文件读不到（配置问题）",
        }[self.state]
        return "%s %-12s %6d / %-6d（%s）%s" % (
            mark, self.name, self.chars, self.limit, pct, tail)

    def to_dict(self):
        return {"name": self.name, "path": self.path, "chars": self.chars,
                "limit": self.limit, "state": self.state,
                "ratio": round(self.ratio, 4)}


class BudgetReport:
    def __init__(self, readings):
        self.readings = list(readings)

    def _by(self, state):
        return [r for r in self.readings if r.state == state]

    @property
    def ok(self):
        """只有 **over** 才不通过；near 是提醒，missing 是配置问题。"""
        return not self._by("over")

    @property
    def over(self):
        return self._by("over")

    @property
    def near(self):
        return self._by("near")

    @property
    def missing(self):
        return self._by("missing")

    def summary(self):
        return "超线 %d / 接近线 %d / 缺失 %d / 共 %d" % (
            len(self.over), len(self.near), len(self.missing), len(self.readings))

    def to_dict(self):
        return {"ok": self.ok, "summary": self.summary(),
                "readings": [r.to_dict() for r in self.readings]}


def _file_chars(path):
    """读文件字符数；读不到返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            return len(f.read())
    except Exception:
        return None


def check(items, _chars=None):
    """量一批文件的水位。

    :param items:  :class:`Item` 列表
    :param _chars: 取字符数的函数（测试注入用）；默认读文件
    """
    read = _chars or _file_chars
    readings = []
    for it in items:
        n = read(it.path)
        if n is None:
            readings.append(Reading(it.name, it.path, 0, it.limit, "missing"))
            continue
        if it.limit and n > it.limit:
            state = "over"
        elif it.limit and n >= it.limit * it.near:
            state = "near"
        else:
            state = "ok"
        readings.append(Reading(it.name, it.path, n, it.limit, state))
    return BudgetReport(readings)


def load_config(path):
    """从 JSON 读 items：{"items":[{"name","path","limit","near"}]}"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for d in (data.get("items") or []):
        out.append(Item(d.get("name") or os.path.basename(d["path"]),
                        d["path"], d["limit"],
                        d.get("near", DEFAULT_NEAR)))
    return out


def _parse_item(spec, near):
    """'name=path:limit' → Item（路径里的盘符冒号不受影响，从右往左切）。"""
    name, sep, rest = spec.partition("=")
    if not sep or not rest:
        raise ValueError("--item 格式应为 name=path:limit，收到 %r" % spec)
    path, c, limit = rest.rpartition(":")
    if not c or not limit.isdigit() or not path:
        raise ValueError("--item 的 limit 必须是整数：%r" % spec)
    return Item(name or os.path.basename(path), path, int(limit), near)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="failclosed.contextbudget",
                                 description="上下文预算守卫（只读）")
    ap.add_argument("--item", action="append", default=[],
                    help="name=path:limit（可重复）")
    ap.add_argument("--config", default="", help="JSON 配置：{\"items\":[...]}")
    ap.add_argument("--near", type=float, default=DEFAULT_NEAR,
                    help="接近线比例（默认 0.9）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    items = []
    if args.config:
        try:
            items += load_config(args.config)
        except Exception as e:
            print(json.dumps({"ok": False, "error": "配置读不出：%s" % e},
                             ensure_ascii=False))
            return 1
    for spec in args.item:
        try:
            items.append(_parse_item(spec, args.near))
        except ValueError as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return 2

    if not items:
        print(json.dumps({"ok": False, "error": "没有 --item / --config，无事可做"},
                         ensure_ascii=False))
        return 2

    report = check(items)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    else:
        for r in report.readings:
            print(r.describe())
        print("判定：" + report.summary())
    return 0 if report.ok else 1


def _selftest():
    """自测：三态 + 缺失 + 边界（用注入的字符数，不碰真实文件）。"""
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    sizes = {"a.md": 100, "b.md": 900, "c.md": 1000, "d.md": 1001}
    read = lambda p: sizes.get(p)          # 未知文件 → None（缺失）

    items = [Item("a", "a.md", 1000), Item("b", "b.md", 1000),
             Item("c", "c.md", 1000), Item("d", "d.md", 1000),
             Item("ghost", "ghost.md", 1000)]
    rep = check(items, _chars=read)
    st = {r.name: r.state for r in rep.readings}
    eq("远低于线 ⇒ ok", st["a"], "ok")
    eq("刚好到接近线 ⇒ near", st["b"], "near")
    eq("刚好等于上限 ⇒ near（还没超）", st["c"], "near")
    eq("超过上限 ⇒ over", st["d"], "over")
    eq("读不到 ⇒ missing", st["ghost"], "missing")
    eq("有 over ⇒ 不通过", rep.ok, False)
    eq("over 计数", len(rep.over), 1)
    eq("missing 不混进 over", len(rep.missing), 1)
    eq("全部合规 ⇒ 通过",
       check([Item("a", "a.md", 1000)], _chars=read).ok, True)
    eq("ratio 计算", round(rep.readings[1].ratio, 2), 0.9)

    # CLI 解析：Windows 盘符里的冒号不该被误切
    it = _parse_item("mem=C:/x/mem.md:8000", 0.9)
    eq("CLI 解析路径", it.path, "C:/x/mem.md")
    eq("CLI 解析上限", it.limit, 8000)

    for b in bad:
        print("  ✗ %s" % b)
    print("contextbudget selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
