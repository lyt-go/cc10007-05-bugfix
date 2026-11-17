"""流失名单诊断与修复：按故障边界读取用户分层记录，重算当前名单并留存审计证据。

诊断会沿“规则状态/模型状态 -> 名单批次 -> 明细数据集 -> 成员明细”的调用链
逐环节核对，定位依赖断点；修复只在诊断确认存在断点时执行，并且整条链路
（批次 + 数据集 + 成员 + 回执 + 审计）在一个事务里提交。
"""
from __future__ import annotations

from typing import Any

from app.domain import build_member, compute_members, dataset_checksum, verification_code
from app.services import churn_list
from app.services.churn_list import ListConflict
from app.store import store

FAULT_BOUNDARY = "FB-RFM-CHURN-01"


def _boundary() -> dict[str, Any] | None:
    return store.find_by(churn_list.BOUNDARY_MODULE, "边界编号", FAULT_BOUNDARY)


def diagnose() -> dict[str, Any]:
    """沿调用链逐环节核对，返回依赖断点、两侧状态与当前名单。

    读取用户分层记录一律按数据快照从 ``rfm_users`` 读取（故障边界
    FB-RFM-CHURN-01），不会复用业务行上的缓存层级。
    """
    batch = churn_list.current_batch()
    snapshot = churn_list.current_snapshot()
    checks: list[dict[str, Any]] = []
    breaks: list[dict[str, Any]] = []

    if snapshot is None:
        breaks.append({"环节": "数据快照", "原因": "缺少当前数据快照", "故障边界": FAULT_BOUNDARY})
        return {"当前批次": None, "链路": checks, "依赖断点": breaks}

    users = churn_list.snapshot_users(str(snapshot["快照编号"]))
    checks.append({
        "环节": "用户分层记录",
        "读取方式": f"按数据快照{snapshot['快照编号']}从{churn_list.USER_MODULE}读取",
        "行数": len(users),
        "故障边界": FAULT_BOUNDARY,
        "结论": "已按故障边界读取" if users else "快照内没有用户分层记录",
    })
    if not users:
        breaks.append({"环节": "用户分层记录", "原因": "快照内没有用户分层记录", "故障边界": FAULT_BOUNDARY})

    rule_checks = [row for row in store.rows(churn_list.RULE_MODULE) if row.get("规则编号") == "RFM-0003"]
    if rule_checks:
        rule = rule_checks[0]
        checks.append({
            "环节": "RFM分层规则",
            "对象": f"{rule.get('规则编号')} 第{rule.get('规则版本')}版",
            "状态": rule.get("status"),
            "生效标记": rule.get("生效标记"),
            "消费频次界": rule.get("消费频次界"),
            "结论": "有效分层规则" if rule.get("status") == churn_list.RULE_ACTIVE_STATUS and rule.get("生效标记")
            else "不是当前有效规则",
        })

    model_checks = [row for row in store.rows(churn_list.MODULE) if row.get("模型编号") == "CHUR-0003"]
    if model_checks:
        model = model_checks[0]
        checks.append({
            "环节": "流失预警模型",
            "对象": f"{model.get('模型编号')} 第{model.get('模型版本')}版",
            "状态": model.get("status"),
            "生效标记": model.get("生效标记"),
            "模型准确率": model.get("模型准确率"),
            "结论": "有效模型" if model.get("status") == churn_list.MODEL_ACTIVE_STATUS and model.get("生效标记")
            else "不是当前有效模型",
        })

    if batch is None:
        breaks.append({"环节": "名单批次", "原因": "缺少当前名单批次", "故障边界": "FB-CHURN-LIST-01"})
        return {"当前批次": None, "链路": checks, "依赖断点": breaks}

    dataset = churn_list.current_dataset(str(batch["批次编号"]))
    checks.append({
        "环节": "当前明细数据集",
        "对象": dataset.get("数据集编号") if dataset else None,
        "状态": dataset.get("状态") if dataset else None,
        "规则引用": f"{dataset.get('规则编号')}/第{dataset.get('规则版本')}版" if dataset else None,
        "模型引用": f"{dataset.get('模型编号')}/第{dataset.get('模型版本')}版" if dataset else None,
        "模型准确率": dataset.get("模型准确率") if dataset else None,
        "名单数": dataset.get("名单数") if dataset else 0,
        "可审计": dataset.get("可审计") if dataset else False,
        "结论": "可审计的当前数据集" if dataset and dataset.get("可审计") else "数据集缺失或不可审计",
    })

    current_datasets = [row for row in churn_list.batch_datasets(str(batch["批次编号"])) if row.get("状态") == "当前"]
    if len(current_datasets) != 1:
        breaks.append({
            "环节": "名单批次",
            "原因": f"同一批次存在{len(current_datasets)}份当前数据集，应只有一份",
            "故障边界": "FB-CHURN-LIST-01",
        })
    if dataset is None:
        breaks.append({"环节": "明细数据集", "原因": "当前批次没有当前明细数据集", "故障边界": "FB-CHURN-LIST-01"})
        return {"当前批次": batch, "链路": checks, "依赖断点": breaks, "用户分层记录": len(users)}

    members = churn_list.dataset_members(str(dataset["数据集编号"]))
    source = dataset.get("来源材料") or {}
    rule_material = source.get("规则材料") or {}
    model_material = source.get("模型材料") or {}

    if rule_material.get("状态") != churn_list.RULE_ACTIVE_STATUS:
        breaks.append({
            "环节": "规则引用传播",
            "原因": f"数据集引用规则{rule_material.get('规则编号')}第{rule_material.get('规则版本')}版，"
                    f"状态为「{rule_material.get('状态')}」，已不能驱动当前名单",
            "故障边界": "FB-RFM-CHURN-01",
        })
    if model_material.get("状态") != churn_list.MODEL_ACTIVE_STATUS:
        breaks.append({
            "环节": "模型引用传播",
            "原因": f"数据集引用模型{model_material.get('模型编号')}第{model_material.get('模型版本')}版，"
                    f"状态为「{model_material.get('状态')}」，未部署模型不应提供准确率",
            "故障边界": FAULT_BOUNDARY,
        })

    effective_rule = churn_list.effective_rule()
    effective_model = churn_list.effective_model()
    if effective_rule is None:
        breaks.append({"环节": "有效规则", "原因": "没有已启用的分层规则", "故障边界": FAULT_BOUNDARY})
    if effective_model is None:
        breaks.append({"环节": "有效模型", "原因": "没有已部署的预警模型", "故障边界": FAULT_BOUNDARY})

    expected_ids = [
        str(item["用户编号"]) for item in
        churn_list.layer_view(effective_rule, str(snapshot["快照编号"]))
        if _is_member(item, effective_rule, effective_model)
    ]
    actual_ids = [str(item["用户编号"]) for item in members]
    if sorted(expected_ids) != sorted(actual_ids):
        breaks.append({
            "环节": "成员范围",
            "原因": f"当前名单实际{len(actual_ids)}人，按有效规则与有效模型应为{len(expected_ids)}人",
            "实际": actual_ids,
            "应为": expected_ids,
            "故障边界": FAULT_BOUNDARY,
        })

    if dataset.get("模型准确率") != (effective_model.get("模型准确率") if effective_model else None):
        breaks.append({
            "环节": "模型准确率",
            "原因": f"数据集展示准确率{dataset.get('模型准确率')}，有效模型准确率为"
                    f"{effective_model.get('模型准确率') if effective_model else '无'}",
            "故障边界": "FB-CHURN-MODEL-01",
        })

    for item in members:
        if item.get("规则编号") and effective_rule and item.get("规则编号") != effective_rule.get("规则编号"):
            breaks.append({
                "环节": "成员口径",
                "原因": f"{item.get('用户编号')}的名单成员仍引用规则{item.get('规则编号')}"
                        f"第{item.get('规则版本')}版（消费频次界{item.get('消费频次界快照')}）",
                "故障边界": "FB-RFM-CHURN-01",
            })
            break

    digest = dataset_checksum(
        str(dataset["数据集编号"]), source, [str(item["用户编号"]) for item in members]
    )
    if digest != dataset.get("校验摘要"):
        breaks.append({
            "环节": "校验摘要",
            "原因": "数据集校验摘要与成员明细不一致，数据集已不可信",
            "故障边界": "FB-CHURN-HISTORY-01",
        })

    return {
        "当前批次": batch,
        "当前数据集": dataset,
        "数据快照": snapshot.get("快照编号"),
        "用户分层记录": len(users),
        "有效规则": effective_rule.get("规则编号") if effective_rule else None,
        "有效模型": effective_model.get("模型编号") if effective_model else None,
        "链路": checks,
        "依赖断点": breaks,
        "需要修复": bool(breaks),
        "历史名单": [
            {"批次编号": row.get("批次编号"), "数据集编号": row.get("数据集编号"),
             "状态": row.get("状态"), "名单数": row.get("名单数"), "预警时点": row.get("预警时点")}
            for row in store.rows(churn_list.BATCH_MODULE) if row.get("状态") == "历史"
        ],
        "故障边界": _boundary(),
    }


