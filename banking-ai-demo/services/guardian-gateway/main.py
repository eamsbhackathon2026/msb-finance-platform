"""guardian-gateway

Service duy nhất mà msb-guardian-fe gọi tới. Phục vụ đúng 7 endpoint mà FE đang
gọi trong `src/lib/api.ts`, trả về đúng kiểu dữ liệu khai báo trong
`src/data/types.ts`.

    GET  /api/copilot/overview
    POST /api/copilot/chat                  (SSE)
    POST /api/risk/assess
    GET  /api/ops/metrics
    GET  /api/ops/alerts
    GET  /api/ops/alerts/{alert_id}
    POST /api/ops/alerts/{alert_id}/decision

PHẠM VI HIỆN TẠI: gateway CHƯA gọi sang 5 service domain. Dữ liệu trả về lấy từ
`catalog.py`. Mục đích của bước này là dựng xong hợp đồng và đường đi mạng để FE
thôi chạy bằng mock trong trình duyệt; phần nối vào customer-profile,
transaction, risk-scoring, scam-knowledge, action-feedback là việc sau.

Vì sao gateway tồn tại thay vì để FE gọi thẳng 5 service: FE gọi cùng origin
`/api/*` với 7 đường dẫn gộp sẵn, còn backend là 5 service riêng với 74 endpoint
ở mức chi tiết hơn hẳn (ví dụ màn Copilot cần gộp portfolio + insights +
cashflow-forecast + recommendations từ 2 service). Gộp ở server giữ cho trình
duyệt chỉ phải biết một địa chỉ và một hợp đồng.

CẢNH BÁO khi kiểm thử: `guardedCall` bên FE bắt mọi lỗi và timeout 6s rồi âm
thầm rơi về dữ liệu demo. Gateway hỏng hoàn toàn thì màn hình vẫn đẹp như
thường. Mọi response ở đây gắn header X-Guardian-Data-Source để phân biệt được
từ tab Network — nhìn giao diện thì không.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import catalog
import domain
import mappers
from plain_stream import PlainTextStreamer
from models import (
    AssessRequest,
    CaseDetail,
    CaseTimelineStep,
    ChatChart,
    ChatChartPoint,
    ChatGrid,
    ChatGridColumn,
    ChatRequest,
    ChatTable,
    ChatTableRow,
    CopilotIntro,
    CopilotOverview,
    Customer,
    DecisionRequest,
    GuardianAction,
    HomeContent,
    InterveneAdvice,
    InterveneDetail,
    InterveneRequest,
    InvestRates,
    LoginRequest,
    LoginResponse,
    MonthlyReport,
    MonthSummary,
    OkResponse,
    Operator,
    OpsAuditLog,
    OpsCase,
    OpsDashboard,
    OpsLoginRequest,
    OpsLoginResult,
    OpsMetrics,
    OpsModelConfig,
    OpsScenario,
    OpsSession,
    PendingTransfer,
    ProtectionToggleRequest,
    QuarterlyReport,
    LoanOption,
    LoanOptions,
    RiskAssessment,
    RiskExplain,
    SavingsOption,
    SavingsOptions,
    SafetyCenter,
    ScenarioCount,
    ScamAlert,
    ScamShieldSignals,
    ScamShieldVerdict,
    TransferActionRequest,
    TransferActionResponse,
    TransferBeneficiary,
    OpenDepositRequest,
    OpenDepositResponse,
    TransferExecuteRequest,
    TransferExecuteResponse,
    TransferHistoryItem,
    TransferPrecheckRequest,
    TransferPrecheckResponse,
)

SERVICE_NAME = "guardian-gateway"


def _now_hms() -> str:
    """Giờ Việt Nam dạng HH:MM:SS, khớp định dạng các mốc có sẵn trong timeline."""
    return datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M:%S")

# Màn "Đang phân tích giao dịch..." bên FE hiển thị theo isPending của
# react-query, tức là nó dài đúng bằng thời gian chờ API. Demo mode giả lập
# 1.800–2.200ms; gateway trả tức thì sẽ làm màn đó loé qua rồi biến mất. Giữ
# 2.000ms để chuyển demo→live không đổi nhịp trình bày. Đặt 0 để tắt.
RISK_ASSESS_DELAY_MS = int(os.getenv("RISK_ASSESS_DELAY_MS", "2000"))

# Tốc độ phát lại token của chat, khớp với replayAsStream bên FE.
CHAT_TOKEN_DELAY_MS = int(os.getenv("CHAT_TOKEN_DELAY_MS", "25"))

app = FastAPI(
    title="guardian-gateway",
    version="0.1.0",
    description=(
        "Backend-for-frontend của **MSB AI Financial Guardian**.\n\n"
        "Phục vụ đúng 7 endpoint mà `msb-guardian-fe` gọi, trả về đúng kiểu dữ liệu "
        "khai báo trong `src/data/types.ts`.\n\n"
        "**Dữ liệu hiện là bản tạm** (`catalog.py`) — gateway chưa gọi sang 5 service "
        "domain. Kiểm tra header `X-Guardian-Data-Source` để biết response đến từ đâu."
    ),
    openapi_tags=[
        {"name": "session", "description": "Khách hàng của phiên hiện tại."},
        {"name": "copilot", "description": "Financial Copilot — tổng quan chi tiêu và chat."},
        {"name": "risk", "description": "Scam Shield — chấm điểm rủi ro cho một lệnh chuyển tiền."},
        {"name": "ops", "description": "Ops Dashboard — chỉ số vận hành, danh sách cảnh báo, quyết định xử lý."},
        {"name": "vận hành", "description": "Health check và mô tả service."},
    ],
)


@app.middleware("http")
async def stamp_data_source(request: Request, call_next):
    """Đánh dấu nguồn dữ liệu lên mọi response.

    Giá trị tính theo TỪNG REQUEST chứ không phải hằng số: "domain" khi mọi lời
    gọi sang service domain đều thành công, "degraded" khi có ít nhất một cái
    hỏng và phần đó lấy từ catalog.py, "stub" khi không gọi domain lần nào.

    Không có dấu này thì không có cách nào phân biệt 'gateway đang đọc database
    thật' với 'gateway đã lặng lẽ rơi về dữ liệu tạm' ngoài việc đọc log.
    """
    domain.reset_request_state()
    response = await call_next(request)
    response.headers["X-Guardian-Data-Source"] = domain.data_source()
    response.headers["X-Guardian-Service"] = SERVICE_NAME
    return response


# Quyết định của chuyên viên vận hành, lưu trong bộ nhớ tiến trình.
# Mất khi pod khởi động lại — chấp nhận được vì đây là bản tạm; khi nối
# action-feedback-service thì chỗ này thành lời gọi PATCH /cases/{id}.
_decisions: dict[str, str] = {}

# Lớp bảo vệ khách bật/tắt trong Trung tâm an toàn. Ghi đè giá trị mặc định
# trong catalog; cùng vòng đời với _decisions nên cũng mất khi pod khởi động lại.
_protections: dict[str, bool] = {}

# Các bước do hành động của khách sinh ra, chèn vào dòng thời gian của case.
# Đây là mắt xích khép vòng: khách bấm huỷ ở màn Scam Shield thì chuyên viên
# vận hành nhìn thấy ngay trong case, thay vì chỉ đổi màn hình phía khách.
_customer_steps: list[CaseTimelineStep] = []

# decision_id của lần chấm điểm gần nhất. risk-scoring cần nó để ghi hành động,
# mà FE thì không có khái niệm decision_id — nó chỉ biết "khách vừa bấm huỷ".
_last_decision_id: str | None = None

# Hành động của khách so với hợp đồng của risk-scoring-service.
DOMAIN_ACTIONS = {
    "cancelled": ("cancel", "prevented"),
    "reported": ("cancel", "prevented"),
    "proceeded": ("continue", "proceeded"),
    "held": ("hold", "held"),
    "contacted": ("contact", "n/a"),
}

# Quyết định của chuyên viên vận hành so với hợp đồng đó.
DOMAIN_DECISIONS = {
    "confirmed": ("cancel", "prevented"),
    "dismissed": ("continue", "proceeded"),
    "investigating": ("hold", "held"),
}


def _alert_by_id(alert_id: str) -> ScamAlert:
    for alert in catalog.OPS_ALERTS:
        if alert.id == alert_id:
            status = _decisions.get(alert_id)
            return alert.model_copy(update={"status": status}) if status else alert
    raise HTTPException(status_code=404, detail=f"Không có cảnh báo nào mang id '{alert_id}'")


def score_for_amount(amount: int) -> int:
    """Điểm rủi ro tạm tính từ số tiền, tất định.

    ĐÂY KHÔNG PHẢI ENGINE RỦI RO. Engine thật là 6 yếu tố trong
    risk-scoring-service, cần customer, tài khoản, người nhận, thời điểm và nội
    dung chuyển khoản — FE hiện chỉ gửi mỗi `amount` nên không đủ đầu vào để gọi.

    Công thức hiệu chỉnh sao cho số tiền chuẩn của demo (85.000.000 ₫) ra đúng
    87 điểm như kịch bản trình bày, và đơn điệu tăng theo số tiền để thử số khác
    vẫn cho kết quả hợp lý.
    """
    raw = 30 + 67 * amount / 100_000_000
    return max(12, min(97, round(raw)))


async def _ops_context() -> tuple[list[dict], dict[int, str], dict[str, str]] | None:
    """Ba mẩu dữ liệu mà mọi màn Ops đều cần, lấy trong ba lời gọi thay vì N.

    Trả None khi không lấy được danh sách quyết định — không có nó thì không
    dựng được màn nào, các phần còn lại có cũng vô nghĩa.
    """
    decisions = await domain.risk_decisions(limit=mappers.DECISION_FETCH_LIMIT)
    if not decisions:
        return None
    rows = decisions.get("decisions") or []

    # Tên khách: lấy cả danh sách một lần rồi tra trong bộ nhớ, thay vì gọi
    # /customers/{id} cho từng quyết định.
    people = await domain.customers()
    names = {
        int(c["customer_id"]): c.get("name_masked") or "—"
        for c in ((people or {}).get("customers") or [])
    }

    # Trạng thái xử lý nằm ở action-feedback, khoá theo decision_id.
    case_rows = await domain.cases(limit=100)
    statuses = {
        c["decision_id"]: mappers.CASE_STATUS_MAP.get(c.get("status", ""), "pending")
        for c in ((case_rows or {}).get("cases") or [])
        if c.get("decision_id")
    }
    # Quyết định của chuyên viên trong phiên này đè lên trạng thái từ database.
    statuses.update(_decisions)
    return rows, names, statuses


# Quyết định của chuyên viên so với trạng thái case trong action-feedback.
# Đóng case bằng hai trạng thái CLOSED_* sẽ sinh feedback nguồn "ops".
CASE_DECISIONS = {
    "confirmed": "CLOSED_FRAUD",
    "dismissed": "CLOSED_LEGIT",
    "investigating": "CALLBACK_DONE",
}

_VN_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]


def _now_vn() -> datetime:
    return datetime.now(timezone(timedelta(hours=7)))


def _vi_now_label() -> str:
    """Nhãn thời gian trên thanh đầu trang Ops, ví dụ "Thứ Ba, 15/09/2026 · 09:41"."""
    now = _now_vn()
    return f"{_VN_WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y · %H:%M}"


# Quyền tối thiểu để vào được /ops. `GET /users/{id}/permissions` của
# identity-service TRA VỀ effective_permissions chứ KHÔNG trả role_scope (dù
# service có truy vấn cột đó nội bộ) — gate ở đây dựa trên quyền thật thay vì
# role_scope như mô tả ban đầu trong phase-02-ops-internal-auth.md, vì đó là
# trường duy nhất mà response thực tế cung cấp.
OPS_REQUIRED_PERMISSION = "ops.dashboard.read"

# Chỉ một vai trò BACKOFFICE tồn tại trong dữ liệu hiện có (ràng buộc CHECK của
# app_user.role, xem phase-02-ops-internal-auth.md) nên không phân biệt được
# analyst/manager/auditor — nhãn vai trò là hằng số, khớp catalog.OPS_SESSION.
OPS_OPERATOR_ROLE_LABEL = "Fraud Ops"


def _ops_shift_label() -> str:
    """Suy ca trực từ giờ hiện tại — Operator.shift là trường bắt buộc nhưng
    identity-service không có khái niệm ca làm việc."""
    hour = _now_vn().hour
    if 5 <= hour < 12:
        return "Ca sáng"
    if 12 <= hour < 18:
        return "Ca chiều"
    return "Ca tối"


def _ops_initials(full_name_masked: str | None, username: str) -> str:
    """'NGUYEN VAN M***' -> 'NM'. Không có tên (nhiều tài khoản để trống
    full_name) thì lấy hai ký tự đầu của tên đăng nhập."""
    if full_name_masked:
        parts = [p.strip("*") for p in full_name_masked.split() if p.strip("*")]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        if parts:
            return parts[0][:2].upper()
    return username[:2].upper()


def _ops_operator_from_user(user: dict) -> Operator:
    """Dựng hồ sơ hiển thị cho sidebar/header Ops từ hồ sơ đã che PII của
    identity-service. `name` có thể là tên đã che (vd "NGUYEN VAN M***") khi
    full_name có dữ liệu; nhiều tài khoản demo để trống nên rơi về username."""
    username = user.get("username") or ""
    name = user.get("full_name_masked") or username
    return Operator(
        name=name,
        role=OPS_OPERATOR_ROLE_LABEL,
        shift=_ops_shift_label(),
        initials=_ops_initials(user.get("full_name_masked"), username),
    )


async def _case_of_decision(decision_id: str, customer_id: int) -> dict | None:
    """Case mở cho một quyết định, nếu có.

    action-feedback không lọc theo decision_id nên lấy theo khách rồi khớp tại
    chỗ — một lời gọi, và danh sách case của một khách rất ngắn.
    """
    rows = await domain.cases(limit=100)
    if not rows:
        return None
    for case in rows.get("cases") or []:
        if str(case.get("decision_id")) == decision_id:
            return case
    return None


async def _scenario_names() -> dict[str, str]:
    """Tên kịch bản theo decision_id.

    Bản ghi risk_decision chỉ nhắc mã kịch bản trong câu giải thích của cơ chế
    nâng mức, mà nâng mức chỉ xảy ra với một phần quyết định. View /ops/decisions
    thì có sẵn scenario_name cho mọi dòng — một lời gọi là đủ cho cả danh sách.
    """
    view = await domain.ops_decisions(limit=mappers.DECISION_FETCH_LIMIT)
    return {
        d["decision_id"]: d["scenario_name"]
        for d in ((view or {}).get("decisions") or [])
        if d.get("decision_id") and d.get("scenario_name")
    }


async def _decisions_with_scenario_names() -> list[dict] | None:
    """Danh sách quyết định, mỗi dòng gắn thêm tên kịch bản.

    Bảng risk_decision KHÔNG có cột scenario_name — tên nằm ở view
    ops_decision_log. Thiếu bước ghép này thì mọi dòng rơi vào nhãn "Chưa khớp
    kịch bản", và biểu đồ theo kịch bản trên Ops Dashboard đếm nhầm hết.
    """
    decisions = await domain.risk_decisions(limit=mappers.DECISION_FETCH_LIMIT)
    if not decisions:
        return None
    names = await _scenario_names()
    return [
        {**d, "scenario_name": names.get(d.get("decision_id"), d.get("scenario_name"))}
        for d in (decisions.get("decisions") or [])
    ]


def _scenario_counts(rows: list[dict]) -> list[ScenarioCount]:
    """Số vụ theo tên kịch bản, KHÔNG cắt bớt.

    Biểu đồ trên Ops Dashboard chỉ vẽ 5 cột cao nhất, nhưng màn playbook liệt kê
    đủ kịch bản nên phải có cả phần đuôi — nếu không, kịch bản thứ sáu trở đi
    luôn hiện 0 dù thực tế có cảnh báo.
    """
    counts: dict[str, int] = {}
    for d in rows:
        name = d.get("scenario_name")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return [ScenarioCount(name=n, count=c) for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]


_SCENARIO_RE = re.compile(r"\bS\d{2}\b")


def _scenario_id_in(text: str) -> str | None:
    """Rút mã kịch bản (S01…) từ câu giải thích của cơ chế nâng mức."""
    m = _SCENARIO_RE.search(text)
    return m.group(0) if m else None


async def _precheck_for(amount: int) -> dict | None:
    """Dựng đầu vào cho engine từ hồ sơ khách và fraud case, rồi gọi precheck."""
    pf = await domain.portfolio(domain.current_customer_id())
    accounts = (pf or {}).get("accounts") or []
    if not accounts:
        return None
    case = await domain.fraud_case(domain.DEMO_FRAUD_CASE_ID)
    series = ((case or {}).get("injection") or {}).get("series") or []
    memo = series[0].get("memo") if series else None
    ben = catalog.MAIN_BENEFICIARY
    return await domain.precheck({
        "customer_id": domain.current_customer_id(),
        "account_id": accounts[0]["account_id"],
        "amount": amount,
        "beneficiary_bank_code": ben.bank_name[:3].upper(),
        "beneficiary_account_no": ben.account_no.replace(" ", ""),
        "memo": memo,
    })


@app.get("/api/copilot/overview", response_model=CopilotOverview, tags=["copilot"],
         # ctaLabel là optional bên TS: bỏ hẳn field khi không có, thay vì trả null.
         response_model_exclude_none=True,
         summary="Tổng quan chi tiêu tháng cho màn Financial Copilot")
async def copilot_overview() -> CopilotOverview:
    monthly = await domain.monthly_summary(domain.current_customer_id())
    if not monthly:
        return catalog.COPILOT_OVERVIEW
    ins = await domain.insights(domain.current_customer_id())
    mapped = mappers.map_overview(monthly, ins)
    if mapped is None:
        # Gọi được nhưng không có kỳ nào — vẫn là suy giảm, phải đánh dấu.
        domain.mark_degraded()
        return catalog.COPILOT_OVERVIEW
    return mapped


# Nhận diện câu hỏi về chi tiêu để đính bảng + biểu đồ số liệu thật. "quý" đặc
# thù hơn nên xét trước "tháng"; phải có cả ý ĐỊNH hỏi chi tiêu lẫn MỐC thời gian
# thì mới đính, tránh gắn bảng vào câu hỏi không liên quan.
_SPEND_RE = re.compile(r"chi tiêu|tiêu|chi |tổng hợp|thống kê|nhóm|phân bổ|báo cáo|xem|show", re.IGNORECASE)
_QUARTER_RE = re.compile(r"quý|quarter", re.IGNORECASE)
_MONTH_RE = re.compile(r"tháng", re.IGNORECASE)
# So sánh nhiều tháng: "so với tháng 8", "so sánh các tháng", "6 tháng gần đây".
_COMPARE_RE = re.compile(r"so sánh|so với|các tháng|mấy tháng|nhiều tháng|từng tháng|những tháng|vài tháng|\d+\s*tháng", re.IGNORECASE)
# Câu hỏi nói về NHÓM chi tiêu chứ không phải mốc thời gian: "so sánh các nhóm
# tháng này" là so các nhóm trong một tháng, không phải so tháng với tháng.
_GROUP_RE = re.compile(r"nhóm|danh mục|hạng mục|khoản mục", re.IGNORECASE)
# Một tháng cụ thể: "tháng 6", "tháng 6/2026" — SỐ đứng NGAY SAU chữ "tháng"
# (khác "6 tháng" = số lượng tháng, đã bắt ở _COMPARE_RE).
_SPECIFIC_MONTH_RE = re.compile(r"tháng\s*(1[0-2]|0?[1-9])(?:\s*[/-]\s*(\d{4}))?", re.IGNORECASE)


def _strip_markdown_for_plain(text: str) -> str:
    """Gỡ markdown khỏi câu trả lời agent cho msb-guardian-fe (render văn bản thuần).

    Agent xuất bảng markdown (kiểu Excel) để render đẹp ở admin-web của nền tảng
    agent. Nhưng bong bóng chat của msb-guardian-fe render {content} dạng văn bản
    thuần và ĐÃ có bảng số liệu riêng do gateway đính (số của domain, đảm bảo
    đúng). Nên ở đường này: bỏ nguyên khối bảng (dòng bắt đầu bằng "|") để không
    hiện dấu | thô và không trùng bảng, đồng thời gỡ **đậm** và # tiêu đề. Phần
    chữ nhận xét và gạch đầu dòng "- " giữ nguyên.
    """
    kept = [ln for ln in text.split("\n") if not ln.lstrip().startswith("|")]
    out = "\n".join(kept)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)   # bỏ **đậm**
    out = re.sub(r"__(.+?)__", r"\1", out)        # bỏ __đậm__
    out = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", out)  # bỏ tiêu đề #
    out = re.sub(r"(?m)^\s{0,3}>\s?", "", out)        # bỏ dấu trích dẫn >
    out = re.sub(r"\n{3,}", "\n\n", out)          # gộp dòng trống thừa
    return out.strip()


# ---- Chuyển bảng markdown của agent thành bảng thật ---------------------------

# Dòng kẻ ngang của bảng markdown: "|---|---:|" (cho phép thiếu | ở hai đầu).
_GRID_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
# Ô toàn số: tiền, phần trăm, dấu +/-. Cột như vậy canh phải cho thẳng hàng.
_GRID_SO_RE = re.compile(r"^[+\-−]?[\d.,%\s₫]+$")
_GRID_MAX = 3          # ba bảng là hết chỗ trong một bong bóng chat
_GRID_MAX_ROWS = 15
_GRID_MAX_COLS = 6


def _grid_cells(line: str) -> list[str]:
    """Bóc các ô của một dòng markdown "| a | b |"."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [re.sub(r"\*\*(.+?)\*\*", r"\1", o).strip() for o in s.split("|")]


