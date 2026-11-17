"""流失预警业务规则：模型状态流转、名单传播、回执核销与筛选口径都收在这里。

状态机：``草稿 -> 训练中 -> 已部署 -> 已下线``，只能前进一步；``部署模型``
要求训练结论为“已完成”。部署/下线会改变“有效模型”，因此模型状态与当前名单
在同一事务里提交，任一侧失败都不会留下半截状态。
"""
from __future__ import annotations

from typing import Any

from datetime import datetime

from app.domain import verification_code
from app.services import churn_list
from app.services.churn_list import ListConflict
from app.store import store

MODULE = "churn"
REQUIRED_FIELDS = ["模型编号", "模型名称", "特征变量"]
STATUS_ORDER = ["草稿", "训练中", "已部署", "已下线"]
ACTION_RULES = {"发起训练": "训练中", "部署模型": "已部署", "下线模型": "已下线"}
NEGATIVE_ACTIONS = ["下线模型"]
TRAINED = "已完成"


def _transitions() -> dict[str, str]:
    return {"发起训练": "草稿", "部署模型": "训练中", "下线模型": "已部署"}


class ChurnService:
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
            rows = [row for row in rows if keyword in str(row.get("模型编号", ""))]
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
            "模型编号": values.get("模型编号"),
            "模型名称": values.get("模型名称"),
            "特征变量": values.get("特征变量"),
            "流失定义天": values.get("流失定义天", 60),
            "训练数据集": values.get("训练数据集", ""),
            "预测阈值": values.get("预测阈值", 0.7),
            "模型准确率": values.get("模型准确率", 0.0),
            "预警推送频率": values.get("预警推送频率", "每日 09:00"),
            "status": STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "模型版本": int(values.get("模型版本") or 1),
            "version": 1,
            "训练结论": "",
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
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流失预警可执行范围", None

        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预警模型 {entry_id} 不存在或已归档", None

        existing = churn_list.applied_receipt(receipt_key)
        if existing is not None:
            duplicate = churn_list.duplicate_receipt(existing)
            return store.find(MODULE, entry_id), duplicate["说明"], duplicate

        current = str(entry.get("status") or "")
        seen_version = int(entry.get("version") or 0)
        required = _transitions()[action]
        if current != required:
            reason = (
                f"{entry.get('模型编号')}当前处于「{current}」，{action}只允许从「{required}」进入"
                "，不允许跳级或回退"
            )
            receipt = self._receipt(
                "模型动作回执", str(entry.get("模型编号")), action, "已拦截", reason,
                receipt_key, at, batch=churn_list.current_batch(),
            )
            self._audit("拦截模型跳级", str(entry.get("模型编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        if action == "部署模型" and entry.get("训练结论") != TRAINED:
            reason = f"{entry.get('模型编号')}的训练结论仍为「{entry.get('训练结论') or '未提交'}」，不能部署"
            receipt = self._receipt(
                "模型动作回执", str(entry.get("模型编号")), action, "已拦截", reason,
                receipt_key, at, batch=churn_list.current_batch(),
            )
            self._audit("拦截模型跳级", str(entry.get("模型编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        if expected_version is not None and seen_version != expected_version:
            reason = (
                f"{entry.get('模型编号')}已被其他回执更新（期望第{expected_version}版，"
                f"当前第{seen_version}版）"
            )
            receipt = self._receipt(
                "模型动作回执", str(entry.get("模型编号")), action, "已拦截", reason,
                receipt_key, at, batch=churn_list.current_batch(),
            )
            self._audit("拦截并发迁移", str(entry.get("模型编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        target = ACTION_RULES[action]
        batch = churn_list.current_batch()
        try:
            with churn_list.batch_owner.claim(
                str(batch.get("批次编号")) if batch else "", receipt_key
            ), store.transaction():
                row = store.find(MODULE, entry_id)
                if row is None:
                    raise ListConflict(f"预警模型 {entry_id} 不存在或已归档")
                # 与读取时看到的版本比对：等待批次期间若被其他回执推进，本次提交放弃。
                if int(row.get("version") or 0) != seen_version:
                    raise ListConflict("模型版本已被其他回执更新")
                if str(row.get("status") or "") != required:
                    raise ListConflict("模型状态已被其他回执更新")

                row["status"] = target
                row["pending"] = target not in {"已部署", "已下线"}
                row["abnormal"] = action in NEGATIVE_ACTIONS
                row["version"] = int(row.get("version") or 0) + 1
                row["生效标记"] = target == "已部署"
                if target == "训练中":
                    row["训练发起时间"] = at
                if target == "已部署":
                    row["训练结论"] = TRAINED
                    row["训练完成时间"] = row.get("训练完成时间") or at
                    row["生效时间"] = at
                if target == "已下线":
                    row["生效标记"] = False
                    row["下线时间"] = at
                    row["下线原因"] = remark or "人工下线"

                dataset = None
                if target in {"已部署", "已下线"}:
                    dataset = churn_list.propagate(
                        trigger=action,
                        at=at,
                        expected_batch_version=int(batch.get("版本") or 0) if batch else None,
                        expected_dataset_id=str(batch.get("数据集编号") or "") if batch else None,
                    )

                receipt = self._receipt(
                    "部署模型回执" if target == "已部署" else "模型动作回执",
                    str(row.get("模型编号")), action, "已生效",
                    f"{row.get('模型编号')}已进入「{target}」，名单批次同步为"
                    f"{dataset.get('数据集编号') if dataset else '未变更'}，"
                    f"模型准确率展示为{row.get('模型准确率') if target == '已部署' else '不适用'}",
                    receipt_key, at, batch=batch, dataset=dataset,
                )
                self._audit(
                    "模型状态迁移", str(row.get("模型编号")), "已生效",
                    f"{current} -> {target}，模型第{row['version']}版", receipt["核验码"], at,
                )
                result = dict(row)
        except ListConflict as error:
            reason = f"{entry.get('模型编号')}的{action}未生效：{error}"
            receipt = self._receipt(
                "模型动作回执", str(entry.get("模型编号")), action, "已拦截", reason,
                receipt_key, at, batch=batch,
            )
            self._audit("拦截并发迁移", str(entry.get("模型编号")), "已拦截", reason, receipt["核验码"], at)
            return None, reason, receipt

        return result, f"预警模型已{action}", receipt

    def complete_training(self, entry_id: int, *, accuracy: float, at: str = "") -> tuple[dict[str, Any] | None, str]:
        """训练系统回写训练结论与准确率；只有“训练中”的模型可以接收结论。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预警模型 {entry_id} 不存在或已归档"
        if entry.get("status") != "训练中":
            return None, f"{entry.get('模型编号')}当前处于「{entry.get('status')}」，不能回写训练结论"
        with store.transaction():
            row = store.find(MODULE, entry_id)
            if row is None or row.get("status") != "训练中":
                return None, "模型状态已被其他回执更新"
            row["训练结论"] = TRAINED
            row["模型准确率"] = round(float(accuracy), 4)
            row["训练完成时间"] = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["version"] = int(row.get("version") or 0) + 1
            self._audit(
                "回写训练结论", str(row.get("模型编号")), "已生效",
                f"训练完成，准确率{row['模型准确率']}，模型第{row['version']}版",
                verification_code(row.get("模型编号"), "训练完成", row["version"]), at,
            )
            return dict(row), "训练结论已回写，模型可进入部署"

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
        return churn_list.record_receipt({
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
        })

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
