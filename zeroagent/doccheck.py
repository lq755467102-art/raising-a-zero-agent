# -*- coding: utf-8 -*-
"""文档自检 —— 引用会静默失效，这个模块让它当场变红。

问题
----
文档里的**引用**有三类，它们都会在改动中悄悄断掉，而且**没有东西会喊**：

1. **文件链接** ``[字](docs/x.md)`` —— 文件改名 / 移动后，链接就 404；
2. **页内锚点** ``](#某标题)`` —— 标题一改，跳转就废
   （最隐蔽的一种：**正文看上去完全正常**，只有点进去才知道错了）；
3. **声明的数量** —— 文档里写「6 个模块」「15 条反模式」，
   而实际已经变了。这正是 [漂移](../docs/drift.md) 里「例 2」讲的那种漂移：
   **一篇讲防静默失败的文档，自己被静默漂移了。**

三者的共同点：不报错。读者点进去才发现，或者**永远发现不了**。

做法
----
一次扫描，三类都查：

- 扫所有 ``*.md``（跳过代码块），抽出 markdown 链接，分成
  「文件链接 / 页内锚点 / 跨文件锚点」三种；
- 逐个解析目标：文件在不在、目标标题的 slug 里有没有这个锚点；
- 数量声明按 ``NUMBER_RULES`` 这张**声明表**核对 ——
  ★ 加一条规则只改这一处（判据收口，别在别处再抄一份）。

三态
----
- ``ok``        : 引用有效
- ``stale``     : 目标不存在 —— **阻塞**
- ``unresolved``: 判不了（文件读不出 / 目录不存在）—— **阻塞**

★ fail-closed：**「判不了」不许冒充「没问题」**。

它自己怎么被验证
----------------
``--selftest`` 会**造一个坏场景**（死链 + 错锚点 + 数量不符）并断言它**确实报红** ——
一个永远通过的检查器，比没有更糟。

CLI::

    python -m zeroagent.doccheck --root .
    python -m zeroagent.doccheck --root . --json
    python -m zeroagent.doccheck --selftest
"""

import argparse
import json
import os
import re
import sys

# ── 解析规则（★ 判据单一真源：要改就改这里）──────────────────────────

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# ★ 行内豁免标记：写在行尾即可跳过该行。
# 用途是「这里写的是**引文**（别人的过期数字、反例里的假数字），不是本仓库的声明」——
# 工具不该逼内容改写去迁就它，所以给一条明确的出口。
_IGNORE = "doccheck-ignore"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_EXTERNAL = re.compile(r"^(https?:|mailto:|tel:|data:)")

# 文档里写「N 个 X」时，N 该等于什么
# ★ 阿拉伯数字与中文数字都要认 —— 否则「八个工具」这类写法会成为盲区。
# ★★ 中文数字必须允许多位（`+`）：否则「十一个模块」会被匹配成尾巴上的「一个模块」。
_NUM = r"([0-9]+|[一二三四五六七八九十]+)"

# ★ 量词与名词之间的**中缀**要穷举，否则「多一个字就漏过」：
#   「N 个模块」这种紧贴写法漏不掉，但「N 个**可跑**模块」「N 项**单元**测试」都漏过 ——
#   而且两次都是**真错漏到线上**（靠人工逆向核对才发现），补规则就是为了不再靠人肉兜。
# ★ 一条声明只归一条规则：同义写法并进同一个正则，别新开一条 ——
#   两条规则命中同一处会**重复计数**，把「共查 N 处引用」这个基线也带偏。
# ★ 注释里别写具体数字（本文件就是抓数字过期的）：只写"量词形状"，数字交给规则去算。
NUMBER_RULES = [
    (re.compile(_NUM + r"\s*个(?:可跑(?:的)?)?模块"), "modules",
     "zeroagent/ 下的模块数（不含 __init__）"),
    (re.compile(_NUM + r"\s*个(?:可跑(?:的)?)?小?工具"), "modules",
     "zeroagent/ 下的模块数（不含 __init__）"),
    (re.compile(_NUM + r"\s*篇文档"), "docs", "docs/ 下的文档数"),
    (re.compile(_NUM + r"\s*项(?:单元测试|单测)"), "unit_tests",
     "tests/ 下的测试数（数 def test_）"),
    (re.compile(_NUM + r"\s*条反模式"), "anti_patterns",
     "anti-patterns.md 里的条目数"),
]

