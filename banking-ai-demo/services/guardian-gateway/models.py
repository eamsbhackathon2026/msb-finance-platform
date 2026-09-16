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


# ---- Đăng nhập ----------------------------------------------------------------

class LoginRequest(Contract):
    username: str
    password: str


class LoginUser(Contract):
    """Hồ sơ người dùng trả về sau khi đăng nhập — PII đã được che ở tầng
    identity-service (email/điện thoại dạng ng***@…, 09** *** 303)."""
    user_id: int
    username: str
    role: str
    customer_id: int | None = None
    full_name_masked: str | None = None
    email_masked: str | None = None
    phone_masked: str | None = None
    user_status: str


class LoginResponse(Contract):
    authenticated: bool
    # "domain" khi xác thực thật qua identity-service, "degraded" khi service
    # không gọi được và gateway cho qua để giữ luồng demo. FE đọc để biết đăng
    # nhập này có thật hay chỉ là bản dự phòng.
    source: Literal["domain", "degraded"]
    user: LoginUser | None = None
    # Lý do khi authenticated=false: invalid_credentials | disabled | locked
    reason: str | None = None


# ---- Màn Home / Login ---------------------------------------------------------

class Customer(Contract):
    id: str
    name: str
    masked_account: str
    balance: int


# ---- Màn Scam Shield ----------------------------------------------------------

class PendingTransfer(Contract):
    """Lệnh chuyển tiền đang chờ duyệt mà màn cảnh báo đang xét."""
    amount: int
    beneficiary: Beneficiary


class TimelineEvent(Contract):
    id: str
    label: str
    detail: str | None = None
    time: str
    tone: Literal["neutral", "warning", "danger"]


class SimilarScenario(Contract):
    name: str
    description: str
    reported_cases: int


class RiskExplain(Contract):
    """Toàn bộ dữ liệu màn "Vì sao chúng tôi cảnh báo?" trong một lời gọi."""
    assessment: RiskAssessment
    beneficiary_timeline: list[TimelineEvent]
    similar_scenario: SimilarScenario


# ---- Màn Trung tâm an toàn ----------------------------------------------------

class SafetyHistoryItem(Contract):
    id: str
    date: str
    amount: int
    scenario_name: str
    status: Literal["blocked", "ignored", "processing"]


class ProtectionLayer(Contract):
    key: str
    label: str
    description: str
    enabled: bool


class SafetyCenter(Contract):
    safety_score: int
    score_label: str
    updated_label: str
    shield_enabled: bool
    blocked_count: int
    warned_count: int
    reported_count: int
    history: list[SafetyHistoryItem]
    protections: list[ProtectionLayer]


# ---- Màn Ops Dashboard --------------------------------------------------------

class KpiDelta(Contract):
    value_label: str
    up: bool


class OpsDeltas(Contract):
    """Khớp Record<keyof OpsMetrics, KpiDelta> bên TypeScript."""
    scanned_today: KpiDelta
    alerts_fired: KpiDelta
    cancel_rate_pct: KpiDelta
    protected_value_vnd: KpiDelta


class HourlyAlertPoint(Contract):
    hour: str
    count: int


class ScenarioCount(Contract):
    name: str
    count: int


class OpsDashboard(Contract):
    """Phần bổ trợ của Ops Dashboard; KPI chính vẫn ở /api/ops/metrics."""
    deltas: OpsDeltas
    hourly_alerts: list[HourlyAlertPoint]
    scenario_counts: list[ScenarioCount]
    model_inputs: list[str]


class CaseTimelineStep(Contract):
    id: str
    time: str
    label: str
    done: bool


# ---- Chat ---------------------------------------------------------------------

class ChatChartPoint(Contract):
    label: str
    value: int


class ChatChart(Contract):
    type: Literal["bar"]
    title: str
    data: list[ChatChartPoint]


class ChatTableRow(Contract):
    label: str
    amount: int
    pct: int
    # None khi chưa có kỳ trước để so — FE hiện "—", khác hẳn 0 (không đổi).
    trend_pct: int | None = None


class ChatTable(Contract):
    """Bảng số liệu đính kèm câu trả lời chat.

    Số ở đây do gateway dựng từ dữ liệu domain (không phải LLM sinh), nên bảng
    hiển thị luôn khớp database — cùng lý do tồn tại của header
    X-Guardian-Data-Source: phần nhìn thì không phân biệt được thật/bịa.
    """
    title: str
    rows: list[ChatTableRow]
    total_label: str
    total_amount: int


# ---- Màn Home / Copilot -------------------------------------------------------

class HomeContent(Contract):
    """Nội dung động của màn Home và màn đăng nhập."""
    greeting: str
    customer_name: str
    product_tier: str
    assistant_hint: str


class CopilotIntro(Contract):
    greeting: str
    suggestions: list[str]
    month_label: str


# ---- Chi tiết case cho Ops ----------------------------------------------------

class CaseTransaction(Contract):
    channel: str
    content: str
    hold_status: str
    sla_minutes: int


class CaseCustomerProfile(Contract):
    customer_since: str
    segment: str
    avg_transfer_vnd: int
    # KHÔNG đặt tên kiểu alerts90d_count: to_camel coi ranh giới chữ số là ranh
    # giới từ nên sinh ra alerts90DCount (chữ D hoa), lệch khỏi hợp đồng TS mà
    # không có gì báo lỗi — FE chỉ nhận undefined.
    recent_alerts_window_days: int
    recent_alerts_count: int
    recent_alerts_top_score: int


class CaseModelInfo(Contract):
    version: str
    method: str
    scoring_ms: int
    confidence_pct: int
    intervene_threshold: int
    soft_warn_min: int
    soft_warn_max: int


class CaseNoteChip(Contract):
    label: str
    primary: bool


class CaseDetail(Contract):
    transaction: CaseTransaction
    customer_profile: CaseCustomerProfile
    model: CaseModelInfo
    note_chips: list[CaseNoteChip]


# ---- Phiên làm việc của chuyên viên Ops ---------------------------------------

class SystemStatusRow(Contract):
    label: str
    value: str
    # tone là ngữ nghĩa, không phải màu: FE tự quyết định biến CSS tương ứng.
    tone: Literal["ok", "warn", "danger"]


class Operator(Contract):
    name: str
    role: str
    shift: str
    initials: str


class OpsSession(Contract):
    operator: Operator
    system_status: list[SystemStatusRow]
    now_label: str


# ---- Thao tác ghi -------------------------------------------------------------

class TransferActionRequest(Contract):
    action: Literal["cancelled", "proceeded", "reported"]


class TransferActionResponse(Contract):
    ok: bool
    case_status: Literal["pending", "confirmed", "dismissed", "investigating"]
    message: str


class ProtectionToggleRequest(Contract):
    enabled: bool


# ---- Thống kê theo quý --------------------------------------------------------

class QuarterCategory(Contract):
    category: str
    label_vi: str
    amount: int
    pct: int
    rank: int
    # None khi chưa có quý trước để so — khác hẳn 0 nghĩa là không đổi.
    delta_vs_prev_pct: float | None = None


class QuarterSummary(Contract):
    period: str
    label: str
    income: int
    expense: int
    net: int
    count: int
    by_category: list[QuarterCategory]


class CategoryTotal(Contract):
    category: str
    label_vi: str
    amount: int
    pct: int


class QuarterlyReport(Contract):
    quarters: list[QuarterSummary]
    category_totals: list[CategoryTotal]
