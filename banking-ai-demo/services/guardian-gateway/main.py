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
from models import (
    AssessRequest,
    CaseDetail,
    CaseTimelineStep,
    ChatRequest,
    CopilotIntro,
    CopilotOverview,
    Customer,
    DecisionRequest,
    HomeContent,
    OkResponse,
    OpsDashboard,
    OpsMetrics,
    OpsSession,
    PendingTransfer,
    ProtectionToggleRequest,
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

# Nguồn dữ liệu hiện tại. Đổi thành "domain" khi đã nối 5 service thật — giá trị
# này đi ra header nên biết ngay đang chạy bằng gì mà không cần đọc code.
DATA_SOURCE = os.getenv("GUARDIAN_DATA_SOURCE", "stub")

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

    Không có dấu này thì không có cách nào phân biệt 'FE đang gọi gateway thật'
    với 'FE đã im lặng rơi về mock' ngoài việc đọc Console.
    """
    response = await call_next(request)
    response.headers["X-Guardian-Data-Source"] = DATA_SOURCE
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


@app.get("/api/copilot/overview", response_model=CopilotOverview, tags=["copilot"],
         # ctaLabel là optional bên TS: bỏ hẳn field khi không có, thay vì trả null.
         response_model_exclude_none=True,
         summary="Tổng quan chi tiêu tháng cho màn Financial Copilot")
async def copilot_overview() -> CopilotOverview:
    return catalog.COPILOT_OVERVIEW


@app.post("/api/copilot/chat", tags=["copilot"],
          summary="Chat với Copilot (Server-Sent Events)")
async def copilot_chat(payload: ChatRequest) -> StreamingResponse:
    """Phát câu trả lời theo từng token.

    Định dạng phải khớp đúng bộ parse bên FE: mỗi dòng `data: {"token": "..."}`,
    kết thúc bằng `data: [DONE]`. FE bỏ qua dòng không bắt đầu bằng `data:`.
    """
    reply, chart = catalog.FALLBACK_REPLY, None
    for pattern, text, attached in catalog.SCRIPTED_REPLIES:
        if re.search(pattern, payload.message, re.IGNORECASE):
            reply, chart = text, attached
            break

    async def stream() -> AsyncIterator[bytes]:
        # Tách giữ nguyên khoảng trắng, giống replayAsStream bên FE, để ghép lại
        # không mất dấu cách.
        for token in (t for t in re.split(r"(\s+)", reply) if t):
            chunk = json.dumps({"token": token}, ensure_ascii=False)
            yield f"data: {chunk}\n\n".encode()
            await asyncio.sleep(CHAT_TOKEN_DELAY_MS / 1000)
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
    if RISK_ASSESS_DELAY_MS > 0:
        await asyncio.sleep(RISK_ASSESS_DELAY_MS / 1000)
    return catalog.build_assessment(score_for_amount(payload.amount), catalog.MAIN_SCENARIO)


@app.get("/api/ops/metrics", response_model=OpsMetrics, tags=["ops"],
         summary="Bốn chỉ số KPI của Ops Dashboard")
async def ops_metrics() -> OpsMetrics:
    return catalog.OPS_METRICS


@app.get("/api/ops/alerts", response_model=list[ScamAlert], tags=["ops"],
         response_model_exclude_none=True,
         summary="Danh sách cảnh báo đang theo dõi")
async def ops_alerts() -> list[ScamAlert]:
    return [_alert_by_id(a.id) for a in catalog.OPS_ALERTS]


@app.get("/api/ops/alerts/{alert_id}", response_model=ScamAlert, tags=["ops"],
         response_model_exclude_none=True,
         summary="Chi tiết một cảnh báo")
async def ops_alert(alert_id: str) -> ScamAlert:
    return _alert_by_id(alert_id)


@app.post("/api/ops/alerts/{alert_id}/decision", response_model=OkResponse, tags=["ops"],
          summary="Ghi quyết định xử lý của chuyên viên vận hành")
async def ops_decision(alert_id: str, payload: DecisionRequest) -> OkResponse:
    _alert_by_id(alert_id)  # 404 nếu id không tồn tại, trước khi ghi bất cứ gì
    _decisions[alert_id] = payload.decision
    return OkResponse(ok=True)


# ---- Màn Home / Login --------------------------------------------------------

@app.get("/api/home", response_model=HomeContent, tags=["session"],
         summary="Nội dung động của màn Home và màn đăng nhập")
async def home_content() -> HomeContent:
    return catalog.HOME_CONTENT


@app.get("/api/session/customer", response_model=Customer, tags=["session"],
         summary="Khách hàng của phiên hiện tại")
async def session_customer() -> Customer:
    # Chưa có đăng nhập: luôn trả khách hàng của kịch bản demo. Khi có xác thực
    # thì đọc từ token thay vì hằng số, chữ ký hàm giữ nguyên.
    return catalog.CUSTOMER


# ---- Màn Scam Shield ---------------------------------------------------------

@app.get("/api/transfer/pending", response_model=PendingTransfer, tags=["risk"],
         summary="Lệnh chuyển tiền đang chờ duyệt")
async def transfer_pending() -> PendingTransfer:
    return catalog.PENDING_TRANSFER


@app.get("/api/risk/explain", response_model=RiskExplain, tags=["risk"],
         response_model_exclude_none=True,
         summary='Dữ liệu màn "Vì sao chúng tôi cảnh báo?"')
async def risk_explain() -> RiskExplain:
    return catalog.RISK_EXPLAIN


@app.get("/api/safety-center", response_model=SafetyCenter, tags=["risk"],
         summary="Trung tâm an toàn")
async def safety_center() -> SafetyCenter:
    if not _protections:
        return catalog.SAFETY_CENTER
    # Trả về trạng thái khách đã bật/tắt, không phải giá trị mặc định.
    layers = [p.model_copy(update={"enabled": _protections.get(p.key, p.enabled)})
              for p in catalog.SAFETY_CENTER.protections]
    return catalog.SAFETY_CENTER.model_copy(update={"protections": layers})


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
    return catalog.OPS_DASHBOARD


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

@app.get("/api/copilot/intro", response_model=CopilotIntro, tags=["copilot"],
         summary="Lời chào, câu hỏi gợi ý và nhãn tháng của màn Copilot")
async def copilot_intro() -> CopilotIntro:
    return catalog.COPILOT_INTRO


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
        "data_source": DATA_SOURCE,
        "integrated_with_domain_services": False,
        "endpoints": [
            "GET /api/home",
            "GET /api/session/customer",
            "GET /api/copilot/overview",
            "GET /api/copilot/intro",
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
            "Nối các endpoint vào 5 service domain và agent-service thay cho catalog.py",
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