def _parse_markdown_grids(text: str) -> list[ChatGrid]:
    """Mọi bảng markdown trong câu trả lời agent → ChatGrid để FE kẻ bảng thật.

    Đây là chỗ khiến "câu hỏi tài chính nào cũng có bảng" mà không phải viết
    thêm luật cho từng loại câu: agent vốn đã xuất bảng markdown (đẹp ở
    admin-web), trước đây gateway vứt đi cho khỏi lộ dấu "|" trên FE.
    """
    grids: list[ChatGrid] = []
    lines = text.split("\n")
    i = 0
    while i < len(lines) and len(grids) < _GRID_MAX:
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        khoi = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            khoi.append(lines[i])
            i += 1
        # Cần tiêu đề + dòng kẻ + ít nhất một dòng dữ liệu mới thành bảng.
        if len(khoi) < 3 or not _GRID_SEP_RE.match(khoi[1]):
            continue
        tieu_de = _grid_cells(khoi[0])[:_GRID_MAX_COLS]
        if not tieu_de:
            continue
        can_le = _grid_cells(khoi[1])[:_GRID_MAX_COLS]
        rows = []
        for dong in khoi[2:][:_GRID_MAX_ROWS]:
            o = _grid_cells(dong)[:len(tieu_de)]
            o += [""] * (len(tieu_de) - len(o))  # dòng thiếu ô thì đệm cho đủ
            rows.append(o)
        if not rows:
            continue
        cols = []
        for idx, nhan in enumerate(tieu_de):
            # Agent thường không ghi canh lề, nên tự suy: cột mà mọi ô đều là
            # số thì canh phải.
            danh_dau = can_le[idx] if idx < len(can_le) else ""
            gia_tri = [r[idx] for r in rows if r[idx]]
            phai = danh_dau.endswith(":") and not danh_dau.strip().startswith(":")
            if not phai and gia_tri:
                phai = all(_GRID_SO_RE.match(v) for v in gia_tri)
            cols.append(ChatGridColumn(label=nhan, align="right" if phai else "left"))
        grids.append(ChatGrid(columns=cols, rows=rows))
    return grids


def _quarter_visual(report: QuarterlyReport) -> tuple[ChatTable | None, ChatChart | None]:
    if not report.quarters:
        return None, None
    q = report.quarters[-1]  # quý gần nhất
    cats = sorted(q.by_category, key=lambda c: c.amount, reverse=True)
    if not cats:
        return None, None
    table = ChatTable(
        title=f"Chi tiêu {q.label} theo nhóm",
        # delta_vs_prev_pct của domain là số thực (3.5, -49.8); làm tròn về số
        # nguyên cho khớp cách khối "Chi tiêu theo quý" hiển thị. Giữ None nguyên
        # (chưa có kỳ trước) — round(None) sẽ nổ.
        rows=[ChatTableRow(
            label=c.label_vi, amount=c.amount, pct=c.pct,
            trend_pct=round(c.delta_vs_prev_pct) if c.delta_vs_prev_pct is not None else None,
        ) for c in cats],
        total_label="Tổng chi", total_amount=q.expense,
    )
    chart = ChatChart(type="bar", title=f"Chi tiêu {q.label}",
                      data=[ChatChartPoint(label=c.label_vi, value=c.amount) for c in cats])
    return table, chart


def _overview_visual(overview: CopilotOverview) -> tuple[ChatTable | None, ChatChart | None]:
    cats = sorted(overview.categories, key=lambda c: c.amount, reverse=True)
    if not cats:
        return None, None
    table = ChatTable(
        title=f"Chi tiêu {overview.budget.month_label} theo nhóm",
        rows=[ChatTableRow(label=c.label_vi, amount=c.amount, pct=c.pct,
                           trend_pct=c.trend_pct) for c in cats],
        total_label="Đã chi", total_amount=overview.budget.spent_vnd,
    )
    chart = ChatChart(type="bar", title=f"Chi tiêu {overview.budget.month_label}",
                      data=[ChatChartPoint(label=c.label_vi, value=c.amount) for c in cats])
    return table, chart


def _months_compare_visual(report: MonthlyReport) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng so sánh: mỗi DÒNG là một tháng (tổng chi + thay đổi so tháng trước)."""
    months = report.months
    if len(months) < 2:  # cần ít nhất hai tháng mới có gì để so
        return None, None
    grand = sum(m.expense for m in months) or 1
    table = ChatTable(
        title=f"So sánh chi tiêu {len(months)} tháng gần nhất",
        rows=[ChatTableRow(
            label=m.label, amount=m.expense, pct=round(m.expense * 100 / grand),
            trend_pct=round(m.delta_vs_prev_pct) if m.delta_vs_prev_pct is not None else None,
        ) for m in months],
        total_label=f"Tổng {len(months)} tháng", total_amount=sum(m.expense for m in months),
        row_header="Tháng",
    )
    chart = ChatChart(type="bar", title="Chi tiêu theo tháng",
                      data=[ChatChartPoint(label=f"T{m.month}", value=m.expense) for m in months])
    return table, chart


def _month_visual(m: MonthSummary) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng theo nhóm cho ĐÚNG một tháng cụ thể (vd tháng 6), không phải tháng nay."""
    cats = sorted(m.by_category, key=lambda c: c.amount, reverse=True)
    if not cats:
        return None, None
    table = ChatTable(
        title=f"Chi tiêu {m.label} theo nhóm",
        rows=[ChatTableRow(
            label=c.label_vi, amount=c.amount, pct=c.pct,
            trend_pct=round(c.delta_vs_prev_pct) if c.delta_vs_prev_pct is not None else None,
        ) for c in cats],
        total_label="Tổng chi", total_amount=m.expense,
    )
    chart = ChatChart(type="bar", title=f"Chi tiêu {m.label}",
                      data=[ChatChartPoint(label=c.label_vi, value=c.amount) for c in cats])
    return table, chart


