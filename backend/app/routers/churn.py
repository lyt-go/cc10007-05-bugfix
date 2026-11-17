"""流失预警接口：维护预警模型，并承接修复后的流失去向名单明细数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services import churn_repair, churn_list
from app.services.churn import ChurnService
from app.store import store

router = APIRouter(prefix="/api/churn", tags=["流失预警"])

service = ChurnService()

LIST_FIELDS = ["模型编号", "模型名称", "特征变量", "流失定义天", "训练数据集", "预测阈值", "模型准确率", "预警推送频率"]
STATUSES = ["草稿", "训练中", "已部署", "已下线"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _current_batch_view() -> dict[str, Any]:
    batch = churn_list.current_batch()
    if batch is None:
        return {"批次": None, "数据集": None, "成员": []}
    dataset = churn_list.current_dataset(str(batch["批次编号"]))
    members = churn_list.dataset_members(str(dataset["数据集编号"])) if dataset else []
    return {"批次": batch, "数据集": dataset, "成员": members}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模型编号检索"),
    status: str | None = Query(default=None, description="草稿、训练中、已部署、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模型编号与状态过滤流失预警列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出流失预警清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "churn", "total": total, "items": items}


# ---------------------------------------------------------------- 名单与数据集
@router.get("/current-list")
def current_list() -> dict[str, Any]:
    """当前流失去向名单：只返回当前批次的一份完整当前名单。"""
    view = _current_batch_view()
    batch = view["批次"]
    dataset = view["数据集"]
    if batch is None:
        return {"批次": None, "数据集": None, "名单数": 0, "成员": []}
    return {
        "批次": batch,
        "数据集": dataset,
        "名单数": len(view["成员"]),
        "成员": view["成员"],
    }


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """名单批次列表：当前批次与历史批次分开标注，历史名单保持原预警时点。"""
    rows = []
    for batch in store.rows(churn_list.BATCH_MODULE):
        dataset = churn_list.current_dataset(str(batch["批次编号"])) or store.find_by(
            churn_list.DATASET_MODULE, "数据集编号", batch.get("数据集编号")
        )
        row = dict(batch)
        row["数据集可审计"] = bool(dataset.get("可审计")) if dataset else False
        row["数据集状态"] = dataset.get("状态") if dataset else None
        rows.append(row)
    return {"items": rows, "total": len(rows)}


@router.get("/datasets")
def list_datasets() -> dict[str, Any]:
    """明细数据集清单：每个版本的材料、摘要与修复说明都保留，便于按快照回查。"""
    rows = []
    for dataset in store.rows(churn_list.DATASET_MODULE):
        row = {key: value for key, value in dataset.items() if key != "成员"}
        row["成员编号"] = [str(item.get("用户编号")) for item in dataset.get("成员") or []]
        rows.append(row)
    return {"items": rows, "total": len(rows)}


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str) -> dict[str, Any]:
    """读取明细数据集（含成员）并按来源版本材料复核。"""
    dataset = store.find_by(churn_list.DATASET_MODULE, "数据集编号", dataset_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail=f"明细数据集 {dataset_id} 不存在")
    member_rows = churn_list.dataset_members(dataset_id)
    if not member_rows:
        member_rows = [dict(item) for item in dataset.get("成员") or []]
    return {
        **{key: value for key, value in dataset.items() if key != "成员"},
        "成员": member_rows,
        "复核": churn_repair.verify(dataset_id),
    }


@router.post("/repair")
def repair_list(payload: EntryPayload | None = None) -> dict[str, Any]:
    """按故障边界重算当前名单，生成修复后的流失去向名单明细数据集。

    幂等：同一 ``receipt_key`` 重复提交返回原回执与数据集，不重复生成新版本。
    """
    payload = payload or EntryPayload()
    result = churn_repair.repair(
        at=_now(),
        receipt_key=payload.receipt_key,
        operator=str(payload.values.get("operator") or "值班管理员"),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message"))
    return result


@router.get("/reconcile")
def reconcile() -> dict[str, Any]:
    """诊断当前名单：按故障边界读取用户分层记录，列出依赖断点与两侧状态。"""
    return churn_repair.diagnose()


@router.get("/receipts")
def list_receipts() -> dict[str, Any]:
    """回执台账：同一批次的停用/部署回执只允许一次状态迁移生效。"""
    rows = store.rows(churn_list.RECEIPT_MODULE)
    return {"items": rows, "total": len(rows)}


@router.get("/audit")
def list_audit() -> dict[str, Any]:
    """审计日志：记录每次迁移、拦截、修复与重复提交的核验码。"""
    rows = store.rows(churn_list.AUDIT_MODULE)
    return {"items": rows, "total": len(rows)}


@router.post("/{entry_id}/training")
def complete_training(entry_id: int, payload: EntryPayload) -> ActionResult:
    """训练系统回写训练结论与准确率；训练完成后模型才能进入部署动作。"""
    try:
        accuracy = float(payload.values.get("模型准确率"))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="缺少可用的模型准确率")
    entry, message = service.complete_training(entry_id, accuracy=accuracy, at=_now())
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预警模型明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预警模型 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预警模型，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预警模型已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行发起训练/部署模型/下线模型。

    部署与下线会同步重算当前流失名单，模型状态与名单在同一事务里提交；
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
    """读取某个模型相关的全部回执。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预警模型 {entry_id} 不存在或已归档")
    code = str(entry.get("模型编号"))
    receipts = [row for row in store.rows("receipts") if row.get("目标编号") == code]
    return {"模型编号": code, "total": len(receipts), "items": receipts}
