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

import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Any

import httpx

logger = logging.getLogger("guardian-gateway.domain")

CUSTOMER_PROFILE_URL = os.getenv("CUSTOMER_PROFILE_SERVICE_URL", "http://customer-profile-service")
TRANSACTION_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://transaction-service")
RISK_SCORING_URL = os.getenv("RISK_SCORING_SERVICE_URL", "http://risk-scoring-service")
SCAM_KNOWLEDGE_URL = os.getenv("SCAM_KNOWLEDGE_SERVICE_URL", "http://scam-knowledge-service")
ACTION_FEEDBACK_URL = os.getenv("ACTION_FEEDBACK_SERVICE_URL", "http://action-feedback-service")
IDENTITY_URL = os.getenv("IDENTITY_SERVICE_URL", "http://identity-service")

# agent-service nằm ở namespace khác nên phải dùng tên đầy đủ trong cluster.
AGENT_SERVICE_URL = os.getenv("AGENT_SERVICE_URL", "")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
AGENT_ID = os.getenv("AGENT_ID", "")
# Agent thứ hai: Scam Shield — kiểm tra dấu hiệu lừa đảo cho lệnh chuyển tới stk
# mới. Cùng nền tảng/khoá, chỉ khác agent id.
SCAMSHIELD_AGENT_ID = os.getenv("SCAMSHIELD_AGENT_ID", "")

PEER_TIMEOUT = float(os.getenv("PEER_TIMEOUT_SECONDS", "5"))

# Bật/tắt toàn bộ việc gọi domain. Đặt "false" để chạy hoàn toàn bằng catalog.py
# (hữu ích khi trình bày offline hoặc khi cluster domain đang bảo trì).
DOMAIN_ENABLED = os.getenv("DOMAIN_ENABLED", "true").lower() != "false"

# Khách hàng của kịch bản demo. 100008 là người cao tuổi trong fraud case F01
# (giả danh công an) — đúng câu chuyện mà màn Scam Shield kể.
DEMO_CUSTOMER_ID = int(os.getenv("DEMO_CUSTOMER_ID", "100008"))
DEMO_FRAUD_CASE_ID = os.getenv("DEMO_FRAUD_CASE_ID", "F01")

# Khách hàng của PHIÊN hiện tại — đặt sau mỗi lần đăng nhập thành công qua
# identity-service. Gateway chưa có token/cookie và app demo chạy một người
# dùng một lúc, nên nhớ ở mức module là đủ; trước đây mọi endpoint neo cứng
# DEMO_CUSTOMER_ID nên đăng nhập kh100001 vẫn thấy danh bạ của 100008 — đây
# chính là lỗi "BEN trả về không đúng khách". Chưa đăng nhập hoặc đăng nhập
# degraded (identity chết) thì vẫn rơi về DEMO_CUSTOMER_ID để demo không gãy.
_session_customer_id: int | None = None


def set_session_customer(customer_id: int | None) -> None:
    global _session_customer_id
    _session_customer_id = customer_id


def current_customer_id() -> int:
    return _session_customer_id or DEMO_CUSTOMER_ID

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


async def _patch(base: str, path: str, payload: dict) -> Any | None:
    if not DOMAIN_ENABLED:
        return None
    _mark_touched()
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT) as client:
            r = await client.patch(f"{base}{path}", json=payload)
            r.raise_for_status()
            return r.json()
    except Exception:
        mark_degraded()
        return None


async def health(base: str) -> bool:
    """Một service có trả lời không. Dùng cho bảng tình trạng hệ thống bên Ops.

    Không đi qua _get: service chết là thông tin cần hiển thị, không phải lỗi
    khiến cả phản hồi bị đánh dấu degraded.
    """
    if not DOMAIN_ENABLED:
        return False
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT) as client:
            r = await client.get(f"{base}/health")
            return r.status_code == 200
    except Exception:
        return False


# ---- customer-profile-service ------------------------------------------------

async def customer(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}")


