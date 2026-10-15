# -*- coding: utf-8 -*-
"""词典脱敏 + 残留自检（fail-closed）。

一个「看起来脱敏了」的文档，和一个「真的脱敏了」的文档，差别往往只在一两处漏网。
只做替换的工具**没法告诉你它漏了**——所以本模块把两件事绑在一起：

    替换 ──► 再扫一遍 ──► 有残留就**抛错**，不产出。

三条不显然的设计
----------------
1. **用精确词典，不靠正则猜。**
   把真值原样列出来逐个替换。正则猜测（比如「像不像手机号」）会漏、也会误伤；
   真值来自配置，本来就已知。

2. **长值优先替换。**
   若词典里同时有 ``A`` 和 ``AB``，必须先替 ``AB``，否则 ``A`` 会把 ``AB`` 的
   子串先吃掉，剩下 ``<占位>B``。本模块**自己在内部排序**，不依赖调用方。

3. **导出后自检（fail-closed）。**
   :meth:`Redactor.residual` 独立于替换逻辑，再扫一遍原文/产物。
   只要命中任何真值或规则，:meth:`Redactor.redact_strict` 就抛 :class:`LeakError`。
   宁可没有输出，也不输出「以为脱敏了、其实没脱干净」的东西。

用法::

    from failclosed.redact import Redactor, ContextRule

    r = Redactor(
        pairs=[("Acme Corp", "<COMPANY>"), ("10000001", "<ACCOUNT>")],
        rules=[ContextRule(r"\\d{8}", ["account", "账号"], "<ACCOUNT>")],
    )
    safe = r.redact_strict(document)      # 有残留就抛错
"""

import re


class LeakError(Exception):
    """脱敏后仍检出残留真值 —— 拒绝产出。"""


class ContextRule:
    """「上下文相关」替换：命中正则**且附近出现关键词**时才替换。

    用于兜住**词典之外**的值（词典只覆盖已知项，文档里可能引用别的同类值）。

    >>> rule = ContextRule(r"\\d{8}", ["account", "账号"], "<ACCOUNT>", window=20)
    >>> rule.findall("实测通过（账号 10000001）")
    ['10000001']
    >>> rule.findall("版本 98765432 已发布")     # 附近没有关键词 ⇒ 不动
    []
    """

    __slots__ = ("pattern", "keywords", "placeholder", "window")

    def __init__(self, pattern, keywords, placeholder, window=20):
        self.pattern = re.compile(pattern)
        self.keywords = [str(k) for k in keywords]
        self.placeholder = placeholder
        self.window = int(window)

    def _near_keyword(self, text, start, end):
        ctx = text[max(0, start - self.window): end + self.window].lower()
        return any(k.lower() in ctx for k in self.keywords)

    def apply(self, text):
        def repl(m):
            if self._near_keyword(text, m.start(), m.end()):
                return self.placeholder
            return m.group(0)
        return self.pattern.sub(repl, text)

    def findall(self, text):
        return [m.group(0) for m in self.pattern.finditer(text)
                if self._near_keyword(text, m.start(), m.end())]


class Redactor:
    """按词典（+ 可选规则）脱敏，并能独立自检。"""

    def __init__(self, pairs=(), rules=()):
        norm, seen = [], set()
        for real, ph in pairs:
            real = "" if real is None else str(real)
            ph = "" if ph is None else str(ph)
            if real and real not in seen:
                seen.add(real)
                norm.append((real, ph))
        # ★ 长值优先：内部自己排序，不依赖调用方
        norm.sort(key=lambda x: -len(x[0]))
        self._pairs = norm
        self._rules = list(rules)

    def __len__(self):
        return len(self._pairs)

    def redact(self, text):
        """替换（不做自检）。"""
        if text is None:
            return ""
        out = str(text)
        for real, ph in self._pairs:
            if real in out:
                out = out.replace(real, ph)
        for rule in self._rules:
            out = rule.apply(out)
        return out

    def residual(self, text):
        """返回仍残留的真值 / 命中规则的文本（空列表 = 干净）；同一值只报一次。"""
        hits, seen = [], set()
        for real, _ph in self._pairs:
            if real in text and real not in seen:
                seen.add(real)
                hits.append(real)
        for rule in self._rules:
            for h in rule.findall(text):
                if h not in seen:
                    seen.add(h)
                    hits.append(h)
        return hits

    def is_clean(self, text):
        return not self.residual(text)

    def redact_strict(self, text):
        """替换 + 自检；有残留则抛 :class:`LeakError`（fail-closed）。"""
        out = self.redact(text)
        hits = self.residual(out)
        if hits:
            preview = ", ".join(repr(h) for h in hits[:5])
            raise LeakError("脱敏后仍检出 %d 处残留：%s" % (len(hits), preview))
        return out


def _selftest():
    """自测：钉死替换与 fail-closed 行为（不读外部文件）。"""
    bad, checks = [], []

    def eq(name, got, want):
        checks.append(name)
        if got != want:
            bad.append("%s：得到 %r，期望 %r" % (name, got, want))

    r = Redactor(pairs=[("Acme Corp", "<COMPANY>"), ("10000001", "<ACCOUNT>")])
    t = "Acme Corp 的账号 10000001 已启用"
    out = r.redact(t)
    eq("公司名被替", "Acme Corp" in out, False)
    eq("账号被替", "10000001" in out, False)
    eq("占位符在", "<COMPANY>" in out and "<ACCOUNT>" in out, True)
    eq("残留能检出", len(r.residual(t)) > 0, True)
    eq("干净文本无残留", r.residual(out), [])
    eq("严格模式：干净输入正常返回", r.redact_strict(t), out)

    # fail-closed 闸门：残留检出时**必须**抛错（用一个故意漏检的子类验证闸门本身）
    class _Leaky(Redactor):
        def residual(self, text):
            return ["forced-residual"]

    try:
        _Leaky().redact_strict("x")
        eq("严格模式：有残留必须抛错", False, True)
    except LeakError:
        eq("严格模式：有残留必须抛错", True, True)

    # 长值优先
    r2 = Redactor(pairs=[("A", "<A>"), ("AB", "<B>")])
    eq("长值优先", r2.redact("AB"), "<B>")

    # 上下文规则（注意：正则要用单反斜杠 r"\d{8}"）
    rule = ContextRule(r"\d{8}", ["账号", "account"], "<ACCOUNT>")
    eq("附近有关键词 ⇒ 替", rule.findall("实测通过（账号 10000001）"), ["10000001"])
    eq("附近无关键词 ⇒ 不动", rule.findall("版本 98765432 已发布"), [])
    eq("规则参与严格自检",
       Redactor(rules=[rule]).residual("账号 10000001") != [], True)

    for b in bad:
        print("  ✗ %s" % b)
    print("redact selftest: %s（%d 项）" % ("OK" if not bad else "FAIL", len(checks)))
    return 1 if bad else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
