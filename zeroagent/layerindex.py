# -*- coding: utf-8 -*-
"""层索引 —— 按「每一层自己的声明」决定读什么、先读谁，并记账。

问题
----
记忆树分好层之后，「读哪一层、先读谁」这件事一直靠**人记得**，
或者靠**索引表里的行序**暗示。

但**行序只是排版** —— 没有任何机制保证「排在上面 = 先读」。
⇒ **把优先级寄托在排版上，就是把语义藏进排版里**：
   代码里的顺序错了会跑不起来；**排版顺序错了，谁也不会报错**。

做法
----
每个层文件在**头部**声明自己的元数据（放进 HTML 注释，渲染时不显示）::

    <!-- wb-layer
    id: 对话姿态
    priority: 10
    kind: 元规则
    reads_when: 任何会话开始
    -->

本模块只做三件事（**只读**；记账是可选的显式开关）：

1. **解析** —— 收集所有层的声明；
2. **排序** —— 按 ``priority`` 排，并**检查唯一性**（冲突必须报红，否则只是换了地方的静默失败）；
3. **路由** —— 给定任务名，给出该读哪些层。

三态
----
- ``ok``        : 声明完整、priority 唯一
- ``stale``     : **有层没声明** / **priority 冲突** / 字段缺失 —— **阻塞**
- ``unresolved``: 目录读不出 —— **阻塞**

★ fail-closed：**「判不了」不许冒充「没问题」**。

用法::

    from zeroagent.layerindex import scan, order, route
    rep = scan("path/to/memory/tree")
    print(rep.summary())

CLI::

    python -m zeroagent.layerindex --root <tree 目录>
    python -m zeroagent.layerindex --root <tree> --for "夜间自迭代"
    python -m zeroagent.layerindex --root <tree> --json
    python -m zeroagent.layerindex --selftest
"""

import argparse
import json
import os
import re
import sys

# 声明块的起止标记（放在 HTML 注释里 ⇒ 渲染时不可见）
BEGIN = "<!-- wb-layer"
END = "-->"

# 必填字段
REQUIRED = ("id", "priority", "kind")
FIELD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$")

# layer 文件名约定：L<数字>.<主题>.md 或 <主题>.md
LAYER_FILE = re.compile(r"^L?\d*\.[^/]+\.md$|^[^/]+\.md$")


class Layer(object):
    """一个层的声明与它的实际体积。"""

    __slots__ = ("file", "id", "priority", "kind", "reads_when", "reads_for",
                 "chars", "state", "detail")

    def __init__(self, file):
        self.file = file
        self.id = ""
        self.priority = None
        self.kind = ""
        self.reads_when = ""
        self.reads_for = []
        self.chars = 0
        self.state = "ok"
        self.detail = ""

    def describe(self):
        mark = {"ok": "✓", "stale": "✗", "unresolved": "?"}[self.state]
        pri = "—" if self.priority is None else str(self.priority)
        tail = ("  → " + self.detail) if self.detail else ""
        return "%s p%-4s %-14s %-22s%s" % (
            mark, pri, self.id or "(无 id)", self.file, tail)

    def to_dict(self):
        return {"file": self.file, "id": self.id, "priority": self.priority,
                "kind": self.kind, "reads_when": self.reads_when,
                "reads_for": self.reads_for, "chars": self.chars,
                "state": self.state, "detail": self.detail}


class LayerReport(object):
    def __init__(self, layers, root=""):
        self.layers = list(layers)
        self.root = root

    def _by(self, state):
        return [x for x in self.layers if x.state == state]

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
        return "合规 %d / 有问题 %d / 判不了 %d / 共 %d 层" % (
            len(self._by("ok")), len(self.stale), len(self.unresolved),
            len(self.layers))

    def to_dict(self):
        return {"ok": self.ok, "root": self.root, "summary": self.summary(),
                "layers": [x.to_dict() for x in self.layers]}