# ★ 明知是盲区、**故意不补**的写法（补了会逼内容就范，比盲区更糟）：
#   - 「N 个可跑示例」：口径未定 —— 数 examples/ 顶层可跑条目，与数 examples/ 下 .py，
#     是两个不同的值；README 同页就同时出现了这两种数法。
#     **口径是人的裁决，不是工具的**：把数字钉死进规则，等于替人拍板。
#   - 「N 项自测」：指代有歧义 —— 「全仓合计」与「某个模块自带」同形（同段话里两个都算对），
#     一刀切必出误报 ⇒ 这个数**只能人工同步**（改 zeroagent/ 就要同步 README / costs / case-study）。
#   - 「N 层」「N 类」：取决于分类口径，而层数口径本身在本仓库里就没统一。

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def to_int(text):
    """把捕获到的数字转成 int：阿拉伯数字，或中文数字（**支持十位以上**）。

    认不出返回 None（由调用方按 fail-closed 报「判不了」）。
    """
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN_DIGITS and ch != "十" for ch in text):
        return None
    if text == "十":
        return 10
    if "十" in text:                       # 十一 / 二十 / 三十五
        a, _, b = text.partition("十")
        tens = _CN_DIGITS.get(a, 1) if a else 1
        ones = _CN_DIGITS.get(b, 0) if b else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text)


class Ref(object):
    """一处引用：从哪来、指向哪、判定如何。"""

    __slots__ = ("src", "line", "kind", "target", "state", "detail")

    def __init__(self, src, line, kind, target, state, detail=""):
        self.src = src
        self.line = int(line)
        self.kind = kind          # file / anchor / file+anchor / number
        self.target = target
        self.state = state        # ok / stale / unresolved
        self.detail = detail

    def describe(self):
        mark = {"ok": "✓", "stale": "✗", "unresolved": "?"}[self.state]
        head = "%s %s:%d" % (mark, self.src, self.line)
        return "%s  [%s] %s%s" % (
            head, self.kind, self.target,
            ("  → " + self.detail) if self.detail else "")

    def to_dict(self):
        return {"src": self.src, "line": self.line, "kind": self.kind,
                "target": self.target, "state": self.state, "detail": self.detail}


class DocReport(object):
    def __init__(self, refs):
        self.refs = list(refs)

    def _by(self, state):
        return [r for r in self.refs if r.state == state]

    @property
    def ok(self):
        """只有 stale / unresolved 才不算通过。"""
        return not (self._by("stale") or self._by("unresolved"))

    @property
    def stale(self):
        return self._by("stale")

    @property
    def unresolved(self):
        return self._by("unresolved")

    def summary(self):
        return "有效 %d / 失效 %d / 判不了 %d / 共查 %d 处引用" % (
            len(self._by("ok")), len(self.stale), len(self.unresolved), len(self.refs))

    def to_dict(self):
        return {"ok": self.ok, "summary": self.summary(),
                "refs": [r.to_dict() for r in self.refs]}


def slug(title):
    """标题 → 锚点。与 GitHub 的做法对齐：小写、去标点、空格转连字符。

    中文标题**原样保留**（``## 怎么从 0？`` → ``怎么从-0``）。
    """
    s = title.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s)
    return s


