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
    # Nhãn cột đầu: "Nhóm" khi mỗi dòng là một nhóm chi tiêu, "Tháng" khi bảng
    # so sánh các tháng. FE viết hoa khi hiển thị.
    row_header: str = "Nhóm"
    # Nhãn ba cột còn lại. Mặc định giữ nguyên bảng chi tiêu; bảng tư vấn (kế
    # hoạch tiết kiệm) đổi thành "Cần/tháng" và "% thu nhập".
    amount_header: str = "Số tiền"
    pct_header: str = "%"
    # None → FE bỏ hẳn cột Δ: bảng kịch bản tiết kiệm không có "kỳ trước" để so,
    # để nguyên sẽ là một cột toàn dấu "—".
    trend_header: str | None = "Δ kỳ trước"
    # Chú thích dưới bảng — nói rõ con số suy ra từ đâu (vd trung vị tiền dư mấy
    # tháng), để người đọc kiểm chứng được thay vì phải tin.
    footnote: str | None = None


class ChatGridColumn(Contract):
    label: str
    # "right" cho cột số để các chữ số thẳng hàng, dễ so.
    align: Literal["left", "right"] = "left"


class ChatGrid(Contract):
    """Bảng tự do chuyển thể từ markdown mà agent viết ra.

    Khác ChatTable ở chỗ gateway KHÔNG hiểu nội dung, chỉ đổi định dạng. Nhờ vậy
    bất kỳ câu hỏi tài chính nào agent kẻ bảng được thì người dùng cũng thấy
    bảng, không cần gateway thêm luật cho từng loại câu hỏi.

    Đánh đổi: số trong đây do LLM viết (dù nó lấy từ tool đọc dữ liệu thật), nên
    KHÔNG dùng thay ChatTable ở những câu gateway tự dựng được bảng chuẩn —
    chỗ nào có ChatTable thì ưu tiên ChatTable.
    """
    # Gateway tự dựng bảng sản phẩm thì có tiêu đề; bảng bóc từ markdown agent
    # thì không (phần chữ ngay trên đã giới thiệu rồi).
    title: str | None = None
    columns: list[ChatGridColumn]
    rows: list[list[str]]


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


# ---- Đăng nhập nội bộ cho Ops --------------------------------------------------

class OpsLoginRequest(Contract):
    username: str
    password: str


class OpsLoginResult(Contract):
    """Khác `LoginResponse` của khách ở hai điểm: không có nhánh "degraded" (identity-
    service chết là không ai vào Ops được, xem `main.ops_login`), và lý do từ chối
    có thêm `not_backoffice` cho tài khoản đúng mật khẩu nhưng không có quyền vận
    hành."""
    authenticated: bool
    operator: Operator | None = None
    # Lý do khi authenticated=false: not_backoffice | invalid_credentials | disabled | locked
    reason: str | None = None


# ---- Bốn màn vận hành phụ trong sidebar Ops -----------------------------------

class OpsCase(Contract):
    """Một case vận hành, dịch từ bảng guardian_case."""
    id: str
    decision_id: str
    customer: str
    scenario_name: str
    status: Literal["open", "callbackDone", "closedFraud", "closedLegit"]
    status_label: str
    narrative: str
    opened_at: str
    closed_at: str | None = None


class OpsScenario(Contract):
    """Một kịch bản lừa đảo trong playbook, dịch từ bảng scam_scenario."""
    id: str
    name: str
    group_label: str
    pattern_label: str
    action_label: str
    can_ask: bool
    advice_title: str
    advice_body: str
    priority: int
    alerts_today: int


class OpsModelFactor(Contract):
    key: str
    label: str
    max_score: int


class OpsModelConfig(Contract):
    """Ngưỡng và trần điểm của risk engine — chỉ đọc, do risk-scoring giữ."""
    soft_warn_min: int
    intervene_min: int
    max_score: int
    factors: list[OpsModelFactor]
    inputs: list[str]


