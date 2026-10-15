# 架构（泛化）

> 这份图描述的是**抽取来源的那套生产系统**，已做泛化处理：不含任何客户、平台或业务细节。
> 重点是**结构**，不是具体做什么。

---

## 本页速览

- **整体分层**：调度 → 执行 → 守卫 → 数据；**守卫层**就是本仓库抽出的部分
- **检查闸门的判定流**：三条硬规则 —— 判不了 / 未声明 / 读不出 ⇒ **一律阻塞**
- **锁的状态迁移**：三个判据（pid 死 / 心跳过期 / 超 TTL）各自挡什么
- **五层各自的静默失败与守卫**（末尾一张表）

> 这篇只画结构；**完整的设计逻辑**（职责边界、任务生命周期、边界与扩展点）
> 见 [`system-design.md`](system-design.md)。

---

## 一、整体分层

```mermaid
flowchart TB
    subgraph SCHED["调度层"]
        direction LR
        T1["定时任务 A"]
        T2["定时任务 B"]
        T3["定时任务 C"]
    end

    subgraph EXEC["执行层"]
        ORCH["任务编排<br/>（断点续跑 / 幂等）"]
        WORK["多租户 worker<br/>（Web 平台自动化）"]
    end

    subgraph GUARD["守卫层 ← 本仓库抽出的部分"]
        LOCK["reslock<br/>按资源分片的锁"]
        CHK["checkpoint<br/>幂等 + 断点续跑"]
        RED["redact<br/>脱敏 + 残留自检"]
        GATE["checkgate<br/>发布前检查闸门"]
        APPR["approvalgate<br/>审批闸门（防自批）"]
        BUDG["contextbudget<br/>上下文预算水位"]
    end

    subgraph DATA["数据层"]
        DB["结构化落库"]
        DOC["文档 / 报表库"]
    end

    SCHED --> EXEC
    ORCH --> WORK
    WORK --> DB
    WORK --> DOC

    WORK -. "独占账号级共享资源" .-> LOCK
    ORCH -. "可安全重跑" .-> CHK
    DOC -. "导出前脱敏" .-> RED
    DB -. "提交前自检" .-> GATE
    WORK -. "有副作用的操作" .-> APPR
    ORCH -. "指令 / 笔记容量" .-> BUDG

    classDef guard fill:#fff4e5,stroke:#d97706,stroke-width:2px;
    class LOCK,CHK,RED,GATE,APPR,BUDG guard;
```

**要点**：守卫层不参与业务，只回答「**这一步出去之后，会不会把不该出去的东西带出去 / 会不会骗自己**」。

---

## 二、`checkgate`：fail-closed 的判定流

```mermaid
flowchart TD
    A["读注册表<br/>registry.json"] --> B{"读得出?"}
    B -- "否" --> X1["✗ 整闸阻塞<br/>（否则会「一项没跑」却看着绿）"]
    B -- "是" --> C["逐项跑检查器"]

    C --> D{"退出码 = 0?"}
    D -- "是" --> P["✓ 通过"]
    D -- "否" --> E{"是否超时?"}
    E -- "是" --> X2["✗ 阻塞<br/>「判不了」≠「通过」"]
    E -- "否" --> F{"注册表声明了 level?"}
    F -- "否" --> X3["✗ 阻塞<br/>未声明 ⇒ 不静默降级"]
    F -- "是" --> G{"level"}
    G -- "fail" --> X4["✗ 阻塞"]
    G -- "warn" --> W["⚠ 警告<br/>显形但不阻塞"]

    classDef block fill:#fee2e2,stroke:#dc2626,stroke-width:2px;
    classDef pass fill:#dcfce7,stroke:#16a34a,stroke-width:2px;
    classDef warn fill:#fef9c3,stroke:#ca8a04,stroke-width:2px;
    class X1,X2,X3,X4 block;
    class P pass;
    class W warn;
```

**三条硬规则**：判不了 ⇒ 阻塞；未声明 ⇒ 阻塞；注册表读不出 ⇒ 阻塞。
一句话：**「没跑成」永远不许冒充「跑通了」。**

---

## 三、`reslock`：一把锁的状态迁移

```mermaid
stateDiagram-v2
    [*] --> 空闲
    空闲 --> 持锁: acquire()
    持锁 --> 空闲: release() / 进程退出(atexit)
    持锁 --> 心跳: 每 N 秒刷新 beat

    持锁 --> 接管: 等待方发现
    心跳 --> 接管: beat 过期(>stale)
    心跳 --> 接管: pid 不存在
    持锁 --> 接管: 超过 TTL

    接管 --> 持锁: 新持有方写入
    note right of 心跳
      有心跳 ⇒ 真在跑时 beat 一直新，
      等待方不会误抢
    end note
```

**为什么三个判据而不是一个**：

| 判据 | 挡住的坏情况 |
|---|---|
| pid 不存在 | 持有进程崩了 |
| `beat` 过期 | 进程还活着但**早已不干活**（会话复用 / 进程池） |
| 超 TTL | 兜底，防 pid 被无关进程复用 |

---

## 四、五层各自的「静默失败」与守卫

| 层 | 可能怎么静默失败 | 守卫 |
|---|---|---|
| 调度 | 任务没跑，却以为跑了 | 完成心跳 / 结果落库校验 |
| 编排 | **重跑**重复副作用；跳步时用了过时输入 | `checkpoint`（输入指纹） |
| 执行 | 抢了别人的资源 / 被抢 | `reslock` 分片锁 |
| 守卫 | 自检没跑却报通过；脱敏漏一处；笔记超限被截断；危险操作被**静默放行** | `checkgate` / `redact` / `contextbudget` / `approvalgate` |
| 数据 | 导出物里带真值 | `redact` 的 fail-closed 自检 |
