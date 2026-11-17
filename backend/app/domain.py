"""RFM 分层与流失预警的领域计算：分层口径、流失概率、名单成员与校验摘要。

这里只做纯函数计算，不读写数据仓库，方便在服务层、修复重算和复核接口之间复用。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

LAYER_HIGH = "高价值"
LAYER_MID = "中价值"
LAYER_LOW = "低价值"
LAYER_UNKNOWN = "未分层"

SOURCE_MODEL_ONLY = "模型命中"
SOURCE_RULE_ONLY = "分层命中"
SOURCE_BOTH = "模型+分层"


def classify(user: dict[str, Any], rule: dict[str, Any] | None) -> str:
    """按有效分层规则判定单个用户的分层层级。

    消费频次界是低价值的边界：频次不超过该界即进入低价值；高于边界的用户再按
    消费频次与消费金额区分高/中价值。没有有效规则时不允许给出分层结论。
    """
    if not rule:
        return LAYER_UNKNOWN
    try:
        frequency = int(user.get("消费频次") or 0)
        amount = float(user.get("消费金额") or 0)
        boundary = int(rule.get("消费频次界") or 0)
    except (TypeError, ValueError):
        return LAYER_UNKNOWN
    if frequency <= boundary:
        return LAYER_LOW
    if frequency >= boundary + 2 and amount >= float(rule.get("消费金额界") or 0):
        return LAYER_HIGH
    return LAYER_MID


def risk_probability(user: dict[str, Any], model: dict[str, Any] | None) -> float | None:
    """流失概率：按最近消费天距流失定义天的超出量、频次和金额折算。"""
    if not model:
        return None
    try:
        recency = int(user.get("最近消费天") or 0)
        frequency = int(user.get("消费频次") or 0)
        amount = float(user.get("消费金额") or 0)
        window = int(model.get("流失定义天") or 0)
    except (TypeError, ValueError):
        return None
    excess = max(recency - window, 0)
    score = 0.30 + min(0.35, excess * 0.01)
    if frequency <= 2:
        score += 0.15
    if amount < 300:
        score += 0.10
    if recency >= window:
        score += 0.05
    return round(min(score, 0.98), 4)


def is_model_hit(user: dict[str, Any], model: dict[str, Any] | None) -> bool:
    if not model:
        return False
    probability = risk_probability(user, model)
    if probability is None:
        return False
    try:
        threshold = float(model.get("预测阈值") or 1.0)
        window = int(model.get("流失定义天") or 0)
        recency = int(user.get("最近消费天") or 0)
    except (TypeError, ValueError):
        return False
    return recency >= window and probability >= threshold


def build_member(
    user: dict[str, Any],
    rule: dict[str, Any] | None,
    model: dict[str, Any] | None,
    *,
    layer: str | None = None,
) -> dict[str, Any] | None:
    """生成一条名单成员；未命中任何一侧时返回 None。"""
    resolved_layer = layer if layer is not None else classify(user, rule)
    model_hit = is_model_hit(user, model)
    rule_hit = bool(rule) and resolved_layer == LAYER_LOW
    if not (model_hit or rule_hit):
        return None
    source = SOURCE_BOTH if (model_hit and rule_hit) else (
        SOURCE_MODEL_ONLY if model_hit else SOURCE_RULE_ONLY
    )
    reasons: list[str] = []
    if model_hit:
        reasons.append(
            f"最近消费天达到流失定义天{model.get('流失定义天')}，"
            f"流失概率{risk_probability(user, model)}不低于预测阈值{model.get('预测阈值')}"
        )
    if rule_hit:
        reasons.append(
            f"分层层级为{LAYER_LOW}，消费频次不超过规则{rule.get('规则编号')}"
            f"第{rule.get('规则版本')}版的消费频次界{rule.get('消费频次界')}"
        )
    return {
        "用户编号": user.get("用户编号"),
        "分层层级": resolved_layer,
        "流失概率": risk_probability(user, model),
        "生成依据": source,
        "命中原因": "；".join(reasons),
        "规则编号": rule.get("规则编号") if rule else "",
        "规则版本": rule.get("规则版本") if rule else 0,
        "消费频次界快照": rule.get("消费频次界") if rule else None,
        "最近消费天界快照": rule.get("最近消费天界") if rule else None,
        "模型编号": model.get("模型编号") if model else "",
        "模型版本": model.get("模型版本") if model else 0,
        "模型准确率": model.get("模型准确率") if model else None,
        "用户状态版本": user.get("数据版本"),
    }


def compute_members(
    users: Iterable[dict[str, Any]],
    rule: dict[str, Any] | None,
    model: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """按“有效模型 + 有效分层”联合生成当前名单。

    模型负责按流失定义天与预测阈值召回高风险用户；分层规则负责补充低价值人群。
    两侧都失效时名单为空，不会回退到任何已失效规则的历史口径。
    """
    members: list[dict[str, Any]] = []
    for user in users:
        member = build_member(user, rule, model)
        if member is not None:
            members.append(member)
    return sorted(members, key=lambda item: str(item.get("用户编号") or ""))


def layer_distribution(
    users: Iterable[dict[str, Any]], rule: dict[str, Any] | None
) -> dict[str, int]:
    distribution = {LAYER_HIGH: 0, LAYER_MID: 0, LAYER_LOW: 0, LAYER_UNKNOWN: 0}
    for user in users:
        layer = classify(user, rule)
        distribution[layer] = distribution.get(layer, 0) + 1
    return distribution


def checksum(payload: Any) -> str:
    """稳定摘要：复核和审计用同一份材料重算即可发现内容漂移。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def dataset_checksum(
    dataset_id: str, source: dict[str, Any], member_ids: Iterable[str]
) -> str:
    """名单明细数据集的校验摘要：数据集 + 来源版本材料 + 数据快照 + 成员范围。"""
    return checksum({
        "dataset": dataset_id,
        "rule": source.get("规则材料"),
        "model": source.get("模型材料"),
        "snapshot": source.get("数据快照"),
        "members": list(member_ids),
    })


def verification_code(*parts: Any) -> str:
    """给每次受理生成可复核的短核验码（回执 + 动作 + 结果 + 版本）。"""
    material = "|".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
