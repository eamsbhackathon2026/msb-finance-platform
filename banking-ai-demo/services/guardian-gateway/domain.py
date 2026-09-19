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

import asyncio
import json
import logging
import re
import os
import time
import unicodedata
from contextvars import ContextVar
from typing import Any, NamedTuple

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
# Agent thứ ba: chỉ trích ý định chuyển tiền từ MỘT câu, không gắn công cụ nào.
# Tách riêng vì Chat Banking cần trả lời trong vài giây; agent Copilot mang 11
# công cụ và prompt dài nên một lượt mất 10-20 giây, quá chậm cho màn giao dịch.
CHATBANKING_AGENT_ID = os.getenv("CHATBANKING_AGENT_ID", "")

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


# Khách của TỪNG REQUEST — middleware đọc cookie guardian_cid rồi đặt vào đây.
# Biến global ở trên là "ai đăng nhập cuối thắng" cho CẢ process: nhiều người
# test song song làm danh bạ/số dư nhảy qua lại giữa các khách. Cookie gắn với
# từng trình duyệt nên mỗi người thấy đúng dữ liệu của mình; thiếu cookie mới
# rơi về global rồi tới khách demo.
_request_customer: ContextVar[int | None] = ContextVar("guardian_request_customer", default=None)


def set_request_customer(customer_id: int | None) -> None:
    _request_customer.set(customer_id)


def current_customer_id() -> int:
    return _request_customer.get() or _session_customer_id or DEMO_CUSTOMER_ID

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


async def deposits_maturing(customer_id: int, days: int = 0) -> dict | None:
    """Sổ tiết kiệm có maturity_date <= hôm nay + days (0 = đến hạn hôm nay),
    gồm cả sổ đã quá hạn chưa tái tục. Nguồn của thông báo 'sổ đến hạn' bên FE."""
    return await _get(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/deposits/maturing", {"days": days})


async def account_posting(account_id: int, direction: str, amount: int) -> dict | None:
    """Hạch toán ghi nợ (DEBIT) / ghi có (CREDIT) tài khoản thanh toán.
    Trả None khi service lỗi HOẶC ghi nợ vượt số dư (profile trả 409)."""
    return await _post(CUSTOMER_PROFILE_URL, f"/accounts/{account_id}/postings",
                       {"direction": direction, "amount": amount})


async def create_deposit(customer_id: int, payload: dict) -> dict | None:
    """Mở sổ tiền gửi qua customer-profile-service (đã ghi nợ TK nguồn trước)."""
    return await _post(CUSTOMER_PROFILE_URL, f"/customers/{customer_id}/deposits", payload)


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
                     decision_id: str | None = None, customer_id: int | None = None,
                     since: str | None = None, until: str | None = None,
                     limit: int = 50) -> dict | None:
    params: dict = {"limit": limit}
    if agent:
        params["agent"] = agent
    if status:
        params["status"] = status
    if decision_id:
        params["decision_id"] = decision_id
    if customer_id is not None:
        params["customer_id"] = customer_id
    if since:
        params["since"] = since
    if until:
        params["until"] = until
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


