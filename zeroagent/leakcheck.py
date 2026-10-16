# -*- coding: utf-8 -*-
"""发布前泄露扫描 —— 把「我以为脱敏了」变成一条会红的命令。

问题
----
把一个内部项目的东西对外发布（收进公开仓库、发给同事、贴给别人看）时，
最常见的失败不是"忘了脱敏"，而是「**以为脱敏了**」：
真值藏在示例里、藏在路径里、藏在"看起来像占位符"的地方。

肉眼检查有两个天然缺陷：**会漏**（人只能同时盯几样）、**会疲**（看多了就滑过去）。

做法
----
**用真实配置当词典**，而不是靠正则猜"什么像敏感信息"：

1. **词典一（硬）**：从 `config/*.json` 里把**真值**全捞出来，逐个精确匹配。
   排除通用词（`self` / `group` / 布尔 / 过短的串）—— 它们是真值，但也是任何项目都有的词。
2. **词典二（软）**：行业专有词黑名单（`config` 里没有，但同样暴露业务）。
3. **模式三**：本机盘符 / 邮箱 / 远端地址 —— **且排除占位符写法**（`git@<host>:` 是示例，不是泄露）。

★ fail-closed：**只要命中，就不放行**。宁可多报一次，也不产出"以为干净"的东西。

三态
----
- ``ok``        : 未检出任何真值 / 业务词 / 本机坐标
- ``leak``      : 检出残留 —— **阻塞**
- ``unresolved``: 词典建不起来（配置读不出）—— **阻塞**（不知道黑名单，就没法判）

CLI::

    python -m zeroagent.leakcheck --config <config 目录> --scan <待扫目录或文件>
    python -m zeroagent.leakcheck --config ../p/config --scan skills --word 某行业词
    python -m zeroagent.leakcheck --selftest
"""

import argparse
import glob
import json
import os
import re
import sys

# config 里会出现、但不构成泄露的通用词（它们是"字段值"，不是"真值"）
GENERIC = {
    "self", "group", "company", "none", "null", "true", "false", "auto", "any",
    "utf-8", "utf8", "json", "md", "txt", "js", "mjs", "py", "sh", "yaml", "yml",
    "yes", "no", "on", "off", "get", "post", "put", "delete",
}

MIN_LEN = 4          # 短于这个长度的串不当"真值"（太容易误报）
MIN_LEN_CJK = 2      # ★ 含中日韩字符时另算：2 个汉字的信息量远大于 2 个字母
_CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")


def is_real_value(v):
    """这个串够不够"像真值"（太短的排除，避免误报）。

    ★ 长度阈值**不能跨语言通用**：`self`（4 个字母）是通用词，
    而 `张三`（2 个汉字）是**人名** —— 后者是最该脱敏的那一类。
    """
    if not v or v.lower() in GENERIC or "<" in v:
        return False
    if _CJK.search(v):
        return len(v) >= MIN_LEN_CJK
    return len(v) >= MIN_LEN


# 本机坐标的模式；命中后还要排除"占位符写法"
PATTERNS = (
    # ★ 负向前瞻：前面不能是字母 —— 否则 `https://` 里的 `s:/` 会被当成盘符（实测踩到）
    (re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]"), "本机盘符路径"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱"),
    (re.compile(r"git@[\w.-]+:"), "远端地址"),
)
# 占位符：`git@<host>:` / `C:\<路径>` / `<user>@...` —— 是示例，不算泄露
PLACEHOLDER = re.compile(r"<[^>]{1,40}>")


def looks_like_real(matched, context):
    """命中不是占位符写法才算真泄露。"""
    return not PLACEHOLDER.search(matched) and not PLACEHOLDER.search(context)


def build_dict(config_dir):
    """从 config 目录里的 JSON 捞真值。返回 (词表, 状态说明)。"""
    if not os.path.isdir(config_dir):
        return set(), "配置目录不存在：%s" % config_dir

    vals = set()
    for path in sorted(glob.glob(os.path.join(config_dir, "*.json"))):
        if ".example" in os.path.basename(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue

        def walk(o):
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str):
                v = o.strip()
                if is_real_value(v):
                    vals.add(v)

        walk(data)
    return vals, ""


class Finding(object):
    """一处命中。"""

    __slots__ = ("file", "kind", "text", "where")

    def __init__(self, file, kind, text, where=""):
        self.file = file
        self.kind = kind
        self.text = text
        self.where = where

    def describe(self):
        return "✗ %s  [%s] %s%s" % (
            self.file, self.kind, self.text[:70],
            ("   ← " + self.where) if self.where else "")

    def to_dict(self):
        return {"file": self.file, "kind": self.kind,
                "text": self.text, "where": self.where}


class ScanReport(object):
    def __init__(self, findings, scanned=0, state="ok", detail=""):
        self.findings = list(findings)
        self.scanned = scanned
        self.state = state
        self.detail = detail

    @property
    def ok(self):
        return self.state == "ok" and not self.findings

    def summary(self):
        if self.state == "unresolved":
            return "判不了：%s" % self.detail
        return "命中 %d 处 / 扫了 %d 个文件" % (len(self.findings), self.scanned)

    def to_dict(self):
        return {"ok": self.ok, "state": self.state, "detail": self.detail,
                "scanned": self.scanned, "summary": self.summary(),
                "findings": [x.to_dict() for x in self.findings]}