async def portfolio(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/portfolio")


async def beneficiaries(customer_id: int, include_full: bool = False) -> dict | None:
    # include_full chỉ dành cho màn danh bạ UI; các đường build ngữ cảnh agent
    # giữ mặc định masked.
    suffix = "?include_full=true" if include_full else ""
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/beneficiaries{suffix}")


async def account_events(customer_id: int) -> dict | None:
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/events")


async def resolve_beneficiary(customer_id: int, bank_code: str, account_no: str) -> dict | None:
    """Tra một số tài khoản người nhận: quen hay mới, tuổi tài khoản, quan hệ,
    trạng thái (SUSPECTED?). Đây là tín hiệu để quyết định có cần Scam Shield."""
    return await _post(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/beneficiaries/resolve",
                       {"bank_code": bank_code, "account_no": account_no})


async def scam_match(payload: dict) -> dict | None:
    """Đối chiếu lệnh chuyển với playbook lừa đảo trong scam-knowledge-service."""
    return await _post(SCAM_KNOWLEDGE_URL, "/scams/match", payload)


# ---- transaction-service -----------------------------------------------------

async def transactions(customer_id: int, date_from: str | None = None,
                       direction: str | None = None, limit: int = 300) -> dict | None:
    params: dict = {"limit": limit}
    if date_from:
        params["date_from"] = date_from
    if direction:
        params["direction"] = direction
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}", params)


async def create_transaction(payload: dict) -> dict | None:
    """Ghi một giao dịch vào transaction_history (POST /transactions)."""
    return await _post(TRANSACTION_URL, "/transactions", payload)



async def monthly_summary(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}/monthly-summary")


async def quarterly_summary(customer_id: int, quarters: int = 8) -> dict | None:
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}/quarterly-summary",
                      {"quarters": quarters})


async def monthly_comparison(customer_id: int, months: int = 6) -> dict | None:
    return await _get(TRANSACTION_URL, f"/transactions/{customer_id}/monthly-comparison",
                      {"months": months})


