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
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import catalog
from models import (
    AssessRequest,
    ChatRequest,
    CopilotOverview,
    DecisionRequest,
    OkResponse,
    OpsMetrics,
    RiskAssessment,
    ScamAlert,
)

SERVICE_NAME = "guardian-gateway"

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
    reply = catalog.FALLBACK_REPLY
    for pattern, text in catalog.SCRIPTED_REPLIES:
        if re.search(pattern, payload.message, re.IGNORECASE):
            reply = text
            break

    async def stream() -> AsyncIterator[bytes]:
        # Tách giữ nguyên khoảng trắng, giống replayAsStream bên FE, để ghép lại
        # không mất dấu cách.
        for token in (t for t in re.split(r"(\s+)", reply) if t):
            chunk = json.dumps({"token": token}, ensure_ascii=False)
            yield f"data: {chunk}\n\n".encode()
            await asyncio.sleep(CHAT_TOKEN_DELAY_MS / 1000)
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
            "GET /api/copilot/overview",
            "POST /api/copilot/chat",
            "POST /api/risk/assess",
            "GET /api/ops/metrics",
            "GET /api/ops/alerts",
            "GET /api/ops/alerts/{alert_id}",
            "POST /api/ops/alerts/{alert_id}/decision",
        ],
        "pending_work": [
            "Nối 7 endpoint vào 5 service domain thay cho catalog.py",
            "FE: streamChat chưa nhận chart ở nhánh live (src/lib/api.ts)",
            "FE: getCaseTimeline chưa có nhánh gọi mạng (src/lib/api.ts)",
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