def _is_member(user: dict[str, Any], rule: dict[str, Any] | None, model: dict[str, Any] | None) -> bool:
    return build_member(user, rule, model) is not None


def repair(*, at: str, receipt_key: str | None = None, operator: str = "值班管理员") -> dict[str, Any]:
    """生成可审计的修复后流失去向名单明细数据集。

    修复前先复用同一份来源版本材料重算一版，与现有数据集逐项比对；修复后
    再按同一材料复核一次，确保“消除错误后的明细数据集”可被独立复盘。
    """
    existing = churn_list.applied_receipt(receipt_key)
    if existing is not None:
        duplicate = churn_list.duplicate_receipt(existing)
        return {"ok": True, "repaired": False, "receipt": duplicate,
                "数据集": store.find_by(churn_list.DATASET_MODULE, "数据集编号", duplicate.get("数据集编号"))}

    report = diagnose()
    batch = report.get("当前批次")
    snapshot = churn_list.current_snapshot()
    if batch is None or snapshot is None:
        return {"ok": False, "repaired": False, "message": "缺少当前名单批次或数据快照，无法修复",
                "诊断": report}

    rule = churn_list.effective_rule()
    model = churn_list.effective_model()
    if rule is None or model is None:
        return {"ok": False, "repaired": False,
                "message": "缺少有效分层规则或有效模型，修复不能凭空生成名单", "诊断": report}

    if not report["需要修复"]:
        current = churn_list.current_dataset(str(batch["批次编号"])) or {}
        return {"ok": True, "repaired": False, "message": "当前名单已对齐，无需重复修复",
                "数据集": current, "复核": verify(str(current.get("数据集编号"))),
                "诊断": report}

    previous_dataset = report.get("当前数据集") or {}
    previous_members = {str(item["用户编号"]): item for item in churn_list.dataset_members(str(previous_dataset.get("数据集编号")))}
    users = churn_list.snapshot_users(str(snapshot["快照编号"]))
    expected = [member for member in (build_member(user, rule, model) for user in users) if member is not None]
    expected_ids = {str(item["用户编号"]) for item in expected}

    eliminated = [
        {"类型": "规则引用失效",
         "修复前": f"{previous_dataset.get('规则编号')}/第{previous_dataset.get('规则版本')}版/"
                   f"消费频次界{(previous_dataset.get('来源材料') or {}).get('规则材料', {}).get('消费频次界')}",
         "修复后": f"{rule.get('规则编号')}/第{rule.get('规则版本')}版/消费频次界{rule.get('消费频次界')}"},
        {"类型": "模型引用失效",
         "修复前": f"{previous_dataset.get('模型编号')}/第{previous_dataset.get('模型版本')}版/"
                   f"{(previous_dataset.get('来源材料') or {}).get('模型材料', {}).get('状态')}",
         "修复后": f"{model.get('模型编号')}/第{model.get('模型版本')}版/{model.get('status')}"},
        {"类型": "准确率失真",
         "修复前": previous_dataset.get("模型准确率"),
         "修复后": model.get("模型准确率")},
        {"类型": "成员范围收缩",
         "修复前": len(previous_members),
         "修复后": len(expected_ids),
         "移出成员": sorted(set(previous_members) - expected_ids),
         "新增成员": sorted(expected_ids - set(previous_members))},
    ]
    # 只保留真实发生变化的项，避免审计材料里出现空修复。
    eliminated = [item for item in eliminated if item["修复前"] != item["修复后"]]

    try:
        with churn_list.batch_owner.claim(str(batch["批次编号"]), receipt_key), store.transaction():
            dataset = churn_list.build_dataset(
                store.find(churn_list.BATCH_MODULE, int(batch["id"])),
                rule=rule,
                model=model,
                snapshot=snapshot,
                mode="修复重算",
                note=f"按故障边界{FAULT_BOUNDARY}从快照{snapshot['快照编号']}读取用户分层记录，"
                     f"改用有效规则{rule.get('规则编号')}第{rule.get('规则版本')}版与"
                     f"有效模型{model.get('模型编号')}第{model.get('模型版本')}版重算",
                eliminated=eliminated,
                generated_at=at,
                trigger="修复流失名单",
            )
            dataset["修复前数据集"] = previous_dataset.get("数据集编号")
            dataset["操作人"] = operator
            receipt = churn_list.record_receipt({
                "通知类型": "名单修复回执",
                "目标编号": str(batch["批次编号"]),
                "动作": "修复流失名单",
                "幂等键": receipt_key,
                "结果": "已修复",
                "说明": f"生成可审计的流失去向名单明细数据集{dataset['数据集编号']}，"
                        f"名单{dataset['名单数']}人，模型准确率{dataset['模型准确率']}",
                "批次编号": batch["批次编号"],
                "数据集编号": dataset["数据集编号"],
                "提交时间": at,
            })
            churn_list.write_audit(
                "修复流失名单", str(batch["批次编号"]), "已修复",
                f"数据集{previous_dataset.get('数据集编号')} -> {dataset['数据集编号']}，"
                f"消除{len(eliminated)}类错位",
                receipt["核验码"], at,
            )
    except ListConflict as error:
        return {"ok": False, "repaired": False, "message": f"修复未生效：{error}", "诊断": report}

    verification = verify(str(dataset["数据集编号"]))
    return {
        "ok": True,
        "repaired": True,
        "message": "已生成修复后的流失去向名单明细数据集",
        "数据集": dataset,
        "消除错误": eliminated,
        "回执": receipt,
        "复核": verification,
    }