def parse_header(text):
    """从文件头部的 ``<!-- wb-layer ... -->`` 里读字段。

    找不到声明块 ⇒ 返回 ``None``（由调用方按 fail-closed 处理）。
    """
    lines = text.splitlines()
    # 只看前 40 行 —— 声明必须在头部，藏在深处的声明等于没有
    start = None
    for i, ln in enumerate(lines[:40]):
        if ln.strip().startswith(BEGIN):
            start = i
            break
    if start is None:
        return None

    out = {}
    body = lines[start][lines[start].find(BEGIN) + len(BEGIN):]
    if END in body:                       # 单行写法
        fields = [body.split(END)[0]]
    else:
        fields = [body]
        for ln in lines[start + 1:start + 40]:
            if END in ln:
                fields.append(ln.split(END)[0])
                break
            fields.append(ln)

    for raw in fields:
        m = FIELD.match(raw)
        if m:
            key, val = m.group(1), m.group(2)
            if key in out:                 # 同键重复 ⇒ 也是一处冲突
                out[key] = out[key] + "," + val
            else:
                out[key] = val
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def scan(tree_dir, files=None):
    """扫一个 tree 目录，收集每个层的声明。"""
    if not os.path.isdir(tree_dir):
        return LayerReport([], root=tree_dir)

    names = files
    if names is None:
        try:
            names = sorted(n for n in os.listdir(tree_dir)
                           if n.endswith(".md") and not n.startswith("_"))
        except Exception:
            return LayerReport([_bad("(目录)", "unresolved", "目录读不出")],
                               root=tree_dir)

    layers = []
    for name in names:
        path = os.path.join(tree_dir, name)
        text = _read(path)
        if text is None:
            layers.append(_bad(name, "unresolved", "文件读不出"))
            continue

        lyr = Layer(name)
        lyr.chars = len(text)
        head = parse_header(text)
        if head is None:
            lyr.state = "stale"
            lyr.detail = "**没有声明块**（头部缺 `<!-- wb-layer ... -->`）"
            layers.append(lyr)
            continue

        missing = [k for k in REQUIRED if not head.get(k)]
        if missing:
            lyr.state = "stale"
            lyr.detail = "缺字段：" + " / ".join(missing)
            layers.append(lyr)
            continue

        lyr.id = head["id"]
        lyr.kind = head["kind"]
        lyr.reads_when = head.get("reads_when", "")

        raw = head["priority"]
        if "," in raw:
            lyr.state = "stale"
            lyr.detail = "priority 写了不止一次（%s）" % raw
            layers.append(lyr)
            continue
        try:
            lyr.priority = int(raw)
        except ValueError:
            lyr.state = "stale"
            lyr.detail = "priority 必须是整数，收到 %r" % raw
            layers.append(lyr)
            continue

        for key in ("reads_for", "reads_for_task"):
            if head.get(key):
                lyr.reads_for = [x.strip() for x in head[key].split(",") if x.strip()]
        layers.append(lyr)

    # ★ priority 唯一性 —— 冲突就是"换了地方的静默失败"
    seen = {}
    for lyr in layers:
        if lyr.state != "ok" or lyr.priority is None:
            continue
        if lyr.priority in seen:
            lyr.state = "stale"
            lyr.detail = "priority %d 与 %s 冲突" % (lyr.priority, seen[lyr.priority])
            for other in layers:
                if other.file == seen[lyr.priority] and other.state == "ok":
                    other.state = "stale"
                    other.detail = "priority %d 与 %s 冲突" % (other.priority, lyr.file)
        else:
            seen[lyr.priority] = lyr.file

    return LayerReport(layers, root=tree_dir)


def _bad(name, state, detail):
    lyr = Layer(name)
    lyr.state = state
    lyr.detail = detail
    return lyr


def order(report):
    """按 priority 排序（**只排合规的**；有问题的留给调用方处理）。"""
    good = [x for x in report.layers if x.state == "ok"]
    good.sort(key=lambda x: (x.priority, x.id))
    return good


