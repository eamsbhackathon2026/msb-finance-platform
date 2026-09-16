"""Hợp đồng dữ liệu giữa gateway và msb-guardian-fe.

Bản dịch 1-1 của `src/data/types.ts` bên FE. Tên field phía Python viết
snake_case, `alias_generator=to_camel` lo phần đổi sang camelCase khi serialize —
nhờ vậy code Python vẫn đúng PEP 8 mà JSON trả ra khớp đúng interface TypeScript.

Sai một tên field ở đây thì FE không báo lỗi: `guardedCall` nuốt exception rồi
rơi về dữ liệu demo, màn hình vẫn đẹp như thường. Đó là lý do mọi response đều
đi qua response_model thay vì trả dict tự do.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SpendingCategory(Contract):
    key: str
    label_vi: str
    amount: int
    pct: int
    trend_pct: int


class Insight(Contract):
    id: str
    kind: Literal["info", "warning", "action"]
    title: str
    body: str
    cta_label: str | None = None


class BudgetSummary(Contract):
    month_label: str
    spent_vnd: int
    budget_vnd: int


class CopilotOverview(Contract):
    budget: BudgetSummary
    categories: list[SpendingCategory]
    insights: list[Insight]


class RiskSignal(Contract):
    id: str
    label: str
    detail: str
    weight: int
    severity: Literal["low", "med", "high"]
    confidence_pct: int | None = None


class RiskAssessment(Contract):
    score: int
    level: Literal["low", "medium", "high"]
    scenario_name: str
    signals: list[RiskSignal]
    recommendations: list[str]


class Beneficiary(Contract):
    account_no: str
    bank_name: str
    holder_name: str
    account_age_days: int
    report_count: int


class ScamAlert(Contract):
    id: str
    timestamp: str
    customer: str
    amount: int
    beneficiary: Beneficiary
    assessment: RiskAssessment
    status: Literal["pending", "confirmed", "dismissed", "investigating"]


class OpsMetrics(Contract):
    scanned_today: int
    alerts_fired: int
    cancel_rate_pct: int
    protected_value_vnd: int


# ---- Payload FE gửi lên ------------------------------------------------------

class AssessRequest(Contract):
    # FE chỉ gửi đúng một trường này (xem assessRisk trong src/lib/api.ts).
    amount: int


class ChatRequest(Contract):
    message: str


class DecisionRequest(Contract):
    decision: Literal["confirmed", "dismissed", "investigating"]
    note: str = ""


class OkResponse(Contract):
    ok: bool