def _resolve_period(month: int, year: int | None) -> str:
    """(tháng, năm?) → "YYYYMM". Không cho năm thì lấy lần gần nhất tháng đó đã
    qua: tháng 6 hỏi vào tháng 9/2026 nghĩa là 202606; tháng 11 nghĩa là 202511."""
    now = datetime.now(timezone(timedelta(hours=7)))
    if not year:
        year = now.year if month <= now.month else now.year - 1
    return f"{year:04d}{month:02d}"


async def _month_report(period: str) -> MonthSummary | None:
    """Lấy đúng một tháng từ dữ liệu so sánh (đủ 12 tháng gần nhất)."""
    report = await copilot_months(months=12)
    for m in report.months:
        if m.period == period:
            return m
    return None


# ---- Tư vấn mục tiêu: kế hoạch tiết kiệm -------------------------------------

# Câu hỏi tư vấn tích lũy: "kế hoạch tiết kiệm mua ô tô 500 triệu".
_GOAL_RE = re.compile(
    r"kế hoạch|tiết kiệm|tích l[uũ]y|dành dụm|để dành|mục tiêu|đủ tiền|"
    r"mua\s+(?:xe|ô\s?tô|oto|nhà|đất|căn hộ)",
    re.IGNORECASE,
)
# Số tiền mục tiêu: "500 triệu", "500tr", "1,5 tỷ". Tiếng Việt dùng dấu phẩy cho
# phần thập phân và dấu chấm cho hàng nghìn, nên đọc theo quy ước đó.
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(tỷ|tỉ|triệu|tr)\b", re.IGNORECASE)
_DON_VI = {"tỷ": 1_000_000_000, "tỉ": 1_000_000_000, "triệu": 1_000_000, "tr": 1_000_000}
# Các mốc đem ra so — đủ ngắn để thấy sức ép, đủ dài để thấy lối ra.
_MOC_LO_TRINH = ((12, "1 năm"), (24, "2 năm"), (36, "3 năm"), (60, "5 năm"))


def _goal_amount(question: str) -> int | None:
    """Số tiền mục tiêu trong câu hỏi, hoặc None.

    Bỏ qua số dưới 10 triệu: đó thường là một khoản chi lẻ ("ăn uống 5 triệu")
    chứ không phải mục tiêu tích lũy, gắn bảng lộ trình vào sẽ lạc đề.
    """
    m = _AMOUNT_RE.search(question)
    if not m:
        return None
    so = float(m.group(1).replace(".", "").replace(",", "."))
    tien = int(so * _DON_VI[m.group(2).lower()])
    return tien if tien >= 10_000_000 else None


def _current_period() -> str:
    now = datetime.now(timezone(timedelta(hours=7)))
    return f"{now.year:04d}{now.month:02d}"


def _trung_vi(xs: list[int]) -> int:
    xs = sorted(xs)
    giua = len(xs) // 2
    return xs[giua] if len(xs) % 2 else (xs[giua - 1] + xs[giua]) // 2


def _saving_capacity(months: list[MonthSummary]) -> tuple[int, int, int]:
    """(tiền dư điển hình, thu nhập điển hình, số tháng dùng để tính).

    Lấy TRUNG VỊ chứ không lấy trung bình: dữ liệu thật có một tháng nhận khoản
    tiền hơn 500 triệu, trung bình sẽ thành "để dành được 88 triệu/tháng" — sai
    hoàn toàn so với nhịp sống thường.

    Bỏ hai loại tháng không đại diện cho một chu kỳ sống:
    - Tháng đang chạy: chưa đủ ngày nên chưa đủ chi.
    - Tháng không ghi nhận đồng thu nhập nào: đó là tháng nằm ở mép cửa sổ dữ
      liệu (bắt đầu từ giữa tháng, chưa kịp có kỳ lương), tính vào sẽ thành một
      tháng "âm 4,6 triệu" bịa ra, kéo mức để dành xuống gần một nửa.
    """
    xong = [m for m in months if m.period != _current_period() and m.income > 0]
    if not xong:
        return 0, 0, 0
    return _trung_vi([m.net for m in xong]), _trung_vi([m.income for m in xong]), len(xong)


def _tien_gon(n: int) -> str:
    """Số tiền dạng gọn cho câu chú thích: 471321000 → "471,3 triệu"."""
    for don_vi, ten in ((1_000_000_000, "tỷ"), (1_000_000, "triệu")):
        if abs(n) >= don_vi:
            # Bỏ ",0" thừa: "500 triệu" đọc gọn hơn "500,0 triệu".
            return f"{n / don_vi:.1f}".rstrip("0").rstrip(".").replace(".", ",") + f" {ten}"
    return f"{n:,}".replace(",", ".") + " ₫"


def _savings_plan_visual(
    target: int, balance: int, months: list[MonthSummary],
) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng "mỗi tháng phải để dành bao nhiêu" theo từng lộ trình.

    Dòng tổng mới là chỗ quan trọng nhất: mức khách THỰC SỰ để dành được. Thiếu
    nó thì lộ trình "7,9 triệu/tháng" trông rất hợp lý, trong khi cả tháng khách
    chỉ thu về 7,6 triệu — tức là bất khả thi, và đó đúng là lời khuyên hỏng mà
    câu trả lời toàn chữ đang đưa ra.
    """
    con_thieu = target - balance
    if con_thieu <= 0:  # đã đủ tiền, không còn gì để lập lộ trình
        return None, None
    kha_nang, thu_nhap, so_thang = _saving_capacity(months)
    rows = [
        ChatTableRow(
            label=ten,
            amount=round(con_thieu / n),
            pct=round(con_thieu * 100 / n / thu_nhap) if thu_nhap else 0,
        )
        for n, ten in _MOC_LO_TRINH
    ]

    chu_thich = f"Số dư {_tien_gon(balance)}, còn thiếu {_tien_gon(con_thieu)}."
    if kha_nang > 0:
        nam = con_thieu / kha_nang / 12
        chu_thich += (
            f" Mức để dành thực tế là trung vị tiền dư {so_thang} tháng đã trọn"
            f" (thu nhập khoảng {_tien_gon(thu_nhap)}/tháng) — giữ nhịp này cần"
            f" khoảng {nam:.0f} năm."
        )
    else:
        chu_thich += f" {so_thang} tháng gần nhất chi gần hết thu, chưa có tiền dư để tích lũy."

    table = ChatTable(
        title=f"Lộ trình tiết kiệm cho mục tiêu {_tien_gon(target)}",
        rows=rows,
        total_label="Thực tế để dành được/tháng",
        total_amount=max(kha_nang, 0),
        row_header="Lộ trình",
        amount_header="Cần/tháng",
        pct_header="% thu nhập",
        trend_header=None,  # kịch bản tương lai không có "kỳ trước" để so
        footnote=chu_thich,
    )
    diem = [ChatChartPoint(label=ten, value=round(con_thieu / n)) for n, ten in _MOC_LO_TRINH]
    if kha_nang > 0:
        # Cột cuối là mức thật — đặt cạnh các cột lộ trình thì khoảng cách giữa
        # "cần" và "có" hiện ra ngay, không cần đọc chữ.
        diem.append(ChatChartPoint(label="Thực tế", value=kha_nang))
    chart = ChatChart(type="bar", title="Cần để dành mỗi tháng theo lộ trình", data=diem)
    return table, chart


def _surplus_visual(months: list[MonthSummary]) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng tiền dư từng tháng — nền cho lời khuyên tích lũy khi chưa nêu mục tiêu."""
    xong = [m for m in months if m.period != _current_period()]
    if len(xong) < 2:
        return None, None
    rows = [
        ChatTableRow(
            label=m.label.replace("Tháng ", "T"),
            amount=m.net,
            pct=round(m.net * 100 / m.income) if m.income else 0,
        )
        for m in xong
    ]
    kha_nang, thu_nhap, so_thang = _saving_capacity(months)
    table = ChatTable(
        title=f"Tiền dư {len(xong)} tháng gần nhất",
        rows=rows,
        total_label="Dư điển hình/tháng",
        total_amount=kha_nang,
        row_header="Tháng",
        amount_header="Thu − chi",
        pct_header="% thu nhập",
        trend_header=None,
        footnote=(
            f"Thu nhập khoảng {_tien_gon(thu_nhap)}/tháng. Dư điển hình là trung vị"
            f" {so_thang} tháng đã trọn, không tính tháng đang chạy."
        ),
    )
    chart = ChatChart(
        type="bar", title="Tiền dư mỗi tháng",
        data=[ChatChartPoint(label=r.label, value=max(r.amount, 0)) for r in rows],
    )
    return table, chart


# ---- Tư vấn gói sản phẩm: vay / gửi tiết kiệm --------------------------------

_LOAN_RE = re.compile(r"\bvay\b|khoản vay|cho vay|trả góp|lãi vay|mua trả góp", re.IGNORECASE)
_DEPOSIT_RE = re.compile(r"gửi tiết kiệm|gửi tiền|sổ tiết kiệm|gói tiết kiệm|đáo hạn", re.IGNORECASE)
_RATE_RE = re.compile(r"lãi suất|biểu lãi|lãi bao nhiêu|kỳ hạn", re.IGNORECASE)
# "5 năm", "12 tháng" — SỐ đứng TRƯỚC đơn vị, khác "tháng 6" là mốc thời gian.
_TERM_RE = re.compile(r"(\d+)\s*(năm|tháng)", re.IGNORECASE)


def _goal_months(question: str) -> int | None:
    """Kỳ hạn khách nói, quy về số tháng: "5 năm" → 60, "12 tháng" → 12."""
    m = _TERM_RE.search(question)
    if not m:
        return None
    so = int(m.group(1))
    thang = so * 12 if m.group(2).lower() == "năm" else so
    return thang if 1 <= thang <= 360 else None


def _tien(n: int) -> str:
    """Tiền kiểu Việt Nam: 10476513 → "10.476.513 ₫"."""
    return f"{n:,}".replace(",", ".") + " ₫"


def _pct(x: float) -> str:
    return f"{x:.1f}".replace(".", ",") + "%"


def _loan_grid(raw: dict) -> ChatGrid:
    """Bảng so sánh gói vay. Số trả góp lấy nguyên từ domain, gateway không tính lại."""
    return ChatGrid(
        title=f"Vay {_tien_gon(raw['amount'])} trong {raw['months']} tháng",
        columns=[
            ChatGridColumn(label="Gói vay"),
            ChatGridColumn(label="Lãi suất", align="right"),
            ChatGridColumn(label="Trả/tháng", align="right"),
            ChatGridColumn(label="Tổng lãi", align="right"),
        ],
        rows=[
            [o["product_name"], _pct(o["rate_pct"]), _tien(o["monthly_payment"]), _tien(o["total_interest"])]
            for o in raw["options"]
        ],
    )


def _savings_grid(raw: dict) -> ChatGrid:
    return ChatGrid(
        title=f"Gửi {_tien_gon(raw['amount'])} trong {raw['months']} tháng",
        columns=[
            ChatGridColumn(label="Gói tiết kiệm"),
            ChatGridColumn(label="Lãi suất", align="right"),
            ChatGridColumn(label="Lãi nhận", align="right"),
            ChatGridColumn(label="Đáo hạn", align="right"),
        ],
        rows=[
            [o["product_name"], _pct(o["rate_pct"]), _tien(o["interest_amount"]), _tien(o["maturity_amount"])]
            for o in raw["options"]
        ],
    )


def _rate_grid(raw: dict, title: str) -> ChatGrid | None:
    """Biểu lãi suất: mỗi dòng một kỳ hạn, mỗi cột một sản phẩm."""
    sp = raw.get("products") or []
    terms = raw.get("terms") or []
    if not sp or not terms:
        return None
    tra = {(r["product_id"], r["term_code"]): r["rate_pct"] for r in raw["rates"]}
    return ChatGrid(
        title=title,
        columns=[ChatGridColumn(label="Kỳ hạn")]
                + [ChatGridColumn(label=p["product_name"], align="right") for p in sp],
        rows=[
            [t["term_label"]] + [
                _pct(tra[(p["product_id"], t["term_code"])]) if (p["product_id"], t["term_code"]) in tra else "—"
                for p in sp
            ]
            for t in terms
        ],
    )