def _read(path):
    """读文本；读不出返回 None（由调用方按 fail-closed 处理）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _strip_fences(text):
    """把代码块里的内容换成空行（保持行号），避免把示例当引用扫出来。"""
    out, in_code = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            out.append("")
            continue
        out.append("" if in_code else line)
    return out


def _heading_slugs(text):
    """一个 markdown 文件里所有标题的锚点集合。"""
    out = set()
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            out.add(slug(m.group(2)))
    return out


# ── 数量声明的计数器（每个 key 对应 NUMBER_RULES 里的一个规则）──────────

def _count(root, key):
    if key == "modules":
        d = os.path.join(root, "zeroagent")
        return len([f for f in os.listdir(d)
                    if f.endswith(".py") and f != "__init__.py"])
    if key == "docs":
        d = os.path.join(root, "docs")
        return len([f for f in os.listdir(d) if f.endswith(".md")])
    if key == "unit_tests":
        d = os.path.join(root, "tests")
        n = 0
        for f in sorted(os.listdir(d)):
            if f.endswith(".py"):
                t = _read(os.path.join(d, f))
                if t is None:
                    return None
                n += len(re.findall(r"^\s*def test_", t, re.M))
        return n
    if key == "anti_patterns":
        t = _read(os.path.join(root, "docs", "anti-patterns.md"))
        if t is None:
            return None
        return len(re.findall(r"^\|\s*\d+[a-z]?\s*\|", t, re.M))
    raise KeyError("未知计数项：%s" % key)


def _list_md(root):
    """仓库里所有待查的 .md（顶层 + docs/，与仓库实际结构一致）。"""
    out = []
    for p in sorted(os.listdir(root)):
        if p.endswith(".md"):
            out.append(p)
    d = os.path.join(root, "docs")
    if os.path.isdir(d):
        for p in sorted(os.listdir(d)):
            if p.endswith(".md"):
                out.append(os.path.join("docs", p))
    return out


def check(root=".", md_files=None):
    """扫一遍文档里的引用，返回 :class:`DocReport`。"""
    refs = []
    files = md_files if md_files is not None else _list_md(root)
    slug_cache = {}

    def slugs_of(relpath):
        if relpath not in slug_cache:
            t = _read(os.path.join(root, relpath))
            slug_cache[relpath] = None if t is None else _heading_slugs(t)
        return slug_cache[relpath]

    for rel in files:
        text = _read(os.path.join(root, rel))
        if text is None:
            refs.append(Ref(rel, 0, "file", rel, "unresolved", "文件读不出"))
            continue

        for i, line in enumerate(_strip_fences(text), 1):
            if _IGNORE in line:      # 显式豁免（见文件头 _IGNORE 的说明）
                continue
            # —— 链接 / 锚点 ——
            for target in _LINK.findall(line):
                if _EXTERNAL.match(target):
                    continue
                path, _, anchor = target.partition("#")
                path = path.strip()
                anchor = anchor.strip()

                if not path:                      # 纯页内锚点
                    got = slugs_of(rel)
                    if got is None:
                        refs.append(Ref(rel, i, "anchor", target, "unresolved", "自身读不出"))
                    elif anchor and slug(anchor) in got:
                        refs.append(Ref(rel, i, "anchor", target, "ok"))
                    elif not anchor:
                        refs.append(Ref(rel, i, "anchor", target, "ok"))
                    else:
                        refs.append(Ref(rel, i, "anchor", target, "stale",
                                        "本文件里没有这个标题"))
                    continue

                # 文件链接（可能带锚点）
                base = os.path.dirname(rel)
                dest = os.path.normpath(os.path.join(base, path)).replace("\\", "/")
                full = os.path.join(root, dest)
                if not os.path.exists(full):
                    refs.append(Ref(rel, i, "file" + ("+anchor" if anchor else ""),
                                    target, "stale", "目标文件不存在：%s" % dest))
                    continue
                if not anchor:
                    refs.append(Ref(rel, i, "file", target, "ok"))
                    continue
                got = slugs_of(dest)
                if got is None:
                    refs.append(Ref(rel, i, "file+anchor", target, "unresolved",
                                    "跨文件读不出：%s" % dest))
                elif slug(anchor) in got:
                    refs.append(Ref(rel, i, "file+anchor", target, "ok"))
                else:
                    refs.append(Ref(rel, i, "file+anchor", target, "stale",
                                    "%s 里没有锚点 #%s" % (dest, anchor)))

            # —— 数量声明 ——
            for pattern, key, desc in NUMBER_RULES:
                for m in pattern.finditer(line):
                    want = to_int(m.group(1))
                    got = _count(root, key)
                    if want is None:
                        refs.append(Ref(rel, i, "number", m.group(0), "unresolved",
                                        "数字认不出：%s" % m.group(1)))
                    elif got is None:
                        refs.append(Ref(rel, i, "number", m.group(0), "unresolved",
                                        "数不出来（%s）" % desc))
                    elif got == want:
                        refs.append(Ref(rel, i, "number", m.group(0), "ok"))
                    else:
                        refs.append(Ref(rel, i, "number", m.group(0), "stale",
                                        "写的是 %s，实际 %d（%s）" % (m.group(1), got, desc)))

    return DocReport(refs)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="zeroagent.doccheck",
                                 description="文档自检：链接 / 锚点 / 数量声明（fail-closed）")
    ap.add_argument("--root", default=".", help="仓库根目录（默认当前目录）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--quiet-ok", action="store_true", help="只打印有问题的项")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not os.path.isdir(args.root):
        print(json.dumps({"ok": False, "error": "根目录不存在：%s" % args.root},
                         ensure_ascii=False))
        return 2

    rep = check(args.root)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1))
    else:
        shown = 0
        for r in rep.refs:
            if args.quiet_ok and r.state == "ok":
                continue
            print(r.describe())
            shown += 1
        if shown == 0:
            print("（无异常项）")
        print("判定：" + rep.summary())
    return 0 if rep.ok else 1


def _selftest():
    """★ 造坏场景并断言它**确实报红** —— 不这么测，就不知道它是真在查。"""
    import tempfile
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    # 锚点算法本身
    eq("slug 中文 + 问号", slug("怎么从 0？"), "怎么从-0")
    eq("slug 英文 + 标点", slug("它是怎么被写出来的"), "它是怎么被写出来的")
    eq("slug 带空格与点", slug("Step 1. Setup"), "step-1-setup")

    # 数字解析：阿拉伯 + 中文（★ 中文那半是后补的 —— 之前「八个工具」是盲区）
    eq("数字：阿拉伯", to_int("6"), 6)
    eq("数字：八", to_int("八"), 8)
    eq("数字：十", to_int("十"), 10)
    eq("★ 数字：十一（多位中文，曾漏）", to_int("十一"), 11)
    eq("数字：二十", to_int("二十"), 20)
    eq("数字：三十五", to_int("三十五"), 35)
    eq("数字：认不出 ⇒ None（不冒充 0）", to_int("余"), None)

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "docs"))
        os.makedirs(os.path.join(tmp, "zeroagent"))
        os.makedirs(os.path.join(tmp, "tests"))
        for name in ("a", "b"):
            open(os.path.join(tmp, "zeroagent", "%s.py" % name), "w").close()
        open(os.path.join(tmp, "zeroagent", "__init__.py"), "w").close()
        open(os.path.join(tmp, "tests", "t.py"), "w", encoding="utf-8").write(
            "def test_one():\n    pass\n")

        # 好文档：链接有效、锚点有效、数量正确（2 个模块）
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n"
            "## 二级\n\n"
            "- [跳到二级](#二级)\n"
            "- [去文档](docs/x.md)\n"
            "- [跨文件锚点](docs/x.md#小节)\n"
            "- 本仓库共 2 个模块\n")
        open(os.path.join(tmp, "docs", "x.md"), "w", encoding="utf-8").write(
            "# X\n\n## 小节\n")

        rep = check(tmp)
        eq("干净仓库 ⇒ 通过", rep.ok, True)
        eq("干净仓库无失效项", len(rep.stale), 0)

        # 注入坏场景 1：死链
        open(os.path.join(tmp, "README.md"), "a", encoding="utf-8").write(
            "\n- [死链](docs/ghost.md)\n")
        rep = check(tmp)
        eq("死链 ⇒ 报红", rep.ok, False)
        eq("死链被认成 stale", any("ghost.md" in r.target and r.state == "stale"
                                   for r in rep.refs), True)

        # 注入坏场景 2：错锚点
        open(os.path.join(tmp, "README.md"), "a", encoding="utf-8").write(
            "\n- [错锚点](#不存在的标题)\n")
        rep = check(tmp)
        eq("错锚点 ⇒ 报红", rep.ok, False)

        # 注入坏场景 3：数量声明与实际不符
        open(os.path.join(tmp, "README.md"), "a", encoding="utf-8").write(
            "\n本仓库共 99 个模块\n")
        rep = check(tmp)
        eq("数量不符 ⇒ 报红", rep.ok, False)
        eq("数量不符给出实际值",
           any("99" in r.detail and "2" in r.detail for r in rep.stale), True)

        # 注入坏场景 4：★ 中文数字写法（「八个工具」）也必须被抓到 —— 曾经是盲区
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n本仓库共八个工具\n")
        eq("中文数字（八个工具）也能被抓到", check(tmp).ok, False)

        # ★ 坏场景 5~7：量词与名词之间**多一个字**的写法 —— 曾经整片是盲区
        #   （「N 个小工具」「N 个可跑模块」「N 项单元测试」都真漏过，靠人工才发现）
        for bad_line, what in (("99 个小工具", "小工具"),
                               ("99 个可跑模块", "可跑模块"),
                               ("99 项单元测试", "单元测试")):
            open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
                "# 标题\n\n本仓库共 %s\n" % bad_line)
            eq("中缀写法「%s」也能被抓到" % what, check(tmp).ok, False)

        # ★ 反面：同三种写法的**正确值**必须放行 —— 补规则不能把对的也判红
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n本仓库共 2 个小工具 / 2 个可跑模块 / 1 项单元测试\n")
        eq("中缀写法的正确值 ⇒ 放行", check(tmp).ok, True)

        # ★ 显式豁免：带 doccheck-ignore 的行整体跳过（用于「引文里的数字」这类误报）
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n它写着 99 个模块 <!-- doccheck-ignore -->\n")
        eq("带豁免标记的行被跳过", check(tmp).ok, True)

        # 代码块里的假链接不该被扫出来（先重写成干净内容，免得上一轮残留干扰）
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n```\n[示例](docs/ghost.md)\n```\n")
        eq("代码块内不误报", check(tmp).ok, True)

        # 判不了 ⇒ 阻塞（fail-closed）：真链接 + 目标被删
        open(os.path.join(tmp, "README.md"), "w", encoding="utf-8").write(
            "# 标题\n\n- [去文档](docs/x.md)\n")
        os.remove(os.path.join(tmp, "docs", "x.md"))
        rep = check(tmp)
        eq("文件被删 ⇒ 链接失效（不是静默通过）", rep.ok, False)

    for b in bad:
        print("  ✗ %s" % b)
    print("doccheck selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
