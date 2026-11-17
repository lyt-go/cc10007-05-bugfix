"""名单生成与状态传播：规则/模型状态变化后，同一批次只保留一份当前名单。

关键约束（来自故障边界 FB-CHURN-LIST-01 / FB-RFM-CHURN-01）：

1. 用户分层记录必须按数据快照从 ``rfm_users`` 读取，不得沿用业务行上的缓存层级；
2. 当前名单只能由“有效模型 + 有效分层规则”共同生成，失效规则只保留历史名单；
3. 规则状态、模型状态与名单批次在同一事务里写入，一侧失败则整体回滚；
4. 停用规则回执与部署模型回执并发提交同一批次时，只有一次状态迁移生效，
   后到的回执拿到明确的“已被其他回执占用”结论，不会产生第二份当前名单。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.domain import (
    LAYER_UNKNOWN,
    build_member,
    classify,
    dataset_checksum,
    verification_code,
)
from app.store import store

MODULE = "churn"
RULE_MODULE = "rfm"
BATCH_MODULE = "churn_batches"
DATASET_MODULE = "churn_datasets"
MEMBER_MODULE = "churn_list_members"
USER_MODULE = "rfm_users"
SNAPSHOT_MODULE = "rfm_snapshots"
RECEIPT_MODULE = "receipts"
AUDIT_MODULE = "audit_log"
BOUNDARY_MODULE = "fault_boundaries"

RULE_ACTIVE_STATUS = "已启用"
MODEL_ACTIVE_STATUS = "已部署"
CURRENT_SNAPSHOT_STATUS = "当前"


class ListConflict(RuntimeError):
    """同一批次已被其他回执处理，当前提交必须整体放弃。"""


class BatchOwner:
    """同一名单批次同一时刻只允许一个回执写入。

    提交前先 ``claim`` 批次，并在锁定期间校验业务行版本：并发的两份回执即使
    先后拿到批次，后到的一方也会因为版本已变而整体放弃，因此只有一次状态迁移
    生效，不会出现两份当前名单互相覆盖。

    ``claim`` 支持可重入：同一回执在事务内再次申请同一批次直接放行。
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._owners: dict[str, tuple[str, int]] = {}
        self._winners: dict[str, str] = {}

    def token(self, receipt_key: str | None) -> str:
        return receipt_key or f"anonymous-{threading.get_ident()}"

    def reset(self, batch_id: str) -> None:
        """批次完成一次迁移后清空归属，允许下一条回执重新竞争。"""
        with self._guard:
            self._owners.pop(batch_id, None)
            self._winners.pop(batch_id, None)

    @contextmanager
    def claim(self, batch_id: str, receipt_key: str | None) -> Iterator[None]:
        """抢占批次写入权。

        同一批次的竞争只允许一个回执进入临界区；拿到写入权的回执在临界区内
        校验版本，版本已变即整体放弃。这样并发提交“停用规则 + 部署模型”时
        只有其中一次迁移生效，另一侧保持原状态且不产生第二份当前名单。
        """
        token = self.token(receipt_key)
        with self._guard:
            owner = self._owners.get(batch_id)
            if owner is not None and owner[0] == token:
                self._owners[batch_id] = (token, owner[1] + 1)
                reentrant = True
            elif owner is not None:
                raise ListConflict(f"名单批次{batch_id}正由回执{owner[0]}处理，本次提交未生效")
            else:
                self._owners[batch_id] = (token, 1)
                self._winners[batch_id] = token
                reentrant = False
        try:
            yield
        finally:
            with self._guard:
                current = self._owners.get(batch_id)
                if current is not None and current[0] == token:
                    if current[1] <= 1:
                        self._owners.pop(batch_id, None)
                    else:
                        self._owners[batch_id] = (token, current[1] - 1)


batch_owner = BatchOwner()


def current_snapshot() -> dict[str, Any] | None:
    for row in store.rows(SNAPSHOT_MODULE):
        if row.get("状态") == CURRENT_SNAPSHOT_STATUS:
            return row
    return None


def snapshot_users(snapshot_id: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(USER_MODULE) if row.get("数据快照") == snapshot_id]


def effective_rule() -> dict[str, Any] | None:
    """当前有效分层规则：已启用、生效标记为真、生效时间最新的一条。"""
    candidates = [
        row for row in store.rows(RULE_MODULE)
        if row.get("status") == RULE_ACTIVE_STATUS and row.get("生效标记")
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (str(row.get("生效时间") or ""), int(row.get("规则版本") or 0)),
    )


def effective_model() -> dict[str, Any] | None:
    """当前有效预警模型：已部署、生效标记为真、生效时间最新的一条。"""
    candidates = [
        row for row in store.rows(MODULE)
        if row.get("status") == MODEL_ACTIVE_STATUS and row.get("生效标记")
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (str(row.get("生效时间") or ""), int(row.get("模型版本") or 0)),
    )