async def _product_visual(question: str) -> ChatGrid | None:
    """Bảng sản phẩm do GATEWAY dựng từ biểu lãi thật, hoặc None.

    Cùng lý do với bảng chi tiêu: lãi suất và tiền trả góp là thứ khách mang đi
    quyết định vay/gửi thật, nên không để LLM tự chép số. Có số tiền + kỳ hạn thì
    ra bảng so sánh đã quy đổi; chỉ hỏi suông lãi suất thì ra biểu lãi.
    """
    vay = _LOAN_RE.search(question) is not None
    gui = _DEPOSIT_RE.search(question) is not None
    if not (vay or gui or _RATE_RE.search(question)):
        return None
    so_tien = _goal_amount(question)
    so_thang = _goal_months(question)
    if vay:
        if so_tien and so_thang:
            raw = await domain.loan_options(so_tien, so_thang)
            return _loan_grid(raw) if raw and raw.get("options") else None
        raw = await domain.product_rates("LOAN")
        return _rate_grid(raw, "Lãi suất vay theo kỳ hạn") if raw and raw.get("rates") else None
    if gui or _RATE_RE.search(question):
        if so_tien and so_thang:
            raw = await domain.savings_options(so_tien, so_thang)
            return _savings_grid(raw) if raw and raw.get("options") else None
        raw = await domain.product_rates("SAVINGS")
        return _rate_grid(raw, "Lãi suất tiết kiệm theo kỳ hạn") if raw and raw.get("rates") else None
    return None


