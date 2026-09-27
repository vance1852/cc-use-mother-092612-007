"""定义协同台账在模块边界使用的数据对象与常量。"""

from __future__ import annotations

from dataclasses import dataclass


# 参与者类别：名中医、护理人员、讲解志愿者、后勤。
PARTICIPANT_ROLES = frozenset({
    "famous_doctor",
    "nurse",
    "volunteer_guide",
    "logistics",
})

# 岗位技能：义诊诊疗、义诊陪同、讲解引导、物资补给、秩序维护、综合协调。
SKILLS = frozenset({
    "diagnosis",
    "accompaniment",
    "guiding",
    "supply",
    "order_keeping",
    "coordination",
})

# 缺员类别：迟到、提前离场、临时缺席。
SHORTAGE_KINDS = frozenset({"late", "early_leave", "absent"})

# 签到事实类别：正常签到、迟到回执。
CHECKIN_KINDS = frozenset({"checkin", "late_receipt"})

# 拒绝调度的具体原因编码。
REASON_QUALIFICATION_MISSING = "qualification_missing"
REASON_QUALIFICATION_EXPIRED = "qualification_expired"
REASON_AVAILABILITY_INSUFFICIENT = "availability_insufficient"
REASON_TIME_CONFLICT = "time_conflict"
REASON_DUPLICATE_IN_PLAN = "duplicate_in_plan"
REASON_MIN_STAFF_SHORTAGE = "min_staff_shortage"
REASON_DEPENDENCY_UNCOVERED = "dependency_uncovered"
REASON_PARTICIPANT_INACTIVE = "participant_inactive"

REASON_TEXT = {
    REASON_QUALIFICATION_MISSING: "缺少岗位所需资质",
    REASON_QUALIFICATION_EXPIRED: "资质有效期不能覆盖班次时段",
    REASON_AVAILABILITY_INSUFFICIENT: "可服务时间不覆盖班次时段",
    REASON_TIME_CONFLICT: "与其他已确认班次时间冲突",
    REASON_DUPLICATE_IN_PLAN: "同一班次内重复排班",
    REASON_MIN_STAFF_SHORTAGE: "最低在岗数不足",
    REASON_DEPENDENCY_UNCOVERED: "岗位依赖未覆盖",
    REASON_PARTICIPANT_INACTIVE: "参与者已停用",
}

# 参与者资料中始终可读的字段。
PUBLIC_PARTICIPANT_FIELDS = frozenset({"participant_id", "name", "role_type"})

# 参与者资料中可按岗位履职需要开放的字段。
RESTRICTED_PARTICIPANT_FIELDS = frozenset({"phone", "title", "qualifications", "profile"})


@dataclass(frozen=True)
class PlanIssue:
    """描述完整排班方案校验发现的一处问题。"""

    reason: str
    message: str
    post_id: str | None = None
    participant_id: str | None = None