def material_for(rule: dict[str, Any] | None, model: dict[str, Any] | None, snapshot_id: str) -> dict[str, Any]:
    rule_material = None
    if rule is not None:
        rule_material = {
            "规则编号": rule.get("规则编号"),
            "规则版本": rule.get("规则版本"),
            "状态": rule.get("status"),
            "最近消费天界": rule.get("最近消费天界"),
            "消费频次界": rule.get("消费频次界"),
            "消费金额界": rule.get("消费金额界"),
        }
    model_material = None
    if model is not None:
        model_material = {
            "模型编号": model.get("模型编号"),
            "模型版本": model.get("模型版本"),
            "状态": model.get("status"),
            "流失定义天": model.get("流失定义天"),
            "预测阈值": model.get("预测阈值"),
            "模型准确率": model.get("模型准确率"),
        }
    return {
        "规则材料": rule_material,
        "模型材料": model_material,
        "数据快照": snapshot_id,
        "故障边界": "FB-RFM-CHURN-01",
    }


def current_batch() -> dict[str, Any] | None:
    for row in store.rows(BATCH_MODULE):
        if row.get("状态") == "当前":
            return row
    return None


def batch_datasets(batch_id: str) -> list[dict[str, Any]]:
    return [row for row in store.rows(DATASET_MODULE) if row.get("批次编号") == batch_id]


def current_dataset(batch_id: str) -> dict[str, Any] | None:
    for row in batch_datasets(batch_id):
        if row.get("状态") == "当前":
            return row
    return None


def dataset_members(dataset_id: str) -> list[dict[str, Any]]:
    rows = [row for row in store.rows(MEMBER_MODULE) if row.get("数据集编号") == dataset_id]
    return sorted(rows, key=lambda row: str(row.get("用户编号") or ""))


def _next_dataset_version(batch_id: str) -> str:
    versions = [str(row.get("数据集版本") or "") for row in batch_datasets(batch_id)]
    numbers = [int(v[1:]) for v in versions if v.startswith("V") and v[1:].isdigit()]
    return f"V{max(numbers, default=0) + 1}"


def write_audit(action: str, target: str, result: str, note: str, code: str, at: str) -> None:
    store.rows(AUDIT_MODULE).append({
        "id": store.next_id(AUDIT_MODULE),
        "动作": action,
        "对象": target,
        "结果": result,
        "说明": note,
        "核验码": code,
        "发生时间": at,
    })


def build_dataset(
    batch: dict[str, Any],
    *,
    rule: dict[str, Any] | None,
    model: dict[str, Any] | None,
    snapshot: dict[str, Any],
    mode: str,
    note: str,
    eliminated: list[dict[str, Any]] | None = None,
    generated_at: str,
    trigger: str,
) -> dict[str, Any]:
    """在事务内生成当前数据集与成员明细，并把批次引用的旧数据集降级为历史。"""
    version = _next_dataset_version(str(batch["批次编号"]))
    batch_suffix = str(batch["批次编号"]).replace("CHLB-", "")
    dataset_id = f"CHDS-{batch_suffix}-{version}"
    users = snapshot_users(str(snapshot["快照编号"]))
    members: list[dict[str, Any]] = []
    for user in users:
        member = build_member(user, rule, model)
        if member is not None:
            members.append(member)
    source = material_for(rule, model, str(snapshot["快照编号"]))
    digest = dataset_checksum(dataset_id, source, [str(item["用户编号"]) for item in members])

    # 同一批次只保留一份当前数据集：旧数据集转为历史，成员一起转档。
    for row in batch_datasets(str(batch["批次编号"])):
        row["状态"] = "历史"
        row["复核结论"] = "已归档"
    for row in store.rows(MEMBER_MODULE):
        if row.get("批次编号") == batch["批次编号"]:
            row["当前"] = False

    dataset = {
        "id": store.next_id(DATASET_MODULE),
        "数据集编号": dataset_id,
        "批次编号": batch["批次编号"],
        "数据集版本": version,
        "状态": "当前",
        "数据快照": source["数据快照"],
        "规则编号": rule.get("规则编号") if rule else "",
        "规则版本": rule.get("规则版本") if rule else 0,
        "模型编号": model.get("模型编号") if model else "",
        "模型版本": model.get("模型版本") if model else 0,
        "模型准确率": model.get("模型准确率") if model else None,
        "名单数": len(members),
        "成员": [dict(item) for item in members],
        "校验摘要": digest,
        "生成时间": generated_at,
        "生成方式": mode,
        "修复说明": note,
        "前序数据集": batch.get("数据集编号"),
        "故障边界": source["故障边界"],
        "可审计": True,
        "来源材料": source,
        "消除错误": eliminated or [],
        "复核结论": "待复核",
        "触发动作": trigger,
    }
    store.rows(DATASET_MODULE).append(dataset)

    start_id = store.next_id(MEMBER_MODULE)
    for offset, member in enumerate(members):
        row = {
            "id": start_id + offset,
            "批次编号": batch["批次编号"],
            "数据集编号": dataset_id,
            "数据集版本": dataset["数据集版本"],
            "当前": True,
        }
        row.update(member)
        store.rows(MEMBER_MODULE).append(row)

    batch.update({
        "版本": int(batch.get("版本") or 0) + 1,
        "数据集版本": dataset["数据集版本"],
        "数据集编号": dataset_id,
        "数据快照": source["数据快照"],
        "规则编号": rule.get("规则编号") if rule else "",
        "规则版本": rule.get("规则版本") if rule else 0,
        "模型编号": model.get("模型编号") if model else "",
        "模型版本": model.get("模型版本") if model else 0,
        "模型准确率": model.get("模型准确率") if model else None,
        "名单数": len(members),
        "校验摘要": digest,
        "生成时间": generated_at,
        # 状态传播生成的是“按当前有效来源重算”的名单，和人工修复区分开，便于审计。
        "修复状态": "已修复" if mode == "修复重算" else "已重算",
    })
    return dataset