class AuditTrace(Contract):
    id: str
    time: str
    agent_label: str
    model: str
    status: Literal["ok", "cache", "timeout", "error"]
    status_label: str
    latency_ms: int | None = None
    decision_id: str | None = None
    # Lượt gọi của Copilot lẫn Scam Shield đều gắn với một khách hàng cụ thể;
    # thiếu cột này thì nhật ký chỉ nói "có một lượt gọi" chứ không nói của ai.
    customer_id: int | None = None
    customer_label: str | None = None


class AuditCustomer(Contract):
    """Một khách hàng có mặt trong nhật ký — dựng ô chọn ở màn vận hành."""
    id: int
    label: str | None = None
    calls: int


class AuditAgentStat(Contract):
    # Khoá agent là thứ endpoint nhận để lọc; nhãn chỉ để đọc. Thiếu khoá thì màn
    # hình phải đoán ngược từ nhãn — đúng kiểu magic string mà repo đang tránh.
    agent_key: str
    agent_label: str
    calls: int
    fallback_calls: int
    avg_latency_ms: int


class OpsAuditLog(Contract):
    """Nhật ký mọi lượt gọi LLM — bằng chứng AI kiểm toán được."""
    total_calls: int
    fallback_rate_pct: int
    avg_latency_ms: int
    per_agent: list[AuditAgentStat]
    traces: list[AuditTrace]
    # Hai mục dưới là của TOÀN BỘ nhật ký, cố tình không đổi theo bộ lọc: ô chọn
    # khách phải còn đủ lựa chọn sau khi lọc, và các khoảng thời gian nhanh phải
    # neo vào một mốc đứng yên.
    customers: list[AuditCustomer] = []
    latest_trace_at: str | None = None


# ---- Thao tác ghi -------------------------------------------------------------

class TransferActionRequest(Contract):
    # Ba giá trị đầu là của màn Scam Shield cũ; "held"/"contacted" thêm cho màn
    # Guardian (wireframe có bốn nút: khóa tạm · huỷ · gọi MSB · vẫn tiếp tục).
    action: Literal["cancelled", "proceeded", "reported", "held", "contacted"]
    # Quyết định cụ thể đang được xử lý. Bỏ trống thì dùng lần chấm gần nhất —
    # giữ cho client cũ không gãy.
    decision_id: str | None = None


class TransferActionResponse(Contract):
    ok: bool
    case_status: Literal["pending", "confirmed", "dismissed", "investigating"]
    message: str


# ---- Luồng chuyển tiền: favorite (bỏ qua Scam Shield) vs stk mới (agent check) --

class TransferBeneficiary(Contract):
    """Một người thụ hưởng trong danh bạ. trusted=True → chuyển thẳng, không cần
    Scam Shield (stk quen, đã dùng, không bị nghi ngờ)."""
    id: str
    name: str
    bank: str
    account: str
    relationship: str
    trusted: bool


class TransferExecuteRequest(Contract):
    """Lệnh chuyển đã qua xác thực PIN trên app — ghi vào transaction_history."""
    bank_code: str
    account_no: str
    holder_name: str = ""
    amount: int
    note: str = ""


class TransferExecuteResponse(Contract):
    ok: bool
    transaction_id: int | None = None


class OpenDepositRequest(Contract):
    """Lệnh mở tiền gửi từ app — gateway hạch toán ghi nợ TK nguồn + tạo sổ."""
    amount: int
    months: int
    rate: float
    # Sản phẩm tiết kiệm trong bảng product; bỏ trống = Tiền gửi lãi suất đặc biệt
    product_id: int | None = None


class OpenDepositResponse(Contract):
    ok: bool
    deposit_no: str | None = None
    start_date: str | None = None
    maturity_date: str | None = None
    balance_after: int | None = None


class TransferHistoryItem(Contract):
    """Một dòng của bảng lịch sử chuyển tiền (màn Lịch sử giao dịch trên app)."""
    id: str
    datetime: str
    name: str
    bank: str
    account: str
    amount: int
    note: str = ""
    status: str = "POSTED"


class TransferPrecheckRequest(Contract):
    bank_code: str
    account_no: str
    amount: int
    note: str = ""
    holder_name: str = ""


