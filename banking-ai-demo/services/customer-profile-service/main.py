"""customer-profile-service

Sở hữu: customer, account, deposit, loan, beneficiary, behavior_profile, account_event.

Vai trò trong hệ: nguồn duy nhất về "khách hàng là ai và bình thường họ hành xử ra sao".
  · Journey A (Copilot tài chính cá nhân) đọc portfolio (TK / sổ tiết kiệm / khoản vay).
  · Journey B (chống rủi ro thanh toán) đọc baseline hành vi (digital twin),
    đặc trưng quan hệ khách–người nhận, và sự kiện gần đây (yếu tố rủi ro số 6).

Toàn bộ response đã mask PII theo quy định trong schema: không endpoint nào trả
full_name, legal_id, phone_no, email, street hay date_of_birth ở dạng thô.
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    as_int,
    db_health,
    execute,
    execute_returning,
    mask_beneficiary_row,
    mask_customer_row,
    now_vn,
    num,
    openapi_description,
    persona_of,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
)

SERVICE_NAME = "customer-profile-service"
OWNS_TABLES = [
    "customer",
    "account",
    "deposit",
    "loan",
    "beneficiary",
    "behavior_profile",
    "account_event",
]
TRANSACTION_SERVICE_URL = os.getenv(
    "TRANSACTION_SERVICE_URL", "http://transaction-service"
)

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": 'customer',
        "description": (
            'Hồ sơ khách hàng và toàn cảnh tài chính. `/portfolio` gộp số dư, sổ tiết kiệm và khoản vay thành một lời gọi cho Copilot.'
        ),
    },
    {
        "name": 'baseline',
        "description": (
            'Digital twin — chân dung hành vi 90 ngày của khách. Engine rủi ro đọc bảng này ở mỗi lần chấm điểm thay vì quét lại lịch sử, để giữ độ trễ thấp.'
        ),
    },
    {
        "name": 'beneficiary',
        "description": (
            'Sổ người nhận theo từng cặp khách–người nhận. `/resolve` là đầu vào trực tiếp của hai yếu tố rủi ro: người nhận mới và lịch sử quan hệ.'
        ),
    },
    {
        "name": 'event',
        "description": (
            'Sự kiện ngoài chuyển tiền: tất toán sổ, nâng hạn mức, đăng nhập thiết bị mới, đổi mật khẩu, nhận tiền lạ. Engine chỉ xét cửa sổ 60 phút trước giao dịch.'
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary='Hồ sơ khách hàng, danh mục tài sản, digital twin (baseline hành vi), người nhận và sự kiện tài khoản.',
    description=openapi_description(SERVICE_NAME, 'Hồ sơ khách hàng, danh mục tài sản, digital twin (baseline hành vi), người nhận và sự kiện tài khoản.', OWNS_TABLES),
    openapi_tags=OPENAPI_TAGS,
    contact={"name": "MSB AI Financial Guardian — xem manifest tại GET /agent/tools"},
    docs_url="/docs",
    redoc_url="/redoc",
)
setup_docs(app, SERVICE_NAME)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class BeneficiaryResolveRequest(BaseModel):
    bank_code: str = Field(..., description="Mã ngân hàng nhận: MSB, VCB, TCB...")
    account_no: str = Field(..., description="Số tài khoản nhận (không lộ ra response)")


class AccountEventRequest(BaseModel):
    event_type: Literal[
        "SAVINGS_CLOSED",
        "LIMIT_RAISED",
        "NEW_DEVICE_LOGIN",
        "PASSWORD_RESET",
        "INBOUND_UNKNOWN",
    ]
    account_id: int | None = None
    amount: float | None = None
    event_time: str | None = Field(
        None, description="ISO 8601; bỏ trống = thời điểm hiện tại"
    )
    source_ref: str | None = None
    meta: dict = Field(default_factory=dict)


class BeneficiaryStatusRequest(BaseModel):
    status: Literal["ACTIVE", "BLOCKED", "SUSPECTED"]
    reason: str | None = None


class BaselineUpsertRequest(BaseModel):
    """Payload khớp đúng các cột của behavior_profile (trừ customer_id)."""

    window_days: int = 90
    out_median: float
    out_p90: float
    out_p99: float
    out_max: float
    monthly_out_avg: float
    monthly_in_avg: float
    known_beneficiaries: int
    new_benef_per_30d: float
    share_to_new_benef: float
    active_hours: list[float]
    night_tx_ratio: float
    weekend_tx_ratio: float
    tx_per_week: float
    max_tx_per_day: int
    max_cum_to_one_benef_14d: float
    balance_median: float
    max_drain_ratio_90d: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _customer_or_404(customer_id: int) -> dict:
    row = query_one("SELECT * FROM customer WHERE customer_id = %s", (customer_id,))
    if row is None:
        raise HTTPException(404, f"customer {customer_id} không tồn tại")
    return row


# Khóa chỉ dùng nội bộ cho việc sinh dữ liệu và kiểm thử. Chúng nằm trong cột meta
# cùng chỗ với dữ liệu nghiệp vụ, nên nếu trả nguyên dòng thì đáp án của bộ kiểm thử
# sẽ đi thẳng ra API và có thể lọt vào prompt LLM.
_INTERNAL_META_KEYS = {"fraud_case_id", "injected", "benign"}


def _event_payload(row: dict) -> dict:
    """Trả sự kiện dưới dạng đã lọc.

    `source_ref` bị bỏ hẳn: nó tham chiếu định danh nội bộ (mã sổ tiết kiệm, mã giao
    dịch, mã thiết bị) và trong dữ liệu mẫu còn mang cả mã fraud case. Engine rủi ro
    chỉ cần `event_type` và `event_time`, không cần tham chiếu này.
    """
    meta = row.get("meta") or {}
    return {
        "event_id": row.get("event_id"),
        "customer_id": row.get("customer_id"),
        "account_id": row.get("account_id"),
        "event_type": row.get("event_type"),
        "amount": float(num(row["amount"])) if row.get("amount") is not None else None,
        "event_time": row.get("event_time"),
        "meta": {k: v for k, v in meta.items() if k not in _INTERNAL_META_KEYS},
    }


def _account_payload(row: dict) -> dict:
    return {
        "account_id": row["account_id"],
        "account_name": row.get("account_name"),
        "product_group": row.get("product_group"),
        "product_id": row.get("product_id"),
        "currency": row.get("currency"),
        "open_acct_bal": float(num(row.get("open_acct_bal"))),
        "working_balance": float(num(row.get("working_balance"))),
    }


# ---------------------------------------------------------------------------
# Common endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    """Shallow — không chạm DB, để DB chập chờn không làm k8s restart pod."""
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/db", tags=["meta"])
def health_db():
    return {"service": SERVICE_NAME, **db_health()}


@app.get("/info", tags=["meta"])
def info():
    return service_info(
        SERVICE_NAME,
        "Hồ sơ khách hàng, danh mục tài sản, digital twin (baseline hành vi), "
        "người nhận và sự kiện tài khoản.",
        OWNS_TABLES,
        AGENT_TOOLS,
    )


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    """Manifest để agent tự discover tool, không cần hardcode endpoint."""
    return agent_tools_payload(
        SERVICE_NAME, "CUSTOMER_PROFILE_SERVICE_URL", AGENT_TOOLS
    )


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------
@app.get("/customers", tags=["customer"])
def list_customers(
    persona: str | None = Query(None, description="SALARY | HNW | SENIOR"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    rows = query(
        "SELECT * FROM customer ORDER BY customer_id LIMIT %s OFFSET %s",
        (limit, offset),
    )
    items = [mask_customer_row(r) for r in rows]
    if persona:
        items = [i for i in items if i["persona"] == persona.upper()]
    return {"count": len(items), "customers": items}


@app.get("/customers/{customer_id}", tags=["customer"])
def get_customer(customer_id: int):
    """Hồ sơ đã mask + số lượng tài khoản/người nhận, đủ cho agent mở đầu hội thoại."""
    row = _customer_or_404(customer_id)
    payload = mask_customer_row(row)
    payload["accounts"] = as_int(
        query_one(
            "SELECT count(*) AS n FROM account WHERE customer_id = %s", (customer_id,)
        )["n"]
    )
    payload["beneficiaries"] = as_int(
        query_one(
            "SELECT count(*) AS n FROM beneficiary WHERE customer_id = %s",
            (customer_id,),
        )["n"]
    )
    payload["has_baseline"] = (
        query_one(
            "SELECT 1 AS x FROM behavior_profile WHERE customer_id = %s", (customer_id,)
        )
        is not None
    )
    return payload


@app.get("/customers/{customer_id}/accounts", tags=["customer"])
def get_accounts(customer_id: int):
    _customer_or_404(customer_id)
    rows = query(
        "SELECT * FROM account WHERE customer_id = %s ORDER BY account_id",
        (customer_id,),
    )
    return {
        "customer_id": customer_id,
        "count": len(rows),
        "accounts": [_account_payload(r) for r in rows],
    }


@app.get("/customers/{customer_id}/portfolio", tags=["customer"])
def get_portfolio(customer_id: int):
    """Journey A: toàn cảnh tài chính — TK thanh toán, sổ tiết kiệm, khoản vay.

    Kèm sổ sắp đáo hạn trong 7 ngày để Copilot chủ động nhắc tái tục.
    """
    customer = _customer_or_404(customer_id)
    accounts = query(
        "SELECT * FROM account WHERE customer_id = %s ORDER BY account_id",
        (customer_id,),
    )
    deposits = query(
        "SELECT * FROM deposit WHERE customer_id = %s ORDER BY maturity_date",
        (customer_id,),
    )
    loans = query(
        "SELECT * FROM loan WHERE customer_id = %s ORDER BY maturity_date",
        (customer_id,),
    )

    today = now_vn().strftime("%Y%m%d")
    horizon = (now_vn() + timedelta(days=7)).strftime("%Y%m%d")

    deposit_items = [
        {
            "deposit_id": d["deposit_id"],
            "product_id": d.get("product_id"),
            "product_group": d.get("product_group"),
            "currency": d.get("currency"),
            "amount": float(num(d.get("amount"))),
            "interest_rate": float(num(d.get("interest"))) + float(num(d.get("interest_margin"))),
            "term_months": as_int(d.get("term")),
            "rollover": d.get("rollover"),
            "start_date": d.get("start_date"),
            "maturity_date": d.get("maturity_date"),
            "linked_account_id": d.get("linked_account_id"),
            "maturing_within_7d": bool(
                d.get("maturity_date") and today <= d["maturity_date"] <= horizon
            ),
        }
        for d in deposits
    ]
    loan_items = [
        {
            "loan_id": l["loan_id"],
            "product_group": l.get("product_group"),
            "currency": l.get("currency"),
            "amount": float(num(l.get("amount"))),
            "interest_rate": float(num(l.get("rate"))) + float(num(l.get("rate_margin"))),
            "term_months": as_int(l.get("term")),
            "start_date": l.get("start_date"),
            "maturity_date": l.get("maturity_date"),
            "payin_account": l.get("payin_account"),
        }
        for l in loans
    ]

    total_balance = sum(float(num(a.get("working_balance"))) for a in accounts)
    total_deposit = sum(d["amount"] for d in deposit_items)
    total_loan = sum(l["amount"] for l in loan_items)

    return {
        "customer_id": customer_id,
        "persona": persona_of(customer.get("target"), customer.get("date_of_birth")),
        "summary": {
            "total_working_balance": total_balance,
            "total_deposit": total_deposit,
            "total_loan_outstanding": total_loan,
            "net_position": total_balance + total_deposit - total_loan,
            "deposits_maturing_7d": sum(
                1 for d in deposit_items if d["maturing_within_7d"]
            ),
        },
        "accounts": [_account_payload(a) for a in accounts],
        "deposits": deposit_items,
        "loans": loan_items,
    }


# ---------------------------------------------------------------------------
# Baseline / digital twin
# ---------------------------------------------------------------------------
@app.get("/customers/{customer_id}/baseline", tags=["baseline"])
def get_baseline(customer_id: int):
    """Digital twin. risk-scoring gọi endpoint này ở mỗi lần precheck nên nó
    phải trả thẳng từ behavior_profile, không quét lại lịch sử giao dịch."""
    customer = _customer_or_404(customer_id)
    row = query_one(
        "SELECT * FROM behavior_profile WHERE customer_id = %s", (customer_id,)
    )
    if row is None:
        raise HTTPException(
            404,
            f"chưa có baseline cho customer {customer_id}; "
            "gọi POST /customers/{id}/baseline/recompute",
        )
    row.pop("profile_id", None)
    return {
        "customer_id": customer_id,
        "persona": persona_of(customer.get("target"), customer.get("date_of_birth")),
        "baseline": row,
    }


@app.put("/customers/{customer_id}/baseline", tags=["baseline"])
def upsert_baseline(customer_id: int, req: BaselineUpsertRequest):
    """Upsert baseline. Tách khỏi recompute để job offline cũng ghi được."""
    _customer_or_404(customer_id)
    execute(
        """
        INSERT INTO behavior_profile (
          customer_id, window_days, out_median, out_p90, out_p99, out_max,
          monthly_out_avg, monthly_in_avg, known_beneficiaries, new_benef_per_30d,
          share_to_new_benef, active_hours, night_tx_ratio, weekend_tx_ratio,
          tx_per_week, max_tx_per_day, max_cum_to_one_benef_14d, balance_median,
          max_drain_ratio_90d, computed_at
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s, now()
        )
        ON CONFLICT (customer_id) DO UPDATE SET
          window_days = EXCLUDED.window_days,
          out_median = EXCLUDED.out_median, out_p90 = EXCLUDED.out_p90,
          out_p99 = EXCLUDED.out_p99, out_max = EXCLUDED.out_max,
          monthly_out_avg = EXCLUDED.monthly_out_avg,
          monthly_in_avg = EXCLUDED.monthly_in_avg,
          known_beneficiaries = EXCLUDED.known_beneficiaries,
          new_benef_per_30d = EXCLUDED.new_benef_per_30d,
          share_to_new_benef = EXCLUDED.share_to_new_benef,
          active_hours = EXCLUDED.active_hours,
          night_tx_ratio = EXCLUDED.night_tx_ratio,
          weekend_tx_ratio = EXCLUDED.weekend_tx_ratio,
          tx_per_week = EXCLUDED.tx_per_week,
          max_tx_per_day = EXCLUDED.max_tx_per_day,
          max_cum_to_one_benef_14d = EXCLUDED.max_cum_to_one_benef_14d,
          balance_median = EXCLUDED.balance_median,
          max_drain_ratio_90d = EXCLUDED.max_drain_ratio_90d,
          computed_at = now()
        """,
        (
            customer_id, req.window_days, req.out_median, req.out_p90, req.out_p99,
            req.out_max, req.monthly_out_avg, req.monthly_in_avg,
            req.known_beneficiaries, req.new_benef_per_30d, req.share_to_new_benef,
            __import__("json").dumps(req.active_hours), req.night_tx_ratio,
            req.weekend_tx_ratio, req.tx_per_week, req.max_tx_per_day,
            req.max_cum_to_one_benef_14d, req.balance_median, req.max_drain_ratio_90d,
        ),
    )
    return get_baseline(customer_id)


@app.post("/customers/{customer_id}/baseline/recompute", tags=["baseline"])
def recompute_baseline(
    customer_id: int,
    window_days: int = Query(90, ge=30, le=365),
    bump_feedback_version: bool = Query(
        False,
        description="Đặt true khi việc tính lại là do áp dụng một nhãn feedback. "
        "feedback_version tăng lên giúp truy ngược một quyết định đã được chấm "
        "trên phiên bản baseline nào.",
    ),
):
    """Lấy số liệu thống kê từ transaction-service (chủ sở hữu transaction_history)
    rồi ghi vào behavior_profile. Ranh giới microservice được giữ: service này
    không truy vấn bảng của service khác."""
    _customer_or_404(customer_id)
    url = f"{TRANSACTION_SERVICE_URL}/transactions/{customer_id}/baseline-metrics"
    try:
        resp = httpx.get(url, params={"window_days": window_days}, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            502, f"không lấy được baseline-metrics từ transaction-service: {exc}"
        ) from exc
    metrics = resp.json()["metrics"]
    result = upsert_baseline(customer_id, BaselineUpsertRequest(**metrics))
    if bump_feedback_version:
        execute(
            "UPDATE behavior_profile SET feedback_version = feedback_version + 1 "
            "WHERE customer_id = %s",
            (customer_id,),
        )
        result["baseline"]["feedback_version"] += 1
    return result


# ---------------------------------------------------------------------------
# Beneficiary
# ---------------------------------------------------------------------------
@app.get("/customers/{customer_id}/beneficiaries", tags=["beneficiary"])
def list_beneficiaries(
    customer_id: int,
    status: str | None = Query(None, description="ACTIVE | BLOCKED | SUSPECTED"),
    include_full: bool = Query(False, description="Trả kèm tên + số TK đầy đủ — chỉ cho màn danh bạ UI, không dùng cho ngữ cảnh agent"),
):
    _customer_or_404(customer_id)
    sql = "SELECT * FROM beneficiary WHERE customer_id = %s"
    params: list = [customer_id]
    if status:
        sql += " AND beneficiary_status = %s"
        params.append(status.upper())
    sql += " ORDER BY beneficiary_last_tx_at DESC NULLS LAST"
    rows = query(sql, params)
    return {
        "customer_id": customer_id,
        "count": len(rows),
        "beneficiaries": [mask_beneficiary_row(r, include_full=include_full) for r in rows],
    }


@app.get("/beneficiaries/{beneficiary_id}", tags=["beneficiary"])
def get_beneficiary(beneficiary_id: int):
    row = query_one(
        "SELECT * FROM beneficiary WHERE beneficiary_id = %s", (beneficiary_id,)
    )
    if row is None:
        raise HTTPException(404, f"beneficiary {beneficiary_id} không tồn tại")
    return mask_beneficiary_row(row)


@app.post("/customers/{customer_id}/beneficiaries/resolve", tags=["beneficiary"])
def resolve_beneficiary(customer_id: int, req: BeneficiaryResolveRequest):
    """Đầu vào trực tiếp của yếu tố rủi ro 2 (người nhận mới) và 5 (lịch sử quan hệ).

    Nhận số TK thô nhưng KHÔNG trả lại — response chỉ có bản mask cùng các đặc
    trưng quan hệ. Người nhận chưa từng có trong sổ trả is_new = true.
    """
    _customer_or_404(customer_id)
    row = query_one(
        """
        SELECT * FROM beneficiary
        WHERE customer_id = %s AND beneficiary_bank_code = %s
          AND beneficiary_account_no = %s
        """,
        (customer_id, req.bank_code.upper(), req.account_no),
    )
    if row is None:
        return {
            "customer_id": customer_id,
            "known": False,
            "is_new": True,
            "bank_code": req.bank_code.upper(),
            "account_masked": (req.account_no[:4] + " ****")
            if len(req.account_no) > 4
            else "****",
            "relationship": "UNKNOWN",
            "tx_count": 0,
            "total_out": 0,
            "total_in": 0,
            "age_days": 0,
            "status": "ACTIVE",
        }
    payload = mask_beneficiary_row(row)
    payload["known"] = True
    return payload


@app.patch("/beneficiaries/{beneficiary_id}/status", tags=["beneficiary"])
def set_beneficiary_status(beneficiary_id: int, req: BeneficiaryStatusRequest):
    """Đóng vòng closed-loop: feedback nhãn `fraud` đẩy người nhận sang SUSPECTED."""
    row = execute_returning(
        """
        UPDATE beneficiary SET beneficiary_status = %s
        WHERE beneficiary_id = %s RETURNING *
        """,
        (req.status, beneficiary_id),
    )
    if row is None:
        raise HTTPException(404, f"beneficiary {beneficiary_id} không tồn tại")
    return mask_beneficiary_row(row)


# ---------------------------------------------------------------------------
# Account event (yếu tố rủi ro số 6 — ngữ cảnh gần đây)
# ---------------------------------------------------------------------------
@app.get("/customers/{customer_id}/events", tags=["event"])
def list_events(
    customer_id: int,
    within_minutes: int | None = Query(
        None,
        ge=1,
        description="Chỉ lấy sự kiện trong N phút trước mốc `before`. Engine rủi ro dùng 60.",
    ),
    before: str | None = Query(
        None,
        description="Mốc thời gian ISO 8601 để tính ngược cửa sổ; bỏ trống = hiện tại. "
        "Engine truyền thời điểm giao dịch vào đây, nhờ vậy chấm lại một giao dịch cũ "
        "vẫn thấy đúng các sự kiện xảy ra ngay trước nó.",
    ),
    limit: int = Query(50, ge=1, le=200),
):
    _customer_or_404(customer_id)
    sql = "SELECT * FROM account_event WHERE customer_id = %s"
    params: list = [customer_id]
    if within_minutes:
        sql += (
            " AND event_time <= COALESCE(%s::timestamptz, now())"
            " AND event_time >= COALESCE(%s::timestamptz, now()) - make_interval(mins => %s)"
        )
        params += [before, before, within_minutes]
    sql += " ORDER BY event_time DESC LIMIT %s"
    params.append(limit)
    rows = query(sql, params)
    return {
        "customer_id": customer_id,
        "within_minutes": within_minutes,
        "before": before,
        "count": len(rows),
        "events": [_event_payload(r) for r in rows],
    }


@app.post("/customers/{customer_id}/events", tags=["event"], status_code=201)
def create_event(customer_id: int, req: AccountEventRequest):
    _customer_or_404(customer_id)
    row = execute_returning(
        """
        INSERT INTO account_event
          (customer_id, account_id, event_type, amount, event_time, source_ref, meta)
        VALUES (%s, %s, %s, %s, COALESCE(%s::timestamptz, now()), %s, %s::jsonb)
        RETURNING *
        """,
        (
            customer_id,
            req.account_id,
            req.event_type,
            req.amount,
            req.event_time,
            req.source_ref,
            __import__("json").dumps(req.meta),
        ),
    )
    return _event_payload(row)


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------
AGENT_TOOLS = [
    tool(
        "list_customers",
        "Liệt kê khách hàng, lọc theo phân khúc SALARY/HNW/SENIOR.",
        "GET", "/customers",
        params={"persona": "SALARY|HNW|SENIOR (tùy chọn)", "limit": "int", "offset": "int"},
        returns="Danh sách hồ sơ đã mask.",
    ),
    tool(
        "get_customer",
        "Hồ sơ một khách hàng: persona, tuổi, tên đã mask, số tài khoản/người nhận.",
        "GET", "/customers/{customer_id}",
        params={"customer_id": "int, mã CIF"},
        returns="Hồ sơ đã mask.",
    ),
    tool(
        "get_portfolio",
        "Toàn cảnh tài chính cho Copilot: số dư, sổ tiết kiệm (kèm cờ sắp đáo hạn "
        "trong 7 ngày), khoản vay và net position.",
        "GET", "/customers/{customer_id}/portfolio",
        params={"customer_id": "int"},
        returns="summary + accounts + deposits + loans.",
    ),
    tool(
        "get_accounts",
        "Danh sách tài khoản thanh toán kèm số dư khả dụng.",
        "GET", "/customers/{customer_id}/accounts",
        params={"customer_id": "int"},
        returns="Danh sách account.",
    ),
    tool(
        "get_baseline",
        "Digital twin — baseline hành vi 90 ngày (out_p90/p99, tỷ lệ giao dịch đêm, "
        "số người nhận đã biết...). Dùng để đánh giá một giao dịch có bất thường không.",
        "GET", "/customers/{customer_id}/baseline",
        params={"customer_id": "int"},
        returns="behavior_profile đầy đủ.",
    ),
    tool(
        "recompute_baseline",
        "Tính lại baseline từ lịch sử giao dịch rồi ghi đè behavior_profile.",
        "POST", "/customers/{customer_id}/baseline/recompute",
        params={
            "customer_id": "int", "window_days": "int, mặc định 90",
            "bump_feedback_version": "bool, true khi tính lại do áp dụng feedback",
        },
        returns="Baseline sau khi tính lại.",
    ),
    tool(
        "resolve_beneficiary",
        "Tra một người nhận theo mã ngân hàng + số tài khoản, trả về đặc trưng quan hệ "
        "(mới hay cũ, số lần đã chuyển, tổng tiền đã nhận về, quan hệ, trạng thái). "
        "Đây là đầu vào chính để chấm yếu tố 'người nhận mới' và 'lịch sử quan hệ'.",
        "POST", "/customers/{customer_id}/beneficiaries/resolve",
        params={"customer_id": "int"},
        body={"bank_code": "str", "account_no": "str"},
        returns="Đặc trưng quan hệ; số tài khoản chỉ trả bản mask.",
    ),
    tool(
        "list_beneficiaries",
        "Sổ người nhận của khách hàng, lọc theo trạng thái ACTIVE/BLOCKED/SUSPECTED.",
        "GET", "/customers/{customer_id}/beneficiaries",
        params={"customer_id": "int", "status": "ACTIVE|BLOCKED|SUSPECTED (tùy chọn)"},
        returns="Danh sách người nhận đã mask.",
    ),
    tool(
        "set_beneficiary_status",
        "Đánh dấu người nhận SUSPECTED hoặc BLOCKED sau khi xác minh gian lận.",
        "PATCH", "/beneficiaries/{beneficiary_id}/status",
        params={"beneficiary_id": "int"},
        body={"status": "ACTIVE|BLOCKED|SUSPECTED", "reason": "str (tùy chọn)"},
        returns="Người nhận sau cập nhật.",
    ),
    tool(
        "list_recent_events",
        "Sự kiện tài khoản gần đây (tất toán sổ, nâng hạn mức, đăng nhập thiết bị mới, "
        "đổi mật khẩu, nhận tiền từ người lạ). Engine chỉ xét cửa sổ 60 phút trước giao dịch.",
        "GET", "/customers/{customer_id}/events",
        params={
            "customer_id": "int", "within_minutes": "int, engine dùng 60",
            "before": "ISO 8601, mốc tính ngược cửa sổ (mặc định: hiện tại)",
            "limit": "int",
        },
        returns="Danh sách account_event.",
    ),
    tool(
        "create_event",
        "Ghi nhận một sự kiện tài khoản mới.",
        "POST", "/customers/{customer_id}/events",
        params={"customer_id": "int"},
        body={
            "event_type": "SAVINGS_CLOSED|LIMIT_RAISED|NEW_DEVICE_LOGIN|PASSWORD_RESET|INBOUND_UNKNOWN",
            "account_id": "int (tùy chọn)",
            "amount": "number (tùy chọn)",
            "event_time": "ISO 8601 (tùy chọn)",
            "source_ref": "str (tùy chọn)",
            "meta": "object",
        },
        returns="Sự kiện vừa tạo.",
    ),
]
