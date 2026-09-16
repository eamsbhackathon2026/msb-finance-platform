"""Lớp gọi sang 5 service domain và agent-service.

Nguyên tắc ở đây có ba điều, và cả ba đều là hệ quả của việc gateway đứng trước
một web app không còn dữ liệu dự phòng nào:

1. MỖI LỜI GỌI ĐỀU CÓ THỂ HỎNG. 5 service deploy độc lập; một cái chết không
   được kéo theo cả màn hình trắng. Hàm ở đây trả None khi hỏng, phần gọi tự
   quyết định dùng dữ liệu tạm trong catalog.py thay thế.

2. HỎNG PHẢI NHÌN THẤY ĐƯỢC. Mỗi lần rơi về dữ liệu tạm đều đánh dấu vào
   ContextVar của request, và middleware đưa ra header X-Guardian-Data-Source
   với giá trị "degraded". Im lặng rơi về mock chính là lỗi đã gặp ở FE.

3. KHÔNG SỬA GÌ Ở PHÍA DOMAIN. Gateway chỉ đọc hợp đồng sẵn có. agent-service
   thuộc phạm vi người khác nên chỉ được gọi, không được đổi.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any

import httpx

CUSTOMER_PROFILE_URL = os.getenv("CUSTOMER_PROFILE_SERVICE_URL", "http://customer-profile-service")
TRANSACTION_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://transaction-service")
RISK_SCORING_URL = os.getenv("RISK_SCORING_SERVICE_URL", "http://risk-scoring-service")
SCAM_KNOWLEDGE_URL = os.getenv("SCAM_KNOWLEDGE_SERVICE_URL", "http://scam-knowledge-service")
ACTION_FEEDBACK_URL = os.getenv("ACTION_FEEDBACK_SERVICE_URL", "http://action-feedback-service")

# agent-service nằm ở namespace khác nên phải dùng tên đầy đủ trong cluster.
AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
AGENT_ID = os.getenv("AGENT_ID", "")

PEER_TIMEOUT = float(os.getenv("PEER_TIMEOUT_SECONDS", "5"))

# Bật/tắt toàn bộ việc gọi domain. Đặt "false" để chạy hoàn toàn bằng catalog.py
# (hữu ích khi trình bày offline hoặc khi cluster domain đang bảo trì).
DOMAIN_ENABLED = os.getenv("DOMAIN_ENABLED", "true").lower() != "false"

# Khách hàng của kịch bản demo. 100008 là người cao tuổi trong fraud case F01
# (giả danh công an) — đúng câu chuyện mà màn Scam Shield kể.
DEMO_CUSTOMER_ID = int(os.getenv("DEMO_CUSTOMER_ID", "100008"))
DEMO_FRAUD_CASE_ID = os.getenv("DEMO_FRAUD_CASE_ID", "F01")

# Trạng thái của request hiện tại.
#
# Phải là một dict SỬA TẠI CHỖ chứ không phải hai ContextVar[bool]. Middleware
# của Starlette chạy endpoint trong một task riêng, mà task con chỉ được COPY
# context: gán lại ContextVar bên trong endpoint thì middleware không nhìn thấy,
# và header nguồn dữ liệu luôn báo "stub" dù đã gọi domain thành công. Task con
# thừa hưởng cùng một tham chiếu dict, nên sửa nội dung thì cả hai cùng thấy.
_state: ContextVar[dict] = ContextVar("guardian_request_state")


def _current() -> dict:
    try:
        return _state.get()
    except LookupError:
        fresh = {"touched": False, "degraded": False}
        _state.set(fresh)
        return fresh


def reset_request_state() -> None:
    _state.set({"touched": False, "degraded": False})


def mark_degraded() -> None:
    _current()["degraded"] = True


def _mark_touched() -> None:
    _current()["touched"] = True


def data_source() -> str:
    """Nguồn dữ liệu của response vừa dựng xong.

    domain   — mọi lời gọi domain đều thành công
    degraded — có ít nhất một lời gọi hỏng, phần đó lấy từ catalog.py
    stub     — không gọi domain lần nào (DOMAIN_ENABLED=false hoặc endpoint tĩnh)
    """
    state = _current()
    if not state["touched"]:
        return "stub"
    return "degraded" if state["degraded"] else "domain"


async def _get(base: str, path: str, params: dict | None = None) -> Any | None:
    if not DOMAIN_ENABLED:
        return None
    _mark_touched()
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT) as client:
            r = await client.get(f"{base}{path}", params=params)
            r.raise_for_status()
            return r.json()
    except Exception:
        mark_degraded()
        return None


async def _post(base: str, path: str, payload: dict) -> Any | None:
    if not DOMAIN_ENABLED:
        return None
    _mark_touched()
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT) as client:
            r = await client.post(f"{base}{path}", json=payload)
            r.raise_for_status()
            return r.json()
    except Exception:
        mark_degraded()
        return None


# ---- customer-profile-service ------------------------------------------------

async def customer(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}")


async def portfolio(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/portfolio")


async def beneficiaries(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/beneficiaries")


async def account_events(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/events")


# ---- transaction-service -----------------------------------------------------

async def monthly_summary(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}/monthly-summary")


async def quarterly_summary(customer_id: int, quarters: int = 8) -> dict | None:
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}/quarterly-summary",
                      {"quarters": quarters})


async def insights(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/customers/{customer_id}/insights")


async def recommendations(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/customers/{customer_id}/recommendations")


# ---- risk-scoring-service ----------------------------------------------------

async def precheck(payload: dict) -> dict | None:
    return await _post(RISK_SCORING_URL, "/transfer/precheck", payload)


async def transfer_action(payload: dict) -> dict | None:
    return await _post(RISK_SCORING_URL, "/transfer/action", payload)


async def ops_summary() -> dict | None:
    return await _get(RISK_SCORING_URL, "/ops/summary")


async def ops_decisions(limit: int = 50) -> dict | None:
    return await _get(RISK_SCORING_URL, "/ops/decisions", {"limit": limit})


async def risk_decisions(limit: int = 50) -> dict | None:
    """Danh sách quyết định kèm customer_id và tx_snapshot.

    Dùng cái này thay cho /ops/decisions: view ops không có customer_id nên
    không tra được tên khách, mà màn Ops Dashboard thì cần hiển thị tên.
    """
    return await _get(RISK_SCORING_URL, "/risk-decisions", {"limit": limit})


async def customers() -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, "/customers")


async def risk_decision(decision_id: str) -> dict | None:
    return await _get(RISK_SCORING_URL, f"/risk-decisions/{decision_id}")


# ---- scam-knowledge-service --------------------------------------------------

async def scenario(scenario_id: str) -> dict | None:
    return await _get(SCAM_KNOWLEDGE_URL, f"/scams/{scenario_id}")


async def scenarios() -> dict | None:
    return await _get(SCAM_KNOWLEDGE_URL, "/scams")


async def fraud_case(case_id: str) -> dict | None:
    return await _get(SCAM_KNOWLEDGE_URL, f"/fraud-cases/{case_id}")


# ---- action-feedback-service -------------------------------------------------

async def cases(limit: int = 50) -> dict | None:
    return await _get(ACTION_FEEDBACK_URL, "/cases", {"limit": limit})


async def notifications(customer_id: int) -> dict | None:
    return await _get(ACTION_FEEDBACK_URL, f"/customers/{customer_id}/notifications")


async def record_action(payload: dict) -> dict | None:
    return await _post(ACTION_FEEDBACK_URL, "/actions", payload)


# ---- agent-service (chỉ gọi, KHÔNG sửa) --------------------------------------

def agent_configured() -> bool:
    """agent-service chỉ được dùng khi đã cấu hình đủ địa chỉ, khóa và agent.

    Nền tảng agent có xác thực riêng và cần một agent được tạo sẵn trong đó.
    Phần thiết lập ấy thuộc phạm vi người khác nên gateway không tự làm; khi
    chưa đủ cấu hình thì chat dùng kịch bản trả lời có sẵn.
    """
    return bool(AGENT_SERVICE_URL and AGENT_API_KEY and AGENT_ID)


async def agent_answer(question: str) -> str | None:
    """Hỏi agent-service một câu và lấy câu trả lời dạng văn bản.

    Dùng đúng hợp đồng công khai của nền tảng (api/openapi.yaml trong repo
    hackathon-agent-platform):

        POST /v1/agents/{agentId}/runs
        {"input": {"message": "..."}, "mode": "sync"}
        → 200 {"status": "...", "output": "câu trả lời", ...}

    `input` là OBJECT có khóa `message`, không phải chuỗi, và chế độ đồng bộ
    khai bằng `mode: "sync"` chứ không phải `stream: false`. Gửi sai thì nền
    tảng trả 400 validation_failed và chat lặng lẽ rơi về kịch bản có sẵn —
    nhìn bên ngoài y như chưa cấu hình agent.

    Mọi lỗi đều trả None để phần gọi dùng kịch bản có sẵn.
    """
    if not agent_configured():
        return None
    _mark_touched()
    try:
        async with httpx.AsyncClient(timeout=max(PEER_TIMEOUT, 60.0)) as client:
            r = await client.post(
                f"{AGENT_SERVICE_URL}/v1/agents/{AGENT_ID}/runs",
                headers={"Authorization": f"Bearer {AGENT_API_KEY}"},
                json={"input": {"message": question}, "mode": "sync"},
            )
            r.raise_for_status()
            body = r.json()
        output = body.get("output")
        return output if isinstance(output, str) and output.strip() else None
    except Exception:
        mark_degraded()
        return None
