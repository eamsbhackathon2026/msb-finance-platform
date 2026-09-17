"""risk-scoring-service

Sở hữu: risk_decision + hai view ops_summary, ops_decision_log.

Là trung tâm của luồng phòng chống rủi ro thanh toán. Một lệnh chuyển tiền = một
dòng risk_decision, đi qua ba bước và luôn cập nhật trên cùng dòng đó:

    POST /transfer/precheck   → chấm điểm, tạo dòng, trả câu hỏi nếu cần can thiệp
    POST /transfer/intervene  → ghi câu trả lời của khách, trả khuyến cáo
    POST /transfer/action     → ghi hành động cuối và kết cục

Điểm số, mức độ và recommended_action do engine tất định quyết định — LLM chỉ được
ghi vào llm_reasons / question / advice_body. Nhờ vậy mọi quyết định đều tái lập
được và kiểm toán được, không phụ thuộc vào việc LLM hôm nay trả lời thế nào.

Service gọi sang customer-profile-service và scam-knowledge-service qua HTTP. Vì
5 service deploy độc lập, mọi lời gọi chéo đều có thể hỏng: khi đó engine vẫn chấm
điểm trên phần dữ liệu lấy được và đánh dấu `degraded` thay vì trả lỗi — thà cảnh
báo thiếu ngữ cảnh còn hơn để giao dịch đi qua mà không ai chấm.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    db_health,
    execute_returning,
    install_db_error_handlers,
    mask_account,
    mask_free_text,
    now_vn,
    num,
    openapi_description,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
)

SERVICE_NAME = "risk-scoring-service"
OWNS_TABLES = ["risk_decision", "ops_summary (view)", "ops_decision_log (view)"]

CUSTOMER_PROFILE_URL = os.getenv("CUSTOMER_PROFILE_SERVICE_URL", "http://customer-profile-service")
SCAM_KNOWLEDGE_URL = os.getenv("SCAM_KNOWLEDGE_SERVICE_URL", "http://scam-knowledge-service")
ACTION_FEEDBACK_URL = os.getenv("ACTION_FEEDBACK_SERVICE_URL", "http://action-feedback-service")
TRANSACTION_URL = os.getenv("TRANSACTION_SERVICE_URL", "http://transaction-service")
PEER_TIMEOUT = float(os.getenv("PEER_TIMEOUT_SECONDS", "5"))

# Trần điểm của từng yếu tố. Tổng trần = 110 nên điểm thô bị chặn ở 100;
# thiết kế này để một giao dịch phải xấu ở nhiều mặt mới chạm ngưỡng can thiệp,
# chứ không phải cứ số tiền lớn là bị chặn.
FACTOR_CAPS = {
    "amount_deviation": 25,
    "new_beneficiary": 20,
    "time_of_day": 10,
    "behavior_drift": 20,
    "relationship_history": 15,
    "recent_context": 20,
}
LEVEL_SOFT_WARN = 40
LEVEL_INTERVENE = 75

# Sự kiện xảy ra ngay trước lệnh chuyển — càng gần càng đáng ngờ.
EVENT_WEIGHTS = {
    "SAVINGS_CLOSED": 20,    # tất toán sổ rồi chuyển đi: mẫu kinh điển của giả danh công an
    "NEW_DEVICE_LOGIN": 16,  # dấu hiệu chiếm quyền thiết bị
    "PASSWORD_RESET": 14,
    "LIMIT_RAISED": 12,      # nâng hạn mức để chuyển vượt mức thường ngày
    "INBOUND_UNKNOWN": 10,   # nhận tiền lạ rồi được yêu cầu "chuyển trả"
}

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": 'transfer',
        "description": (
            'Ba bước của một lệnh chuyển tiền, tất cả ghi lên cùng một dòng quyết định. Điểm số và hành động khuyến nghị do engine quyết định, LLM không được đổi.'
        ),
    },
    {
        "name": 'decision',
        "description": (
            'Tra cứu lại quyết định đã chấm — mỗi quyết định lưu đủ 6 yếu tố thành phần nên luôn giải thích được vì sao ra điểm đó.'
        ),
    },
    {
        "name": 'ops',
        "description": (
            'Số liệu vận hành: đã ngăn chặn bao nhiêu, còn bao nhiêu vẫn đi qua sau cảnh báo.'
        ),
    },
    {
        "name": 'legacy',
        "description": (
            'Endpoint chấm điểm theo cờ truyền tay, giữ để tương thích ngược. Luồng thật nên dùng `/transfer/precheck` vì nó tự lấy ngữ cảnh khách hàng.'
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary='Chấm điểm rủi ro tất định 0-100 theo 6 yếu tố và quản lý vòng đời quyết định precheck → intervene → action.',
    description=openapi_description(SERVICE_NAME, 'Chấm điểm rủi ro tất định 0-100 theo 6 yếu tố và quản lý vòng đời quyết định precheck → intervene → action.', OWNS_TABLES),
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
class PrecheckRequest(BaseModel):
    customer_id: int
    account_id: int
    amount: float = Field(..., gt=0, description="Số tiền chuyển, VND")
    beneficiary_bank_code: str
    beneficiary_account_no: str = Field(
        ..., description="Số TK nhận; không bao giờ xuất hiện lại trong response"
    )
    memo: str | None = Field(None, description="Nội dung chuyển khoản khách nhập")
    tx_time: str | None = Field(None, description="ISO 8601; bỏ trống = hiện tại")
    channel: Literal["MOBILE", "INTERNET", "BRANCH", "ATM"] = "MOBILE"
    session_flags: dict = Field(
        default_factory=dict,
        description="Cờ phiên từ app: screen_sharing, remote_app, new_device, "
        "on_call, accessibility_service (bool)",
    )


class InterveneRequest(BaseModel):
    decision_id: str
    selected_option: str | None = None
    free_text: str | None = Field(None, description="Sẽ được mask SĐT/CCCD trước khi lưu")
    llm_reasons: list[str] | None = None
    question: str | None = None
    advice_title: str | None = None
    advice_body: str | None = None


class ActionRequest(BaseModel):
    decision_id: str
    action_taken: Literal["cancel", "hold", "contact", "continue"]
    outcome: Literal["prevented", "held", "proceeded", "n/a"] | None = None
    transaction_id: int | None = None


class LegacyRiskRequest(BaseModel):
    """Giữ nguyên hợp đồng của endpoint /risk-score cũ để client cũ không gãy."""

    customer_id: str
    amount: float = 0
    new_beneficiary: bool = False
    unusual_time: bool = False
    device_changed: bool = False
    geo_anomaly: bool = False
    velocity_high: bool = False


# ---------------------------------------------------------------------------
# Gọi service khác — luôn bao dung với lỗi
# ---------------------------------------------------------------------------
def _peer_get(url: str, **kwargs) -> tuple[dict | None, str | None]:
    try:
        resp = httpx.get(url, timeout=PEER_TIMEOUT, **kwargs)
        if resp.status_code == 404:
            return None, None  # không tìm thấy là câu trả lời hợp lệ, không phải lỗi
        resp.raise_for_status()
        return resp.json(), None
    except httpx.HTTPError as exc:
        return None, f"{url.split('/')[2]}: {type(exc).__name__}"


def _peer_post(url: str, payload: dict) -> tuple[dict | None, str | None]:
    try:
        resp = httpx.post(url, json=payload, timeout=PEER_TIMEOUT)
        resp.raise_for_status()
        return resp.json(), None
    except httpx.HTTPError as exc:
        return None, f"{url.split('/')[2]}: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Sáu yếu tố rủi ro
# ---------------------------------------------------------------------------
def _factor_amount_deviation(amount: float, baseline: dict | None, balance: float | None) -> dict:
    """Số tiền lệch bao xa so với thói quen, và có đang vét sạch tài khoản không."""
    cap = FACTOR_CAPS["amount_deviation"]
    if not baseline:
        return {"score": 8, "detail": "chưa có baseline, dùng điểm trung tính", "ratio": None}

    p90 = float(num(baseline.get("out_p90")))
    p99 = float(num(baseline.get("out_p99")))
    out_max = float(num(baseline.get("out_max")))

    score = 0
    reasons = []
    if p99 and amount > out_max:
        score = 20
        reasons.append(f"vượt mức chuyển lớn nhất từng thực hiện ({out_max:,.0f})")
        # Vượt trần cũ 1.2 lần và vượt 10 lần là hai mức độ bất thường khác hẳn nhau.
        # Không phân biệt thì một cụ hưu trí quen chuyển 2 triệu bỗng chuyển 22 triệu
        # lại bị chấm ngang với người chuyển nhỉnh hơn thường lệ một chút.
        if amount > p99 * 5:
            score += 5
            reasons.append(f"gấp {amount / p99:.0f} lần ngưỡng p99 ({p99:,.0f})")
    elif p99 and amount > p99:
        score = 16
        reasons.append(f"vượt ngưỡng p99 ({p99:,.0f})")
    elif p90 and amount > p90:
        score = 9
        reasons.append(f"vượt ngưỡng p90 ({p90:,.0f})")

    drain = None
    if balance and balance > 0:
        # Chặn trần ở 1.0: khi số tiền vượt số dư thì tỷ lệ thô có thể lên hàng nghìn
        # phần trăm, vừa vô nghĩa khi hiển thị cho khách vừa làm lệch mọi so sánh.
        raw = amount / balance
        drain = min(raw, 1.0)
        baseline_drain = float(num(baseline.get("max_drain_ratio_90d")))
        if raw > 1.0:
            score += 5
            reasons.append("số tiền vượt quá số dư khả dụng")
        elif drain >= 0.9 and drain > baseline_drain:
            score += 5
            reasons.append(f"vét {drain:.0%} số dư khả dụng")
        elif drain >= 0.7 and drain > baseline_drain:
            score += 3
            reasons.append(f"dùng {drain:.0%} số dư khả dụng")

    return {
        "score": min(score, cap),
        "detail": "; ".join(reasons) or "trong biên độ thường ngày",
        "ratio": round(drain, 4) if drain is not None else None,
    }


def _factor_new_beneficiary(benef: dict | None) -> dict:
    cap = FACTOR_CAPS["new_beneficiary"]
    if benef is None:
        return {"score": 12, "detail": "không tra được thông tin người nhận"}
    if not benef.get("known") or benef.get("is_new"):
        return {"score": cap, "detail": "người nhận hoàn toàn mới, chưa từng chuyển"}
    age_days = int(benef.get("age_days") or 0)
    tx_count = int(benef.get("tx_count") or 0)
    if age_days < 7 or tx_count <= 1:
        return {"score": 14, "detail": f"người nhận mới quen ({age_days} ngày, {tx_count} lần)"}
    if age_days < 30:
        return {"score": 7, "detail": f"người nhận quen chưa lâu ({age_days} ngày)"}
    return {"score": 0, "detail": f"người nhận quen thuộc ({tx_count} lần, {age_days} ngày)"}


def _factor_time_of_day(when: datetime, baseline: dict | None) -> dict:
    """Giao dịch đêm chỉ đáng ngờ với người vốn không giao dịch đêm."""
    cap = FACTOR_CAPS["time_of_day"]
    hour = when.hour
    is_night = hour >= 23 or hour < 6
    if not is_night:
        return {"score": 0, "detail": f"{hour:02d}h — trong khung giờ thường ngày"}
    night_ratio = float(num(baseline.get("night_tx_ratio"))) if baseline else 0.0
    if night_ratio >= 0.15:
        return {"score": 3, "detail": f"{hour:02d}h nhưng khách vốn hay giao dịch đêm"}
    return {"score": cap, "detail": f"{hour:02d}h — ngoài khung giờ thường ngày của khách"}


def _factor_behavior_drift(
    amount: float, baseline: dict | None, benef: dict | None, session_flags: dict
) -> dict:
    """Lệch nhịp hành vi: tần suất, tổng dồn về một người, và cờ chiếm quyền thiết bị."""
    cap = FACTOR_CAPS["behavior_drift"]
    score = 0
    reasons = []

    # Cờ chiếm quyền thiết bị nặng nhất: khách đang bị người khác điều khiển máy.
    if session_flags.get("screen_sharing") or session_flags.get("remote_app"):
        score += 12
        reasons.append("đang chia sẻ màn hình hoặc chạy ứng dụng điều khiển từ xa")
    if session_flags.get("accessibility_service"):
        score += 8
        reasons.append("ứng dụng trợ năng lạ đang hoạt động")
    if session_flags.get("new_device"):
        score += 5
        reasons.append("thiết bị mới")
    if session_flags.get("on_call"):
        score += 5
        reasons.append("khách đang trong cuộc gọi khi thao tác")

    if baseline and benef and benef.get("known"):
        cum_cap = float(num(baseline.get("max_cum_to_one_benef_14d")))
        already = float(num(benef.get("total_out")))
        if cum_cap and (already + amount) > cum_cap * 1.5:
            score += 8
            reasons.append("tổng dồn về một người nhận vượt xa mức từng có")

        # Chuỗi chuyển tiền tăng dần cho một người mới quen là mẫu chung của bẫy đầu
        # tư, việc nhẹ lương cao và lừa tình cảm: nạn nhân bị dẫn dắt nâng dần số tiền.
        # Xét riêng từng giao dịch thì khoản nào cũng có vẻ chấp nhận được; chỉ khi
        # nhìn cả chuỗi mới thấy hình dạng của nó.
        tx_count = int(benef.get("tx_count") or 0)
        age_days = int(benef.get("age_days") or 0)
        if tx_count >= 1 and age_days <= 30 and already > 0:
            if amount > (already / tx_count) * 1.5:
                score += 8
                reasons.append(
                    f"lần chuyển thứ {tx_count + 1} cho người mới quen, "
                    "số tiền tăng dần qua từng lần"
                )

    return {
        "score": min(score, cap),
        "detail": "; ".join(reasons) or "nhịp giao dịch bình thường",
    }


def _factor_relationship_history(benef: dict | None, amount: float) -> dict:
    """Quan hệ lạ + từng nhận tiền mồi là dấu hiệu của bẫy đầu tư / việc nhẹ lương cao."""
    cap = FACTOR_CAPS["relationship_history"]
    if benef is None:
        return {"score": 8, "detail": "không tra được lịch sử quan hệ"}

    score = 0
    reasons = []
    if benef.get("status") in ("SUSPECTED", "BLOCKED"):
        return {"score": cap, "detail": f"người nhận đang ở trạng thái {benef['status']}"}
    if (benef.get("relationship") or "UNKNOWN") == "UNKNOWN":
        score += 7
        reasons.append("chưa xác định quan hệ với người nhận")

    total_in = float(num(benef.get("total_in")))
    total_out = float(num(benef.get("total_out")))
    if total_in > 0 and amount > total_in * 3:
        score += 8
        reasons.append(
            f"từng nhận {total_in:,.0f} từ người này rồi giờ chuyển đi gấp nhiều lần "
            "— mẫu tiền mồi"
        )
    elif total_out == 0 and total_in == 0:
        score += 4
        reasons.append("chưa có lịch sử giao dịch hai chiều")

    return {"score": min(score, cap), "detail": "; ".join(reasons) or "quan hệ đã được xác lập"}


def _factor_recent_context(events: list[dict], when: datetime) -> dict:
    """Chỉ xét sự kiện trong 60 phút trước giao dịch — ngoài cửa sổ đó thì không
    còn là 'ngữ cảnh' của lệnh chuyển này nữa."""
    cap = FACTOR_CAPS["recent_context"]
    if not events:
        return {"score": 0, "detail": "không có sự kiện bất thường trong 60 phút qua", "events": []}

    score = 0
    seen = []
    for ev in events:
        etype = ev.get("event_type")
        weight = EVENT_WEIGHTS.get(etype, 0)
        if weight:
            score = max(score, weight)  # lấy sự kiện nặng nhất, không cộng dồn
            seen.append(etype)
    if len(set(seen)) >= 2:
        score += 5  # nhiều loại sự kiện chồng nhau thì đáng ngờ hơn hẳn
    return {
        "score": min(score, cap),
        "detail": "sự kiện gần đây: " + ", ".join(sorted(set(seen))) if seen else "không đáng kể",
        "events": sorted(set(seen)),
    }


def score_transfer(
    amount: float,
    when: datetime,
    baseline: dict | None,
    benef: dict | None,
    balance: float | None,
    events: list[dict],
    session_flags: dict,
) -> dict:
    factors = {
        "amount_deviation": _factor_amount_deviation(amount, baseline, balance),
        "new_beneficiary": _factor_new_beneficiary(benef),
        "time_of_day": _factor_time_of_day(when, baseline),
        "behavior_drift": _factor_behavior_drift(amount, baseline, benef, session_flags),
        "relationship_history": _factor_relationship_history(benef, amount),
        "recent_context": _factor_recent_context(events, when),
    }
    total = min(100, sum(f["score"] for f in factors.values()))
    if total >= LEVEL_INTERVENE:
        level = "intervene"
    elif total >= LEVEL_SOFT_WARN:
        level = "soft_warn"
    else:
        level = "pass"
    top = sorted(factors.items(), key=lambda kv: kv[1]["score"], reverse=True)[:3]
    return {
        "score": total,
        "level": level,
        "factors": factors,
        "top_factors": ",".join(k for k, v in top if v["score"] > 0) or "none",
    }


def _template_text(score: int, level: str, factors: dict, amount: float) -> str:
    """Lời cảnh báo rule-based, dùng làm fallback khi LLM lỗi hoặc timeout.
    Luồng demo không bao giờ được rơi vào cảnh không có gì để nói với khách."""
    if level == "pass":
        return f"Giao dịch {amount:,.0f} VND trong ngưỡng bình thường của bạn."
    notable = [f"{k.replace('_', ' ')}: {v['detail']}" for k, v in factors.items() if v["score"] > 0]
    head = (
        f"Giao dịch {amount:,.0f} VND có dấu hiệu bất thường (điểm rủi ro {score}/100)."
        if level == "soft_warn"
        else f"Giao dịch {amount:,.0f} VND có rủi ro cao (điểm {score}/100). "
        "Vui lòng xác nhận trước khi tiếp tục."
    )
    return head + " Lý do: " + "; ".join(notable[:3]) + "."


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
        "Chấm điểm rủi ro tất định 0-100 theo 6 yếu tố và quản lý vòng đời quyết định.",
        OWNS_TABLES,
        AGENT_TOOLS,
    )
    payload["factor_caps"] = FACTOR_CAPS
    payload["levels"] = {
        "pass": f"< {LEVEL_SOFT_WARN}",
        "soft_warn": f"{LEVEL_SOFT_WARN} - {LEVEL_INTERVENE - 1}",
        "intervene": f">= {LEVEL_INTERVENE}",
    }
    return payload


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    return agent_tools_payload(SERVICE_NAME, "RISK_SCORING_SERVICE_URL", AGENT_TOOLS)


# ---------------------------------------------------------------------------
# Vòng đời quyết định
# ---------------------------------------------------------------------------
def _require_transaction(transaction_id: int | None) -> None:
    """404 khi giao dịch không tồn tại.

    `risk_decision.transaction_id` là khóa ngoại cho phép NULL, nên chỉ kiểm tra
    khi người gọi có truyền — không được biến cột tùy chọn thành bắt buộc."""
    if transaction_id is None:
        return
    row = query_one(
        "SELECT 1 AS x FROM transaction_history WHERE transaction_id = %s", (transaction_id,)
    )
    if row is None:
        raise HTTPException(404, f"transaction {transaction_id} không tồn tại")


def _require_customer_and_account(customer_id: int, account_id: int) -> None:
    """404 khi khách hoặc tài khoản không tồn tại.

    Hai khóa ngoại `risk_decision_customer_id_fkey` và
    `risk_decision_account_id_fkey` là thứ chặn cuối cùng; để chúng nổ ra thì
    thông điệp lỗi là của Postgres chứ không phải của nghiệp vụ.
    """
    if query_one("SELECT 1 AS x FROM customer WHERE customer_id = %s", (customer_id,)) is None:
        raise HTTPException(404, f"customer {customer_id} không tồn tại")
    if query_one("SELECT 1 AS x FROM account WHERE account_id = %s", (account_id,)) is None:
        raise HTTPException(404, f"account {account_id} không tồn tại")


@app.post("/transfer/precheck", tags=["transfer"])
def precheck(req: PrecheckRequest):
    """Bước 1 — chấm điểm một lệnh chuyển trước khi tiền rời tài khoản.

    Gom ngữ cảnh từ customer-profile-service (baseline, người nhận, sự kiện 60 phút),
    chấm 6 yếu tố, đối chiếu playbook lừa đảo, ghi một dòng risk_decision rồi trả về
    điểm, mức độ, câu hỏi cần hỏi khách và khuyến cáo.
    """
    # Khách hoặc tài khoản không tồn tại thì dừng NGAY, đừng để Postgres từ chối
    # lúc INSERT: khóa ngoại vỡ thành ForeignKeyViolation không ai bắt, endpoint
    # trả 500 và người gọi tưởng nền tảng hỏng. Các endpoint khác của service này
    # đã trả 404 cho dữ liệu không tồn tại; chỗ này phải giống.
    _require_customer_and_account(req.customer_id, req.account_id)

    when = datetime.fromisoformat(req.tx_time) if req.tx_time else now_vn()
    if when.tzinfo is None:
        when = when.replace(tzinfo=now_vn().tzinfo)
    degraded: list[str] = []

    baseline_resp, err = _peer_get(f"{CUSTOMER_PROFILE_URL}/customers/{req.customer_id}/baseline")
    if err:
        degraded.append(f"baseline ({err})")
    baseline = (baseline_resp or {}).get("baseline")
    persona = (baseline_resp or {}).get("persona", "SALARY")

    benef, err = _peer_post(
        f"{CUSTOMER_PROFILE_URL}/customers/{req.customer_id}/beneficiaries/resolve",
        {"bank_code": req.beneficiary_bank_code, "account_no": req.beneficiary_account_no},
    )
    if err:
        degraded.append(f"beneficiary ({err})")

    events_resp, err = _peer_get(
        f"{CUSTOMER_PROFILE_URL}/customers/{req.customer_id}/events",
        # Neo cửa sổ vào thời điểm giao dịch chứ không phải lúc gọi API: chấm lại
        # một giao dịch của hôm qua vẫn phải thấy sự kiện xảy ra ngay trước nó.
        params={"within_minutes": 60, "before": when.isoformat()},
    )
    if err:
        degraded.append(f"events ({err})")
    events = (events_resp or {}).get("events", [])

    accounts_resp, err = _peer_get(f"{CUSTOMER_PROFILE_URL}/customers/{req.customer_id}/accounts")
    if err:
        degraded.append(f"accounts ({err})")
    balance = None
    for acc in (accounts_resp or {}).get("accounts", []):
        if acc["account_id"] == req.account_id:
            balance = float(acc.get("working_balance") or 0)
            break

    result = score_transfer(
        req.amount, when, baseline, benef, balance, events, req.session_flags
    )

    # Đối chiếu playbook: kịch bản quyết định câu hỏi và khuyến cáo hiển thị cho khách.
    scenario: dict = {}
    escalated = False
    if result["level"] != "pass":
        match, err = _peer_post(
            f"{SCAM_KNOWLEDGE_URL}/scams/match",
            {
                "persona": persona,
                "memo": mask_free_text(req.memo) or "",
                "amount": req.amount,
                "is_new_beneficiary": bool(benef is None or benef.get("is_new")),
                "drain_ratio": result["factors"]["amount_deviation"].get("ratio") or 0,
                "is_night": when.hour >= 23 or when.hour < 6,
                "recent_events": result["factors"]["recent_context"].get("events", []),
                "session_flags": req.session_flags,
                "beneficiary_total_in": float(num((benef or {}).get("total_in"))),
                # Số lần đã chuyển cho người nhận này, chỉ tính khi quan hệ còn mới.
                # Đây là xấp xỉ của "số lần trong 14 ngày": với người nhận quen lâu
                # thì con số đó vô nghĩa, còn với người mới quen vài ngày thì toàn bộ
                # lịch sử chính là chuỗi giao dịch đang diễn ra.
                "series_count_14d": (
                    int((benef or {}).get("tx_count") or 0)
                    if benef and (benef.get("age_days") or 0) <= 14 else 0
                ),
            },
        )
        if err:
            degraded.append(f"scam match ({err})")
        scenario = (match or {}).get("scenario") or {}

    # Sáu yếu tố chỉ nhìn hành vi; chúng không đọc khách đang chuyển tiền để làm gì.
    # Một cụ hưu trí chuyển 22 triệu kèm nội dung "phí bảo hiểm kiện hàng" là gần như
    # tự khai ra kịch bản lừa tình cảm, nhưng xét thuần hành vi thì chỉ tới mức cảnh
    # báo nhẹ. Vì vậy: khi playbook khớp nhờ bằng chứng TỪ KHÓA (chứ không phải chỉ
    # vài cờ chung chung) và chính playbook xếp kịch bản đó vào loại phải dừng giao
    # dịch (cancel/hold), thì nâng cảnh báo nhẹ lên mức can thiệp.
    #
    # Ba ràng buộc giữ cho cơ chế này không bị lạm dụng:
    #   · Không cộng điểm — điểm vẫn thuần hành vi, nên kẻ gian đổi nội dung chuyển
    #     khoản chỉ mất phần nâng mức chứ không kéo điểm xuống.
    #   · Chỉ nâng từ soft_warn lên intervene, không bao giờ nâng từ pass.
    #   · Ghi lại tường minh trong factors để mọi lần nâng đều truy vết được.
    escalated = False
    if (
        result["level"] == "soft_warn"
        and scenario.get("recommended_action") in ("cancel", "hold")
        and any(sig.startswith("từ khóa") for sig in scenario.get("matched_signals") or [])
    ):
        escalated = True
        result["level"] = "intervene"
        result["factors"]["scenario_escalation"] = {
            "score": 0,
            "detail": (
                f"Nâng từ soft_warn lên intervene: nội dung chuyển khoản khớp kịch bản "
                f"{scenario.get('scenario_id')} ({scenario.get('scenario_name')}) "
                f"bằng bằng chứng từ khóa, và playbook khuyến nghị "
                f"{scenario.get('recommended_action')}."
            ),
        }

    account_masked = mask_account(req.beneficiary_account_no)
    tx_snapshot = {
        "amount": req.amount,
        "beneficiary_masked": account_masked,
        "bank_code": req.beneficiary_bank_code.upper(),
        "tx_time": when.isoformat(),
        "memo_masked": mask_free_text(req.memo),
        "channel": req.channel,
        "session_flags": req.session_flags,
        "working_balance": balance,
    }
    template = _template_text(result["score"], result["level"], result["factors"], req.amount)

    row = execute_returning(
        """
        INSERT INTO risk_decision (
          customer_id, account_id, tx_snapshot, score, level, factors, top_factors,
          scenario_id, template_text, question, options, advice_title, advice_body,
          recommended_action, intervened_at
        ) VALUES (
          %s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,
          CASE WHEN %s = 'intervene' THEN now() ELSE NULL END
        ) RETURNING *
        """,
        (
            req.customer_id, req.account_id, json.dumps(tx_snapshot, default=str),
            result["score"], result["level"], json.dumps(result["factors"], default=str),
            result["top_factors"], scenario.get("scenario_id"), template,
            scenario.get("question"), json.dumps(scenario.get("options") or []),
            scenario.get("advice_title"), scenario.get("advice_body"),
            scenario.get("recommended_action"), result["level"],
        ),
    )

    return {
        "decision_id": str(row["decision_id"]),
        "customer_id": req.customer_id,
        "persona": persona,
        "score": result["score"],
        "level": result["level"],
        "top_factors": result["top_factors"].split(",") if result["top_factors"] != "none" else [],
        "factors": result["factors"],
        "template_text": template,
        "scenario": scenario or None,
        "question": scenario.get("question"),
        "options": scenario.get("options") or [],
        "recommended_action": scenario.get("recommended_action"),
        "agent_can_ask": scenario.get("agent_can_ask", "Y"),
        "tx_snapshot": tx_snapshot,
        "level_escalated_by_scenario": escalated,
        "degraded": degraded or None,
        "next_step": {
            "pass": "Không cần hỏi, cho giao dịch đi tiếp.",
            "soft_warn": "Hiển thị cảnh báo nhẹ rồi gọi POST /transfer/action.",
            "intervene": "Hỏi khách bằng `question`, gửi câu trả lời qua POST /transfer/intervene.",
        }[result["level"]],
    }


@app.post("/transfer/intervene", tags=["transfer"])
def intervene(req: InterveneRequest):
    """Bước 2 — ghi câu trả lời của khách và phần diễn giải của LLM vào cùng dòng
    quyết định. Free text được mask SĐT/CCCD trước khi chạm vào DB."""
    existing = query_one(
        "SELECT * FROM risk_decision WHERE decision_id = %s::uuid", (req.decision_id,)
    )
    if existing is None:
        raise HTTPException(404, f"decision {req.decision_id} không tồn tại")

    row = execute_returning(
        """
        UPDATE risk_decision SET
          selected_option  = COALESCE(%s, selected_option),
          free_text_masked = COALESCE(%s, free_text_masked),
          llm_reasons      = COALESCE(%s::jsonb, llm_reasons),
          question         = COALESCE(%s, question),
          advice_title     = COALESCE(%s, advice_title),
          advice_body      = COALESCE(%s, advice_body),
          intervened_at    = COALESCE(intervened_at, now())
        WHERE decision_id = %s::uuid
        RETURNING *
        """,
        (
            req.selected_option,
            mask_free_text(req.free_text),
            json.dumps(req.llm_reasons) if req.llm_reasons else None,
            req.question, req.advice_title, req.advice_body, req.decision_id,
        ),
    )
    return {
        "decision_id": str(row["decision_id"]),
        "score": row["score"],
        "level": row["level"],
        "selected_option": row["selected_option"],
        "advice_title": row["advice_title"],
        "advice_body": row["advice_body"],
        "recommended_action": row["recommended_action"],
        "next_step": "Gọi POST /transfer/action với hành động khách chọn.",
    }


@app.post("/transfer/action", tags=["transfer"])
def take_action(req: ActionRequest):
    """Bước 3 — chốt hành động và kết cục.

    `hold` và `contact` sẽ mở case bên action-feedback-service; nếu service đó
    không sẵn sàng, quyết định vẫn được ghi và phần mở case báo lỗi riêng để ops
    xử lý thủ công — không nuốt lỗi, cũng không làm hỏng cả lời gọi.
    """
    existing = query_one(
        "SELECT * FROM risk_decision WHERE decision_id = %s::uuid", (req.decision_id,)
    )
    if existing is None:
        raise HTTPException(404, f"decision {req.decision_id} không tồn tại")
    _require_transaction(req.transaction_id)

    outcome = req.outcome or {
        "cancel": "prevented",
        "hold": "held",
        "contact": "held",
        "continue": "proceeded",
    }[req.action_taken]

    row = execute_returning(
        """
        UPDATE risk_decision SET
          action_taken   = %s,
          outcome        = %s,
          transaction_id = COALESCE(%s, transaction_id),
          actioned_at    = now()
        WHERE decision_id = %s::uuid
        RETURNING *
        """,
        (req.action_taken, outcome, req.transaction_id, req.decision_id),
    )

    case_result: dict | str | None = None
    if req.action_taken in ("hold", "contact"):
        payload, err = _peer_post(
            f"{ACTION_FEEDBACK_URL}/cases",
            {
                "decision_id": req.decision_id,
                "customer_id": row["customer_id"],
                "scenario_id": row.get("scenario_id"),
                "narrative": row.get("template_text") or "",
                "trigger": req.action_taken,
            },
        )
        case_result = payload if payload else f"không mở được case ({err})"

    return {
        "decision_id": str(row["decision_id"]),
        "action_taken": row["action_taken"],
        "outcome": row["outcome"],
        "score": row["score"],
        "level": row["level"],
        "case": case_result,
    }


# ---------------------------------------------------------------------------
# Tra cứu quyết định
# ---------------------------------------------------------------------------
@app.get("/risk-decisions/{decision_id}", tags=["decision"])
def get_decision(decision_id: str):
    row = query_one(
        "SELECT * FROM risk_decision WHERE decision_id = %s::uuid", (decision_id,)
    )
    if row is None:
        raise HTTPException(404, f"decision {decision_id} không tồn tại")
    row["decision_id"] = str(row["decision_id"])
    return row


@app.get("/risk-decisions", tags=["decision"])
def list_decisions(
    customer_id: int | None = None,
    level: Literal["pass", "soft_warn", "intervene"] | None = None,
    outcome: str | None = None,
    limit: int = Query(50, ge=1, le=200),
):
    sql = "SELECT * FROM risk_decision WHERE 1 = 1"
    params: list = []
    if customer_id:
        sql += " AND customer_id = %s"
        params.append(customer_id)
    if level:
        sql += " AND level = %s"
        params.append(level)
    if outcome:
        sql += " AND outcome = %s"
        params.append(outcome)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, params)
    for r in rows:
        r["decision_id"] = str(r["decision_id"])
    return {"count": len(rows), "decisions": rows}


# ---------------------------------------------------------------------------
# Ops view
# ---------------------------------------------------------------------------
@app.get("/ops/summary", tags=["ops"])
def ops_summary():
    """Bốn con số để pitch: đã chặn bao nhiêu, còn bao nhiêu vẫn đi qua."""
    row = query_one("SELECT * FROM ops_summary") or {}
    return {"service": SERVICE_NAME, "summary": row}


@app.get("/ops/decisions", tags=["ops"])
def ops_decisions(limit: int = Query(50, ge=1, le=500)):
    rows = query("SELECT * FROM ops_decision_log LIMIT %s", (limit,))
    for r in rows:
        r["decision_id"] = str(r["decision_id"])
    return {"count": len(rows), "decisions": rows}


# ---------------------------------------------------------------------------
# Endpoint cũ — giữ tương thích ngược
# ---------------------------------------------------------------------------
@app.post("/risk-score", tags=["legacy"])
def legacy_risk_score(req: LegacyRiskRequest):
    """Chấm điểm theo cờ truyền tay, không chạm DB. Dùng để thử nhanh; luồng thật
    nên gọi /transfer/precheck vì nó tự lấy ngữ cảnh từ dữ liệu khách hàng."""
    weights = {
        "new_beneficiary": 20, "unusual_time": 15, "device_changed": 10,
        "geo_anomaly": 40, "velocity_high": 15, "large_amount": 20,
    }
    active = {
        "new_beneficiary": req.new_beneficiary,
        "unusual_time": req.unusual_time,
        "device_changed": req.device_changed,
        "geo_anomaly": req.geo_anomaly,
        "velocity_high": req.velocity_high,
        "large_amount": req.amount >= 100_000_000,
    }
    factors = [{"name": n, "score": weights[n]} for n, on in active.items() if on]
    score = min(100, sum(f["score"] for f in factors))
    level = "LOW" if score <= 29 else "MEDIUM" if score <= 59 else "HIGH" if score <= 79 else "CRITICAL"
    return {"customer_id": req.customer_id, "score": score, "level": level, "factors": factors}


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------
AGENT_TOOLS = [
    tool(
        "precheck_transfer",
        "Chấm điểm rủi ro một lệnh chuyển tiền TRƯỚC khi thực hiện. Tự lấy baseline "
        "hành vi, lịch sử người nhận và sự kiện 60 phút gần nhất, rồi trả về điểm "
        "0-100, mức pass/soft_warn/intervene, kịch bản lừa đảo khớp nhất và câu hỏi "
        "cần hỏi khách. Đây là tool đầu tiên agent phải gọi ở mọi lệnh chuyển tiền.",
        "POST", "/transfer/precheck",
        body={
            "customer_id": "int", "account_id": "int", "amount": "number",
            "beneficiary_bank_code": "str", "beneficiary_account_no": "str",
            "memo": "str (tùy chọn)", "tx_time": "ISO 8601 (tùy chọn)",
            "channel": "MOBILE|INTERNET|BRANCH|ATM",
            "session_flags": "object: screen_sharing, remote_app, new_device, on_call, accessibility_service",
        },
        returns="decision_id, score, level, factors, scenario, question, options, recommended_action.",
    ),
    tool(
        "record_intervention",
        "Ghi lại câu trả lời của khách cho câu hỏi ở bước precheck, cùng phần diễn "
        "giải của LLM. Free text tự động được mask SĐT/CCCD.",
        "POST", "/transfer/intervene",
        body={
            "decision_id": "uuid", "selected_option": "str",
            "free_text": "str (tùy chọn)", "llm_reasons": "list[str] (tùy chọn)",
            "question": "str", "advice_title": "str", "advice_body": "str",
        },
        returns="Khuyến cáo và hành động đề xuất.",
    ),
    tool(
        "take_action",
        "Chốt hành động cuối: cancel (hủy), hold (khóa tạm), contact (yêu cầu gọi lại) "
        "hoặc continue (khách vẫn tiếp tục). hold và contact tự động mở case.",
        "POST", "/transfer/action",
        body={
            "decision_id": "uuid", "action_taken": "cancel|hold|contact|continue",
            "outcome": "prevented|held|proceeded|n/a (tùy chọn, tự suy ra)",
            "transaction_id": "int (tùy chọn)",
        },
        returns="Quyết định sau khi chốt, kèm case đã mở nếu có.",
    ),
    tool(
        "get_decision",
        "Đọc lại một quyết định rủi ro đầy đủ theo decision_id.",
        "GET", "/risk-decisions/{decision_id}",
        params={"decision_id": "uuid"},
        returns="Bản ghi risk_decision.",
    ),
    tool(
        "list_decisions",
        "Tra cứu lịch sử quyết định, lọc theo khách hàng, mức độ hoặc kết cục.",
        "GET", "/risk-decisions",
        params={"customer_id": "int", "level": "pass|soft_warn|intervene", "outcome": "str", "limit": "int"},
        returns="Danh sách quyết định.",
    ),
    tool(
        "ops_summary",
        "Bốn chỉ số vận hành: số giao dịch đáng ngờ, số đã ngăn chặn, số vẫn tiếp tục "
        "sau cảnh báo, thời gian phản hồi trung bình.",
        "GET", "/ops/summary",
        returns="Các chỉ số tổng hợp.",
    ),
    tool(
        "ops_decision_log",
        "Nhật ký quyết định cho màn hình vận hành, đã join sẵn persona và tên kịch bản.",
        "GET", "/ops/decisions",
        params={"limit": "int"},
        returns="Danh sách dòng nhật ký.",
    ),
]
