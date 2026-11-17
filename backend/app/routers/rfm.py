"""RFM分层接口：维护分层规则，覆盖提交规则评审、启用规则、停用规则等动作。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.rfm import RfmService
from app.store import store

router = APIRouter(prefix="/api/rfm", tags=["RFM分层"])

service = RfmService()

LIST_FIELDS = ["规则编号", "最近消费天界", "消费频次界", "消费金额界", "分层层级", "层间流转规则", "更新周期", "覆盖用户量"]
STATUSES = ["草稿", "规则评审中", "已启用", "已停用"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤RFM分层列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出RFM分层清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "rfm", "total": total, "items": items}


@router.get("/result-check/{entry_id}")
def result_check(entry_id: int, snapshot_id: str | None = None) -> dict[str, Any]:
    """RFM-0003 结果回查：沿规则 -> 快照 -> 名单批次 -> 明细数据集复核依赖断点。"""
    result = service.result_check(entry_id, snapshot_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"分层规则 {entry_id} 不存在或已归档")
    return result


@router.get("/users/{snapshot_id}")
def user_layers(snapshot_id: str) -> dict[str, Any]:
    """按数据快照读取用户分层记录，并给出按有效规则重算后的层级。"""
    result = service.user_layers(snapshot_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"数据快照 {snapshot_id} 不存在")
    return result


@router.post("/{entry_id}/review")
def complete_review(entry_id: int, payload: EntryPayload) -> ActionResult:
    """评审系统回写评审结论；结论为“通过”后规则才能进入启用动作。"""
    conclusion = str(payload.values.get("评审结论") or "").strip()
    entry, message = service.complete_review(entry_id, conclusion=conclusion, at=_now())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分层规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分层规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分层规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分层规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交规则评审/启用规则/停用规则。

    停用与启用会同步重算当前流失名单，规则状态与名单在同一事务里提交；
    跳级、版本过期或名单传播失败都会返回 ``ok=False`` 并说明原因。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message, receipt = service.run_action(
        entry_id,
        action,
        expected_version=payload.expected_version,
        receipt_key=payload.receipt_key,
        remark=payload.remark,
        at=_now(),
    )
    return ActionResult(ok=entry is not None, message=message, entry=entry, receipt=receipt)


@router.get("/{entry_id}/receipts")
def entry_receipts(entry_id: int) -> dict[str, Any]:
    """读取某条规则相关的全部回执，用于复核重复提交与拦截结论。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分层规则 {entry_id} 不存在或已归档")
    code = str(entry.get("规则编号"))
    receipts = [row for row in store.rows("receipts") if row.get("目标编号") == code]
    return {"规则编号": code, "total": len(receipts), "items": receipts}