async def _spending_visual(question: str) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng + biểu đồ số liệu thật cho câu hỏi, hoặc (None, None).

    Dùng lại đúng các endpoint copilot_months/quarters/overview nên số ở bảng
    khớp từng đồng với màn hình và với điều agent nói — một nguồn số duy nhất.

    Thứ tự xét (quan trọng — sai thứ tự là gắn nhầm bảng):
    1. TƯ VẤN có số tiền mục tiêu ("mua ô tô 500 triệu") → bảng lộ trình tiết
       kiệm. Phải xét TRƯỚC cổng _SPEND_RE vì câu hỏi tư vấn thường không nhắc
       chữ "chi tiêu" nào.
       Câu hỏi về VAY hoặc GỬI một gói cụ thể thì KHÔNG đi nhánh này — đó là
       chuyện chọn sản phẩm, do _product_visual lo.
    2. SO SÁNH nhiều tháng ("so với tháng 8", "6 tháng gần đây") → bảng nhiều
       tháng. Trừ khi câu nhắc "nhóm": "so sánh các nhóm tháng này" là so các
       nhóm TRONG một tháng, đưa bảng nhiều tháng vào là lạc đề.
    3. MỘT THÁNG CỤ THỂ ("tháng 6", "tháng 6/2026") → bảng đúng tháng đó. Phải xét
       TRƯỚC nhánh "tháng chung", nếu không "tháng 6" rơi vào bảng tháng hiện tại.
    4. Quý → bảng theo nhóm của quý.
    5. "tháng" chung ("tháng này") → bảng tháng hiện tại.
    6. TƯ VẤN không nêu số tiền ("nên tiết kiệm thế nào") → bảng tiền dư. Xét
       SAU các nhánh tháng để "kế hoạch chi tiêu tháng 6" vẫn ra bảng tháng 6.
    """
    # "vay 500 triệu mua ô tô" cũng khớp _GOAL_RE (có "mua ô tô" + số tiền),
    # nhưng khách đang muốn ĐI VAY chứ không phải tích cóp — gắn bảng lộ trình
    # tiết kiệm vào là khuyên ngược hẳn điều họ hỏi. Tương tự "gửi tiết kiệm
    # 100 triệu 12 tháng" là chọn gói gửi, không phải lập kế hoạch tích lũy.
    san_pham = _LOAN_RE.search(question) or _DEPOSIT_RE.search(question)
    tu_van = _GOAL_RE.search(question) is not None and not san_pham
    if tu_van:
        muc_tieu = _goal_amount(question)
        if muc_tieu:
            report = await copilot_months(months=12)
            khach = await session_customer()
            return _savings_plan_visual(muc_tieu, khach.balance, report.months)
    if _SPEND_RE.search(question):
        if _COMPARE_RE.search(question) and _MONTH_RE.search(question) and not _GROUP_RE.search(question):
            return _months_compare_visual(await copilot_months(months=6))
        mm = _SPECIFIC_MONTH_RE.search(question)
        if mm:
            period = _resolve_period(int(mm.group(1)), int(mm.group(2)) if mm.group(2) else None)
            m = await _month_report(period)
            return _month_visual(m) if m else (None, None)
        if _QUARTER_RE.search(question):
            return _quarter_visual(await copilot_quarters(quarters=8))
        if _MONTH_RE.search(question):
            return _overview_visual(await copilot_overview())
    if tu_van:
        return _surplus_visual((await copilot_months(months=6)).months)
    return None, None


@app.post("/api/copilot/chat", tags=["copilot"],
          summary="Chat với Copilot (Server-Sent Events)")
async def copilot_chat(payload: ChatRequest) -> StreamingResponse:
    """Phát câu trả lời theo từng token, lấy thẳng từ dòng chảy của agent.

    Định dạng phải khớp đúng bộ parse bên FE: mỗi dòng `data: {"token": "..."}`,
    kết thúc bằng `data: [DONE]`. FE bỏ qua dòng không bắt đầu bằng `data:`, nên
    FE không phải sửa gì khi đường này đổi từ phát lại sang phát thẳng.
    """
    # Lấy bảng + biểu đồ số liệu THẬT cho câu hỏi chi tiêu TRƯỚC, để biết câu này
    # có bảng hay không (và để middleware kịp đóng dấu X-Guardian-Data-Source
    # theo các lời gọi domain này trước khi trả StreamingResponse).
    table, data_chart = await _spending_visual(payload.message)
    # Bảng sản phẩm (lãi vay / lãi gửi) cũng do gateway dựng từ biểu lãi thật.
    # Chỉ xét khi câu hỏi không rơi vào bảng chi tiêu nào, để một câu không bao
    # giờ kèm hai bảng nói về hai chuyện khác nhau.
    grid_gw = await _product_visual(payload.message) if table is None else None

    # Câu dẫn dự phòng, chỉ dùng khi agent không phát ra chữ nào. KHÔNG bao giờ
    # đọc kịch bản viết cứng (tháng 9) khi câu hỏi đã có bảng số thật kèm theo,
    # vì kịch bản sẽ mâu thuẫn với bảng (hỏi tháng 7, kịch bản nói tháng 9). Lúc
    # đó lấy câu dẫn thẳng từ tiêu đề bảng để chữ và bảng luôn khớp nhau.
    fallback, chart = catalog.FALLBACK_REPLY, None
    if table is not None:
        fallback = f"{table.title}:"
    elif grid_gw is not None:
        fallback = f"{grid_gw.title}:"
    else:
        for pattern, text, attached in catalog.SCRIPTED_REPLIES:
            if re.search(pattern, payload.message, re.IGNORECASE):
                fallback, chart = text, attached
                break

    # Chart số thật ghi đè chart kịch bản tĩnh (nếu có).
    if data_chart is not None:
        chart = data_chart

    def sse(obj: dict) -> bytes:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()

    async def replay(text: str) -> AsyncIterator[bytes]:
        """Phát lại một câu đã có sẵn theo nhịp token, cho nhánh dự phòng.

        Tách giữ nguyên khoảng trắng, giống replayAsStream bên FE, để ghép lại
        không mất dấu cách.
        """
        for token in (t for t in re.split(r"(\s+)", text) if t):
            yield sse({"token": token})
            await asyncio.sleep(CHAT_TOKEN_DELAY_MS / 1000)

    async def stream() -> AsyncIterator[bytes]:
        # Phát THẲNG từ agent: chữ ra tới trình duyệt ngay khi mô hình sinh ra,
        # thay vì chờ trọn lần xử lý rồi mới phát lại. Bộ gỡ markdown chạy trên
        # dòng chảy vì FE render văn bản thuần.
        plain = PlainTextStreamer()
        # Giữ NGUYÊN BẢN song song với phần đã gỡ: bảng markdown bị bộ gỡ bỏ
        # khỏi phần chữ, nhưng chính những dòng đó mới dựng được bảng thật.
        raw: list[str] = []
        emitted = False
        try:
            async for piece in domain.agent_stream(
                payload.message, kind="copilot",
                session_key=domain.copilot_session_key(),
            ):
                raw.append(piece)
                clean = plain.feed(piece)
                if clean:
                    emitted = True
                    yield sse({"token": clean})
            rest = plain.close()
            if rest:
                emitted = True
                yield sse({"token": rest})
        except domain.AgentBusy:
            # Hỏi dồn khi câu trước chưa trả lời xong. Nói thành lời, đừng im
            # lặng rơi về kịch bản — người dùng cần biết mình chỉ phải đợi.
            async for chunk in replay("Mình đang trả lời câu trước, bạn đợi một chút rồi hỏi lại nhé."):
                yield chunk
            emitted = True

        if not emitted:
            # Agent chưa cấu hình hoặc hỏng trước khi kịp phát chữ nào.
            async for chunk in replay(fallback):
                yield chunk

        # Bảng sản phẩm của gateway thay cho mọi bảng agent tự kẻ: số lãi và tiền
        # trả góp phải là số của biểu lãi, không phải số LLM chép lại. Không có
        # bảng chuẩn nào thì mới chuyển thể bảng markdown agent vừa viết ra.
        if grid_gw is not None:
            grids = [grid_gw]
        elif table is None and raw:
            grids = _parse_markdown_grids("".join(raw))
        else:
            grids = []

        if table is not None:
            # Sự kiện bảng phát sau khi hết token, trước biểu đồ. FE bỏ qua object
            # không có "token" nếu chưa hỗ trợ, nên không làm hỏng client cũ.
            yield sse({"table": table.model_dump(by_alias=True)})
        for g in grids:
            yield sse({"grid": g.model_dump(by_alias=True)})
        if chart is not None:
            yield sse({"chart": chart.model_dump(by_alias=True)})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            # Chặn mọi tầng đệm trên đường đi; buffer một cái là hiệu ứng
            # streaming biến mất và câu trả lời hiện ra một cục.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/risk/assess", response_model=RiskAssessment, tags=["risk"],
          response_model_exclude_none=True,
          summary="Chấm điểm rủi ro cho một lệnh chuyển tiền")
async def assess_risk(payload: AssessRequest) -> RiskAssessment:
    """Chấm điểm bằng engine thật trong risk-scoring-service.

    Engine cần nhiều hơn số tiền: khách nào, tài khoản nào, người nhận nào, nội
    dung chuyển khoản gì. FE chỉ gửi amount nên phần còn lại lấy từ hồ sơ khách
    và fraud case — cùng bộ đầu vào mà bộ kiểm thử engine đang dùng.
    """
    global _last_decision_id
    pre = await _precheck_for(payload.amount)
    if pre and pre.get("decision_id"):
        _last_decision_id = pre["decision_id"]
    if RISK_ASSESS_DELAY_MS > 0:
        await asyncio.sleep(RISK_ASSESS_DELAY_MS / 1000)
    if not pre:
        return catalog.build_assessment(score_for_amount(payload.amount), catalog.MAIN_SCENARIO)
    scen = await domain.scenario(pre["scenario_id"]) if pre.get("scenario_id") else None
    if scen is None:
        # Engine trả kịch bản trong phần nâng mức chứ không phải trường riêng.
        escalation = (pre.get("factors") or {}).get("scenario_escalation") or {}
        sid = _scenario_id_in(escalation.get("detail") or "")
        scen = await domain.scenario(sid) if sid else None
    return mappers.map_assessment(pre, scen)


@app.get("/api/ops/metrics", response_model=OpsMetrics, tags=["ops"],
         summary="Bốn chỉ số KPI của Ops Dashboard")
async def ops_metrics() -> OpsMetrics:
    summary = await domain.ops_summary()
    decisions = await domain.risk_decisions(limit=mappers.DECISION_FETCH_LIMIT)
    if not summary or not decisions:
        return catalog.OPS_METRICS
    return mappers.map_metrics(summary, decisions.get("decisions") or [])


@app.get("/api/ops/alerts", response_model=list[ScamAlert], tags=["ops"],
         response_model_exclude_none=True,
         summary="Danh sách cảnh báo đang theo dõi")
async def ops_alerts() -> list[ScamAlert]:
    ctx = await _ops_context()
    if ctx is None:
        return [_alert_by_id(a.id) for a in catalog.OPS_ALERTS]
    rows, names, statuses = ctx
    known = ((await domain.beneficiaries(domain.current_customer_id())) or {}).get("beneficiaries") or []
    by_decision = await _scenario_names()
    return [
        mappers.map_alert(
            d, names, known, statuses,
            {"scenario_name": by_decision[d["decision_id"]]} if d["decision_id"] in by_decision else None,
        )
        for d in rows
    ]


@app.get("/api/ops/alerts/{alert_id}", response_model=ScamAlert, tags=["ops"],
         response_model_exclude_none=True,
         summary="Chi tiết một cảnh báo")
async def ops_alert(alert_id: str) -> ScamAlert:
    dec = await domain.risk_decision(alert_id)
    if not dec:
        # id dạng ALT-xxxx là của dữ liệu tạm; domain dùng UUID.
        return _alert_by_id(alert_id)
    ctx = await _ops_context()
    names, statuses = (ctx[1], ctx[2]) if ctx else ({}, dict(_decisions))
    known = ((await domain.beneficiaries(int(dec.get("customer_id") or 0))) or {}).get("beneficiaries") or []
    scen = await domain.scenario(dec["scenario_id"]) if dec.get("scenario_id") else None
    return mappers.map_alert(dec, names, known, statuses, scen)


@app.post("/api/ops/alerts/{alert_id}/decision", response_model=OkResponse, tags=["ops"],
          summary="Ghi quyết định xử lý của chuyên viên vận hành")
async def ops_decision(alert_id: str, payload: DecisionRequest) -> OkResponse:
    # id dạng UUID là quyết định thật trong database; ghi thẳng xuống
    # risk-scoring. id dạng ALT-xxxx là dữ liệu tạm, chỉ ghi trong bộ nhớ.
    dec = await domain.risk_decision(alert_id)
    if dec:
        action_taken, outcome = DOMAIN_DECISIONS[payload.decision]
        await domain.transfer_action({
            "decision_id": alert_id,
            "action_taken": action_taken,
            "outcome": outcome,
        })
        # Và đóng case tương ứng. Đóng bằng CLOSED_FRAUD/CLOSED_LEGIT khiến
        # action-feedback sinh một feedback nguồn "ops": kết luận của chuyên
        # viên quay lại thành nhãn huấn luyện, thay vì dừng ở màn hình.
        case_row = await _case_of_decision(alert_id, int(dec.get("customer_id") or 0))
        if case_row and case_row.get("case_id"):
            await domain.update_case(case_row["case_id"], CASE_DECISIONS[payload.decision],
                                     payload.note or None)
    else:
        _alert_by_id(alert_id)  # 404 nếu id không tồn tại, trước khi ghi gì
    # Bản ghi trong bộ nhớ chỉ còn phục vụ id dạng ALT-xxxx của dữ liệu tạm:
    # chúng không có dòng nào trong database để ghi vào.
    _decisions[alert_id] = payload.decision
    return OkResponse(ok=True)


# ---- Màn Home / Login --------------------------------------------------------

@app.get("/api/home", response_model=HomeContent, tags=["session"],
         summary="Nội dung động của màn Home và màn đăng nhập")
async def home_content() -> HomeContent:
    cust = await domain.customer(domain.current_customer_id())
    if not cust:
        return catalog.HOME_CONTENT
    ins = await domain.insights(domain.current_customer_id())
    return mappers.map_home(cust, ins, datetime.now(timezone(timedelta(hours=7))))


@app.post("/api/auth/login", response_model=LoginResponse, tags=["session"],
          response_model_exclude_none=True,
          summary="Đăng nhập khách hàng — xác thực thật qua identity-service")
async def auth_login(payload: LoginRequest) -> LoginResponse:
    """Xác thực thật tên đăng nhập + mật khẩu.

    Trước đây FE chỉ gọi `login()` phía client rồi chuyển màn — không có xác
    thực nào. Nay gọi identity-service so khớp bcrypt với bảng app_user; đăng
    nhập của kịch bản demo là kh100008 / 123456.

    Ba nhánh:
    - identity-service không gọi được → cho qua với hồ sơ demo (source
      "degraded"), để buổi trình bày không bị chặn bởi một service đang lỗi.
      Đây là cùng triết lý xuống cấp thấy được của cả gateway.
    - đúng mật khẩu, tài khoản ACTIVE → authenticated=true kèm hồ sơ đã che PII.
    - sai mật khẩu / tài khoản khoá → authenticated=false kèm lý do; FE hiện
      thông báo và không cho vào.
    """
    result = await domain.auth_verify(payload.username, payload.password)

    if result is None:
        domain.mark_degraded()
        # identity chết → không biết khách nào: về khách demo để luồng không gãy.
        domain.set_session_customer(None)
        return LoginResponse(authenticated=True, source="degraded", user=catalog.DEMO_LOGIN_USER)

    if result.get("authenticated"):
        user = result.get("user") or {}
        # Ghi nhớ khách của PHIÊN: từ đây /api/session/customer, /api/home,
        # /api/transfer/beneficiaries... đọc đúng dữ liệu của người vừa đăng
        # nhập (vd kh100001 → BEN của 100001), không còn neo cứng khách demo.
        cid = user.get("customer_id")
        domain.set_session_customer(int(cid) if cid else None)
        return LoginResponse(authenticated=True, source="domain", user=user)

    return LoginResponse(authenticated=False, source="domain", reason=result.get("reason", "invalid_credentials"))


@app.get("/api/session/customer", response_model=Customer, tags=["session"],
         summary="Khách hàng của phiên hiện tại")
async def session_customer() -> Customer:
    # Khách của phiên = người đăng nhập gần nhất (domain.set_session_customer
    # đặt trong auth_login); chưa đăng nhập thì là DEMO_CUSTOMER_ID.
    cust = await domain.customer(domain.current_customer_id())
    if not cust:
        return catalog.CUSTOMER
    pf = await domain.portfolio(domain.current_customer_id())
    return mappers.map_customer(cust, pf)


# ---- Màn Scam Shield ---------------------------------------------------------

@app.get("/api/transfer/pending", response_model=PendingTransfer, tags=["risk"],
         summary="Lệnh chuyển tiền đang chờ duyệt")
async def transfer_pending() -> PendingTransfer:
    """Lệnh chuyển tiền mà màn Scam Shield đang xét.

    Lấy số tiền và nội dung từ fraud case trong scam-knowledge — đó là bộ kiểm
    thử engine, nên số tiền ở đây chắc chắn đủ để engine đẩy sang mức can thiệp.
    Người nhận là tài khoản mới nên không có trong danh sách người nhận của
    khách; giữ phần mô tả người nhận trong catalog.
    """
    case = await domain.fraud_case(domain.DEMO_FRAUD_CASE_ID)
    series = ((case or {}).get("injection") or {}).get("series") or []
    if not series:
        return catalog.PENDING_TRANSFER
    return catalog.PENDING_TRANSFER.model_copy(update={"amount": int(series[0].get("amount") or 0)})


@app.get("/api/risk/explain", response_model=RiskExplain, tags=["risk"],
         response_model_exclude_none=True,
         summary='Dữ liệu màn "Vì sao chúng tôi cảnh báo?"')
async def risk_explain() -> RiskExplain:
    """Giải thích cảnh báo bằng dữ liệu thật.

    Ba khối: điểm và phân rã yếu tố từ engine, dòng thời gian hành vi của người
    nhận từ sự kiện tài khoản của khách, và kịch bản lừa đảo từ playbook.
    """
    case = await domain.fraud_case(domain.DEMO_FRAUD_CASE_ID)
    series = ((case or {}).get("injection") or {}).get("series") or []
    amount = int(series[0].get("amount") or 0) if series else catalog.SCAM_AMOUNT
    pre = await _precheck_for(amount)
    if not pre:
        return catalog.RISK_EXPLAIN

    sid = pre.get("scenario_id") or _scenario_id_in(
        ((pre.get("factors") or {}).get("scenario_escalation") or {}).get("detail") or ""
    )
    scen = await domain.scenario(sid) if sid else None
    assessment = mappers.map_assessment(pre, scen)

    # Dòng thời gian: sự kiện tài khoản ngay trước giao dịch chính là bằng chứng
    # mà yếu tố recent_context dựa vào, nên đưa đúng chúng lên màn hình.
    events = await domain.account_events(domain.current_customer_id())
    timeline = mappers.map_event_timeline((events or {}).get("events") or [], amount)

    similar = catalog.SIMILAR_SCENARIO
    if scen:
        similar = similar.model_copy(update={
            "name": scen.get("scenario_name") or similar.name,
            "description": scen.get("advice_body") or similar.description,
        })
    return RiskExplain(
        assessment=assessment,
        beneficiary_timeline=timeline or catalog.BENEFICIARY_TIMELINE,
        similar_scenario=similar,
    )


@app.get("/api/safety-center", response_model=SafetyCenter, tags=["risk"],
         summary="Trung tâm an toàn")
async def safety_center() -> SafetyCenter:
    base = catalog.SAFETY_CENTER
    case_rows = await domain.cases(limit=50)
    if case_rows is not None:
        mine = [c for c in (case_rows.get("cases") or [])
                if int(c.get("customer_id") or 0) == domain.current_customer_id()]
        history = mappers.map_safety_history(mine)
        if history:
            blocked = sum(1 for h in history if h.status == "blocked")
            base = base.model_copy(update={
                "history": history,
                "blocked_count": blocked,
                "warned_count": len(history),
                "reported_count": sum(1 for h in history if h.status == "processing"),
            })

    if not _protections:
        return base
    # Trả về trạng thái khách đã bật/tắt, không phải giá trị mặc định.
    layers = [p.model_copy(update={"enabled": _protections.get(p.key, p.enabled)})
              for p in base.protections]
    return base.model_copy(update={"protections": layers})


@app.patch("/api/safety-center/protections/{key}", response_model=OkResponse, tags=["risk"],
           summary="Bật/tắt một lớp bảo vệ")
async def toggle_protection(key: str, payload: ProtectionToggleRequest) -> OkResponse:
    if all(p.key != key for p in catalog.SAFETY_CENTER.protections):
        raise HTTPException(status_code=404, detail=f"Không có lớp bảo vệ '{key}'")
    _protections[key] = payload.enabled
    return OkResponse(ok=True)


@app.post("/api/transfer/action", response_model=TransferActionResponse, tags=["risk"],
          summary="Ghi hành động của khách sau cảnh báo Scam Shield")
async def transfer_action(payload: TransferActionRequest) -> TransferActionResponse:
    """Khép vòng luồng Scam Shield.

    Trước đây ba nút Huỷ / Vẫn chuyển / Báo cáo chỉ đổi state trong trình duyệt,
    backend không hề biết. Nay mỗi hành động cập nhật trạng thái case và thêm
    một bước vào dòng thời gian, nên chuyên viên vận hành nhìn thấy ngay.
    """
    status, step_label, message = catalog.CUSTOMER_ACTIONS[payload.action]

    # Ghi xuống risk-scoring để vòng đời quyết định khép lại trong database,
    # không chỉ trong bộ nhớ gateway. Cần decision_id của lần chấm gần nhất —
    # chưa chấm lần nào thì chỉ ghi nhận cục bộ.
    # Màn Guardian gửi kèm decision_id của đúng lệnh đang xét; client cũ không
    # gửi thì rơi về lần chấm gần nhất.
    quyet_dinh = payload.decision_id or _last_decision_id
    if quyet_dinh:
        action_taken, outcome = DOMAIN_ACTIONS[payload.action]
        await domain.transfer_action({
            "decision_id": quyet_dinh,
            "action_taken": action_taken,
            "outcome": outcome,
        })
        _decisions[quyet_dinh] = status

    _decisions[catalog.CUSTOMER_CASE_ID] = status
    _customer_steps.append(
        CaseTimelineStep(id=f"ct-cust-{len(_customer_steps) + 1}", time=_now_hms(), label=step_label, done=True)
    )
    return TransferActionResponse(ok=True, case_status=status, message=message)


# ---- Luồng chuyển tiền: favorite bỏ qua Scam Shield, stk mới qua agent -------

def _is_trusted(resolve: dict) -> bool:
    """stk quen: đã có trong danh bạ (known), không phải mới, không bị nghi ngờ.
    Đủ ba điều này thì chuyển thẳng, không cần Scam Shield."""
    return (bool(resolve.get("known"))
            and not resolve.get("is_new", True)
            and resolve.get("status") != "SUSPECTED")


async def _collect_signals(bank_code: str, account_no: str, amount: int, note: str) -> ScamShieldSignals:
    """Gom tín hiệu gian lận: hồ sơ người nhận + đối chiếu playbook lừa đảo."""
    resolve = await domain.resolve_beneficiary(domain.current_customer_id(), bank_code, account_no) or {}
    match = await domain.scam_match({
        "persona": "SENIOR", "memo": note or "", "amount": amount,
        "is_new_beneficiary": bool(resolve.get("is_new", True)),
    }) or {}
    scen = match.get("scenario") or {}
    return ScamShieldSignals(
        bank_code=bank_code, account_no=account_no,
        account_masked=resolve.get("account_masked") or account_no,
        known=bool(resolve.get("known")), is_new=bool(resolve.get("is_new", True)),
        relationship=resolve.get("relationship") or "UNKNOWN",
        status=resolve.get("status") or "ACTIVE",
        age_days=int(resolve.get("age_days") or 0),
        tx_count=int(resolve.get("tx_count") or 0),
        amount=amount, note=note or "",
        scenario_match=(scen.get("scenario_name") if match.get("matched") else None),
    )


_LEVEL_TAGS = {"NGUY_HIEM": "danger", "NGHI_NGO": "suspect", "AN_TOAN": "safe"}
_LEVEL_TITLES = {"danger": "Cảnh báo: dấu hiệu lừa đảo",
                 "suspect": "Cần thận trọng", "safe": "Chưa thấy dấu hiệu bất thường"}


def _parse_verdict(text: str) -> ScamShieldVerdict | None:
    """Bóc verdict từ câu trả lời agent: dòng ĐẦU là NGUY_HIEM|NGHI_NGO|AN_TOAN,
    các dòng sau là lý do (gạch đầu dòng) + khuyến nghị."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    level = next((lv for tag, lv in _LEVEL_TAGS.items() if tag in lines[0].upper()), None)
    if level is None:
        return None
    body = lines[1:] or lines
    reasons = [re.sub(r"^[-•*]\s*", "", ln) for ln in body if ln.lstrip()[:1] in "-•*"]
    recommendation = next((ln for ln in body if "khuyến" in ln.lower() or "không nên" in ln.lower()), "")
    summary = " ".join(ln for ln in body if ln.lstrip()[:1] not in "-•*").strip()[:400]
    return ScamShieldVerdict(
        level=level, title=_LEVEL_TITLES[level], summary=summary or _LEVEL_TITLES[level],
        reasons=reasons or ([summary] if summary else []),
        recommendation=recommendation, source="agent",
    )


