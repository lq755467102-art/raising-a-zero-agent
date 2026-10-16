---
name: generality-guard
description: 「通用化（可移植）守则 + 改造后等价性验证」——用在一个会分发到多台机器/多个人的自动化项目上。当你要求「通用化」「换人换电脑也能跑通」「去掉硬编码」「脚本里有我的名字/空间ID/客户ID」「新增/修改脚本」，或即将改动 `scripts/` 下任何脚本时用它：先按规范改，再跑自检，最后做等价性验证与回读校验再部署。触发词：通用化、可移植、换人、换电脑、硬编码、字面量、配置外置、等价性验证、半通用化。
agent_created: true
---

# 通用化守则与验证（换个人、换台电脑都能跑通）

> **来源**：本文件脱胎于一个真实的多机协作自动化项目（已去业务坐标）。
> 里面的每个检查项都对应一次**真实踩过的坑** —— 值钱的地方就在这里，所以坐标被替换、方法都留着。

**仓库位置**：`{{REPO}}`

目标原话：「**换一个员工换一台电脑也能跑通**」。
判据不是"能启动"，而是**配置缺失时绝不静默套用别人的身份**。

---

## 零、铁律（违反即视为缺陷）

1. **个人/业务值零字面量**：源码里只允许出现**配置键名**，不得出现真实的人名、空间 ID、表 ID、客户 ID、绝对路径。
2. **兜底默认值不得是"某个真实的人"**：宁可 `null` + 显式报错，也不要悄悄用别人的身份跑通。
   —— 这类项目最危险的一类 bug 是「**看起来通了、实际没通**」（本机测试全绿，换人后跑到**别人**的账户上）。
3. **业务清单进数据文件**：账号/主体清单放 `config/accounts.json`（**本机配置**，不入库），不放脚本源码。
4. **新脚本必须接配置通道**：JS `import paths.mjs`；Python `_load_paths()`。
5. **改完必须验证**（§三），并**回读校验**部署结果（§四）。
6. **模板不得当配置用**：`config/*.example.json` 里的 `<...>` 占位符 = **未填写**；加载器会拒载（§一）。
7. **入库文件不得含本机绝对路径**：`skills/` / `automations/` / `rules/` 里一律写**占位符**
   （`{{REPO}}` / `{{NODE}}` / `{{SESSION}}` …）；真实路径只出现在**部署位置**，由同步器自动展开。
   —— 否则多人协作时 `git diff` 全是路径噪声、真改动被淹没。

---

## 一、值该放哪（分成两个文件）

| 文件 | 放什么 | 入库 | 谁改 |
|---|---|---|---|
| `config/paths.json` | 本机路径 + **部署身份**（`accountTag` / `spaceId` / `databases` / `archiveFolderTitle`）+ **可见范围**（`accountScope` / `accountScopeTag`） | ❌（`.gitignore`） | 安装脚本生成；身份项**人工填** |
| `config/accounts.json` | **本机账号清单** `[{name,id}]`，**数组顺序 = 遍历顺序** | ❌（**每人一份**） | 人工 / 某个业务流程自动写 |
| `config/*.example.json` | 上面两者的**模板**（全占位符） | ✅ | 基本不动 |

**判断准则**：**随机器 / 人 / 角色变 → `paths.json`；随个人业务变 → `accounts.json`**。
两者都是**本机配置**（不入库），只有模板入库；且都**不许留占位符**（占位符 = 未填写，会被拒载）。

加载器暴露：`PATHS` / `CONFIG_SOURCE` / `ACCOUNTS` / `ACCOUNTS_SOURCE` /
`SUBJECT_MAP`（name→id）/ `SUBJECT_IDS` / `ACCOUNT_SCOPE`，
以及 `requireIdentity(key,hint)` / `requireAccounts()` / `commentFilter()`。

### 可见范围三级（`commentFilter()`）

账号标签**不一定就是操作者名**（真实项目里这一点反复踩）：

| 层级 | 标签形态 | `accountScope` | `commentFilter()` 返回 |
|---|---|---|---|
| 普通成员 | 操作者名（如 `<accountTag>`） | `self`（默认） | `["<accountTag>"]` |
| 上级管理者 | 组名前缀（`<组码>`） | `group` + `accountScopeTag` | `["<组码>"]` |
| 公司级 | 标签无区分意义 | `company` | `null`（**不加**过滤条件） |

消费侧：**过滤**走 `CMTS`（取自 `accountTag`），**署名**走 `operator` —— 公司级登录时二者**不同**：