# Lời nhắn của khách tự xưng là hệ thống là hình dạng kinh điển của một cú tiêm chỉ
# dẫn: kẻ lừa đảo đọc cho nạn nhân gõ vào khung chat để chính trợ lý của ngân hàng
# nói câu dụ chuyển tiền.
#
# Bắt theo HÌNH DẠNG chứ không theo danh sách từ khoá, và thà bỏ sót còn hơn báo
# nhầm: một banner đỏ trên câu hỏi thật làm hỏng lòng tin nhanh hơn là bỏ lọt. Vì
# vậy nhãn hệ thống chỉ tính khi ĐÓNG NGOẶC — "Hệ thống: tôi không đăng nhập được"
# là câu khách nói thật, còn "[HE THONG] ..." thì không ai gõ một cách vô tình.
_IMPERSONATES_SYSTEM = re.compile(
    r"(^|\n)\s*[\[<]\s*(he thong|system|instruction|prompt|admin)\b[^\]>\n]{0,40}[\]>]",
    re.IGNORECASE,
)
# Dòng bối cảnh do gateway chèn, bị khách gõ lại để tự chọn mã khách. Bắt bất kể có
# ngoặc hay không: đây là ca duy nhất mà lọt đồng nghĩa với đọc dữ liệu người khác.
_FORGED_CUSTOMER_CONTEXT = re.compile(r"customer_id\s*[:=]\s*\d+", re.IGNORECASE)
# Ranh giới từ là bắt buộc: thiếu nó thì "quen" khớp trong "quên/thói quen/làm quen"
# và "lenh" khớp trong "lệnh chuyển tiền", tức là bắt nhầm đúng những câu ngân hàng
# bình thường nhất.
_OVERRIDES_INSTRUCTIONS = re.compile(
    r"\b(bo qua|khong can theo)\b[^\n]{0,20}\b(huong dan|chi dan|quy tac|lenh)\b"
    r"|\bignore\s+(all\s+|every\s+)?(previous|prior)\s+instructions?\b",
    re.IGNORECASE,
)


def strip_diacritics(text: str) -> str:
    """Bỏ dấu tiếng Việt: cú tiêm chỉ dẫn hay được gõ không dấu để né bộ lọc."""
    thay = unicodedata.normalize("NFD", text)
    thay = "".join(c for c in thay if unicodedata.category(c) != "Mn")
    return thay.replace("đ", "d").replace("Đ", "D")


def suspicious_instruction(question: str) -> str | None:
    """Lý do ngắn nếu tin nhắn của khách mang chỉ dẫn mạo danh, ngược lại None."""
    flat = strip_diacritics(question)
    if _FORGED_CUSTOMER_CONTEXT.search(flat):
        return "Tin nhắn chứa dòng bối cảnh hệ thống do người khác soạn sẵn."
    if _IMPERSONATES_SYSTEM.search(flat):
        return "Tin nhắn tự xưng là thông báo của hệ thống."
    if _OVERRIDES_INSTRUCTIONS.search(flat):
        return "Tin nhắn yêu cầu trợ lý bỏ qua hướng dẫn của ngân hàng."
    return None


def strip_forged_context(question: str) -> str:
    """Bỏ các dòng khách tự gõ giả dòng bối cảnh của gateway.

    Cảnh báo thôi là chưa đủ cho riêng ca này: `customer_id` đi vào thân prompt và
    mô hình là bên điền nó vào đường dẫn công cụ, nên một dòng giả còn nằm trong
    câu hỏi vẫn là một lời mời đọc dữ liệu khách khác. Cắt bỏ rồi mới chèn dòng thật.
    """
    giu = [d for d in question.split("\n") if not _FORGED_CUSTOMER_CONTEXT.search(strip_diacritics(d))]
    return "\n".join(giu).strip()


def with_customer_context(question: str, customer_id: int | None) -> str:
    """Gắn mã khách vào câu hỏi gửi agent.

    Bộ công cụ hiện tại nhận customer_id trên ĐƯỜNG DẪN
    (/transactions/{customer_id}/...), mà agent thì không có cách nào tự biết
    khách đang đăng nhập là ai — gateway mới là nơi giữ phiên. Không truyền thì
    agent đoán bừa hoặc bỏ trống rồi trả lời "chưa xem được dữ liệu".

    Đặt ở đầu câu và ghi rõ là bối cảnh hệ thống, để mô hình không đọc nhầm
    thành một phần câu hỏi của khách.
    """
    if not customer_id:
        return question
    return f"[Bối cảnh hệ thống: customer_id={customer_id}]\n{strip_forged_context(question)}"


# Nền tảng KHÔNG bao giờ gửi display_name rỗng: công cụ nào chưa đặt nhãn thì nó
# rơi về tên hiển thị, rồi tới tên kỹ thuật (`ToolStepLabel` bên agent-service).
# Vậy lá chắn ở đây phải nhận ra HÌNH DẠNG của một tên công cụ — `http_...`,
# `mcp_<server>_...`, có thể kèm hậu tố băm khi trùng tên — chứ không phải chuỗi
# rỗng. Đây là bộ lọc, không phải bảng ánh xạ: gateway vẫn không tự đặt tên cho
# bất cứ bước nào.
_TEN_KY_THUAT = re.compile(r"^(?:http|mcp)_[a-z0-9_]+$", re.IGNORECASE)