def _fallback_verdict(s: ScamShieldSignals) -> ScamShieldVerdict:
    """Agent lỗi → suy verdict từ tín hiệu (deterministic) để luồng không kẹt."""
    reasons: list[str] = []
    if s.status == "SUSPECTED":
        reasons.append("Tài khoản người nhận đã bị báo cáo nghi ngờ lừa đảo.")
    if s.is_new:
        reasons.append("Tài khoản mới, chưa từng giao dịch với bạn.")
    if s.age_days == 0:
        reasons.append("Tài khoản vừa được mở gần đây.")
    if s.scenario_match:
        reasons.append(f"Khớp kịch bản lừa đảo: {s.scenario_match}.")
    if s.amount >= 50_000_000:
        reasons.append(f"Số tiền lớn: {s.amount:,} ₫.".replace(",", "."))
    danger = s.status == "SUSPECTED" or bool(s.scenario_match)
    level = "danger" if danger else ("suspect" if s.is_new else "safe")
    rec = ("Không nên chuyển. Hãy gọi lại người nhận qua số bạn tự biết để xác minh."
           if level == "danger" else
           "Xác minh kỹ người nhận trước khi chuyển." if level == "suspect" else
           "Có thể tiếp tục, vẫn nên kiểm tra lại thông tin người nhận.")
    return ScamShieldVerdict(
        level=level, title=_LEVEL_TITLES[level],
        summary=("Đây là tài khoản mới, hệ thống ghi nhận các dấu hiệu dưới đây."
                 if reasons else "Chưa thấy dấu hiệu bất thường rõ rệt."),
        reasons=reasons, recommendation=rec, source="fallback",
    )


async def _scamshield_verdict(bank_code: str, account_no: str, amount: int, note: str,
                              signals: ScamShieldSignals) -> ScamShieldVerdict:
    """Hỏi agent Scam Shield; agent lỗi thì suy từ tín hiệu."""
    question = (
        f"Khách chuẩn bị chuyển {amount:,} đồng tới tài khoản {account_no} tại ngân hàng "
        f"{bank_code}, nội dung chuyển khoản: \"{note}\". Hãy dùng công cụ kiểm tra dấu hiệu "
        "lừa đảo cho tài khoản này rồi kết luận."
    ).replace(",", ".")
    answer = await domain.agent_answer(
        question, agent_id=domain.SCAMSHIELD_AGENT_ID, kind="shield_advice",
        # _last_decision_id là lệnh chuyển vừa được chấm điểm; có nó thì dòng
        # nhật ký mở thẳng được case tương ứng bên Ops.
        decision_id=_last_decision_id,
    )
    if answer and (parsed := _parse_verdict(answer)):
        return parsed
    return _fallback_verdict(signals)


@app.get("/api/transfer/beneficiaries", response_model=list[TransferBeneficiary], tags=["risk"],
         summary="Danh bạ người thụ hưởng đã lưu (favorite)")
async def transfer_beneficiaries() -> list[TransferBeneficiary]:
    # Danh bạ của chính chủ trên app: hiện tên + số TK đầy đủ, không che.
    data = await domain.beneficiaries(domain.current_customer_id(), include_full=True)
    rows = (data or {}).get("beneficiaries") if isinstance(data, dict) else data
    if not rows:
        return catalog.TRANSFER_BENEFICIARIES
    return [
        TransferBeneficiary(
            id=str(b.get("beneficiary_id")),
            name=b.get("name") or b.get("name_masked") or "Người nhận",
            bank=b.get("bank_code") or "",
            account=b.get("account_no") or b.get("account_masked") or "",
            relationship=b.get("relationship") or "UNKNOWN",
            trusted=(not b.get("is_new", True)) and b.get("status") == "ACTIVE",
        )
        for b in rows
    ]


@app.post("/api/transfer/execute", response_model=TransferExecuteResponse, tags=["risk"],
          response_model_exclude_none=True,
          summary="Ghi giao dịch chuyển tiền đã xác thực PIN vào transaction_history")
async def transfer_execute(payload: TransferExecuteRequest) -> TransferExecuteResponse:
    """FE gọi sau khi khách nhập đúng PIN. Ghi bản ghi OUT/POSTED để màn Lịch sử
    giao dịch truy vấn lại được (bảng transaction_history, cửa sổ 3 tháng)."""
    cust_id = domain.current_customer_id()
    pf = await domain.portfolio(cust_id)
    accounts = (pf or {}).get("accounts") or []
    if not accounts:
        return TransferExecuteResponse(ok=False)
    resolve = await domain.resolve_beneficiary(cust_id, payload.bank_code, payload.account_no) or {}
    account_id = int(accounts[0]["account_id"])
    # HẠCH TOÁN THẬT: ghi nợ (DEBIT) tài khoản nguồn trước khi ghi bút toán —
    # số dư working_balance giảm đúng số tiền chuyển. Service hạch toán lỗi thì
    # vẫn ghi bản ghi (degraded, balance_after trống) để demo không bị chặn.
    posting = await domain.account_posting(account_id, "DEBIT", payload.amount)
    # Tên người nhận không có cột riêng trong transaction_history: nhét vào đầu
    # description theo dạng "→ TÊN: nội dung" để history bóc lại được với stk
    # ngoài danh bạ (danh bạ FE local không trùng danh bạ DB).
    note = payload.note or ""
    desc = f"→{payload.holder_name}: {note}" if payload.holder_name else note
    created = await domain.create_transaction({
        "customer_id": cust_id,
        "account_id": account_id,
        "direction": "OUT",
        "amount": payload.amount,
        "balance_after": (posting or {}).get("balance_after"),
        "beneficiary_id": resolve.get("beneficiary_id"),
        "beneficiary_bank_code": payload.bank_code,
        # Cột vốn chứa bản masked; màn lịch sử cần số đầy đủ nên lưu full —
        # nhất quán với chủ trương bỏ che dữ liệu hiển thị UI.
        "beneficiary_account_masked": payload.account_no,
        "transaction_type": "FT",
        "category": "TRANSFER_P2P",
        "transaction_description": desc,
        "channel": "MOBILE",
        "status": "POSTED",
    })
    if not created:
        return TransferExecuteResponse(ok=False)
    return TransferExecuteResponse(ok=True, transaction_id=created.get("transaction_id"))


@app.get("/api/transfer/history", response_model=list[TransferHistoryItem], tags=["risk"],
         summary="Lịch sử chuyển tiền (FT, chiều OUT) trong 3 tháng gần nhất")
async def transfer_history() -> list[TransferHistoryItem]:
    cust_id = domain.current_customer_id()
    date_from = (datetime.now(timezone(timedelta(hours=7))) - timedelta(days=90)).strftime("%Y%m%d")
    data = await domain.transactions(cust_id, date_from=date_from, direction="OUT")
    rows = (data or {}).get("transactions") or []
    bens = await domain.beneficiaries(cust_id, include_full=True)
    by_id = {b.get("beneficiary_id"): b for b in (bens or {}).get("beneficiaries") or []}

    items: list[TransferHistoryItem] = []
    for r in rows:
        if r.get("transaction_type") != "FT":
            continue  # màn này chỉ hiện giao dịch chuyển tiền
        desc = r.get("description") or ""
        name, note = "", desc
        if desc.startswith("→") and ":" in desc:
            head, tail = desc[1:].split(":", 1)
            name, note = head.strip(), tail.strip()
        b = by_id.get(r.get("beneficiary_id"))
        if b:
            name = b.get("name") or b.get("name_masked") or name
            bank = b.get("bank_code") or r.get("beneficiary_bank_code") or ""
            account = b.get("account_no") or b.get("account_masked") or r.get("beneficiary_account_masked") or ""
        else:
            bank = r.get("beneficiary_bank_code") or ""
            account = r.get("beneficiary_account_masked") or ""
        # _tx_payload của transaction-service trả giờ dưới key "time" (HHMMSS)
        t = (r.get("time") or "000000").ljust(6, "0")
        items.append(TransferHistoryItem(
            id=str(r.get("transaction_id")),
            datetime=f"{r.get('date')}T{t[0:2]}:{t[2:4]}:{t[4:6]}",
            name=name or account or "Người nhận",
            bank=bank,
            account=account,
            amount=int(r.get("amount") or 0),
            note=note,
            status=r.get("status") or "POSTED",
        ))
    return items


@app.get("/api/scamshield/signals", response_model=ScamShieldSignals, tags=["risk"],
         summary="Tín hiệu gian lận của một lệnh chuyển — công cụ cho agent Scam Shield")
async def scamshield_signals(bank_code: str, account_no: str, amount: int, note: str = "") -> ScamShieldSignals:
    return await _collect_signals(bank_code, account_no, amount, note)


@app.post("/api/transfer/precheck", response_model=TransferPrecheckResponse, tags=["risk"],
          response_model_exclude_none=True,
          summary="Quyết định: stk quen (bỏ qua Scam Shield) hay stk mới (agent Scam Shield check)")
async def transfer_precheck(payload: TransferPrecheckRequest) -> TransferPrecheckResponse:
    """Chấm điểm lệnh chuyển và trả về mức Guardian cho FE rẽ nhánh.

    Ba mức theo wireframe (ngưỡng của engine): <40 pass · 40–74 soft_warn ·
    >=75 intervene. FE dùng `level`:
      - pass      → đi thẳng sang xác nhận, kèm "Đã chuyển N lần" cho yên tâm.
      - soft_warn → hiện banner inline ngay trên màn nhập lệnh, KHÔNG thêm bước.
      - intervene → chèn màn Guardian (/transfer/guardian) trước khi xác thực.

    KHÔNG gọi LLM ở đây: wireframe yêu cầu bước này dưới 300 ms, mà một lượt
    agent mất 25–35 giây. Lời của LLM chỉ xuất hiện ở lượt 2 của màn Guardian,
    và ngay cả ở đó cũng bị chặn thời gian.
    """
    global _last_decision_id
    resolve = await domain.resolve_beneficiary(
        domain.current_customer_id(), payload.bank_code, payload.account_no) or {}
    # FE gửi sẵn số TK đầy đủ; ưu tiên nó để màn xác nhận không bị che.
    masked = payload.account_no or resolve.get("account_masked") or ""
    name = payload.holder_name or masked
    trusted = _is_trusted(resolve)

    eng = await _engine_precheck(payload)
    # Engine chết thì vẫn phải quyết được: stk quen cho qua, stk lạ cảnh báo nhẹ.
    # Tuyệt đối không chặn chuyển tiền chỉ vì Guardian lỗi.
    level = eng.get("level") or ("pass" if trusted else "soft_warn")
    decision_id = str(eng.get("decision_id") or "")
    if decision_id:
        _last_decision_id = decision_id
    kich_ban = eng.get("scenario")
    return TransferPrecheckResponse(
        requires_review=(level == "intervene"),
        trusted=trusted,
        is_new=bool(resolve.get("is_new", not trusted)),
        beneficiary_name=name,
        beneficiary_bank=payload.bank_code,
        beneficiary_account=masked,
        score=int(eng.get("score") or 0),
        level=level,
        top_factors=_factor_reasons(eng),
        template_text=eng.get("template_text") or "",
        decision_id=decision_id,
        question=eng.get("question"),
        options=list(eng.get("options") or []),
        scenario_id=(kich_ban or {}).get("scenario_id") if isinstance(kich_ban, dict) else None,
        tx_count=int(resolve.get("tx_count") or 0),
    )


# ---- Guardian 3 mức: pass · soft-warn · intervene (wireframe màn Transfer) ----

# Tên yếu tố của engine → nhãn ngắn cho người đọc. Phần mô tả lấy nguyên
# `detail` engine sinh ra, nên chữ luôn khớp điểm đã chấm.
_FACTOR_LABEL = {
    "amount_deviation": "Số tiền",
    "new_beneficiary": "Người nhận",
    "time_of_day": "Thời điểm",
    "behavior_drift": "Hành vi",
    "relationship_history": "Quan hệ",
    "recent_context": "Bối cảnh",
}

# Bốn hành động của wireframe. Nút nào được khuyến nghị là do engine quyết
# (recommended_action), KHÔNG phải LLM.
_GUARDIAN_ACTIONS = [
    ("hold", "Khóa tạm 24 giờ"),
    ("cancel", "Hủy giao dịch"),
    ("contact", "Gọi MSB 1900 6083"),
    ("continue", "Vẫn tiếp tục"),
]


def _factor_reasons(raw: dict, limit: int = 3) -> list[str]:
    """top_factors (tên yếu tố) → câu người đọc hiểu được.

    top_factors đến từ hai nguồn với hai kiểu khác nhau: response của engine đã
    tách sẵn thành list, còn cột trong risk_decision là chuỗi ngăn bởi dấu phẩy.
    Duyệt thẳng chuỗi sẽ ra từng KÝ TỰ và không khớp yếu tố nào — màn Guardian
    mở bằng deep-link sẽ trắng phần lý do.
    """
    factors = raw.get("factors") or {}
    tho = raw.get("top_factors") or []
    if isinstance(tho, str):
        tho = [x.strip() for x in tho.split(",") if x.strip() and x.strip() != "none"]
    out: list[str] = []
    for ten in tho[:limit]:
        chi_tiet = (factors.get(ten) or {}).get("detail")
        if chi_tiet:
            out.append(f"{_FACTOR_LABEL.get(ten, ten)}: {chi_tiet}")
    return out


async def _engine_precheck(payload: TransferPrecheckRequest) -> dict:
    """Chấm điểm CHÍNH lệnh khách đang nhập.

    Khác _precheck_for (dựng từ fraud case mẫu để minh hoạ màn Ops): ở đây số
    tiền, người nhận và nội dung là của khách, nên điểm trả về mới là điểm của
    giao dịch này.
    """
    khach = domain.current_customer_id()
    pf = await domain.portfolio(khach)
    accounts = (pf or {}).get("accounts") or []
    if not accounts:
        return {}
    return await domain.precheck({
        "customer_id": khach,
        "account_id": accounts[0]["account_id"],
        "amount": payload.amount,
        "beneficiary_bank_code": (payload.bank_code or "").upper(),
        "beneficiary_account_no": (payload.account_no or "").replace(" ", ""),
        "memo": payload.note,
    }) or {}