async def insights(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/customers/{customer_id}/insights")


async def savings_rates() -> dict | None:
    """Biểu lãi suất hiện tại của sản phẩm tiết kiệm.

    transaction-service join product × interest_rate × interest_rate_term và tự
    chọn đợt hiệu lực mới nhất cho từng cặp (sản phẩm, kỳ hạn)."""
    return await _get(TRANSACTION_URL, "/products/savings/rates")


async def product_rates(product_group: str = "SAVINGS") -> dict | None:
    """Biểu lãi suất theo (sản phẩm × kỳ hạn) — SAVINGS hoặc LOAN."""
    return await _get(TRANSACTION_URL, "/products/rates", {"product_group": product_group})


async def loan_options(amount: int, months: int, limit: int = 5) -> dict | None:
    """Các gói vay cho (số tiền, kỳ hạn) kèm tiền trả hàng tháng do service tính."""
    return await _get(TRANSACTION_URL, "/products/loan-options",
                      {"amount": amount, "months": months, "limit": limit})


async def savings_options(amount: int, months: int, limit: int = 5) -> dict | None:
    """Các gói tiết kiệm cho (số tiền, kỳ hạn) kèm lãi dự kiến do service tính."""
    return await _get(TRANSACTION_URL, "/products/savings-options",
                      {"amount": amount, "months": months, "limit": limit})


async def recommendations(customer_id: int) -> dict | None:
    return await _get(TRANSACTION_URL, f"/customers/{customer_id}/recommendations")


# ---- risk-scoring-service ----------------------------------------------------

async def precheck(payload: dict) -> dict | None:
    return await _post(RISK_SCORING_URL, "/transfer/precheck", payload)


async def scam_scenario(scenario_id: str) -> dict | None:
    """Một kịch bản lừa đảo trong playbook (khuyến cáo, câu hỏi, lựa chọn)."""
    return await _get(SCAM_KNOWLEDGE_URL, f"/scams/{scenario_id}")


async def intervene(payload: dict) -> dict | None:
    """Ghi câu trả lời của khách và lời khuyến cáo vào đúng dòng quyết định."""
    return await _post(RISK_SCORING_URL, "/transfer/intervene", payload)


async def risk_decision(decision_id: str) -> dict | None:
    """Đọc lại một quyết định đã chấm (điểm, yếu tố, câu hỏi, kịch bản)."""
    return await _get(RISK_SCORING_URL, f"/risk-decisions/{decision_id}")


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


async def customer_decisions(customer_id: int, limit: int = 50) -> dict | None:
    """Các lần chấm điểm của riêng một khách, mới nhất trước.

    Chi tiết case cần nó để nói "mức chuyển trung bình" và "đã cảnh báo mấy lần
    trong 90 ngày" bằng số thật của chính khách đó.
    """
    return await _get(RISK_SCORING_URL, "/risk-decisions",
                      {"customer_id": customer_id, "limit": limit})


async def risk_info() -> dict | None:
    """Cấu hình engine: trần điểm 6 yếu tố và ba ngưỡng pass/soft_warn/intervene.

    Màn "Mô hình & ngưỡng" đọc từ đây thay vì chép lại con số 40/75 sang FE —
    ngưỡng đổi bên risk-scoring thì màn hình đổi theo, không phải sửa hai nơi.
    """
    return await _get(RISK_SCORING_URL, "/info")


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


async def update_case(case_id: str, status: str, note: str | None = None) -> dict | None:
    """Đổi trạng thái một case.

    Đóng bằng CLOSED_FRAUD/CLOSED_LEGIT thì action-feedback tự sinh một feedback
    nguồn `ops` — kết luận của chuyên viên chính là nhãn huấn luyện đáng tin nhất,
    nên quyết định ở màn Ops phải đi qua đây chứ không chỉ nằm trong bộ nhớ.
    """
    return await _patch(ACTION_FEEDBACK_URL, f"/cases/{case_id}",
                        {"status": status, "note": note})


async def llm_traces(agent: str | None = None, status: str | None = None,
                     decision_id: str | None = None, limit: int = 50) -> dict | None:
    params: dict = {"limit": limit}
    if agent:
        params["agent"] = agent
    if status:
        params["status"] = status
    if decision_id:
        params["decision_id"] = decision_id
    return await _get(ACTION_FEEDBACK_URL, "/llm-traces", params)


async def llm_trace_stats() -> dict | None:
    return await _get(ACTION_FEEDBACK_URL, "/llm-traces/stats")


async def notifications(customer_id: int) -> dict | None:
    return await _get(ACTION_FEEDBACK_URL, f"/customers/{customer_id}/notifications")


async def record_action(payload: dict) -> dict | None:
    return await _post(ACTION_FEEDBACK_URL, "/actions", payload)


# ---- identity-service --------------------------------------------------------

async def auth_verify(username: str, password: str) -> dict | None:
    """Xác thực tên đăng nhập + mật khẩu qua identity-service.

    identity-service là nơi DUY NHẤT đọc password_hash; gateway chỉ chuyển tiếp
    và không bao giờ thấy mật khẩu đã băm. Trả None khi service không gọi được,
    để phần gọi giữ luồng demo chạy tiếp thay vì chặn người trình bày.

    Chỉ /auth/verify (chỉ đọc) được gọi ở đây. Việc ghi nhận số lần đăng nhập
    sai và khoá tài khoản nằm ở /users/{id}/login-attempt, CỐ TÌNH không gọi từ
    gateway: một người trình bày gõ nhầm mật khẩu vài lần không nên làm khoá tài
    khoản demo ngay giữa buổi.
    """
    return await _post(IDENTITY_URL, "/auth/verify", {"username": username, "password": password})


async def user_permissions(user_id: int) -> dict | None:
    """Quyền hiệu lực của một tài khoản, dùng để gate đăng nhập Ops.

    `role_defined=False` nghĩa là vai trò của user chưa được khai trong
    app_role — coi đó là KHÔNG có quyền vận hành, khác hẳn "vai trò không có
    quyền nào" (mảng rỗng nhưng role_defined=True). Nhầm hai trường hợp này sẽ
    chặn nhầm người dùng hợp lệ hoặc cho lọt người không có quyền.
    """
    return await _get(IDENTITY_URL, f"/users/{user_id}/permissions")


async def user_by_username(username: str) -> dict | None:
    """Tra một tài khoản theo tên đăng nhập — dùng để hiện tên chuyên viên đang
    trực trên `GET /api/ops/session` sau khi họ đã đăng nhập ở FE."""
    return await _get(IDENTITY_URL, f"/users/by-username/{username}")


# ---- agent-service (chỉ gọi, KHÔNG sửa) --------------------------------------

def agent_configured(agent_id: str | None = None) -> bool:
    """agent-service chỉ được dùng khi đã cấu hình đủ địa chỉ, khóa và agent.

    Nền tảng agent có xác thực riêng và cần một agent được tạo sẵn trong đó.
    Phần thiết lập ấy thuộc phạm vi người khác nên gateway không tự làm; khi
    chưa đủ cấu hình thì phần gọi dùng dữ liệu/kịch bản dự phòng.
    """
    return bool(AGENT_SERVICE_URL and AGENT_API_KEY and (agent_id or AGENT_ID))


async def _write_llm_trace(kind: str, question: str, answer: str | None,
                           status: str, latency_ms: int, agent_id: str,
                           customer_id: int | None, decision_id: str | None) -> None:
    """Ghi lại một lượt gọi Agent Platform vào bảng llm_trace.

    Trước đây không ai ghi bảng này lúc chạy: tool `log_llm_call` không được gán
    cho trợ lý nào, còn gateway thì gọi agent xong là thôi. Hệ quả là màn "Nhật
    ký quyết định AI" chỉ hiện dữ liệu mẫu, và mọi lượt gọi thật — kể cả lượt
    hỏng — không để lại dấu vết nào.

    Ghi hỏng thì bỏ qua: nhật ký không được phép làm hỏng câu trả lời cho khách.
    Vì vậy hàm này KHÔNG dùng _post (nó đánh dấu degraded) — hỏng ở đây không có
    nghĩa là dữ liệu trả cho người dùng bị suy giảm.
    """
    if not DOMAIN_ENABLED:
        return
    payload = {
        "decision_id": decision_id,
        "customer_id": customer_id if customer_id is not None else DEMO_CUSTOMER_ID,
        "agent": kind,
        # Response Run của Agent Platform không trả tên model (RunUsage chỉ có số
        # token), nên gateway thật sự không biết model nào đã chạy. Ghi đúng thứ
        # mình biết thay vì đoán.
        "model": "agent-platform",
        "prompt_key": f"{kind}@{agent_id[:8]}",
        # action-feedback tự chạy mask_free_text khi ghi; gateway không có
        # common.py nên không tự mask được.
        "prompt_masked": question[:500],
        "response": (answer or "")[:1000],
        "latency_ms": latency_ms,
        "status": status,
    }
    try:
        async with httpx.AsyncClient(timeout=PEER_TIMEOUT) as client:
            await client.post(f"{ACTION_FEEDBACK_URL}/llm-traces", json=payload)
    except Exception:
        return


class AgentBusy(Exception):
    """Hội thoại còn một lần xử lý chưa xong (nền tảng trả 409 run_in_progress).

    Khác hẳn "agent hỏng": người dùng chỉ cần đợi câu trước trả lời xong. Nuốt
    nó thành lỗi chung sẽ khiến màn hình im lặng rơi về kịch bản viết sẵn.
    """


async def agent_stream(question: str, agent_id: str | None = None, kind: str = "copilot",
                       customer_id: int | None = None, session_key: str | None = None):
    """Phát từng mẩu chữ của agent NGAY khi nền tảng gửi ra.

    Đường `mode=sync` phải chờ trọn lần xử lý rồi mới có chữ, nên màn hình khách
    trắng suốt thời gian mô hình chạy. Endpoint stream của nền tảng gửi SSE với
    `event` trùng `type`; ở đây chỉ quan tâm `message.delta` (có `text`) và hai
    sự kiện kết thúc `run.completed` / `run.failed`.

    Ngắt kết nối giữa chừng sẽ HỦY lần xử lý bên nền tảng — đúng thiết kế của nó.

    Ném `AgentBusy` khi gặp 409. Mọi hỏng khác kết thúc dòng chảy êm và để lại
    một dòng `llm_trace`, giống đường sync.
    """
    aid = agent_id or AGENT_ID
    if not agent_configured(aid):
        return
    _mark_touched()
    started = time.monotonic()
    body = {"input": {"message": question}}
    if session_key:
        body["session_key"] = session_key

    full: list[str] = []
    status, logged = "ok", ""
    try:
        async with httpx.AsyncClient(timeout=max(PEER_TIMEOUT, 120.0)) as client:
            async with client.stream("POST", f"{AGENT_SERVICE_URL}/v1/agents/{aid}/runs/stream",
                                     headers={"X-API-Key": AGENT_API_KEY}, json=body) as r:
                if r.status_code == 409:
                    await r.aread()
                    raise AgentBusy()
                r.raise_for_status()
                async for line in r.aiter_lines():
                    # Bỏ qua heartbeat (": ping") và dòng event:/id:; chỉ data mới có nội dung.
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind_of = event.get("type")
                    if kind_of == "message.delta":
                        text = event.get("text") or ""
                        if text:
                            full.append(text)
                            yield text
                    elif kind_of == "run.failed":
                        status = "error"
                        logged = f"run.failed: {(event.get('error') or {}).get('code', 'không rõ')}"
                        break
                    elif kind_of == "run.completed":
                        break
    except AgentBusy:
        raise
    except Exception as err:
        if isinstance(err, httpx.TimeoutException):
            status, logged = "timeout", "stream quá hạn"
        else:
            status, logged = "error", f"{type(err).__name__}: {err}"
        logger.warning("agent_stream hỏng (%s): %s", kind, logged)
        mark_degraded()
    finally:
        answer = "".join(full)
        if status == "ok" and not answer:
            status, logged = "error", "stream không có nội dung"
        try:
            await _write_llm_trace(kind, question, logged or answer, status,
                                   round((time.monotonic() - started) * 1000),
                                   aid, customer_id, None)
        except Exception as err:  # nhật ký hỏng không được làm hỏng câu trả lời
            logger.warning("không ghi được nhật ký lượt stream: %s", err)


def copilot_session_key(customer_id: int | None = None) -> str:
    """Khoá hội thoại của Copilot, một hội thoại cho mỗi khách.

    Thiếu khoá này thì mỗi câu hỏi mở một hội thoại mới bên Agent Platform và
    trợ lý không nhớ gì: hỏi "còn tháng trước thì sao?" được trả lời như câu đầu.
    `session_key` là cách nền tảng cho phép người gọi bằng API key tự đặt tên
    hội thoại (duy nhất theo trợ lý + khoá).
    """
    return f"copilot-{customer_id if customer_id is not None else DEMO_CUSTOMER_ID}"


async def agent_answer(question: str, agent_id: str | None = None, kind: str = "copilot",
                       customer_id: int | None = None, decision_id: str | None = None,
                       session_key: str | None = None) -> str | None:
    """Hỏi agent-service một câu và lấy câu trả lời dạng văn bản.

    Dùng đúng hợp đồng công khai của nền tảng (api/openapi.yaml trong repo
    hackathon-agent-platform):

        POST /v1/agents/{agentId}/runs
        X-API-Key: <khóa api-key, scope runs:write>
        {"input": {"message": "..."}, "mode": "sync"}
        → 200 {"status": "...", "output": "câu trả lời", ...}

    Hai cái dễ sai, cả hai đều cho ra 4xx rồi chat lặng lẽ rơi về kịch bản —
    nhìn bên ngoài y như chưa cấu hình agent:

    1) XÁC THỰC. api-key phải đi trong header `X-API-Key`, KHÔNG phải
       `Authorization: Bearer`. Endpoint runs nhận cả JWT (bearer) lẫn api-key,
       nhưng api-key chỉ được nhận diện qua X-API-Key; gửi api-key dưới dạng
       Bearer sẽ nhận 401 unauthenticated.
    2) THÂN REQUEST. `input` là OBJECT có khóa `message`, không phải chuỗi, và
       chế độ đồng bộ khai bằng `mode: "sync"` chứ không phải `stream: false`.

    Mọi lỗi đều trả None để phần gọi dùng kịch bản có sẵn.
    """
    aid = agent_id or AGENT_ID
    if not agent_configured(aid):
        return None
    _mark_touched()
    started = time.monotonic()

    def elapsed_ms() -> int:
        return round((time.monotonic() - started) * 1000)

    answer: str | None = None
    try:
        async with httpx.AsyncClient(timeout=max(PEER_TIMEOUT, 60.0)) as client:
            r = await client.post(
                f"{AGENT_SERVICE_URL}/v1/agents/{aid}/runs",
                headers={"X-API-Key": AGENT_API_KEY},
                json={
                    "input": {"message": question},
                    "mode": "sync",
                    **({"session_key": session_key} if session_key else {}),
                },
            )
            r.raise_for_status()
            body = r.json()
        output = body.get("output")
        answer = output if isinstance(output, str) and output.strip() else None
        status, logged = ("ok", answer) if answer else ("error", "agent trả lời rỗng")
    except Exception as err:
        # Ba lý do hỏng khác hẳn nhau nhưng trước đây cho ra cùng một None không
        # dấu vết: 401 vì sai cách gửi khóa, 404 vì agent id không có trong môi
        # trường này, 409 vì hội thoại còn run đang chạy. Ghi lại để lần sau đọc
        # nhật ký là biết, thay vì phải đoán.
        if isinstance(err, httpx.TimeoutException):
            status, logged = "timeout", f"quá {max(PEER_TIMEOUT, 60.0):.0f}s không có phản hồi"
        elif isinstance(err, httpx.HTTPStatusError):
            status, logged = "error", f"HTTP {err.response.status_code}: {err.response.text[:200]}"
        else:
            status, logged = "error", f"{type(err).__name__}: {err}"
        logger.warning("agent_answer hỏng (%s): %s", kind, logged)
        mark_degraded()

    # Ghi nhật ký nằm NGOÀI khối trên và có lưới riêng: nhật ký là việc phụ,
    # hỏng ở đây không được phép nuốt mất câu trả lời đã lấy được.
    try:
        await _write_llm_trace(kind, question, logged, status, elapsed_ms(),
                               aid, customer_id, decision_id)
    except Exception as err:
        logger.warning("không ghi được nhật ký lượt gọi agent: %s", err)
    return answer