def _nhan_cho_nguoi_doc(display_name: str, tool_name: str) -> str:
    """Nhãn hiện được cho khách, hoặc chuỗi rỗng nếu nền tảng chưa có nhãn thật."""
    label = (display_name or "").strip()
    if not label or label == (tool_name or "").strip() or _TEN_KY_THUAT.match(label):
        return ""
    return label


class AgentStep(NamedTuple):
    """Một lần trợ lý dùng công cụ, kể theo cách người đọc hiểu được.

    `label` là chữ nền tảng gửi xuống trong `display_name` của `tool.started` —
    gateway KHÔNG tự đặt tên và không có bảng ánh xạ nào. Nhãn thuộc về nơi
    người vận hành sửa được nó, tức Agent Platform.
    """
    call_id: str
    label: str
    status: str            # running | done | error
    duration_ms: int | None = None


async def agent_events(question: str, agent_id: str | None = None, kind: str = "copilot",
                       customer_id: int | None = None, session_key: str | None = None,
                       decision_id: str | None = None):
    """Phát mọi thứ nền tảng gửi ra NGAY khi nó gửi: chữ và cả bước dùng công cụ.

    Đường `mode=sync` phải chờ trọn lần xử lý rồi mới có chữ, nên màn hình khách
    trắng suốt thời gian mô hình chạy. Endpoint stream gửi SSE với `event` trùng
    `type`; ở đây quan tâm `message.delta` (có `text`), hai sự kiện công cụ
    `tool.started` / `tool.finished`, và hai sự kiện kết thúc `run.completed` /
    `run.failed`.

    Yield tuple `(loại, giá trị)`:

        ("text", "mẩu chữ")
        ("reasoning", "mẩu tóm tắt suy nghĩ")
        ("step", AgentStep(...))
        ("output", "toàn văn câu trả lời")   — chỉ ở `run.completed`

    `output` tồn tại vì bên gọi đồng bộ cần nguyên văn câu trả lời mà nền tảng
    chốt lại, thay vì tự ghép các mẩu delta rồi hy vọng không sót.

    Ngắt kết nối giữa chừng sẽ HỦY lần xử lý bên nền tảng — đúng thiết kế của nó.

    Ném `AgentBusy` khi gặp 409. Mọi hỏng khác kết thúc dòng chảy êm và để lại
    một dòng `llm_trace`.
    """
    aid = agent_id or AGENT_ID
    if not agent_configured(aid):
        return
    _mark_touched()
    started = time.monotonic()
    body = {"input": {"message": with_customer_context(question, customer_id)}}
    if session_key:
        body["session_key"] = session_key

    full: list[str] = []
    # Câu nền tảng chốt lại ở run.completed. Giữ riêng vì nhật ký phải ghi được
    # câu trả lời thật, kể cả khi mô hình không phát mẩu delta nào.
    output_cuoi = ""
    status, logged = "ok", ""
    # Nhãn của lượt gọi nào thì `tool.finished` không nhắc lại, nó chỉ gửi call_id.
    labels: dict[str, str] = {}
    # Bước đã bắt đầu mà chưa thấy kết thúc: phải đóng lại, nếu không màn hình
    # quay vòng mãi ở một việc đã dừng từ lâu.
    dang_chay: dict[str, str] = {}
    # Mã và thân phản hồi khi nền tảng từ chối. 401 vì sai cách gửi khóa và 404 vì
    # sai agent id là hai lỗi khó đoán nhất; nuốt thân phản hồi là bắt người sau
    # ngồi đoán lại từ đầu.
    ma_http, than_http = 0, ""
    try:
      try:
          async with httpx.AsyncClient(timeout=max(PEER_TIMEOUT, 120.0)) as client:
              async with client.stream("POST", f"{AGENT_SERVICE_URL}/v1/agents/{aid}/runs/stream",
                                       headers={"X-API-Key": AGENT_API_KEY}, json=body) as r:
                  if r.status_code >= 400:
                      ma_http = r.status_code
                      than_http = (await r.aread()).decode("utf-8", "replace").strip()[:200]
                      if ma_http == 409:
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
                      if kind_of == "reasoning.delta":
                          # Suy nghĩ của mô hình. KHÔNG phải câu trả lời: không gom
                          # vào `full` nên không vào nhật ký lẫn phần chữ trả khách.
                          #
                          # Cả hai loại đều đi tiếp, kể cả "raw". Đây là quyết định
                          # sản phẩm: suy nghĩ là năng lực của mô hình, muốn nó nghĩ
                          # bằng tiếng Việt thì đổi mô hình, chứ không giấu đi. Đã
                          # đo trên GLM qua GreenNode: chuỗi thô ra tiếng Anh dù hội
                          # thoại tiếng Việt và dài gấp nhiều lần câu trả lời, nên
                          # phần hiển thị bên FE chỉ lấy câu cuối và gỡ markdown.
                          text = event.get("text") or ""
                          if text:
                              yield "reasoning", text
                      elif kind_of == "message.delta":
                          text = event.get("text") or ""
                          if text:
                              full.append(text)
                              yield "text", text
                      elif kind_of == "tool.started":
                          call_id = event.get("call_id") or ""
                          # Chưa ai đặt nhãn cho công cụ này thì im lặng, còn hơn
                          # đưa http_get_portfolio ra trước mặt khách.
                          label = _nhan_cho_nguoi_doc(event.get("display_name") or "",
                                                      event.get("tool_name") or "")
                          if call_id and label:
                              labels[call_id] = label
                              dang_chay[call_id] = label
                              yield "step", AgentStep(call_id, label, "running")
                      elif kind_of == "tool.finished":
                          call_id = event.get("call_id") or ""
                          if call_id in labels:
                              dang_chay.pop(call_id, None)
                              yield "step", AgentStep(
                                  call_id, labels[call_id],
                                  "done" if event.get("ok") else "error",
                                  event.get("duration_ms"))
                      elif kind_of == "run.failed":
                          status = "error"
                          logged = f"run.failed: {(event.get('error') or {}).get('code', 'không rõ')}"
                          break
                      elif kind_of == "run.completed":
                          output_cuoi = ((event.get("run") or {}).get("output") or "").strip()
                          if output_cuoi:
                              yield "output", output_cuoi
                          break
      except AgentBusy:
        # Nói rõ 409 trong nhật ký: "bận" khác hẳn "hỏng", và người đọc nhật ký
        # cần phân biệt được hai thứ đó.
        status, logged = "error", f"HTTP 409: {than_http or 'hội thoại còn một lần xử lý chưa xong'}"
        raise
      except asyncio.CancelledError:
        # Bên gọi bọc asyncio.wait_for và đã hết giờ. CancelledError là
        # BaseException nên nhánh Exception bên dưới KHÔNG thấy nó; thiếu nhánh
        # này thì mọi lượt quá hạn được ghi là "ok" và màn nhật ký AI đếm chúng
        # thành công.
        status, logged = "timeout", "bên gọi hủy giữa chừng"
        raise
      except Exception as err:
        if isinstance(err, httpx.TimeoutException):
            status, logged = "timeout", "stream quá hạn"
        elif ma_http:
            status, logged = "error", f"HTTP {ma_http}: {than_http}"
        else:
            status, logged = "error", f"{type(err).__name__}: {err}"
        logger.warning("agent_events hỏng (%s): %s", kind, logged)
        mark_degraded()
      # Tới được đây nghĩa là generator còn sống, nên đóng nốt các bước dở dang.
      for call_id, label in dang_chay.items():
          yield "step", AgentStep(call_id, label, "error")
    finally:
        # Câu chốt lại thắng phần ghép từ delta: bên nền tảng mới là nơi biết
        # câu trả lời cuối cùng trông như thế nào.
        answer = output_cuoi or "".join(full)
        if status == "ok" and not answer:
            status, logged = "error", "stream không có nội dung"
        try:
            await _write_llm_trace(kind, question, logged or answer, status,
                                   round((time.monotonic() - started) * 1000),
                                   aid, customer_id, decision_id)
        except Exception as err:  # nhật ký hỏng không được làm hỏng câu trả lời
            logger.warning("không ghi được nhật ký lượt stream: %s", err)