class ScamShieldSignals(Contract):
    """Tín hiệu gian lận cho một lệnh chuyển — công cụ để agent Scam Shield đọc."""
    bank_code: str
    account_no: str
    account_masked: str
    known: bool
    is_new: bool
    relationship: str
    status: str
    age_days: int
    tx_count: int
    amount: int
    note: str
    scenario_match: str | None = None
    scenario_confidence: int | None = None


class ScamShieldVerdict(Contract):
    """Kết luận của agent Scam Shield về một lệnh chuyển tới stk mới."""
    level: Literal["safe", "suspect", "danger"]
    title: str
    summary: str
    reasons: list[str]
    recommendation: str
    # "agent" = do agent LLM kết luận; "fallback" = agent lỗi, suy ra từ tín hiệu.
    source: Literal["agent", "fallback"]


class TransferPrecheckResponse(Contract):
    # requires_review=False → stk quen, chuyển thẳng tới màn xác nhận.
    # requires_review=True  → stk mới/nghi ngờ, hiện màn Scam Shield với verdict.
    requires_review: bool
    trusted: bool
    is_new: bool
    beneficiary_name: str
    beneficiary_bank: str
    beneficiary_account: str
    verdict: ScamShieldVerdict | None = None

    # ---- Guardian 3 mức (wireframe màn Transfer) ----
    # Điểm và mức do risk engine chấm, KHÔNG phải LLM. Ngưỡng: <40 pass,
    # 40–74 soft_warn, >=75 intervene.
    score: int = 0
    level: Literal["pass", "soft_warn", "intervene"] = "pass"
    # Ba yếu tố nặng nhất, đã diễn giải thành câu cho người đọc.
    top_factors: list[str] = []
    # Câu cảnh báo rule-based hiện ngay, không phải chờ LLM.
    template_text: str = ""
    decision_id: str = ""
    # Câu hỏi + lựa chọn của playbook, chỉ có khi level = intervene.
    question: str | None = None
    options: list[str] = []
    scenario_id: str | None = None
    # "Đã chuyển N lần" — tín hiệu tin cậy ngầm ở trạng thái pass.
    tx_count: int = 0


class ChatBankingRequest(Contract):
    message: str


class ChatBankingDraft(Contract):
    """Ý định chuyển tiền bóc từ MỘT câu của khách.

    Agent chỉ làm đúng việc hiểu câu; số tiền và tên người nhận sau đó được
    gateway đối chiếu với danh bạ THẬT, nên thẻ soạn lệnh hiện trên màn hình
    không bao giờ là người nhận do mô hình bịa ra.
    """
    intent: Literal["transfer", "list_beneficiaries", "other"]
    amount: int | None = None
    # Tên hoặc số tài khoản agent trích được, giữ nguyên văn để FE hiển thị lại.
    recipient: str | None = None
    # Danh bạ khớp: 0 = không thấy, 1 = soạn lệnh luôn, nhiều = hỏi khách chọn.
    matches: list[TransferBeneficiary] = []
    # "agent" = LLM hiểu câu; "fallback" = agent lỗi/chậm, FE tự dùng regex.
    source: Literal["agent", "fallback"] = "agent"


class GuardianAction(Contract):
    """Một nút hành động ở lượt 2 của màn Guardian."""
    key: Literal["hold", "cancel", "contact", "continue"]
    label: str
    # Nút engine khuyến nghị — tô đậm. Do engine chọn, không phải LLM.
    recommended: bool = False


class InterveneDetail(Contract):
    """Lượt 1 (SHOW_REASONS): vì sao Guardian dừng, và câu cần hỏi khách."""
    decision_id: str
    score: int
    level: Literal["pass", "soft_warn", "intervene"]
    amount: int
    beneficiary_label: str
    reasons: list[str]
    question: str
    options: list[str]
    scenario_id: str | None = None


class InterveneRequest(Contract):
    decision_id: str
    selected_option: str
    free_text: str | None = None


class InterveneAdvice(Contract):
    """Lượt 2 (SHOW_ADVICE): khuyến cáo và bốn hành động để khách tự quyết."""
    decision_id: str
    selected_option: str
    advice_title: str
    advice_body: str
    recommended_action: Literal["hold", "cancel", "contact", "continue"]
    actions: list[GuardianAction]
    # "agent" = lời do LLM diễn giải; "playbook" = lấy thẳng kịch bản.
    source: Literal["agent", "playbook"]


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