def propagate(
    *,
    trigger: str,
    at: str,
    expected_batch_version: int | None = None,
    expected_dataset_id: str | None = None,
) -> dict[str, Any]:
    """按当前有效规则与模型重算当前批次名单。

    调用方必须已经处在 ``store.transaction()`` 里；这里只做业务计算和写入。
    期望版本用于并发保护：两个回执同时提交同一批次时，后到的一方会得到冲突。
    """
    batch = current_batch()
    if batch is None:
        raise ListConflict("没有可更新的当前名单批次")
    if expected_batch_version is not None and int(batch.get("版本") or 0) != expected_batch_version:
        raise ListConflict("名单批次已被其他回执更新")
    if expected_dataset_id is not None and batch.get("数据集编号") != expected_dataset_id:
        raise ListConflict("名单批次的数据集已被其他回执替换")

    snapshot = current_snapshot()
    if snapshot is None:
        raise ListConflict("缺少当前数据快照，无法生成名单")
    rule = effective_rule()
    model = effective_model()
    dataset = build_dataset(
        batch,
        rule=rule,
        model=model,
        snapshot=snapshot,
        mode="状态传播",
        note=f"由{trigger}触发的状态传播重算",
        generated_at=at,
        trigger=trigger,
    )
    return dataset


def applied_receipt(receipt_key: str | None) -> dict[str, Any] | None:
    """幂等查询：只认已经真正生效的回执。

    被拦截的回执只代表一次失败的尝试，不应屏蔽后续重试——状态被合法前序动作
    推进后，同一批次再提交同一动作仍应被重新判断。
    """
    if not receipt_key:
        return None
    existing = store.find_by(RECEIPT_MODULE, "幂等键", receipt_key)
    if existing is None or existing.get("结果") != "已生效":
        return None
    return existing


def duplicate_receipt(existing: dict[str, Any]) -> dict[str, Any]:
    duplicate = dict(existing)
    duplicate["重复提交"] = True
    duplicate["说明"] = f"重复提交，沿用受理码{existing['受理码']}的结论：{existing['说明']}"
    duplicate["本次受理码"] = existing.get("受理码")
    return duplicate


def record_receipt(values: dict[str, Any]) -> dict[str, Any]:
    """登记回执与核验码；同一幂等键已生效时返回上一份回执，不重复迁移状态。"""
    existing = applied_receipt(values.get("幂等键"))
    if existing is not None:
        return duplicate_receipt(existing)
    receipt = {
        "id": store.next_id(RECEIPT_MODULE),
        "受理码": f"RCPT-{store.next_id(RECEIPT_MODULE):06d}",
        "通知类型": values.get("通知类型"),
        "目标编号": values.get("目标编号"),
        "动作": values.get("动作"),
        "幂等键": values.get("幂等键"),
        "结果": values.get("结果"),
        "说明": values.get("说明"),
        "批次编号": values.get("批次编号"),
        "数据集编号": values.get("数据集编号"),
        "提交时间": values.get("提交时间"),
        "重复提交": False,
    }
    receipt["核验码"] = verification_code(
        receipt["受理码"], receipt["动作"], receipt["结果"],
        values.get("数据集编号") or "", values.get("批次编号") or "",
    )
    store.rows(RECEIPT_MODULE).append(receipt)
    return receipt


def layer_view(rule: dict[str, Any] | None, snapshot_id: str) -> list[dict[str, Any]]:
    """按有效规则重算快照内每个用户的分层层级，供 RFM 结果回查。"""
    rows: list[dict[str, Any]] = []
    for user in snapshot_users(snapshot_id):
        layer = classify(user, rule)
        rows.append({
            "用户编号": user.get("用户编号"),
            "数据快照": user.get("数据快照"),
            "数据版本": user.get("数据版本"),
            "最近消费天": user.get("最近消费天"),
            "消费频次": user.get("消费频次"),
            "消费金额": user.get("消费金额"),
            "分层层级": layer,
            "缓存分层层级": user.get("缓存分层层级"),
            "缓存规则版本": user.get("缓存规则版本"),
            "与缓存一致": layer == user.get("缓存分层层级"),
            "规则编号": rule.get("规则编号") if rule else "",
            "规则版本": rule.get("规则版本") if rule else 0,
            "未分层": layer == LAYER_UNKNOWN,
        })
    return rows