@app.get("/api/transfer/intervene/{decision_id}", response_model=InterveneDetail,
         tags=["risk"], response_model_exclude_none=True,
         summary="Lượt 1 màn Guardian: lý do dừng và câu hỏi cho khách")
async def transfer_intervene_detail(decision_id: str) -> InterveneDetail:
    """Đọc lại quyết định đã chấm, KHÔNG chấm lại.

    Chấm lại sẽ ra điểm khác (thời điểm đổi, sự kiện 60 phút trôi đi) và màn
    lượt 1 sẽ lệch điểm đã hiện ở màn nhập lệnh.
    """
    row = await domain.risk_decision(decision_id)
    if not row:
        raise HTTPException(404, f"không tìm thấy quyết định {decision_id}")
    snap = row.get("tx_snapshot") or {}
    return InterveneDetail(
        decision_id=str(row["decision_id"]),
        score=int(row.get("score") or 0),
        level=row.get("level") or "intervene",
        amount=int(snap.get("amount") or 0),
        beneficiary_label=f"{snap.get('bank_code') or ''} {snap.get('beneficiary_masked') or ''}".strip(),
        reasons=_factor_reasons(row),
        question=row.get("question") or "Có ai đang hướng dẫn bạn thực hiện giao dịch này không?",
        options=list(row.get("options") or [
            "Có, đang có người hướng dẫn", "Không, tôi tự chuyển", "Tôi không chắc"]),
        scenario_id=row.get("scenario_id"),
    )


async def _guardian_advice(row: dict, kb: dict, chon: str, them: str | None) -> tuple[str, str, str]:
    """(tiêu đề, nội dung, nguồn) cho lượt 2.

    Playbook là nguồn chính vì khuyến cáo đã được viết sẵn cho đúng kịch bản.
    Agent chỉ diễn giải lại cho hợp câu trả lời của khách và bị CHẶN THỜI GIAN:
    wireframe yêu cầu màn này hiện trong 1–3 giây, chờ LLM 30 giây là hỏng buổi
    demo — hết giờ thì dùng nguyên lời playbook.
    """
    tieu_de = kb.get("advice_title") or "Giao dịch này có dấu hiệu bất thường"
    noi_dung = kb.get("advice_body") or row.get("template_text") or (
        "Hãy dừng lại và xác minh trực tiếp với người nhận trước khi chuyển tiền.")
    if not domain.agent_configured(domain.SCAMSHIELD_AGENT_ID):
        return tieu_de, noi_dung, "playbook"
    hoi = (
        f'Khách vừa chọn: "{chon}".\n'
        + (f"Khách nói thêm: {them}\n" if them else "")
        + f"Kịch bản nghi ngờ: {tieu_de}. Khuyến cáo gốc: {noi_dung}\n"
        "Viết lại khuyến cáo trên thành 2-3 câu tiếng Việt, xưng hô thân mật, bám đúng "
        "lựa chọn của khách. Chỉ trả về đoạn văn, không tiêu đề, không bảng."
    )
    try:
        loi = await asyncio.wait_for(
            domain.agent_answer(hoi, domain.SCAMSHIELD_AGENT_ID), timeout=8)
    except Exception:
        loi = None
    if loi:
        return tieu_de, _strip_markdown_for_plain(loi) or noi_dung, "agent"
    return tieu_de, noi_dung, "playbook"


@app.post("/api/transfer/intervene", response_model=InterveneAdvice, tags=["risk"],
          summary="Lượt 2 màn Guardian: khuyến cáo và bốn hành động")
async def transfer_intervene(payload: InterveneRequest) -> InterveneAdvice:
    """Ghi câu trả lời của khách, trả khuyến cáo và bốn nút để khách tự quyết."""
    row = await domain.risk_decision(payload.decision_id)
    if not row:
        raise HTTPException(404, f"không tìm thấy quyết định {payload.decision_id}")
    kb = {}
    if row.get("scenario_id"):
        kb = await domain.scam_scenario(row["scenario_id"]) or {}
    tieu_de, noi_dung, nguon = await _guardian_advice(
        row, kb, payload.selected_option, payload.free_text)
    khuyen = row.get("recommended_action") or kb.get("recommended_action") or "hold"
    if khuyen not in {k for k, _ in _GUARDIAN_ACTIONS}:
        khuyen = "hold"

    # Ghi xuống database để vòng đời quyết định khép lại ở domain, không chỉ
    # trong trình duyệt — màn Ops đọc chính dòng này.
    await domain.intervene({
        "decision_id": payload.decision_id,
        "selected_option": payload.selected_option,
        "free_text": payload.free_text,
        "advice_title": tieu_de,
        "advice_body": noi_dung,
    })
    return InterveneAdvice(
        decision_id=payload.decision_id,
        selected_option=payload.selected_option,
        advice_title=tieu_de,
        advice_body=noi_dung,
        recommended_action=khuyen,
        actions=[GuardianAction(key=k, label=nhan, recommended=(k == khuyen))
                 for k, nhan in _GUARDIAN_ACTIONS],
        source=nguon,
    )


# ---- Màn Ops -----------------------------------------------------------------

@app.post("/api/ops/login", response_model=OpsLoginResult, tags=["ops"],
          response_model_exclude_none=True,
          summary="Đăng nhập nội bộ cho Ops — chỉ tài khoản vận hành mới vào được")
async def ops_login(payload: OpsLoginRequest) -> OpsLoginResult:
    """Xác thực rồi gate bằng quyền vận hành thật, không phải chỉ so `role`.

    Khác `POST /api/auth/login` (đăng nhập khách) ở hai điểm cố ý:

    1. KHÔNG CÓ NHÁNH "DEGRADED". identity-service không gọi được thì không ai
       vào Ops được — trả lỗi 503 để FE báo "chưa kết nối được", chứ không cho
       qua bằng hồ sơ demo như màn khách (màn khách ưu tiên không chặn buổi
       trình bày; màn nội bộ ưu tiên không mở cửa khi không xác minh được).
    2. TỪ CHỐI TÀI KHOẢN KHÔNG CÓ QUYỀN VẬN HÀNH. Đúng mật khẩu chưa đủ: còn
       phải có `OPS_REQUIRED_PERMISSION` trong quyền hiệu lực của tài khoản
       (tra qua `domain.user_permissions`), vì `role` chỉ là mã CUSTOMER/ADMIN
       còn quyền là thứ nghiệp vụ khai trong app_role.

    Giữ nguyên quyết định cũ: không gọi `/users/{id}/login-attempt` (xem
    `domain.auth_verify`) — gõ sai vài lần không được khoá tài khoản demo.
    """
    result = await domain.auth_verify(payload.username, payload.password)
    if result is None:
        raise HTTPException(status_code=503,
                            detail="Không gọi được hệ thống danh tính (identity-service).")

    if not result.get("authenticated"):
        return OpsLoginResult(authenticated=False, reason=result.get("reason") or "invalid_credentials")

    user = result.get("user") or {}
    user_id = user.get("user_id")
    perms = await domain.user_permissions(user_id) if user_id is not None else None
    if perms is None:
        raise HTTPException(status_code=503,
                            detail="Không gọi được hệ thống danh tính (identity-service).")

    granted = perms.get("effective_permissions") or []
    if not perms.get("role_defined") or OPS_REQUIRED_PERMISSION not in granted:
        return OpsLoginResult(authenticated=False, reason="not_backoffice")

    return OpsLoginResult(authenticated=True, operator=_ops_operator_from_user(user))


@app.get("/api/ops/session", response_model=OpsSession, tags=["ops"],
         summary="Chuyên viên đang trực và tình trạng hệ thống")
async def ops_session(username: str | None = None) -> OpsSession:
    """Chuyên viên đang trực, tình trạng hệ thống và giờ hiện tại.

    `username` đến từ phiên Ops đã đăng nhập ở FE (`src/lib/ops-auth.ts`). Có
    thì tra identity-service để hiện đúng người đang trực; không có (hoặc tra
    hỏng) thì giữ hằng số catalog như trước phase đăng nhập nội bộ.
    """
    if not domain.DOMAIN_ENABLED:
        domain.mark_degraded()
        return catalog.OPS_SESSION
    risk, scam, feedback = await asyncio.gather(
        domain.health(domain.RISK_SCORING_URL),
        domain.health(domain.SCAM_KNOWLEDGE_URL),
        domain.health(domain.ACTION_FEEDBACK_URL),
    )
    operator = catalog.OPS_SESSION.operator
    if username:
        user = await domain.user_by_username(username)
        if user:
            operator = _ops_operator_from_user(user)
    return OpsSession(
        operator=operator,
        system_status=mappers.map_system_status(
            {"Risk Engine": risk, "Tri thức lừa đảo": scam, "Ghi nhận & phản hồi": feedback},
            domain.agent_configured(domain.SCAMSHIELD_AGENT_ID) or domain.agent_configured(),
        ),
        now_label=_vi_now_label(),
    )


@app.get("/api/ops/alerts/{alert_id}/detail", response_model=CaseDetail, tags=["ops"],
         summary="Chi tiết giao dịch, hồ sơ khách và thông tin mô hình của case")
async def ops_alert_detail(alert_id: str) -> CaseDetail:
    """Trước đây hàm này trả hằng số cho MỌI case, và id dạng UUID thật luôn 404
    vì `_alert_by_id` chỉ tra trong dữ liệu tạm."""
    dec = await domain.risk_decision(alert_id)
    if not dec:
        _alert_by_id(alert_id)  # 404 nếu id không tồn tại ở cả dữ liệu tạm
        domain.mark_degraded()
        return catalog.CASE_DETAIL
    customer_id = int(dec.get("customer_id") or 0)
    cust, info, history, case_row = await asyncio.gather(
        domain.customer(customer_id),
        domain.risk_info(),
        domain.customer_decisions(customer_id),
        _case_of_decision(alert_id, customer_id),
    )
    return mappers.map_case_detail(
        dec, cust, case_row, info,
        (history or {}).get("decisions") or [],
        catalog.CASE_DETAIL, _now_vn(),
    )


@app.get("/api/ops/dashboard", response_model=OpsDashboard, tags=["ops"],
         summary="Phần bổ trợ của Ops Dashboard (delta, biểu đồ, đầu vào mô hình)")
async def ops_dashboard() -> OpsDashboard:
    rows = await _decisions_with_scenario_names()
    if rows is None:
        domain.mark_degraded()
        return catalog.OPS_DASHBOARD
    return mappers.map_dashboard(
        rows,
        catalog.OPS_DASHBOARD.model_inputs,  # mô tả đầu vào mô hình, không phải dữ liệu
    )


@app.get("/api/ops/alerts/{alert_id}/timeline", response_model=list[CaseTimelineStep], tags=["ops"],
         summary="Dòng thời gian xử lý của một case")
async def ops_alert_timeline(alert_id: str) -> list[CaseTimelineStep]:
    """Trước đây trả đúng năm bước viết cứng cho mọi case, bất kể chuyện gì đã
    thật sự xảy ra."""
    dec = await domain.risk_decision(alert_id)
    if not dec:
        _alert_by_id(alert_id)  # 404 nếu id không tồn tại ở cả dữ liệu tạm
        domain.mark_degraded()
        if alert_id != catalog.CUSTOMER_CASE_ID or not _customer_steps:
            return catalog.CASE_TIMELINE
        # Chèn bước của khách TRƯỚC bước cuối ("chờ quyết định xử lý"), vì bước
        # đó luôn là mốc chưa hoàn thành và phải nằm ở cuối danh sách.
        return [*catalog.CASE_TIMELINE[:-1], *_customer_steps, catalog.CASE_TIMELINE[-1]]
    customer_id = int(dec.get("customer_id") or 0)
    traces, case_row = await asyncio.gather(
        domain.llm_traces(decision_id=alert_id, limit=20),
        _case_of_decision(alert_id, customer_id),
    )
    return mappers.map_case_timeline(dec, case_row, (traces or {}).get("traces") or [])


@app.get("/api/ops/cases", response_model=list[OpsCase], tags=["ops"],
         response_model_exclude_none=True,
         summary="Danh sách case vận hành")
async def ops_cases(status: str | None = None) -> list[OpsCase]:
    """Case mở cho chuyên viên, từ bảng guardian_case.

    Lọc theo trạng thái làm ở đây chứ không đẩy xuống action-feedback: danh sách
    case của một ngày demo rất ngắn, và lọc tại chỗ thì đổi bộ lọc không phải
    gọi mạng lại.
    """
    rows = await domain.cases(limit=100)
    if not rows:
        domain.mark_degraded()
        cases = catalog.OPS_CASES
    else:
        people = await domain.customers()
        names = {
            int(c["customer_id"]): c.get("name_masked") or "—"
            for c in ((people or {}).get("customers") or [])
        }
        scen = await domain.scenarios()
        scenario_names = {
            s["scenario_id"]: s["scenario_name"]
            for s in ((scen or {}).get("scenarios") or [])
        }
        cases = mappers.map_ops_cases(rows.get("cases") or [], names, scenario_names)
    return [c for c in cases if not status or c.status == status]


@app.get("/api/ops/scenarios", response_model=list[OpsScenario], tags=["ops"],
         summary="Playbook kịch bản lừa đảo đang áp dụng")
