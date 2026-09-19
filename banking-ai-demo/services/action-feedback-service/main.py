"""action-feedback-service

Sở hữu: guardian_case, feedback, notification, llm_trace.

Đây là đoạn khép vòng của hệ thống. Ba việc nó làm:

  1. Mở và đóng case khi Guardian quyết định khóa tạm hoặc yêu cầu gọi lại.
  2. Thu nhãn phản hồi (fraud / legit) từ khách hoặc từ đội vận hành, rồi áp nhãn
     đó ngược trở lại: `legit` đưa giao dịch vào baseline, `fraud` đẩy người nhận
     sang trạng thái SUSPECTED. Không có bước này thì hệ thống cảnh báo sai mãi mãi
     một kiểu.
  3. Ghi llm_trace cho mọi lượt gọi LLM — bằng chứng "AI kiểm toán được". Bản ghi
     timeout/error vẫn phải tồn tại, nếu không thì tỷ lệ fallback sẽ bị tô hồng.

`POST /actions` giữ nguyên hợp đồng cũ nhưng nay ghi xuống DB thay vì bộ nhớ.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    db_health,
    execute,
    execute_returning,
    install_db_error_handlers,
    mask_free_text,
    mask_phone,
    now_vn,
    openapi_description,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
)

SERVICE_NAME = "action-feedback-service"
OWNS_TABLES = ["guardian_case", "feedback", "notification", "llm_trace"]

CUSTOMER_PROFILE_URL = os.getenv("CUSTOMER_PROFILE_SERVICE_URL", "http://customer-profile-service")
RISK_SCORING_URL = os.getenv("RISK_SCORING_SERVICE_URL", "http://risk-scoring-service")
PEER_TIMEOUT = float(os.getenv("PEER_TIMEOUT_SECONDS", "5"))

ALLOWED_ACTIONS = {"CANCEL", "HOLD", "CONTACT", "CASE_OPEN", "ALERT"}
# Hành động nào thì mở case: khóa tạm và yêu cầu gọi lại đều cần người thật xử lý tiếp.
CASE_TRIGGERS = {"HOLD", "CONTACT", "CASE_OPEN"}

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": 'action',
        "description": (
            'Ghi nhận hành động với khách. HOLD, CONTACT và CASE_OPEN tự mở case và gửi thông báo.'
        ),
    },
    {
        "name": 'case',
        "description": (
            'Case cho đội vận hành xử lý. Đóng bằng CLOSED_FRAUD hoặc CLOSED_LEGIT sẽ tự sinh nhãn phản hồi nguồn ops.'
        ),
    },
    {
        "name": 'feedback',
        "description": (
            'Vòng phản hồi khép kín: nhãn `legit` đưa hành vi vào baseline để lần sau khách không bị hỏi lại, nhãn `fraud` đẩy người nhận sang SUSPECTED.'
        ),
    },
    {
        "name": 'notification',
        "description": (
            'Kênh thông báo mock. Quan trọng nhất là `post_continue_warning` gửi sau khi khách chọn vẫn tiếp tục ở mức can thiệp.'
        ),
    },
    {
        "name": 'audit',
        "description": (
            'Nhật ký mọi lượt gọi LLM — bằng chứng AI kiểm toán được. Bản ghi timeout và error cũng phải lưu, nếu không tỷ lệ fallback sẽ bị tính sai.'
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary='Case, phản hồi closed-loop, thông báo và nhật ký kiểm toán LLM.',
    description=openapi_description(SERVICE_NAME, 'Case, phản hồi closed-loop, thông báo và nhật ký kiểm toán LLM.', OWNS_TABLES),
    openapi_tags=OPENAPI_TAGS,
    contact={"name": "MSB AI Financial Guardian — xem manifest tại GET /agent/tools"},
    docs_url="/docs",
    redoc_url="/redoc",
)
setup_docs(app, SERVICE_NAME)
# Lỗi Postgres do id sai của người gọi phải ra 404/409/422 chứ không phải
# 500, vì trợ lý đọc thẳng thân phản hồi này để quyết bước tiếp theo.
install_db_error_handlers(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ActionRequest(BaseModel):
    """Hợp đồng cũ của /actions, nay có thêm decision_id để nối vào quyết định rủi ro."""

    customer_id: int
    transaction_id: str | None = None
    action: str
    reason: str = ""
    decision_id: str | None = None


class CaseCreateRequest(BaseModel):
    decision_id: str
    customer_id: int
    scenario_id: str | None = None
    narrative: str = ""
    trigger: str = "hold"


class CaseUpdateRequest(BaseModel):
    status: Literal["OPEN", "CALLBACK_DONE", "CLOSED_FRAUD", "CLOSED_LEGIT"]
    note: str | None = None


class FeedbackRequest(BaseModel):
    decision_id: str
    customer_id: int
    label: Literal["fraud", "legit"]
    source: Literal["customer", "ops"] = "customer"
    note: str | None = Field(None, description="Không được chứa PII")


class NotificationRequest(BaseModel):
    customer_id: int
    channel: Literal["push", "sms", "call_request"]
    template_key: str = Field(
        ..., description="post_continue_warning | hold_confirmed | callback_scheduled | copilot_monthly_insight"
    )
    payload: dict = Field(default_factory=dict)
    decision_id: str | None = None


class NotificationStatusRequest(BaseModel):
    status: Literal["SENT", "DELIVERED", "ACTIONED", "EXPIRED"]


class LlmTraceRequest(BaseModel):
    # `agent` là "kind" do gateway sinh (copilot, chat_banking, shield_verdict,
    # shield_advice...), KHÔNG phải input người dùng. Trước đây để Literal cứng
    # nên mỗi lần gateway thêm một kind mới là bản ghi bị 422 và rớt IM LẶNG —
    # đã mất dấu vết của chat_banking và shield_verdict theo đúng cách đó. Với
    # một bảng kiểm toán, mất một lượt gọi thật tệ hơn nhận một nhãn lạ, nên
    # dùng str có ràng buộc thay vì enum: thêm kind mới không bao giờ làm mất bản ghi.
    agent: str = Field(..., min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    model: str
    prompt_key: str = Field(..., description='Ví dụ "shield_explain@v3"')
    prompt_masked: str
    response: str | None = None
    latency_ms: int | None = None
    status: Literal["ok", "timeout", "error", "cache"] = "ok"
    decision_id: str | None = None
    customer_id: int | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _next_case_id() -> str:
    year = now_vn().year
    row = query_one(
        "SELECT count(*) AS n FROM guardian_case WHERE case_id LIKE %s", (f"CASE-{year}-%",)
    )
    return f"CASE-{year}-{int(row['n']) + 1:04d}"


def _peer_get(url: str, **kwargs) -> dict | None:
    try:
        resp = httpx.get(url, timeout=PEER_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def _peer_patch(url: str, payload: dict) -> bool:
    try:
        resp = httpx.patch(url, json=payload, timeout=PEER_TIMEOUT)
        resp.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Common endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/db", tags=["meta"])
def health_db():
    return {"service": SERVICE_NAME, **db_health()}


@app.get("/info", tags=["meta"])
def info():
    payload = service_info(
        SERVICE_NAME,
        "Mở/đóng case, thu và áp dụng phản hồi closed-loop, gửi thông báo, ghi nhật ký LLM.",
        OWNS_TABLES,
        AGENT_TOOLS,
    )
    payload["allowed_actions"] = sorted(ALLOWED_ACTIONS)
    return payload


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    return agent_tools_payload(SERVICE_NAME, "ACTION_FEEDBACK_SERVICE_URL", AGENT_TOOLS)


# ---------------------------------------------------------------------------
# Cổng kiểm tra tồn tại
# ---------------------------------------------------------------------------
# Ghi thẳng rồi để khóa ngoại vỡ thì thông điệp trả về là của Postgres chứ không
# phải của nghiệp vụ. Tra trước và nêu đúng thực thể thì trợ lý đọc là biết phải
# đi tra lại id nào. Khóa ngoại cho phép NULL chỉ kiểm tra khi có giá trị — cột
# tùy chọn không được biến thành bắt buộc.
def _require_customer(customer_id: int | None) -> None:
    if customer_id is None:
        return
    if query_one("SELECT 1 AS x FROM customer WHERE customer_id = %s", (customer_id,)) is None:
        raise HTTPException(404, f"customer {customer_id} không tồn tại")


def _require_decision(decision_id: str | None) -> None:
    if decision_id is None:
        return
    row = query_one(
        "SELECT 1 AS x FROM risk_decision WHERE decision_id = %s::uuid", (decision_id,)
    )
    if row is None:
        raise HTTPException(404, f"decision {decision_id} không tồn tại")


def _require_scenario(scenario_id: str | None) -> None:
    if scenario_id is None:
        return
    row = query_one(
        "SELECT 1 AS x FROM scam_scenario WHERE scenario_id = %s", (scenario_id,)
    )
    if row is None:
        raise HTTPException(404, f"kịch bản {scenario_id} không tồn tại")


# ---------------------------------------------------------------------------
# Action (hợp đồng cũ, nay ghi xuống DB)
# ---------------------------------------------------------------------------
@app.post("/actions", tags=["action"], status_code=201)
def create_action(req: ActionRequest):
    """Ghi nhận một hành động. HOLD/CONTACT/CASE_OPEN sẽ tự mở case nếu có decision_id."""
    action = req.action.upper()
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"action phải thuộc {sorted(ALLOWED_ACTIONS)}")

    case = None
    if action in CASE_TRIGGERS and req.decision_id:
        case = create_case(
            CaseCreateRequest(
                decision_id=req.decision_id,
                customer_id=req.customer_id,
                narrative=req.reason or f"Hành động {action} từ luồng Guardian.",
                trigger=action.lower(),
            )
        )

    notif = create_notification(
        NotificationRequest(
            customer_id=req.customer_id,
            channel="call_request" if action == "CONTACT" else "push",
            template_key={
                "HOLD": "hold_confirmed",
                "CONTACT": "callback_scheduled",
                "CANCEL": "hold_confirmed",
            }.get(action, "post_continue_warning"),
            payload={"action": action, "reason": mask_free_text(req.reason) or ""},
            decision_id=req.decision_id,
        )
    )
    return {
        "customer_id": req.customer_id,
        "transaction_id": req.transaction_id,
        "action": action,
        "reason": mask_free_text(req.reason),
        "created_at": now_vn(),
        "case": case,
        "notification_id": notif["notification_id"],
    }


@app.get("/actions/{customer_id}", tags=["action"])
def get_actions(customer_id: int):
    """Lịch sử hành động suy ra từ các case đã mở của khách hàng."""
    rows = query(
        """
        SELECT case_id, decision_id, scenario_id, status, narrative, created_at, closed_at
        FROM guardian_case WHERE customer_id = %s ORDER BY created_at DESC
        """,
        (customer_id,),
    )
    for r in rows:
        r["decision_id"] = str(r["decision_id"])
    return {"customer_id": customer_id, "count": len(rows), "actions": rows}


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------
@app.post("/cases", tags=["case"], status_code=201)
def create_case(req: CaseCreateRequest):
    """Mở case. Quan hệ với risk_decision là 1-1, nên gọi lại trên cùng decision_id
    sẽ trả về case đã có thay vì báo lỗi trùng khóa — luồng retry của agent không
    nên vì thế mà gãy."""
    existing = query_one(
        "SELECT * FROM guardian_case WHERE decision_id = %s::uuid", (req.decision_id,)
    )
    if existing:
        existing["decision_id"] = str(existing["decision_id"])
        existing["already_existed"] = True
        return existing

    _require_decision(req.decision_id)
    _require_customer(req.customer_id)
    _require_scenario(req.scenario_id)

    profile = _peer_get(f"{CUSTOMER_PROFILE_URL}/customers/{req.customer_id}")
    callback = (profile or {}).get("phone_masked")

    row = execute_returning(
        """
        INSERT INTO guardian_case
          (case_id, decision_id, customer_id, scenario_id, status, narrative, callback_phone_masked)
        VALUES (%s, %s::uuid, %s, %s, 'OPEN', %s, %s)
        RETURNING *
        """,
        (
            _next_case_id(), req.decision_id, req.customer_id, req.scenario_id,
            mask_free_text(req.narrative) or f"Case mở tự động do hành động {req.trigger}.",
            callback,
        ),
    )
    row["decision_id"] = str(row["decision_id"])
    row["already_existed"] = False
    return row


@app.get("/cases", tags=["case"])
def list_cases(
    status: str | None = Query(None, description="OPEN|CALLBACK_DONE|CLOSED_FRAUD|CLOSED_LEGIT"),
    customer_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    sql = "SELECT * FROM guardian_case WHERE 1 = 1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status.upper())
    if customer_id:
        sql += " AND customer_id = %s"
        params.append(customer_id)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, params)
    for r in rows:
        r["decision_id"] = str(r["decision_id"])
    return {"count": len(rows), "cases": rows}


@app.get("/cases/{case_id}", tags=["case"])
def get_case(case_id: str):
    row = query_one("SELECT * FROM guardian_case WHERE case_id = %s", (case_id,))
    if row is None:
        raise HTTPException(404, f"case {case_id} không tồn tại")
    row["decision_id"] = str(row["decision_id"])
    return row


@app.patch("/cases/{case_id}", tags=["case"])
def update_case(case_id: str, req: CaseUpdateRequest):
    """Đóng case bằng CLOSED_FRAUD hoặc CLOSED_LEGIT sẽ tự sinh feedback nguồn `ops`
    — kết luận của đội vận hành chính là nhãn huấn luyện đáng tin nhất mà hệ có."""
    case = query_one("SELECT * FROM guardian_case WHERE case_id = %s", (case_id,))
    if case is None:
        raise HTTPException(404, f"case {case_id} không tồn tại")

    row = execute_returning(
        """
        UPDATE guardian_case
        SET status = %s,
            closed_at = CASE WHEN %s IN ('CLOSED_FRAUD','CLOSED_LEGIT') THEN now() ELSE closed_at END
        WHERE case_id = %s RETURNING *
        """,
        (req.status, req.status, case_id),
    )
    row["decision_id"] = str(row["decision_id"])

    generated = None
    if req.status in ("CLOSED_FRAUD", "CLOSED_LEGIT"):
        generated = create_feedback(
            FeedbackRequest(
                decision_id=str(case["decision_id"]),
                customer_id=case["customer_id"],
                label="fraud" if req.status == "CLOSED_FRAUD" else "legit",
                source="ops",
                note=mask_free_text(req.note) or f"Đóng case {case_id}",
            )
        )
    return {"case": row, "feedback": generated}


# ---------------------------------------------------------------------------
# Feedback closed-loop
# ---------------------------------------------------------------------------
@app.post("/feedback", tags=["feedback"], status_code=201)
def create_feedback(req: FeedbackRequest):
    _require_decision(req.decision_id)
    _require_customer(req.customer_id)
    row = execute_returning(
        """
        INSERT INTO feedback (decision_id, customer_id, label, source, note)
        VALUES (%s::uuid, %s, %s, %s, %s) RETURNING *
        """,
        (req.decision_id, req.customer_id, req.label, req.source, mask_free_text(req.note)),
    )
    row["decision_id"] = str(row["decision_id"])
    return row


@app.get("/feedback", tags=["feedback"])
def list_feedback(
    applied: Literal["Y", "N"] | None = None,
    customer_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
):
    sql = "SELECT * FROM feedback WHERE 1 = 1"
    params: list = []
    if applied:
        sql += " AND applied_to_baseline = %s"
        params.append(applied)
    if customer_id:
        sql += " AND customer_id = %s"
        params.append(customer_id)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, params)
    for r in rows:
        r["decision_id"] = str(r["decision_id"])
    return {"count": len(rows), "feedback": rows}


@app.post("/feedback/apply", tags=["feedback"])
def apply_feedback(limit: int = Query(50, ge=1, le=200)):
    """Áp các nhãn chưa xử lý vào hệ thống.

    `fraud` → đẩy người nhận của giao dịch đó sang SUSPECTED và tính lại baseline
    (giao dịch gian lận bị loại khỏi baseline nhờ cờ is_fraud).
    `legit`  → tính lại baseline để hành vi hợp lệ được thừa nhận là bình thường,
    nhờ đó lần sau khách không bị hỏi lại cùng một câu.

    Chỉ đánh dấu đã áp dụng khi thao tác phía sau thành công; nếu peer service lỗi,
    nhãn được giữ nguyên để lần chạy sau thử lại.
    """
    pending = query(
        "SELECT * FROM feedback WHERE applied_to_baseline = 'N' ORDER BY created_at LIMIT %s",
        (limit,),
    )
    applied, skipped = [], []

    for fb in pending:
        customer_id = fb["customer_id"]
        ok = True

        if fb["label"] == "fraud":
            decision = _peer_get(f"{RISK_SCORING_URL}/risk-decisions/{fb['decision_id']}")
            benef_masked = ((decision or {}).get("tx_snapshot") or {}).get("beneficiary_masked")
            if benef_masked:
                listing = _peer_get(
                    f"{CUSTOMER_PROFILE_URL}/customers/{customer_id}/beneficiaries"
                )
                for b in (listing or {}).get("beneficiaries", []):
                    if b.get("account_masked") == benef_masked:
                        ok = _peer_patch(
                            f"{CUSTOMER_PROFILE_URL}/beneficiaries/{b['beneficiary_id']}/status",
                            {"status": "SUSPECTED", "reason": "feedback label = fraud"},
                        ) and ok

        # behavior_profile thuộc customer-profile-service; việc tính lại và tăng
        # feedback_version phải do chính service đó làm, không UPDATE xuyên bảng.
        try:
            resp = httpx.post(
                f"{CUSTOMER_PROFILE_URL}/customers/{customer_id}/baseline/recompute",
                params={"bump_feedback_version": "true"},
                timeout=PEER_TIMEOUT * 3,
            )
            ok = resp.status_code in (200, 404) and ok
        except httpx.HTTPError:
            ok = False

        if not ok:
            skipped.append({"feedback_id": fb["feedback_id"], "reason": "peer service không phản hồi"})
            continue

        execute(
            """
            UPDATE feedback SET applied_to_baseline = 'Y', applied_at = now()
            WHERE feedback_id = %s
            """,
            (fb["feedback_id"],),
        )
        applied.append({"feedback_id": fb["feedback_id"], "label": fb["label"], "customer_id": customer_id})

    return {
        "pending_found": len(pending),
        "applied": len(applied),
        "skipped": len(skipped),
        "details": {"applied": applied, "skipped": skipped},
    }


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------
@app.post("/notifications", tags=["notification"], status_code=201)
def create_notification(req: NotificationRequest):
    _require_customer(req.customer_id)
    _require_decision(req.decision_id)
    row = execute_returning(
        """
        INSERT INTO notification (customer_id, decision_id, channel, template_key, payload)
        VALUES (%s, %s::uuid, %s, %s, %s::jsonb) RETURNING *
        """,
        (
            req.customer_id, req.decision_id, req.channel, req.template_key,
            json.dumps(req.payload, default=str),
        ),
    )
    if row.get("decision_id"):
        row["decision_id"] = str(row["decision_id"])
    return row


@app.get("/customers/{customer_id}/notifications", tags=["notification"])
def list_notifications(customer_id: int, limit: int = Query(50, ge=1, le=200)):
    rows = query(
        "SELECT * FROM notification WHERE customer_id = %s ORDER BY sent_at DESC LIMIT %s",
        (customer_id, limit),
    )
    for r in rows:
        if r.get("decision_id"):
            r["decision_id"] = str(r["decision_id"])
    return {"customer_id": customer_id, "count": len(rows), "notifications": rows}


@app.patch("/notifications/{notification_id}", tags=["notification"])
def update_notification(notification_id: int, req: NotificationStatusRequest):
    row = execute_returning(
        """
        UPDATE notification
        SET status = %s,
            actioned_at = CASE WHEN %s = 'ACTIONED' THEN now() ELSE actioned_at END
        WHERE notification_id = %s RETURNING *
        """,
        (req.status, req.status, notification_id),
    )
    if row is None:
        raise HTTPException(404, f"notification {notification_id} không tồn tại")
    if row.get("decision_id"):
        row["decision_id"] = str(row["decision_id"])
    return row


# ---------------------------------------------------------------------------
# LLM trace
# ---------------------------------------------------------------------------
@app.post("/llm-traces", tags=["audit"], status_code=201)
def create_llm_trace(req: LlmTraceRequest):
    """Ghi một lượt gọi LLM. `prompt_masked` phải đã mask PII trước khi gửi tới đây —
    service không thể biết chuỗi nào là tên thật nên không mask hộ được."""
    _require_decision(req.decision_id)
    _require_customer(req.customer_id)
    row = execute_returning(
        """
        INSERT INTO llm_trace
          (decision_id, customer_id, agent, model, prompt_key, prompt_masked,
           response, latency_ms, status)
        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            req.decision_id, req.customer_id, req.agent, req.model, req.prompt_key,
            mask_free_text(req.prompt_masked), req.response, req.latency_ms, req.status,
        ),
    )
    if row.get("decision_id"):
        row["decision_id"] = str(row["decision_id"])
    return row


