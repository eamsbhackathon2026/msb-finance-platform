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
from models import (
    AssessRequest,
    CaseDetail,
    CaseTimelineStep,
    ChatChart,
    ChatChartPoint,
    ChatRequest,
    ChatTable,
    ChatTableRow,
    CopilotIntro,
    CopilotOverview,
    Customer,
    DecisionRequest,
    HomeContent,
    LoginRequest,
    LoginResponse,
    MonthlyReport,
    OkResponse,
    OpsDashboard,
    OpsMetrics,
    OpsSession,
    PendingTransfer,
    ProtectionToggleRequest,
    QuarterlyReport,
    RiskAssessment,
    RiskExplain,
    SafetyCenter,
    ScamAlert,
    TransferActionRequest,
    TransferActionResponse,
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
    decisions = await domain.risk_decisions(limit=50)
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


async def _scenario_names() -> dict[str, str]:
    """Tên kịch bản theo decision_id.

    Bản ghi risk_decision chỉ nhắc mã kịch bản trong câu giải thích của cơ chế
    nâng mức, mà nâng mức chỉ xảy ra với một phần quyết định. View /ops/decisions
    thì có sẵn scenario_name cho mọi dòng — một lời gọi là đủ cho cả danh sách.
    """
    view = await domain.ops_decisions(limit=50)
    return {
        d["decision_id"]: d["scenario_name"]
        for d in ((view or {}).get("decisions") or [])
        if d.get("decision_id") and d.get("scenario_name")
    }


_SCENARIO_RE = re.compile(r"\bS\d{2}\b")


def _scenario_id_in(text: str) -> str | None:
    """Rút mã kịch bản (S01…) từ câu giải thích của cơ chế nâng mức."""
    m = _SCENARIO_RE.search(text)
    return m.group(0) if m else None


async def _precheck_for(amount: int) -> dict | None:
    """Dựng đầu vào cho engine từ hồ sơ khách và fraud case, rồi gọi precheck."""
    pf = await domain.portfolio(domain.DEMO_CUSTOMER_ID)
    accounts = (pf or {}).get("accounts") or []
    if not accounts:
        return None
    case = await domain.fraud_case(domain.DEMO_FRAUD_CASE_ID)
    series = ((case or {}).get("injection") or {}).get("series") or []
    memo = series[0].get("memo") if series else None
    ben = catalog.MAIN_BENEFICIARY
    return await domain.precheck({
        "customer_id": domain.DEMO_CUSTOMER_ID,
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
    monthly = await domain.monthly_summary(domain.DEMO_CUSTOMER_ID)
    if not monthly:
        return catalog.COPILOT_OVERVIEW
    ins = await domain.insights(domain.DEMO_CUSTOMER_ID)
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
# So sánh nhiều tháng: "so với tháng 8", "so sánh các tháng", "mấy tháng gần đây".
_COMPARE_RE = re.compile(r"so sánh|so với|các tháng|mấy tháng|nhiều tháng|từng tháng|những tháng|vài tháng", re.IGNORECASE)


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


async def _spending_visual(question: str) -> tuple[ChatTable | None, ChatChart | None]:
    """Bảng + biểu đồ số liệu thật cho câu hỏi chi tiêu, hoặc (None, None).

    Dùng lại đúng các endpoint copilot_months/quarters/overview nên số ở bảng
    khớp từng đồng với màn hình và với điều agent nói — một nguồn số duy nhất.

    Thứ tự xét: SO SÁNH tháng trước (bảng nhiều tháng) → quý (bảng theo nhóm) →
    một tháng (bảng theo nhóm). So sánh xét trước vì "so với tháng 8" cũng khớp
    _MONTH_RE, mà ý là so nhiều tháng chứ không phải xem một tháng.
    """
    if not _SPEND_RE.search(question):
        return None, None
    if _COMPARE_RE.search(question) and _MONTH_RE.search(question):
        return _months_compare_visual(await copilot_months(months=6))
    if _QUARTER_RE.search(question):
        return _quarter_visual(await copilot_quarters(quarters=8))
    if _MONTH_RE.search(question):
        return _overview_visual(await copilot_overview())
    return None, None


@app.post("/api/copilot/chat", tags=["copilot"],
          summary="Chat với Copilot (Server-Sent Events)")
async def copilot_chat(payload: ChatRequest) -> StreamingResponse:
    """Phát câu trả lời theo từng token.

    Định dạng phải khớp đúng bộ parse bên FE: mỗi dòng `data: {"token": "..."}`,
    kết thúc bằng `data: [DONE]`. FE bỏ qua dòng không bắt đầu bằng `data:`.
    """
    # agent-service trả lời nếu đã được cấu hình. Nền tảng agent có xác thực
    # riêng và cần một agent tạo sẵn trong đó; phần thiết lập ấy thuộc phạm vi
    # người khác nên gateway chỉ gọi, không tự tạo. Chưa cấu hình hoặc gọi hỏng
    # thì dùng kịch bản trả lời có sẵn.
    reply, chart = catalog.FALLBACK_REPLY, None
    from_agent = await domain.agent_answer(payload.message)
    if from_agent:
        reply = from_agent
    else:
        for pattern, text, attached in catalog.SCRIPTED_REPLIES:
            if re.search(pattern, payload.message, re.IGNORECASE):
                reply, chart = text, attached
                break

    # Đính bảng + biểu đồ số liệu THẬT cho câu hỏi chi tiêu. Chart số thật ghi đè
    # chart kịch bản tĩnh nếu có. Phải lấy TRƯỚC khi trả StreamingResponse để
    # middleware kịp đóng dấu X-Guardian-Data-Source theo các lời gọi domain này.
    table, data_chart = await _spending_visual(payload.message)
    if data_chart is not None:
        chart = data_chart

    async def stream() -> AsyncIterator[bytes]:
        # Tách giữ nguyên khoảng trắng, giống replayAsStream bên FE, để ghép lại
        # không mất dấu cách.
        for token in (t for t in re.split(r"(\s+)", reply) if t):
            chunk = json.dumps({"token": token}, ensure_ascii=False)
            yield f"data: {chunk}\n\n".encode()
            await asyncio.sleep(CHAT_TOKEN_DELAY_MS / 1000)
        if table is not None:
            # Sự kiện bảng phát sau khi hết token, trước biểu đồ. FE bỏ qua object
            # không có "token" nếu chưa hỗ trợ, nên không làm hỏng client cũ.
            payload_table = json.dumps({"table": table.model_dump(by_alias=True)}, ensure_ascii=False)
            yield f"data: {payload_table}\n\n".encode()
        if chart is not None:
            # Sự kiện riêng sau khi hết token. FE bỏ qua object không có "token"
            # nếu chưa hỗ trợ, nên thêm dòng này không làm hỏng client cũ.
            payload_chart = json.dumps({"chart": chart.model_dump(by_alias=True)}, ensure_ascii=False)
            yield f"data: {payload_chart}\n\n".encode()
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
    decisions = await domain.risk_decisions(limit=50)
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
    known = ((await domain.beneficiaries(domain.DEMO_CUSTOMER_ID)) or {}).get("beneficiaries") or []
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
    else:
        _alert_by_id(alert_id)  # 404 nếu id không tồn tại, trước khi ghi gì
    _decisions[alert_id] = payload.decision
    return OkResponse(ok=True)


# ---- Màn Home / Login --------------------------------------------------------

@app.get("/api/home", response_model=HomeContent, tags=["session"],
         summary="Nội dung động của màn Home và màn đăng nhập")
async def home_content() -> HomeContent:
    cust = await domain.customer(domain.DEMO_CUSTOMER_ID)
    if not cust:
        return catalog.HOME_CONTENT
    ins = await domain.insights(domain.DEMO_CUSTOMER_ID)
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
        return LoginResponse(authenticated=True, source="degraded", user=catalog.DEMO_LOGIN_USER)

    if result.get("authenticated"):
        return LoginResponse(authenticated=True, source="domain", user=result.get("user"))

    return LoginResponse(authenticated=False, source="domain", reason=result.get("reason", "invalid_credentials"))


@app.get("/api/session/customer", response_model=Customer, tags=["session"],
         summary="Khách hàng của phiên hiện tại")
async def session_customer() -> Customer:
    # Chưa có đăng nhập nên khách hàng của phiên là DEMO_CUSTOMER_ID. Khi có xác
    # thực thì đọc id từ token, phần còn lại của hàm giữ nguyên.
    cust = await domain.customer(domain.DEMO_CUSTOMER_ID)
    if not cust:
        return catalog.CUSTOMER
    pf = await domain.portfolio(domain.DEMO_CUSTOMER_ID)
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
    events = await domain.account_events(domain.DEMO_CUSTOMER_ID)
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
                if int(c.get("customer_id") or 0) == domain.DEMO_CUSTOMER_ID]
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
    if _last_decision_id:
        action_taken, outcome = DOMAIN_ACTIONS[payload.action]
        await domain.transfer_action({
            "decision_id": _last_decision_id,
            "action_taken": action_taken,
            "outcome": outcome,
        })
        _decisions[_last_decision_id] = status

    _decisions[catalog.CUSTOMER_CASE_ID] = status
    _customer_steps.append(
        CaseTimelineStep(id=f"ct-cust-{len(_customer_steps) + 1}", time=_now_hms(), label=step_label, done=True)
    )
    return TransferActionResponse(ok=True, case_status=status, message=message)


# ---- Màn Ops -----------------------------------------------------------------

@app.get("/api/ops/session", response_model=OpsSession, tags=["ops"],
         summary="Chuyên viên đang trực và tình trạng hệ thống")
async def ops_session() -> OpsSession:
    return catalog.OPS_SESSION


@app.get("/api/ops/alerts/{alert_id}/detail", response_model=CaseDetail, tags=["ops"],
         summary="Chi tiết giao dịch, hồ sơ khách và thông tin mô hình của case")
async def ops_alert_detail(alert_id: str) -> CaseDetail:
    _alert_by_id(alert_id)  # 404 nếu id không tồn tại
    return catalog.CASE_DETAIL


@app.get("/api/ops/dashboard", response_model=OpsDashboard, tags=["ops"],
         summary="Phần bổ trợ của Ops Dashboard (delta, biểu đồ, đầu vào mô hình)")
async def ops_dashboard() -> OpsDashboard:
    decisions = await domain.risk_decisions(limit=50)
    if not decisions:
        return catalog.OPS_DASHBOARD
    return mappers.map_dashboard(
        decisions.get("decisions") or [],
        catalog.OPS_DASHBOARD.model_inputs,  # mô tả đầu vào mô hình, không phải dữ liệu
    )


@app.get("/api/ops/alerts/{alert_id}/timeline", response_model=list[CaseTimelineStep], tags=["ops"],
         summary="Dòng thời gian xử lý của một case")
async def ops_alert_timeline(alert_id: str) -> list[CaseTimelineStep]:
    _alert_by_id(alert_id)  # 404 nếu id không tồn tại
    if alert_id != catalog.CUSTOMER_CASE_ID or not _customer_steps:
        return catalog.CASE_TIMELINE
    # Chèn bước của khách TRƯỚC bước cuối ("chờ quyết định xử lý"), vì bước đó
    # luôn là mốc chưa hoàn thành và phải nằm ở cuối danh sách.
    return [*catalog.CASE_TIMELINE[:-1], *_customer_steps, catalog.CASE_TIMELINE[-1]]


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
    payload = await domain.quarterly_summary(domain.DEMO_CUSTOMER_ID, quarters)
    mapped = mappers.map_quarterly(payload) if payload else None
    if mapped is None:
        if payload is not None:
            domain.mark_degraded()
        return catalog.QUARTERLY_REPORT
    return mapped


@app.get("/api/copilot/months", response_model=MonthlyReport, tags=["copilot"],
         summary="So sánh chi tiêu giữa các tháng gần nhất")
async def copilot_months(months: int = 6) -> MonthlyReport:
    """Nhiều tháng gần nhất kèm thay đổi so tháng trước — cho câu hỏi so sánh.

    Cùng nguồn với tool `monthly-comparison` của agent: khách hỏi "tháng này so
    với tháng 8 thế nào" thì agent gọi tool này, còn màn chat dựng bảng so sánh
    từ đúng dữ liệu ấy. Một nguồn số, hai bên không lệch.
    """
    payload = await domain.monthly_comparison(domain.DEMO_CUSTOMER_ID, months)
    mapped = mappers.map_monthly(payload) if payload else None
    if mapped is None:
        if payload is not None:
            domain.mark_degraded()
        return catalog.MONTHLY_REPORT
    return mapped


@app.get("/api/copilot/intro", response_model=CopilotIntro, tags=["copilot"],
         summary="Lời chào, câu hỏi gợi ý và nhãn tháng của màn Copilot")
async def copilot_intro() -> CopilotIntro:
    monthly = await domain.monthly_summary(domain.DEMO_CUSTOMER_ID)
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
        "endpoints": [
            "GET /api/home",
            "POST /api/auth/login",
            "GET /api/session/customer",
            "GET /api/copilot/overview",
            "GET /api/copilot/intro",
            "GET /api/copilot/quarters",
            "GET /api/copilot/months",
            "POST /api/copilot/chat",
            "GET /api/transfer/pending",
            "POST /api/risk/assess",
            "GET /api/risk/explain",
            "POST /api/transfer/action",
            "GET /api/safety-center",
            "PATCH /api/safety-center/protections/{key}",
            "GET /api/ops/session",
            "GET /api/ops/metrics",
            "GET /api/ops/dashboard",
            "GET /api/ops/alerts",
            "GET /api/ops/alerts/{alert_id}",
            "GET /api/ops/alerts/{alert_id}/detail",
            "GET /api/ops/alerts/{alert_id}/timeline",
            "POST /api/ops/alerts/{alert_id}/decision",
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