def verify(dataset_id: str) -> dict[str, Any]:
    """按来源版本材料复核数据集：重算成员、比对摘要、核对两侧状态。"""
    dataset = store.find_by(churn_list.DATASET_MODULE, "数据集编号", dataset_id)
    if dataset is None:
        return {"found": False, "一致": False, "原因": "数据集不存在"}
    source = dataset.get("来源材料") or {}
    rule_material = source.get("规则材料")
    model_material = source.get("模型材料")
    if not model_material:
        return {"found": True, "一致": False, "原因": "来源版本材料缺少模型材料"}

    users = churn_list.snapshot_users(str(source.get("数据快照")))
    rule_for_check = None
    if rule_material:
        rule_for_check = {
            "规则编号": rule_material["规则编号"],
            "规则版本": rule_material["规则版本"],
            "最近消费天界": rule_material["最近消费天界"],
            "消费频次界": rule_material["消费频次界"],
            "消费金额界": rule_material["消费金额界"],
        }
    recomputed = compute_members(
        users,
        rule_for_check,
        {
            "模型编号": model_material["模型编号"],
            "模型版本": model_material["模型版本"],
            "流失定义天": model_material["流失定义天"],
            "预测阈值": model_material["预测阈值"],
            "模型准确率": model_material["模型准确率"],
        },
    )
    members = churn_list.dataset_members(dataset_id)
    expected_ids = [str(item["用户编号"]) for item in recomputed]
    actual_ids = [str(item["用户编号"]) for item in members]
    digest = dataset_checksum(dataset_id, source, actual_ids)
    recomputed_digest = dataset_checksum(dataset_id, source, expected_ids)

    issues: list[str] = []
    if expected_ids != actual_ids:
        issues.append(f"成员明细与来源材料重算不一致：应为{expected_ids}，实际{actual_ids}")
    if digest != dataset.get("校验摘要"):
        issues.append("存储校验摘要与成员明细不一致")
    if recomputed_digest != dataset.get("校验摘要"):
        issues.append("按来源版本材料重算的摘要与存储摘要不一致")
    if rule_material and rule_material.get("状态") != churn_list.RULE_ACTIVE_STATUS:
        issues.append(f"来源规则状态为{rule_material.get('状态')}，不是有效规则")
    if model_material.get("状态") != churn_list.MODEL_ACTIVE_STATUS:
        issues.append(f"来源模型状态为{model_material.get('状态')}，不是有效模型")
    if dataset.get("名单数") != len(actual_ids):
        issues.append("名单数与成员明细条数不一致")

    return {
        "found": True,
        "数据集编号": dataset_id,
        "数据快照": source.get("数据快照"),
        "来源版本材料": source,
        "重算成员": expected_ids,
        "实际成员": actual_ids,
        "校验摘要": digest,
        "重算摘要": recomputed_digest,
        "一致": not issues,
        "问题": issues,
        "复核码": verification_code(dataset_id, digest, len(actual_ids)),
    }
