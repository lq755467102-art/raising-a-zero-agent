# -*- coding: utf-8 -*-
"""跨实例反馈：采集 / 回流 / **对账**（fail-closed）。

要解决什么
----------
``clonecheck`` 只能回答「空白机器**能不能跑**」——那是**代码层**的克隆验证。
但真正要紧的问题是：**机器跑起来了，它判断得对不对？**

于是还缺**判断层**的那一半：**另一个实例，在没有我在场的时候，做出的判断和我是否一致。**
这不是日志能回答的——日志回答「发生了什么」；**对账**回答「两个实例对同一件事的看法是否一致」。
后者才是「能力是否被复刻」的判据，也是本模块与 logger 的分界。

三个不显然的设计点
------------------
1. **fail-closed 落在数据入口，而不是出口**。
   「缺 ``topic``」和「缺 ``basis``」的条目**在写入时就被拒绝**，不是等到对账时再过滤。
   理由：一条无法归组、或没有依据的判断，进入库之后只会污染对账结果——
   **让它进不来，比让它进来再忽略更安全**。

2. **「只有一方表态」必须判成『判不了』，而不是『一致』**。
   单方观点看起来"没有分歧"，但那只是**没有第二个人**，不是**同意**。
   把它当一致，正是本仓库要治的病：**把"不知道"说成"没问题"**。

3. **分歧不自动选边**。
   对账**只负责把分歧显形，并把双方依据一并列出**，由人裁决。
   「谁对」由依据决定，不由多数决定，也不由调用方决定——

用法::

    from zeroagent.feedback import record, reconcile, merge, register_expectation

    record(topic="锁按什么分片", judgment="按脚本", basis="内外循环本不重合")
    ...
    r = reconcile()          # 同一 topic 下不同实例的判断是否一致
    print(r["disagree"])     # 分歧条数（>0 需人裁决）

CLI::

    python -m zeroagent.feedback --selftest
    python -m zeroagent.feedback reconcile
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

DEFAULT_DIR = os.environ.get("ZEROAGENT_FEEDBACK_DIR", os.path.join(os.path.expanduser("~"), ".zeroagent", "feedback"))
EVENTS_FILE = "events.jsonl"

# 对账结果的三种结论（★ 不许有第四种"说不清但算了"）
AGREE = "一致"
DISAGREE = "★ 分歧"
UNCOMPARABLE = "判不了"        # 证据不足（缺 basis / 只有一方有）⇒ fail-closed 显形


# ---------------------------------------------------------------- 采集 / 读

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def instance_id() -> str:
    """本实例标识。★ 不猜、不伪造：环境变量优先，其次主机名（含用户名），最后 'unknown'。"""
    v = (os.environ.get("ZEROAGENT_INSTANCE") or "").strip()
    if v:
        return v
    try:
        import socket
        host = socket.gethostname() or "host"
    except Exception:
        host = "host"
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "u"
    return f"{user}@{host}"


def events_path(d: str | None = None) -> str:
    d = d or DEFAULT_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, EVENTS_FILE)


def read_events(d: str | None = None):
    """读全部事件。返回 (events, problems)。

    ★ fail-closed：坏行**不忽略、不静默** —— 记进 problems 让调用方显形。
      （"读不出来的那一行"很可能正是最该看的那一行。）
    """
    p = events_path(d)
    events, problems = [], []
    if not os.path.exists(p):
        return events, problems
    with open(p, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception as e:
                problems.append(f"{EVENTS_FILE}:{i} JSON 解析失败：{e}")
                continue
            if not isinstance(o, dict):
                problems.append(f"{EVENTS_FILE}:{i} 不是对象")
                continue
            events.append(o)
    return events, problems


def record(topic: str, judgment: str, basis: str, outcome: str = "", d: str | None = None, instance: str | None = None):
    """采集一条事件。★ 缺 topic / basis ⇒ 直接拒绝（fail-closed：无法对账的不许入账）。"""
    if not (topic or "").strip():
        raise ValueError("缺 topic ⇒ 无法归入任何一组 ⇒ 拒绝入账（fail-closed）")
    if not (judgment or "").strip():
        raise ValueError("缺 judgment ⇒ 拒绝入账")
    if not (basis or "").strip():
        raise ValueError("缺 basis ⇒ 没有依据的判断在对账时等于噪声 ⇒ 拒绝入账（fail-closed）")
    ev = {
        "ts": _now(),
        "instance": instance or instance_id(),
        "topic": topic.strip(),
        "judgment": judgment.strip(),
        "basis": basis.strip(),
        "outcome": outcome.strip(),
    }
    with open(events_path(d), "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


# ---------------------------------------------------------------- 回流

def merge(paths, d: str | None = None):
    """回流：把别处的 events.jsonl 并入本机。★ 同一条事件（ts+instance+topic）不重复入。"""
    seen = set()
    for e in read_events(d)[0]:
        seen.add((e.get("ts"), e.get("instance"), e.get("topic")))
    added, skipped, problems = 0, 0, []
    for p in paths:
        if not os.path.exists(p):
            problems.append(f"文件不存在：{p}")
            continue
        with open(p, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception as e:
                    problems.append(f"{os.path.basename(p)}:{i} 解析失败：{e}")
                    continue
                key = (o.get("ts"), o.get("instance"), o.get("topic"))
                if key in seen:
                    skipped += 1
                    continue
                with open(events_path(d), "a", encoding="utf-8") as g:
                    g.write(json.dumps(o, ensure_ascii=False) + "\n")
                seen.add(key)
                added += 1
    return {"added": added, "skipped": skipped, "problems": problems}


# ---------------------------------------------------------------- ★ 对账

def reconcile(d: str | None = None):
    """★ 对账：同一 topic 下，不同实例的判断是否一致。

    返回 {"topics": [...], "agree": n, "disagree": n, "uncomparable": n, "problems": [...]}

    判据（三条，全部 fail-closed）：
      1. 只有**一个**实例表过态 ⇒ `判不了`（单方观点无法对账，**不算一致**）；
      2. 缺 basis 的条目 ⇒ 该 topic 记 `判不了`（无从判断分歧是"真分歧"还是"理解不同"）；
      3. 判断**字面去空白后相等** ⇒ 一致；否则 ⇒ 分歧（列出双方依据，供人裁决）。
    """
    events, problems = read_events(d)
    by_topic: dict[str, list] = {}
    for e in events:
        by_topic.setdefault(str(e.get("topic") or ""), []).append(e)

    out = []
    agree = disagree = uncomparable = 0
    for topic, evs in sorted(by_topic.items()):
        if not topic:
            uncomparable += 1
            out.append({"topic": "(空 topic)", "verdict": UNCOMPARABLE, "why": "事件缺 topic ⇒ 无法归组", "views": evs})
            continue
        insts = {str(e.get("instance")) for e in evs}
        if len(insts) < 2:
            uncomparable += 1
            out.append({"topic": topic, "verdict": UNCOMPARABLE,
                        "why": f"只有 {len(insts)} 个实例表过态（{sorted(insts)}）⇒ 单方观点无法对账",
                        "views": evs})
            continue
        if any(not str(e.get("basis") or "").strip() for e in evs):
            uncomparable += 1
            out.append({"topic": topic, "verdict": UNCOMPARABLE,
                        "why": "有条目缺 basis ⇒ 无法判断分歧是『真分歧』还是『理解不同』", "views": evs})
            continue
        js = {str(e.get("judgment") or "").strip() for e in evs}
        if len(js) == 1:
            agree += 1
            out.append({"topic": topic, "verdict": AGREE, "why": f"judgment 一致：{sorted(js)[0]}", "views": evs})
        else:
            disagree += 1
            out.append({"topic": topic, "verdict": DISAGREE, "why": f"judgment 不一致：{sorted(js)}", "views": evs})

    return {"topics": out, "agree": agree, "disagree": disagree,
            "uncomparable": uncomparable, "problems": problems}


# ---------------------------------------------------------------- 期待（可对账）

def register_expectation(topic: str, basis: str, check_by: str = "", d: str | None = None):
    """登记一条「可对账的期待」（把"怀疑"变成能被现实打脸的东西）。

    ★ 出处：三条主干里「怀疑自己」唯一缺硬判据 —— 建议硬化为
      「每次怀疑必须落成一个具体期待，到期没对账 ⇒ 该怀疑作废」。
    """
    return record(topic=topic, judgment="待对账", basis=basis, outcome=f"checkBy={check_by}", d=d)


# ---------------------------------------------------------------- 渲染

def render_reconcile(r) -> str:
    lines = ["═══ 跨实例对账（fail-closed）═══"]
    for t in r["topics"]:
        mark = {AGREE: "✓", DISAGREE: "✗", UNCOMPARABLE: "?"}[t["verdict"]]
        lines.append(f"{mark} {t['verdict']} · {t['topic']}")
        lines.append(f"    {t['why']}")
        for e in t["views"]:
            lines.append(f"    - [{e.get('instance')}] {e.get('judgment')}  ← {e.get('basis')}")
    if r["problems"]:
        lines.append("")
        lines.append(f"⚠ 读取问题 {len(r['problems'])} 条（**不当作没有分歧**）：")
        for p in r["problems"]:
            lines.append(f"    - {p}")
    lines.append("")
    lines.append(f"判定：一致 {r['agree']} / 分歧 {r['disagree']} / 判不了 {r['uncomparable']}"
                 + (f" / 读取问题 {len(r['problems'])}" if r["problems"] else ""))
    if r["disagree"]:
        lines.append("⇒ 有分歧：**列出供人裁决**，不自动选边（谁对由依据决定，不由多数决定）。")
    if r["uncomparable"]:
        lines.append("⇒ 有『判不了』：**显形**，不当成通过。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    ok = fail = 0
    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1; print(f"  ✓ {name}")
        else:
            fail += 1; print(f"  ✗ {name}")

    with tempfile.TemporaryDirectory() as d:
        # 1) 缺 topic / basis ⇒ 拒绝（fail-closed）
        for bad in ({"topic": "", "judgment": "x", "basis": "y"},
                    {"topic": "t", "judgment": "x", "basis": ""}):
            try:
                record(d=d, **bad); chk(f"缺字段应拒绝 {bad}", False)
            except ValueError:
                chk("缺 topic/basis 会被拒绝", True)
        # 2) 正常采集
        record("给员工开 Developer", "开", "不给权限违反复杂度留给自己", d=d, instance="B")
        record("给员工开 Developer", "开", "与设计思路一致", d=d, instance="C")
        chk("采集成功", len(read_events(d)[0]) == 2)
        # 3) 两实例一致 ⇒ agree
        r = reconcile(d)
        chk("两实例一致 ⇒ agree", r["agree"] == 1 and r["disagree"] == 0)
        # 4) 单实例 ⇒ 判不了（不许算一致）
        d2 = d + "-2"
        record("只有一方", "是", "依据", d=d2, instance="B")
        r2 = reconcile(d2)
        chk("单实例 ⇒ 判不了", r2["uncomparable"] == 1 and r2["agree"] == 0)
        # 5) 分歧 ⇒ 列出双方
        d3 = d + "-3"
        record("某判断", "是", "依据甲", d=d3, instance="B")
        record("某判断", "否", "依据乙", d=d3, instance="C")
        r3 = reconcile(d3)
        chk("分歧 ⇒ disagree", r3["disagree"] == 1)
        chk("分歧输出含双方依据", "依据甲" in render_reconcile(r3) and "依据乙" in render_reconcile(r3))
        # 6) 坏行 ⇒ 进 problems，不算"无分歧"
        d4 = d + "-4"
        record("t", "j", "b", d=d4, instance="B")
        with open(events_path(d4), "a", encoding="utf-8") as f:
            f.write("{坏行\n")
        r4 = reconcile(d4)
        chk("坏行进 problems", len(r4["problems"]) == 1)
        # 7) 回流去重
        m = merge([events_path(d3)], d=d4)
        chk("回流新增 2 条", m["added"] == 2)
        m2 = merge([events_path(d3)], d=d4)
        chk("再次回流全跳过（幂等）", m2["added"] == 0 and m2["skipped"] == 2)
        # 8) 期待登记
        e = register_expectation("某期待", "因为它可被证伪", "2026-11-01", d=d)
        chk("期待登记带 checkBy", "checkBy=2026-11-01" in e["outcome"])

    print(f"\n自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


# ---------------------------------------------------------------- CLI

def _arg(name, default=""):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def main() -> int:
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__); return 0
    if a[0] == "--selftest":
        return _selftest()

    cmd = a[0]
    if cmd == "record":
        ev = record(_arg("--topic"), _arg("--judgment"), _arg("--basis"), _arg("--outcome"), instance=_arg("--instance") or None)
        print(json.dumps(ev, ensure_ascii=False)); return 0
    if cmd == "expect":
        ev = register_expectation(_arg("--topic"), _arg("--because"), _arg("--check-by"))
        print(json.dumps(ev, ensure_ascii=False)); return 0
    if cmd == "show":
        evs, probs = read_events()
        for e in evs:
            print(json.dumps(e, ensure_ascii=False))
        for p in probs:
            print("⚠", p, file=sys.stderr)
        return 0
    if cmd == "merge":
        files = [x for x in a[1:] if not x.startswith("--")]
        if not files:
            print("用法：feedback merge <事件文件...>", file=sys.stderr); return 2
        m = merge(files)
        print(f"回流：新增 {m['added']} / 跳过 {m['skipped']}")
        for p in m["problems"]:
            print("⚠", p, file=sys.stderr)
        return 0
    if cmd == "reconcile":
        r = reconcile()
        print(render_reconcile(r))
        return 1 if (r["disagree"] or r["uncomparable"] or r["problems"]) else 0

    print(f"未知命令：{cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