async def ops_scenarios() -> list[OpsScenario]:
    rows = await domain.scenarios()
    if not rows:
        domain.mark_degraded()
        return catalog.OPS_SCENARIOS
    # Đếm từ chính tập quyết định mà biểu đồ trên Ops Dashboard dùng, nên hai màn
    # không bao giờ nói hai con số khác nhau cho cùng một kịch bản. Khác ở chỗ
    # màn này lấy đủ, không chỉ 5 kịch bản dẫn đầu.
    counts = _scenario_counts(await _decisions_with_scenario_names() or [])
    return mappers.map_ops_scenarios(rows.get("scenarios") or [], counts)


@app.get("/api/ops/model", response_model=OpsModelConfig, tags=["ops"],
         summary="Ngưỡng và trần điểm của risk engine")
async def ops_model() -> OpsModelConfig:
    """Chỉ đọc. Ngưỡng do risk-scoring-service giữ; đổi ở đây không có tác dụng."""
    info = await domain.risk_info()
    if not info or not info.get("factor_caps"):
        domain.mark_degraded()
        return catalog.OPS_MODEL
    return mappers.map_ops_model(info, catalog.OPS_DASHBOARD.model_inputs)


@app.get("/api/ops/audit", response_model=OpsAuditLog, tags=["ops"],
         response_model_exclude_none=True,
         summary="Nhật ký mọi lượt gọi LLM")
async def ops_audit(agent: str | None = None, status: str | None = None) -> OpsAuditLog:
    """Bằng chứng "AI kiểm toán được": bản ghi timeout/error vẫn còn nguyên nên
    tỷ lệ dùng bản dự phòng là con số thật, không phải con số tự khai."""
    traces = await domain.llm_traces(agent=agent, status=status, limit=100)
    stats = await domain.llm_trace_stats()
    if not traces or not stats:
        domain.mark_degraded()
        # Lọc cả dữ liệu dự phòng: bộ lọc trên màn hình phải có tác dụng kể cả
        # khi domain chưa gọi được, nếu không người dùng tưởng màn hình hỏng.
        return mappers.filter_audit(catalog.OPS_AUDIT, status=status)
    return mappers.map_ops_audit(traces.get("traces") or [], stats)


# ---- Gợi ý câu hỏi cho chat --------------------------------------------------

@app.get("/api/copilot/quarters", response_model=QuarterlyReport, tags=["copilot"],
         response_model_exclude_none=False,
         summary="Thu chi theo quý, phân rã theo nhóm chi tiêu")
async def copilot_quarters(quarters: int = 8) -> QuarterlyReport:
    """Dữ liệu cho màn quản lý tài chính cá nhân và cho agent.

    Cùng một endpoint phục vụ hai bên: web app vẽ biểu đồ, còn agent gọi nó như
    một tool khi khách hỏi về xu hướng chi tiêu. Một nguồn số liệu duy nhất nên
    hai bên không bao giờ nói hai con số khác nhau.
    """
    payload = await domain.quarterly_summary(domain.current_customer_id(), quarters)
    mapped = mappers.map_quarterly(payload) if payload else None
    if mapped is None:
        if payload is not None:
            domain.mark_degraded()
        return catalog.QUARTERLY_REPORT
    return mapped


@app.get("/api/copilot/month", response_model=MonthSummary, tags=["copilot"],
         summary="Chi tiêu của ĐÚNG một tháng cụ thể (period = YYYYMM)")
async def copilot_month(period: str) -> MonthSummary:
    """Một tháng cụ thể theo nhóm — cho câu hỏi "chi tiêu tháng 6".

    period là YYYYMM (vd 202606 = tháng 6/2026). Cùng nguồn với tool agent
    `month-detail`: agent hỏi tháng nào thì lấy đúng tháng đó, và màn chat đính
    bảng đúng tháng đó — hết cảnh hỏi tháng 6 mà hiện bảng tháng 9.
    """
    if not re.fullmatch(r"\d{6}", period or ""):
        raise HTTPException(status_code=422, detail="period phải dạng YYYYMM, ví dụ 202606")
    m = await _month_report(period)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Chưa có dữ liệu chi tiêu cho tháng {period}")
    return m


@app.get("/api/copilot/months", response_model=MonthlyReport, tags=["copilot"],
         summary="So sánh chi tiêu giữa các tháng gần nhất")
async def copilot_months(months: int = 6) -> MonthlyReport:
    """Nhiều tháng gần nhất kèm thay đổi so tháng trước — cho câu hỏi so sánh.

    Cùng nguồn với tool `monthly-comparison` của agent: khách hỏi "tháng này so
    với tháng 8 thế nào" thì agent gọi tool này, còn màn chat dựng bảng so sánh
    từ đúng dữ liệu ấy. Một nguồn số, hai bên không lệch.
    """
    payload = await domain.monthly_comparison(domain.current_customer_id(), months)
    mapped = mappers.map_monthly(payload) if payload else None
    if mapped is None:
        if payload is not None:
            domain.mark_degraded()
        return catalog.MONTHLY_REPORT
    return mapped


@app.post("/api/invest/open", response_model=OpenDepositResponse, tags=["copilot"],
          response_model_exclude_none=True,
          summary="Mở tiền gửi: hạch toán ghi nợ TK nguồn + tạo sổ + ghi bút toán")
async def invest_open(payload: OpenDepositRequest) -> OpenDepositResponse:
    """Mở tiền gửi có hạch toán thật: (1) DEBIT tài khoản thanh toán nguồn,
    (2) tạo bản ghi deposit (CREDIT vào sổ), (3) ghi bút toán OUT/INVESTMENT
    vào transaction_history. Tạo sổ hỏng thì CREDIT hoàn tiền lại nguồn."""
    cust_id = domain.current_customer_id()
    pf = await domain.portfolio(cust_id)
    accounts = (pf or {}).get("accounts") or []
    if not accounts:
        return OpenDepositResponse(ok=False)
    account_id = int(accounts[0]["account_id"])
    posting = await domain.account_posting(account_id, "DEBIT", payload.amount)
    if posting is None:
        # Không đủ số dư (409) hoặc service hạch toán lỗi — không mở sổ.
        return OpenDepositResponse(ok=False)
    dep = await domain.create_deposit(cust_id, {
        "amount": payload.amount,
        "term_months": payload.months,
        "rate": payload.rate,
        "linked_account_id": account_id,
    })
    if dep is None:
        # Sổ không tạo được: hoàn tiền để hai vế nợ/có cân nhau.
        await domain.account_posting(account_id, "CREDIT", payload.amount)
        return OpenDepositResponse(ok=False)
    deposit_no = f"2000{int(dep.get('deposit_id') or 0):07d}"
    await domain.create_transaction({
        "customer_id": cust_id,
        "account_id": account_id,
        "direction": "OUT",
        "amount": payload.amount,
        "balance_after": posting.get("balance_after"),
        "beneficiary_bank_code": "MSB",
        "beneficiary_account_masked": deposit_no,
        "transaction_type": "FT",
        "category": "INVESTMENT",
        "transaction_description": f"Mo tien gui lai suat dac biet ky han {payload.months} thang",
        "channel": "MOBILE",
        "status": "POSTED",
    })

    def _iso(d: str | None) -> str | None:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 else None

    return OpenDepositResponse(
        ok=True,
        deposit_no=deposit_no,
        start_date=_iso(dep.get("start_date")),
        maturity_date=_iso(dep.get("maturity_date")),
        balance_after=int(posting.get("balance_after") or 0),
    )


@app.get("/api/invest/rates", response_model=InvestRates, tags=["copilot"],
         summary="Biểu lãi suất tiết kiệm — product × interest_rate × interest_rate_term")
async def invest_rates() -> InvestRates:
    """Biểu lãi suất hiện tại cho màn Khám phá sản phẩm → Biểu lãi suất.

    transaction-service join 3 bảng và chọn đợt hiệu lực mới nhất của từng cặp
    (sản phẩm, kỳ hạn); gateway chỉ xoay dữ liệu phẳng thành bảng theo kỳ hạn để
    FE vẽ bảng và biểu đồ so sánh lãi suất cùng kỳ hạn. Service chết thì trả
    biểu cố định trong catalog (khớp seed) để buổi trình bày không trắng màn."""
    raw = await domain.savings_rates()
    if not raw or not raw.get("rates"):
        return catalog.INVEST_RATES
    return mappers.map_invest_rates(raw)


@app.get("/api/products/rates", response_model=InvestRates, tags=["copilot"],
         summary="Biểu lãi suất theo nhóm sản phẩm (SAVINGS hoặc LOAN)")
async def product_rates(group: str = "SAVINGS") -> InvestRates:
    """Cùng bảng với /api/invest/rates nhưng chọn được nhóm — LOAN để tra lãi vay."""
    raw = await domain.product_rates(group.upper())
    if not raw or not raw.get("rates"):
        if group.upper() == "SAVINGS":
            return catalog.INVEST_RATES
        raise HTTPException(503, "chưa lấy được biểu lãi suất nhóm này")
    return mappers.map_invest_rates(raw)


@app.get("/api/products/loan-options", response_model=LoanOptions, tags=["copilot"],
         summary="Gói vay phù hợp cho một số tiền và kỳ hạn")
async def loan_options(amount: int, months: int, limit: int = 5) -> LoanOptions:
    """Tiền trả hàng tháng do transaction-service tính theo dư nợ giảm dần.

    Gateway không tự tính lại: một con số trả góp sai trên màn hình là thứ khách
    mang đi quyết định vay thật.
    """
    raw = await domain.loan_options(amount, months, limit)
    if not raw or not raw.get("options"):
        raise HTTPException(503, "chưa lấy được biểu lãi vay")
    return LoanOptions(
        amount=raw["amount"], months=raw["months"], as_of=str(raw.get("as_of") or ""),
        options=[LoanOption(**o) for o in raw["options"]],
    )


@app.get("/api/products/savings-options", response_model=SavingsOptions, tags=["copilot"],
         summary="Gói tiết kiệm phù hợp cho một số tiền và kỳ hạn gửi")
async def savings_options(amount: int, months: int, limit: int = 5) -> SavingsOptions:
    raw = await domain.savings_options(amount, months, limit)
    if not raw or not raw.get("options"):
        raise HTTPException(503, "chưa lấy được biểu lãi tiết kiệm")
    return SavingsOptions(
        amount=raw["amount"], months=raw["months"], as_of=str(raw.get("as_of") or ""),
        options=[SavingsOption(**o) for o in raw["options"]],
    )


@app.get("/api/copilot/intro", response_model=CopilotIntro, tags=["copilot"],
         summary="Lời chào, câu hỏi gợi ý và nhãn tháng của màn Copilot")
async def copilot_intro() -> CopilotIntro:
    monthly = await domain.monthly_summary(domain.current_customer_id())
    overview = mappers.map_overview(monthly, None) if monthly else None
    if overview is None:
        return catalog.COPILOT_INTRO
    return catalog.COPILOT_INTRO.model_copy(update={"month_label": overview.budget.month_label})


# ---- Vận hành ----------------------------------------------------------------

@app.get("/health", tags=["vận hành"], summary="Kiểm tra sống")
async def health() -> dict:
    # Không chạm gì bên ngoài, giống 5 service domain: probe phụ thuộc thành
    # phần khác sẽ biến một sự cố thành hai.
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/info", tags=["vận hành"], summary="Mô tả service")
async def info() -> dict:
    return {
        "service": SERVICE_NAME,
        "description": "Backend-for-frontend cho msb-guardian-fe. Phục vụ 7 endpoint FE gọi.",
        "data_source": domain.data_source(),
        "integrated_with_domain_services": domain.DOMAIN_ENABLED,
        "demo_customer_id": domain.DEMO_CUSTOMER_ID,
        "agent_service_configured": domain.agent_configured(),
        "scamshield_agent_configured": domain.agent_configured(domain.SCAMSHIELD_AGENT_ID),
        "endpoints": [
            "GET /api/home",
            "POST /api/auth/login",
            "GET /api/session/customer",
            "GET /api/copilot/overview",
            "GET /api/copilot/intro",
            "GET /api/copilot/quarters",
            "GET /api/copilot/months",
            "GET /api/copilot/month",
            "POST /api/copilot/chat",
            "GET /api/invest/rates",
            "POST /api/invest/open",
            "GET /api/transfer/pending",
            "POST /api/risk/assess",
            "GET /api/risk/explain",
            "POST /api/transfer/action",
            "GET /api/transfer/beneficiaries",
            "POST /api/transfer/precheck",
            "POST /api/transfer/execute",
            "GET /api/transfer/history",
            "GET /api/scamshield/signals",
            "GET /api/safety-center",
            "PATCH /api/safety-center/protections/{key}",
            "POST /api/ops/login",
            "GET /api/ops/session",
            "GET /api/ops/metrics",
            "GET /api/ops/dashboard",
            "GET /api/ops/alerts",
            "GET /api/ops/alerts/{alert_id}",
            "GET /api/ops/alerts/{alert_id}/detail",
            "GET /api/ops/alerts/{alert_id}/timeline",
            "POST /api/ops/alerts/{alert_id}/decision",
            "GET /api/ops/cases",
            "GET /api/ops/scenarios",
            "GET /api/ops/model",
            "GET /api/ops/audit",
        ],
        "pending_work": [
            "agent-service: cần API key và agent id thì chat mới dùng LLM thật",
        ],
    }


@app.get("/", include_in_schema=False)
async def root() -> JSONResponse:
    return JSONResponse({
        "service": SERVICE_NAME,
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "health": "/health",
        "info": "/info",
    })