```js
const CMTS = commentFilter ? commentFilter() : [ACCOUNT_TAG];   // 兜底 = self，等价历史
...(CMTS ? { comments: CMTS } : {}),                      // 原写法：comments: [ACCOUNT_TAG]
```

> ⚠ 别把所有 `ACCOUNT_TAG` 一律换成 `CMTS`：归档/入库的**署名**始终是当前操作者。

### 消费侧写法（照抄）

```js
// JS
import { PATHS, ACCOUNTS, CONFIG_SOURCE } from "./paths.mjs";
const ACCOUNT_TAG = PATHS.accountTag || "";
if (!ACCOUNT_TAG) { console.error("✗ 未配置 accountTag：请在 config/paths.json 设置…"); process.exit(1); }
```

```python
# Python
_CFG = _load_paths()                       # 三级降级：环境变量 → ../config/paths.json → config/paths.json
SPACE_ID = _CFG.get("spaceId")             # ⚠ 不给默认值
if not SPACE_ID:
    print("[XXX] ✗ 未配置空间 ID：请在 config/paths.json 设置 spaceId。本次不处理。")
    return                                  # 退出码 0，不影响主流程
```

### 入库文件里的绝对路径 → 占位符（铁律 7 的落地）

技能 / prompt / 规则**必须写绝对路径**（agent 要照着执行），但它们**入库** ⇒ 会污染协作 diff。
解法：**双形态 + 自动转换**。

| 形态 | 内容 | 位置 |
|---|---|---|
| 仓库形态 | 占位符 `{{REPO}}` / `{{WB_ROOT}}` / `{{PROJECT}}` / `{{SESSION}}` / `{{HOME_SCRIPTS}}` / `{{NODE}}` / `{{PY}}` / `{{HOME}}` | `skills/` `automations/` `rules/` |
| 本机形态 | 本机真实绝对路径 | 各部署位置 |

```bash
node install-skills.mjs            # 下发：expand（占位符 → 真实路径）
node install-skills.mjs --push     # 回收：shrink（真实路径 → 占位符）
node scripts/placeholder.mjs list  # 查看 占位符 ↔ 本机值 映射
node scripts/placeholder.mjs check # 校验入库文件已全部占位符化（= 自检 F 项）
```

实现要点（改转换器时别踩）：

- **替换顺序必须是「值的长度降序」**：`{{HOME}}` 是 `{{NODE}}`/`{{REPO}}` 等的**前缀**，
  若先替换 HOME，`{{NODE}}` 就再也匹配不上。
- **不变式**：`expand(shrink(L)) == L`、`shrink(expand(R)) == R`；对不含相关内容的文本是 no-op。
- **同步器的比对必须基于转换后的文本**，否则「仓库占位符版」与「本机真实路径版」永远被判为"不同"。
- 新建/修改**任何**入库的 md / txt / json 时，若写了绝对路径 → 事后 `shrink` 收敛，或直接写成占位符。

---

## 二、改完先自检（防退化，硬编码会复发）

```bash
cd "{{REPO}}"
node scripts/check-generality.mjs        # 跑到「✅ 全部通过」
```

| 检查 | 抓什么 |
|---|---|
| A 本机账号清单 | `accounts.json` 存在、非空、无重名/重复 ID |
| B 部署身份 | `accountTag`/`spaceId`/`databases` 就绪；`accountScope` 合法且 `group` 必带 `accountScopeTag` |
| C 禁止字面量 | 源码含员工名/空间 ID/表 ID/主体 ID/用户名 → **失败**；注释示例 → 告警（非阻塞） |
| D **配置传播** ★ | 注入 `WB_PATHS_FILE` 后 `accountTag`/`spaceId` 必须随之改变 |
| E 消费方接入 | 关键脚本必须 `import paths.mjs`；账户过滤必须走 `commentFilter()`（**出现字面量数组即失败**） |
| F 仓库形态 | **全仓入库文件（含 `docs/` `README.md`）**不得含**本机绝对路径 / 个人坐标**（应存占位符 `{{REPO}}` / `{{REMOTE}}`）→ 抓到即说明有人绕过占位符体系 |
| **G 脱敏形态** | **所有入库文件**不得含**本机 config 里的真实业务值** → 抓到即说明有人把真实值写回了仓库。**这是脱敏的防线**：黑名单取自 `config/*.json`，所以在别人机器上跑时保护的是**他自己的**值 |
| H 远端坐标 | 入库文件不得含**真实远端地址**（形如 `git@<host>:<ns>/<repo>.git`）→ 一律写 `{{REMOTE}}` |
| I 本机钩子 | `.git/hooks/pre-push` 应与模板一致（不一致 = 策略没生效）→ 重装钩子 |
| J 文档新鲜度 | `README.md` 顶部须有「最后更新：YYYY-MM-DD」（只查存在性 + 格式） |
| **K 三件套一致性** ★ | 本机配置文件必须**同时**登记在三处：`.gitignore` / 安装器的文件清单 / 本脚本的跳过清单。漏 ① = 真实配置被提交（**泄密**）；漏 ② = 换机下发缺文件（跑不起来）；漏 ③ = 把本机文件当入库文件扫描并**误报**。★ 这个清单曾被**复制成三份、且三份都漏了同一个文件** —— 就是"同一份清单存在多处"的典型事故 |