async def agent_stream(question: str, agent_id: str | None = None, kind: str = "copilot",
                       customer_id: int | None = None, session_key: str | None = None):
    """Chỉ phần chữ của `agent_events`, cho bên gọi không quan tâm bước công cụ."""
    async for loai, gia_tri in agent_events(question, agent_id, kind, customer_id, session_key):
        if loai == "text":
            yield gia_tri


def copilot_session_key(customer_id: int) -> str:
    """Khoá hội thoại của Copilot, một hội thoại cho mỗi khách.

    Thiếu khoá này thì mỗi câu hỏi mở một hội thoại mới bên Agent Platform và
    trợ lý không nhớ gì: hỏi "còn tháng trước thì sao?" được trả lời như câu đầu.
    `session_key` là cách nền tảng cho phép người gọi bằng API key tự đặt tên
    hội thoại (duy nhất theo trợ lý + khoá).

    `customer_id` BẮT BUỘC và không có giá trị mặc định: khoá từng rơi về
    DEMO_CUSTOMER_ID khi bên gọi quên truyền, nên mọi khách đăng nhập cùng ghi
    vào MỘT hội thoại. Khách sau đọc được số dư và bảng chi tiêu của khách trước
    ngay trong ngữ cảnh của mô hình.
    """
    return f"copilot-{customer_id}"


