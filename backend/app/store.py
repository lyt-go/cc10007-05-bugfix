"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

这里额外提供两个基础设施能力：

* ``transaction()``：写操作的工作副本。事务内的读写都落在当前线程的副本上，
  提交成功才整体换到全局引用上；任何异常都会丢弃副本。跨模块的“规则状态 +
  模型状态 + 流失去向名单”因此可以一起成功或一起失败。
* 每个业务行都带 ``version`` 字段，动作与回执都按版本做乐观锁，并发提交时
  只有一个状态迁移能生效。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS
from app.seed_ops import SEED_EXTRA_ROWS, SEED_OVERRIDES

# 内部支撑表：不参与运营概览的模块统计，但可以通过各自的复核接口读取。
INTERNAL_TABLES = {
    "audit_log",
    "rfm_users",
    "rfm_snapshots",
    "rfm_rule_versions",
    "churn_batches",
    "churn_list_members",
    "churn_datasets",
    "receipts",
    "fault_boundaries",
}


class Store:
    def __init__(self) -> None:
        merged: dict[str, list[dict[str, Any]]] = {}
        for name, rows in SEED_ROWS.items():
            merged[name] = [dict(row) for row in rows]
        for code, values in SEED_OVERRIDES.items():
            for rows in merged.values():
                for row in rows:
                    if row.get("规则编号") == code or row.get("模型编号") == code:
                        row.update(values)
        for name, rows in SEED_EXTRA_ROWS.items():
            merged.setdefault(name, []).extend(copy.deepcopy(dict(row)) for row in rows)
        self._tables: dict[str, list[dict[str, Any]]] = merged
        self._lock = threading.RLock()
        self._local = threading.local()

    # ---------------------------------------------------------------- 事务
    def _visible(self) -> dict[str, list[dict[str, Any]]]:
        tables = getattr(self._local, "tables", None)
        return tables if tables is not None else self._tables

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """在当前线程开启一个写事务。

        采用写时复制：其他线程始终只能看到上一次提交完成的完整快照，不会读到
        半截状态。嵌套调用复用最外层事务，异常从任意一层抛出都会整体回滚。
        """
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            try:
                yield self
            finally:
                self._local.depth = depth
            return

        with self._lock:
            working = copy.deepcopy(self._tables)
            self._local.tables = working
            self._local.depth = 1
            try:
                yield self
            except BaseException:
                self._local.tables = None
                self._local.depth = 0
                raise
            else:
                self._tables = working
                self._local.tables = None
                self._local.depth = 0

    # ---------------------------------------------------------------- 读取
    def module_names(self) -> list[str]:
        return sorted(self._visible())

    def business_modules(self) -> list[str]:
        return [name for name in self.module_names() if name not in INTERNAL_TABLES]

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._visible().setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def table_snapshot(self, module: str) -> list[dict[str, Any]]:
        """只读副本，给复核、比对和审计留证据。"""
        return copy.deepcopy(self.rows(module))

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.business_modules():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