> ★ **D 项是核心**：它能抓出「看起来接入了、其实没生效」的**半通用化** bug ——
> 例：主配置读对了（`ACCOUNT_TAG = P.accountTag`），但拼 URL 的地方写成硬编码数组（**错**）。
> 改配置后署名变了、过滤却没变。这种 bug **只有 D / 正反向测试能抓到**。
>
> 需豁免的单行加注释 `gen-ok`。

---

## 三、等价性验证（改造前后行为必须一致）

**顺序**：先记录「改造前的基准值」→ 改造 → 逐项比对。**缺了基准就没法证明等价。**

| # | 项 | 做法 |
|---|---|---|
| 1 | **JS 解析值** | 直接 import 配置模块，打印关键字段 + 清单长度 |
| 2 | **Python 解析值** | 用 `importlib.util.spec_from_file_location` 导入（**文件名带 `-` 不能直接 import**），比对解析后的值（**按 JSON 解析比，不比原串**，空格差异无语义） |
| 3 | 语法 | `node --check` × 全部 mjs；`python -m py_compile` × 全部 py |
| 4 | **反向测试** ★ | 构造「缺 `accountTag`」的临时配置跑脚本 → **必须退出码 1 + 明确报错**，不得静默兜底 |
| 5 | 正向冒烟 | 从**每个部署位置**各自 import 配置，确认就近读到；有只读模式的脚本跑一下 |

**必须记档的"刻意差异"**：若为统一配置而改变了某个行为（如**遍历顺序**），
要写清楚：① 顺序敏感的是哪个脚本；② 其余只是日志顺序；③ 有无子集副作用。

---

## 四、部署与回读校验

```bash
node install-skills.mjs --dry-run          # 预演：确认「拦截 0」再动手
node install-skills.mjs --scripts-only     # 下发脚本（含 config/）
node install-skills.mjs --skills-only      # 下发技能
```

**回读校验**（部署后必做）：逐文件比对「仓库 vs 实机」md5，应 100% 一致。

> ⚠ `config/paths.json` 与 `config/accounts.json` 会被复制到**每个**部署位置旁；
> 只放一处会让另一侧退化为"自动探测"，而自动探测**故意没有**身份默认值 → 直接报错。

---

## 五、易错点

- **别把同名文件当"用户迭代"**：某些部署位可能存在**有意的过期副本**（带运行闸门）。部署前先看文件头注释，别误判成冲突。
- **安装脚本不许往新机种入真实身份**：取值顺序是「保留已有的 → 环境变量 → `null`」。
- **文档与技能里的真实值一律标「示例」**，避免被后来者当约定抄进代码。
- **标签体系要正视**：标签往往是**平台侧的业务约定**（格式改不了），
  且**不一定就是操作者名** —— 上级看到的是组名，公司级则不筛选。
  对策：把「拿哪一段去筛」做成**配置项**（`accountScope` + `accountScopeTag`），解析逻辑按分隔符切分（**与具体人名无关**）。
- **新增脚本沿用同一套**：**从第一天**就按本守则写，不要留历史包袱。

---

## 六、这个方法为什么值钱

通用化最容易失败在**"改了一半"**：配置接进来了，但某个角落还留着旧值 ——
**它不会报错，只会在换人之后默默跑到别人那里去**。

所以这里的核心不是"怎么改"，是「**怎么证明改完了**」：
**正向（配置变了行为跟着变）+ 反向（配置缺了必须报错）+ 回读（部署后逐字节一致）**，
三条都过，才算改完。

> 这三条对应的是同一个判断：**"看起来通了"和"真的通了"是两件事。**