def collect(paths):
    """把传入的目录/文件展开成文件清单。"""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    out.append(os.path.join(root, f))
        elif os.path.isfile(p):
            out.append(p)
    return sorted(set(out))


def scan(paths, values, biz_words=(), unresolved=""):
    """扫一批文件。"""
    if unresolved:
        return ScanReport([], 0, "unresolved", unresolved)

    files = collect(paths)
    findings = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:
            findings.append(Finding(f, "读不出", str(e)[:60]))
            continue

        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            for v in values:
                if v in line:
                    findings.append(Finding(f, "真值", v, "第 %d 行" % i))
            for w in biz_words:
                if w and w in line:
                    findings.append(Finding(f, "业务词", w, "第 %d 行" % i))
            for pat, tag in PATTERNS:
                for m in pat.findall(line):
                    if looks_like_real(m, line):
                        findings.append(Finding(f, tag, m, "第 %d 行" % i))

    return ScanReport(findings, len(files), "ok" if not findings else "leak")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="zeroagent.leakcheck",
        description="发布前泄露扫描：用真实配置当词典（fail-closed）")
    ap.add_argument("--config", default="", help="config 目录（从这里取真值词典）")
    ap.add_argument("--scan", action="append", default=[],
                    help="待扫的目录或文件（可重复）")
    ap.add_argument("--word", action="append", default=[],
                    help="额外的行业专有词（可重复）")
    ap.add_argument("--words-file", default="", help="额外词的清单文件（一行一词）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not args.scan:
        print(json.dumps({"ok": False, "error": "没有 --scan，无事可做"},
                         ensure_ascii=False))
        return 2

    values, problem = build_dict(args.config)
    biz = list(args.word)
    if args.words_file and os.path.isfile(args.words_file):
        with open(args.words_file, encoding="utf-8") as f:
            biz += [x.strip() for x in f if x.strip()]

    rep = scan(args.scan, values, biz, unresolved=problem)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=1))
    else:
        print("★ 泄露扫描：词典 %d 条真值 + %d 个业务词" % (len(values), len(biz)))
        print()
        for f in rep.findings:
            print(f.describe())
        if not rep.findings and rep.state == "ok":
            print("（未检出残留）")
        print()
        print("判定：" + rep.summary())
        if rep.findings:
            print("⇒ **有残留就不许发布** —— 宁可多报一次，也不产出「以为干净」的东西。")
    return 0 if rep.ok else 1


def _selftest():
    """★ 造一个"看起来脱敏了、其实没脱干净"的样本，断言它**确实报红**。"""
    import tempfile
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "config")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "paths.json"), "w", encoding="utf-8") as f:
            json.dump({"spaceId": "1234567890", "accountTag": "张三",
                       "scope": "self", "ok": True, "databases": ["db_alpha"]}, f)

        vals, problem = build_dict(cfg)
        eq("词典建得起来", problem, "")
        eq("抓到真值 spaceId", "1234567890" in vals, True)
        eq("抓到真值 accountTag", "张三" in vals, True)
        eq("★ 通用词 self 不入词典", "self" in vals, False)
        eq("过短的值不入词典", "" in vals, False)
        eq("★ 占位符不入词典", is_real_value("<你的路径>"), False)
        eq("★ 2 个汉字 = 人名，必须入词典", is_real_value("李四"), True)
        eq("★ 2 个字母太短，不入", is_real_value("ab"), False)

        work = os.path.join(tmp, "out")
        os.makedirs(work)

        def write(name, text):
            with open(os.path.join(work, name), "w", encoding="utf-8") as f:
                f.write(text)

        # 干净样本
        write("clean.md", "# 标题\n\n文案里写的是 <accountTag> 与 <spaceId>。\n")
        eq("干净 ⇒ 通过", scan([work], vals).ok, True)

        # 坏场景 1：示例里夹了真值（最常见的泄露）
        write("dirty.md", "# 标题\n\n示例：`spaceId: 1234567890`\n")
        rep = scan([work], vals)
        eq("示例里夹真值 ⇒ 报红", rep.ok, False)
        eq("命中被标成真值", rep.findings[0].kind, "真值")

        # 坏场景 2：本机盘符路径
        write("dirty.md", "# 标题\n\n路径是 C:\\Users\\someone\\x\n")
        eq("本机盘符 ⇒ 报红", scan([work], vals).ok, False)

        # 坏场景 3：占位符写法**不该**误报
        write("dirty.md", "# 标题\n\n远端写 `git@<host>:<ns>/x.git`，路径写 `C:\\<你的路径>`\n")
        eq("占位符写法不误报", scan([work], vals).ok, True)

        # 坏场景 4：业务词
        write("dirty.md", "# 标题\n\n这个是某行业的专有名词\n")
        eq("业务词 ⇒ 报红", scan([work], vals, biz_words=["某行业的专有名词"]).ok, False)

        # 坏场景 5：★ URL 不该被当成盘符（`https://` 里的 `s:/`）
        write("dirty.md", "# 标题\n\n报错原文：could not read Username for 'https://x.com'\n")
        eq("★ URL 不误报为盘符", scan([work], vals).ok, True)

        # 判不了 ⇒ 阻塞
        eq("配置目录不存在 ⇒ 判不了", scan([work], vals, unresolved="配置目录不存在").ok, False)

    for b in bad:
        print("  ✗ %s" % b)
    print("leakcheck selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
