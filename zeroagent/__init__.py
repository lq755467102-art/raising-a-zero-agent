"""zeroagent —— 让自动化在无人值守下也不「静默出错」的小工具。

自动化最危险的失败**不是崩溃**，而是「没报错，但结果是错的」：

- ``redact``        —— 脱敏只替换一次，漏了一处却看起来干净；本工具**替换完再扫一遍**，
  有任何残留就**拒绝产出**（fail-closed）。
- ``checkgate``     —— 自检脚本挂了，流程却打印「全部通过」；本工具把「判不了」
  （超时 / 未声明 / 登记表读不出）一律当**阻塞**处理，绝不冒充「通过」。
- ``reslock``       —— 任务早就干完了，锁还占着，后续任务被**无声堵住**；
  本工具用**按资源分片 + 持有方心跳**让锁既不过粗、也不「假生效」。
- ``contextbudget`` —— 笔记 / 记忆超限后被**按字符静默截断**，尾部的约束悄悄消失；
  本工具量水位并预警（ok / near / over），只读不写。
- ``approvalgate``  —— 危险操作没有审批就被执行；本工具保证**只有批准才放行**，
  且**申请人不能同时是审批人**。
- ``checkpoint``    —— 重跑要么重复副作用、要么跳过时用了过时输入；本工具按
  **「key + 输入指纹」**判断这一步做过没有，进度文件读不出就**全部重跑**。

纯标准库，无第三方依赖。Python 3.8+。
"""

from .redact import ContextRule, LeakError, Redactor
from .checkgate import GateResult, RegistryError, load_registry, run_gate
from .reslock import LockBusy, LockInfo, ResourceLock, pid_alive
from .contextbudget import BudgetReport, Item, Reading, check as check_budget
from .approvalgate import (APPROVED, PENDING, REJECTED, ApprovalError,
                           ApprovalStore, SelfApprovalError, UnknownRequest)
from .checkpoint import Progress, run_steps, signature
from .feedback import (AGREE, DISAGREE, UNCOMPARABLE, merge, read_events,
                       reconcile, record, register_expectation)

__all__ = [
    "Redactor", "ContextRule", "LeakError",
    "run_gate", "load_registry", "GateResult", "RegistryError",
    "ResourceLock", "LockInfo", "LockBusy", "pid_alive",
    "Item", "Reading", "BudgetReport", "check_budget",
    "ApprovalStore", "ApprovalError", "SelfApprovalError", "UnknownRequest",
    "PENDING", "APPROVED", "REJECTED",
    "Progress", "run_steps", "signature",
    "record", "read_events", "merge", "reconcile", "register_expectation",
    "AGREE", "DISAGREE", "UNCOMPARABLE",
]
__version__ = "0.5.0"