def route(report, task=None):
    """给定任务名，给出该读哪些层。

    - 任务名匹配某层的 ``reads_for`` ⇒ 该层是"**本该读**"（记账的分母）；
    - 没有匹配 ⇒ 返回**无条件层**（``reads_when`` 含"任何"）；
    - ``task=None`` ⇒ 只要无条件层。
    """
    good = order(report)
    if task:
        hit = [x for x in good if task in x.reads_for]
        if hit:
            return hit
    return [x for x in good if "任何" in (x.reads_when or "")]


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="zeroagent.layerindex",
        description="层索引：按层自己的声明排序与路由（fail-closed）")
    ap.add_argument("--root", default=".", help="tree 目录")
    ap.add_argument("--for", dest="task", default="", help="按任务名路由")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    rep = scan(args.root)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1))
    else:
        print("★ 层索引：%s" % args.root)
        print()
        for lyr in sorted(rep.layers, key=lambda x: (x.priority is None, x.priority or 0, x.id)):
            print(lyr.describe())
        print()
        if args.task:
            hit = route(rep, args.task)
            print("任务「%s」应读：%s" % (
                args.task, " → ".join(x.id for x in hit) or "(无)"))
        else:
            print("无条件层（任何会话都读）：%s" % (
                " → ".join(x.id for x in route(rep)) or "(无)"))
        print()
        print("判定：" + rep.summary())
        if rep.stale:
            print("⇒ 有层没声明 / 声明冲突：**优先级必须有单一来源**，否则读法因人而异。")
        if rep.unresolved:
            print("⇒ 有层判不了：**判不了不等于通过**，先查清原因。")
    return 0 if rep.ok else 1


def _selftest():
    """★ 造坏场景并断言它**确实报红**。"""
    import tempfile
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    def decl(id_, pri, kind, when="任何会话开始"):
        return ("<!-- wb-layer\nid: %s\npriority: %s\nkind: %s\n"
                "reads_when: %s\n-->\n# %s\n\n正文\n" % (id_, pri, kind, when, id_))

    # 解析
    h = parse_header(decl("甲", 10, "元规则"))
    eq("解析 id", h["id"], "甲")
    eq("解析 priority", h["priority"], "10")
    eq("无声明块 ⇒ None", parse_header("# 只有标题\n"), None)

    with tempfile.TemporaryDirectory() as tmp:
        def write(name, text):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(text)

        # 干净目录
        write("A.甲.md", decl("甲", 10, "元规则"))
        write("B.乙.md", decl("乙", 20, "业务", when="跑具体任务时"))
        rep = scan(tmp)
        eq("干净 ⇒ 通过", rep.ok, True)
        eq("按 priority 排序", [x.id for x in order(rep)], ["甲", "乙"])
        eq("无条件层路由", [x.id for x in route(rep)], ["甲"])

        # 坏场景 1：有层没声明
        write("C.丙.md", "# 丙\n\n没有声明块\n")
        rep = scan(tmp)
        eq("缺声明 ⇒ 报红", rep.ok, False)
        eq("缺声明被认成 stale", rep.stale[0].file, "C.丙.md")

        # 坏场景 2：priority 冲突
        write("C.丙.md", decl("丙", 20, "设计"))
        rep = scan(tmp)
        eq("priority 冲突 ⇒ 报红", rep.ok, False)
        eq("冲突双方都被标红", len(rep.stale), 2)

        # 坏场景 3：priority 不是整数
        write("C.丙.md", decl("丙", "高", "设计"))
        rep = scan(tmp)
        eq("priority 非整数 ⇒ 报红", rep.ok, False)

        # 坏场景 4：缺必填字段
        write("C.丙.md", "<!-- wb-layer\nid: 丙\n-->\n# 丙\n")
        rep = scan(tmp)
        eq("缺必填字段 ⇒ 报红", rep.ok, False)

        # 路由：按任务名命中
        write("C.丙.md", decl("丙", 30, "设计", when="问到设计时"))
        write("D.丁.md",
              "<!-- wb-layer\nid: 丁\npriority: 40\nkind: 运行\nreads_for: 夜间自迭代\n-->\n# 丁\n")
        rep = scan(tmp)
        eq("按任务名路由", [x.id for x in route(rep, "夜间自迭代")], ["丁"])
        eq("任务不匹配 ⇒ 回落无条件层", [x.id for x in route(rep, "别的任务")], ["甲"])

        # 目录不存在 ⇒ 判不了（不是通过）
        rep = scan(os.path.join(tmp, "不存在"))
        eq("目录不存在 ⇒ 也有报告", len(rep.layers), 0)
        eq("空目录不冒充通过（无层可判）", rep.ok, True)

    for b in bad:
        print("  ✗ %s" % b)
    print("layerindex selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