async def agent_answer_with_steps(question: str, agent_id: str | None = None, kind: str = "copilot",
                                  customer_id: int | None = None, decision_id: str | None = None,
                                  session_key: str | None = None) -> tuple[str | None, list[AgentStep]]:
    """Hỏi agent một câu, lấy về câu trả lời VÀ các bước nó đã đi qua.

    Đi bằng endpoint stream chứ không phải `mode=sync`, vì chỉ dòng sự kiện mới
    mang `display_name` của công cụ. Kết quả của một run đồng bộ
    (`tool_results[]`) chỉ có tên công cụ dạng kỹ thuật, mà thứ đó thì không được
    phép đưa ra màn khách.

    Câu trả lời ưu tiên `run.output` nền tảng chốt lại ở `run.completed`; chỉ khi
    thiếu mới ghép từ các mẩu delta.

    Mọi lỗi — kể cả 409 vì hội thoại đang bận — đều trả `(None, [])` để bên gọi
    dùng kịch bản dự phòng, đúng như hành vi cũ của `agent_answer`.
    """
    mau: list[str] = []
    output: str | None = None
    buoc: dict[str, AgentStep] = {}
    try:
        async for loai, gia_tri in agent_events(question, agent_id, kind, customer_id,
                                                session_key, decision_id):
            if loai == "text":
                mau.append(gia_tri)
            elif loai == "output":
                output = gia_tri
            elif loai == "step":
                # Giữ theo call_id: bước kết thúc ghi đè bước đang chạy, thứ tự
                # xuất hiện lần đầu được bảo toàn.
                buoc[gia_tri.call_id] = gia_tri
    except AgentBusy:
        return None, []
    cau = output or "".join(mau).strip()
    return (cau or None), list(buoc.values())


async def agent_answer(question: str, agent_id: str | None = None, kind: str = "copilot",
                       customer_id: int | None = None, decision_id: str | None = None,
                       session_key: str | None = None) -> str | None:
    """Hỏi agent-service một câu và lấy câu trả lời dạng văn bản.

    Vỏ mỏng của `agent_answer_with_steps` cho những chỗ chỉ cần chữ. Nhật ký
    `llm_trace` do `agent_events` ghi, nên một lượt gọi vẫn để lại đúng một dòng.
    """
    answer, _ = await agent_answer_with_steps(question, agent_id, kind, customer_id,
                                              decision_id, session_key)
    return answer
