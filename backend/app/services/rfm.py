"""RFM分层业务规则：状态流转、字段校验、名单传播与筛选口径都收在这里。

状态机是严格的：``草稿 -> 规则评审中 -> 已启用 -> 已停用``，相邻状态之间只能前进
一步；``启用规则`` 还要求评审结论为“通过”，``停用规则`` 只对已启用规则开放。
任何跳级或回退都会被拒绝，并且不会改动业务行上的任何字段。
"""
from __future__ import annotations

from typing import Any

from datetime import datetime

from app.domain import layer_distribution, verification_code
from app.services import churn_list
from app.services.churn_list import ListConflict
from app.store import store

MODULE = "rfm"
REQUIRED_FIELDS = ["规则编号", "最近消费天界", "消费频次界"]
STATUS_ORDER = ["草稿", "规则评审中", "已启用", "已停用"]
ACTION_RULES = {"提交规则评审": "规则评审中", "启用规则": "已启用", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]
APPROVED = "通过"


def _transitions() -> dict[str, str]:
    """合法前序状态 -> 目标状态；只允许向下一步，不允许跳级。"""
    return {
        "提交规则评审": "草稿",
        "启用规则": "规则评审中",
        "停用规则": "已启用",
    }


class RfmService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        entry = {
            "id": store.next_id(MODULE),
            "规则编号": values.get("规则编号"),
            "最近消费天界": values.get("最近消费天界"),
            "消费频次界": values.get("消费频次界"),
            "消费金额界": values.get("消费金额界", 0),
            "分层层级": values.get("分层层级", "高价值/中价值/低价值"),
            "层间流转规则": values.get("层间流转规则", ""),
            "更新周期": values.get("更新周期", "及时"),
            "覆盖用户量": values.get("覆盖用户量", 0),
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "规则版本": int(values.get("规则版本") or 1),
            "version": 1,
            "评审结论": "",
            "生效标记": False,
        }
        store.rows(MODULE).append(entry)
        return entry, []

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
        receipt_key: str | None = None,
        remark: str | None = None,
        at: str = "",
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        """执行一次状态迁移，返回 ``(entry, message, receipt)``。

        规则状态、名单批次与明细数据集在同一事务内完成；只要名单传播失败，
        规则状态也不会落库。并发提交时按 ``version`` 做乐观锁，只有一方生效。
        """
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于RFM分层可执行范围", None

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分层规则 {entry_id} 不存在或已归档", None

        existing = churn_list.applied_receipt(receipt_key)
        if existing is not None:
            duplicate = churn_list.duplicate_receipt(existing)
            return store.find(MODULE, entry_id), duplicate["说明"], duplicate

        current = str(entry.get("status") or "")
        seen_version = int(entry.get("version") or 0)
        required = _transitions()[action]
        if current != required:
            reason = (
                f"{entry.get('规则编号')}当前处于「{current}」，{action}只允许从「{required}」进入"
                "，不允许跳级或回退"
            )
            receipt = self._receipt(
                "规则动作回执", str(entry.get("规则编号")), action, "已拦截", reason,
                receipt_key, at, batch=None,
            )
            self._audit("拦截规则跳级", str(entry.get("规则编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        if action == "启用规则" and entry.get("评审结论") != APPROVED:
            reason = f"{entry.get('规则编号')}的评审结论仍为「{entry.get('评审结论') or '未提交'}」，不能启用"
            receipt = self._receipt(
                "规则动作回执", str(entry.get("规则编号")), action, "已拦截", reason,
                receipt_key, at, batch=None,
            )
            self._audit("拦截规则跳级", str(entry.get("规则编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        if expected_version is not None and seen_version != expected_version:
            reason = (
                f"{entry.get('规则编号')}已被其他回执更新（期望第{expected_version}版，"
                f"当前第{seen_version}版）"
            )
            receipt = self._receipt(
                "规则动作回执", str(entry.get("规则编号")), action, "已拦截", reason,
                receipt_key, at, batch=None,
            )
            self._audit("拦截并发迁移", str(entry.get("规则编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        target = ACTION_RULES[action]
        batch = churn_list.current_batch()
        try:
            with churn_list.batch_owner.claim(
                str(batch.get("批次编号")) if batch else "", receipt_key
            ), store.transaction():
                row = store.find(MODULE, entry_id)
                if row is None:
                    raise ListConflict(f"分层规则 {entry_id} 不存在或已归档")
                # 与读取时看到的版本比对：等待批次期间若被其他回执推进，本次提交放弃。
                if int(row.get("version") or 0) != seen_version:
                    raise ListConflict("规则版本已被其他回执更新")
                if str(row.get("status") or "") != required:
                    raise ListConflict("规则状态已被其他回执更新")

                row["status"] = target
                row["pending"] = target not in {"已启用", "已停用"}
                row["abnormal"] = action in NEGATIVE_ACTIONS
                row["version"] = int(row.get("version") or 0) + 1
                row["生效标记"] = target == "已启用"
                if target == "已启用":
                    row["生效时间"] = at
                if target == "已停用":
                    # 生效标记必须与 status 一起撤销，否则停用规则仍会被当成有效来源。
                    row["生效标记"] = False
                if target == "已停用":
                    row["停用时间"] = at
                    row["停用原因"] = remark or "人工停用"
                if action == "提交规则评审":
                    row["评审发起时间"] = at
                    row["评审结论"] = ""
                if action == "启用规则" and entry.get("评审完成时间") is None:
                    # 评审通过时间必须落在启用之前，有效来源按生效时间排序时才不会被误判。
                    row["评审完成时间"] = at
                if action == "启用规则":
                    row["评审结论"] = APPROVED
                    row["评审完成时间"] = row.get("评审完成时间") or at

                dataset = None
                if target in {"已启用", "已停用"}:
                    # 停用/启用会改变“有效分层”，必须与名单一起成功。
                    dataset = churn_list.propagate(
                        trigger=action,
                        at=at,
                        expected_batch_version=int(batch.get("版本") or 0) if batch else None,
                        expected_dataset_id=str(batch.get("数据集编号") or "") if batch else None,
                    )

                receipt = self._receipt(
                    "停用规则回执" if target == "已停用" else "规则动作回执",
                    str(row.get("规则编号")), action, "已生效",
                    f"{row.get('规则编号')}已进入「{target}」，名单批次同步为"
                    f"{dataset.get('数据集编号') if dataset else '未变更'}",
                    receipt_key, at, batch=batch, dataset=dataset,
                )
                self._audit(
                    "规则状态迁移", str(row.get("规则编号")), "已生效",
                    f"{current} -> {target}，规则第{row['version']}版", receipt["核验码"], at,
                )
                result = dict(row)
        except ListConflict as error:
            reason = f"{entry.get('规则编号')}的{action}未生效：{error}"
            receipt = self._receipt(
                "规则动作回执", str(entry.get("规则编号")), action, "已拦截", reason,
                receipt_key, at, batch=batch,
            )
            self._audit("拦截并发迁移", str(entry.get("规则编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        return result, f"分层规则已{action}", receipt

    # ------------------------------------------------------------------ 内部
    def _receipt(
        self,
        kind: str,
        target: str,
        action: str,
        result: str,
        note: str,
        key: str | None,
        at: str,
        *,
        batch: dict[str, Any] | None,
        dataset: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = {
            "通知类型": kind,
            "目标编号": target,
            "动作": action,
            "幂等键": key,
            "结果": result,
            "说明": note,
            "批次编号": batch.get("批次编号") if batch else None,
            "数据集编号": dataset.get("数据集编号") if dataset else (
                batch.get("数据集编号") if batch else None
            ),
            "提交时间": at,
        }
        return churn_list.record_receipt(values)

    def _audit(self, action: str, target: str, result: str, note: str, code: str, at: str) -> None:
        store.rows(churn_list.AUDIT_MODULE).append({
            "id": store.next_id(churn_list.AUDIT_MODULE),
            "动作": action,
            "对象": target,
            "结果": result,
            "说明": note,
            "核验码": code,
            "发生时间": at,
        })

    # ------------------------------------------------------------------ 回查
    def complete_review(self, entry_id: int, *, conclusion: str, at: str = "") -> tuple[dict[str, Any] | None, str]:
        """评审系统回写评审结论；只有“规则评审中”的规则可以接收结论。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分层规则 {entry_id} 不存在或已归档"
        if entry.get("status") != "规则评审中":
            return None, f"{entry.get('规则编号')}当前处于「{entry.get('status')}」，不能回写评审结论"
        if conclusion not in {"通过", "驳回"}:
            return None, "评审结论只能是「通过」或「驳回」"
        with store.transaction():
            row = store.find(MODULE, entry_id)
            if row is None or row.get("status") != "规则评审中":
                return None, "规则状态已被其他回执更新"
            row["评审结论"] = conclusion
            row["评审完成时间"] = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["version"] = int(row.get("version") or 0) + 1
            if conclusion == "驳回":
                row["status"] = "草稿"
                row["pending"] = True
                row["abnormal"] = False
                row["生效标记"] = False
            self._audit(
                "回写评审结论", str(row.get("规则编号")), "已生效",
                f"评审结论{conclusion}，规则第{row['version']}版",
                verification_code(row.get("规则编号"), conclusion, row["version"]), at,
            )
            return dict(row), f"评审结论已回写为「{conclusion}」"

    def result_check(self, rule_id: int, snapshot_id: str | None = None) -> dict[str, Any] | None:
        """RFM-0003 结果回查：沿原调用链复核分层结论与名单引用是否对齐。"""
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None
        snapshot = churn_list.current_snapshot()
        if snapshot_id:
            for row in store.rows(churn_list.SNAPSHOT_MODULE):
                if row.get("快照编号") == snapshot_id:
                    snapshot = row
                    break
        if snapshot is None:
            return {"规则": rule, "断点": ["缺少数据快照"], "分层明细": [], "链路": []}

        rows = churn_list.layer_view(rule, str(snapshot["快照编号"]))
        current = churn_list.current_batch()
        dataset = churn_list.current_dataset(str(current["批次编号"])) if current else None
        members = churn_list.dataset_members(str(dataset["数据集编号"])) if dataset else []
        member_map = {str(item.get("用户编号")): item for item in members}

        chain = [
            {"环节": "分层规则", "对象": f"{rule.get('规则编号')} 第{rule.get('规则版本')}版",
             "状态": rule.get("status"), "结论": "有效" if rule.get("status") == "已启用" else "不参与当前名单"},
            {"环节": "数据快照", "对象": snapshot.get("快照编号"), "状态": snapshot.get("状态"),
             "结论": f"读取{len(rows)}条用户分层记录"},
            {"环节": "流失名单批次", "对象": current.get("批次编号") if current else "无",
             "状态": current.get("状态") if current else "无", "结论": current.get("修复状态") if current else "无"},
            {"环节": "明细数据集", "对象": dataset.get("数据集编号") if dataset else "无",
             "状态": dataset.get("状态") if dataset else "无",
             "结论": f"{dataset.get('名单数') if dataset else 0}人"},
        ]

        breaks: list[str] = []
        if rule.get("status") != "已启用":
            breaks.append(f"规则{rule.get('规则编号')}未处于已启用，不能驱动当前名单")
        if dataset is None:
            breaks.append("当前批次缺少当前明细数据集")
        else:
            if not dataset.get("规则编号"):
                breaks.append("当前数据集没有引用任何分层规则，无法回查分层结论")
            elif dataset.get("规则编号") != rule.get("规则编号") or int(dataset.get("规则版本") or 0) != int(rule.get("规则版本") or 0):
                breaks.append(
                    f"数据集引用规则{dataset.get('规则编号')}第{dataset.get('规则版本')}版，"
                    f"与回查规则{rule.get('规则编号')}第{rule.get('规则版本')}版不一致"
                )
            model = churn_list.effective_model()
            if dataset.get("模型编号") != (model.get("模型编号") if model else None):
                breaks.append("数据集引用的模型不是当前有效模型")
        for row in rows:
            member = member_map.get(str(row["用户编号"]))
            if member is not None and member.get("分层层级") != row["分层层级"]:
                breaks.append(
                    f"{row['用户编号']}在名单中为{member.get('分层层级')}，按规则重算为{row['分层层级']}"
                )

        warnings = [
            f"{row['用户编号']}缓存层级{row['缓存分层层级']}与按有效规则重算的{row['分层层级']}不一致"
            for row in rows if not row["与缓存一致"]
        ]
        return {
            "规则": rule,
            "数据快照": snapshot.get("快照编号"),
            "分层明细": rows,
            "分层分布": layer_distribution(
                churn_list.snapshot_users(str(snapshot["快照编号"])), rule
            ),
            "链路": chain,
            "依赖断点": breaks,
            "缓存告警": warnings,
            "当前数据集": dataset.get("数据集编号") if dataset else None,
            "校验": verification_code(rule.get("规则编号"), rule.get("规则版本"), snapshot.get("快照编号")),
        }

    def user_layers(self, snapshot_id: str) -> dict[str, Any]:
        """按数据快照读取用户分层记录并重算层级，供“故障边界”核对。"""
        snapshot = store.find_by(churn_list.SNAPSHOT_MODULE, "快照编号", snapshot_id)
        if snapshot is None:
            return {"found": False}
        rule = churn_list.effective_rule()
        return {
            "found": True,
            "规则": rule.get("规则编号") if rule else None,
            "规则版本": rule.get("规则版本") if rule else None,
            "用户分层记录": churn_list.layer_view(rule, snapshot_id),
        }