@app.get("/llm-traces", tags=["audit"])
def list_llm_traces(
    decision_id: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    customer_id: int | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    """`since`/`until` là mốc tuyệt đối có múi giờ (ISO 8601), khoảng nửa mở
    [since, until). Không nhận ngày trần: `created_at` là TIMESTAMPTZ, nhận ngày
    trần thì service phải đoán múi giờ của người gọi và sẽ cắt nhầm ngày cho mọi
    bản ghi rạng sáng."""
    sql = "SELECT * FROM llm_trace WHERE 1 = 1"
    params: list = []
    if decision_id:
        sql += " AND decision_id = %s::uuid"
        params.append(decision_id)
    if agent:
        sql += " AND agent = %s"
        params.append(agent)
    if status:
        sql += " AND status = %s"
        params.append(status)
    if customer_id is not None:
        sql += " AND customer_id = %s"
        params.append(customer_id)
    if since:
        sql += " AND created_at >= %s::timestamptz"
        params.append(since)
    if until:
        sql += " AND created_at < %s::timestamptz"
        params.append(until)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, params)
    for r in rows:
        if r.get("decision_id"):
            r["decision_id"] = str(r["decision_id"])
    return {"count": len(rows), "traces": rows}


@app.get("/llm-traces/stats", tags=["audit"])
def llm_trace_stats():
    """Tỷ lệ fallback và độ trễ trung bình theo từng agent — số liệu để trả lời
    câu hỏi 'AI của các bạn có đáng tin không'."""
    rows = query(
        """
        SELECT agent, status, count(*) AS n, round(avg(latency_ms)) AS avg_latency_ms
        FROM llm_trace GROUP BY agent, status ORDER BY agent, status
        """
    )
    total = sum(int(r["n"]) for r in rows) or 1
    failed = sum(int(r["n"]) for r in rows if r["status"] in ("timeout", "error"))
    # Hai mục dưới đây là của TOÀN BỘ nhật ký, không theo bộ lọc: màn vận hành
    # cần danh sách khách để chọn (lọc rồi mà danh sách co lại theo thì không
    # chọn tiếp được) và một mốc thời gian cố định để tính các khoảng nhanh.
    customers = query(
        """
        SELECT customer_id, count(*) AS n FROM llm_trace
        WHERE customer_id IS NOT NULL GROUP BY customer_id ORDER BY customer_id
        """
    )
    latest = query_one("SELECT max(created_at) AS latest_at FROM llm_trace")
    return {
        "total_calls": total,
        "fallback_rate": round(failed / total, 4),
        "breakdown": rows,
        "customers": customers,
        "latest_at": (latest or {}).get("latest_at"),
    }


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------
AGENT_TOOLS = [
    tool(
        "record_action",
        "Ghi nhận hành động với khách hàng (CANCEL/HOLD/CONTACT/CASE_OPEN/ALERT). "
        "HOLD, CONTACT và CASE_OPEN tự mở case và gửi thông báo cho khách.",
        "POST", "/actions",
        body={
            "customer_id": "int", "transaction_id": "str (tùy chọn)",
            "action": "CANCEL|HOLD|CONTACT|CASE_OPEN|ALERT",
            "reason": "str", "decision_id": "uuid (tùy chọn)",
        },
        returns="Hành động đã ghi kèm case và thông báo.",
    ),
    tool(
        "create_case",
        "Mở case cho đội vận hành xử lý tiếp. Gọi lại trên cùng decision_id sẽ trả "
        "về case đã có thay vì báo lỗi.",
        "POST", "/cases",
        body={"decision_id": "uuid", "customer_id": "int", "scenario_id": "str", "narrative": "str"},
        returns="Case vừa mở.",
    ),
    tool(
        "list_cases",
        "Danh sách case, lọc theo trạng thái hoặc khách hàng.",
        "GET", "/cases",
        params={"status": "OPEN|CALLBACK_DONE|CLOSED_FRAUD|CLOSED_LEGIT", "customer_id": "int", "limit": "int"},
        returns="Danh sách case.",
    ),
    tool(
        "update_case",
        "Cập nhật trạng thái case. Đóng bằng CLOSED_FRAUD hoặc CLOSED_LEGIT sẽ tự "
        "sinh feedback nguồn ops.",
        "PATCH", "/cases/{case_id}",
        params={"case_id": "CASE-YYYY-NNNN"},
        body={"status": "OPEN|CALLBACK_DONE|CLOSED_FRAUD|CLOSED_LEGIT", "note": "str (tùy chọn)"},
        returns="Case sau cập nhật và feedback sinh kèm.",
    ),
    tool(
        "submit_feedback",
        "Gửi nhãn phản hồi cho một quyết định rủi ro: fraud hay legit, từ khách hay "
        "từ đội vận hành.",
        "POST", "/feedback",
        body={
            "decision_id": "uuid", "customer_id": "int",
            "label": "fraud|legit", "source": "customer|ops", "note": "str không chứa PII",
        },
        returns="Bản ghi feedback.",
    ),
    tool(
        "apply_feedback",
        "Áp các nhãn chưa xử lý: fraud đẩy người nhận sang SUSPECTED, cả hai nhãn "
        "đều kích hoạt tính lại baseline hành vi.",
        "POST", "/feedback/apply",
        params={"limit": "int, số nhãn xử lý mỗi lần"},
        returns="Số nhãn đã áp dụng và số bị bỏ qua.",
    ),
    tool(
        "send_notification",
        "Gửi thông báo mock cho khách. Quan trọng nhất là post_continue_warning — "
        "gửi sau khi khách chọn 'vẫn tiếp tục' ở mức can thiệp.",
        "POST", "/notifications",
        body={
            "customer_id": "int", "channel": "push|sms|call_request",
            "template_key": "post_continue_warning|hold_confirmed|callback_scheduled|copilot_monthly_insight",
            "payload": "object đã render và mask", "decision_id": "uuid (tùy chọn)",
        },
        returns="Thông báo vừa tạo.",
    ),
    tool(
        "log_llm_call",
        "Ghi nhật ký một lượt gọi LLM để kiểm toán. Bản ghi timeout/error cũng phải "
        "ghi, nếu không tỷ lệ fallback sẽ bị tính sai.",
        "POST", "/llm-traces",
        body={
            "agent": "copilot|shield_explain|shield_interview|shield_advice",
            "model": "str", "prompt_key": "str", "prompt_masked": "str đã mask PII",
            "response": "str", "latency_ms": "int", "status": "ok|timeout|error|cache",
            "decision_id": "uuid (tùy chọn)", "customer_id": "int (tùy chọn)",
        },
        returns="Bản ghi trace.",
    ),
    tool(
        "llm_trace_stats",
        "Tỷ lệ fallback và độ trễ trung bình của LLM theo từng agent.",
        "GET", "/llm-traces/stats",
        returns="total_calls, fallback_rate, breakdown.",
    ),
]