# ---- So sánh theo tháng -------------------------------------------------------

class MonthCategory(Contract):
    category: str
    label_vi: str
    amount: int
    pct: int
    rank: int
    delta_vs_prev_pct: float | None = None


class MonthSummary(Contract):
    period: str
    label: str
    year: int
    month: int
    income: int
    expense: int
    net: int
    count: int
    # Thay đổi TỔNG chi so với tháng liền trước. None ở tháng đầu cửa sổ.
    delta_vs_prev_pct: float | None = None
    by_category: list[MonthCategory]


class MonthlyReport(Contract):
    months: list[MonthSummary]
    category_totals: list[CategoryTotal]


# ---- Màn Biểu lãi suất (Khám phá sản phẩm → Biểu lãi suất) --------------------

class RateTerm(Contract):
    code: str        # KKH | 1W | 1M | 3M ... — khớp interest_rate_term.term_code
    # float vì có kỳ hạn ngắn hơn tháng ("1 tuần" = 0.25); ép int sẽ biến 1 tuần
    # thành 0 và đụng với "không kỳ hạn".
    months: float
    label: str


class RateProduct(Contract):
    # Mã sản phẩm lõi là chuỗi ("RB.TK.LSCN"), không phải số.
    id: str
    name: str


class RateCell(Contract):
    product_id: str
    rate_pct: float  # %/năm — đợt hiệu lực mới nhất của cặp (sản phẩm, kỳ hạn)


class RateRow(Contract):
    term: RateTerm
    rates: list[RateCell]


class LoanOption(Contract):
    """Một gói vay đã quy ra tiền trả hàng tháng cho đúng khoản khách hỏi."""
    product_id: str
    product_name: str
    term_label: str
    rate_pct: float
    monthly_payment: int          # trả góp đều, dư nợ giảm dần — service tính
    total_interest: int
    total_payment: int


class LoanOptions(Contract):
    amount: int
    months: int
    as_of: str
    options: list[LoanOption]     # xếp theo lãi suất tăng dần


class SavingsOption(Contract):
    product_id: str
    product_name: str
    term_label: str
    rate_pct: float
    interest_amount: int          # lãi khi đáo hạn — service tính
    maturity_amount: int


class SavingsOptions(Contract):
    amount: int
    months: int
    as_of: str
    options: list[SavingsOption]  # xếp theo lãi suất giảm dần


class InvestRates(Contract):
    as_of: str                    # Ngày hiệu lực mới nhất trong biểu — hiển thị "Áp dụng từ ..."
    products: list[RateProduct]
    rows: list[RateRow]           # Mỗi dòng một kỳ hạn, để FE vẽ bảng + biểu đồ so sánh cùng kỳ hạn


# ---- Màn Financial Copilot: thông báo dưới nhóm chi tiêu ----------------------

class CopilotNotification(Contract):
    id: str
    # saving = sổ tiết kiệm đến hạn (CTA mở biểu lãi suất chọn sản phẩm mới)
    # card   = sao kê thẻ chưa thanh toán   ·   loan = đến kỳ trả nợ khoản vay
    kind: Literal["saving", "card", "loan"]
    title: str
    body: str
    cta_label: str | None = None


# ---- Sổ tiết kiệm đến hạn (nguồn của thông báo trên màn Copilot) ---------------

class MaturingDeposit(Contract):
    deposit_id: int
    product_name: str | None = None
    amount: int              # VND
    rate_pct: float          # %/năm đang hưởng (interest + margin)
    term_months: int | None = None
    maturity_date: str       # ISO yyyy-mm-dd — gateway đổi từ YYYYMMDD của core
    due_today: bool
    overdue: bool            # đã qua ngày đáo hạn mà chưa tái tục


class MaturingDeposits(Contract):
    as_of: str               # Ngày (giờ VN) dùng để so — FE hiển thị "hôm nay"
    count: int
    deposits: list[MaturingDeposit]
