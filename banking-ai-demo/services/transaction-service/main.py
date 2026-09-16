"""transaction-service

Sở hữu: transaction_history, spending_insight, product, product_recommendation.

Vai trò trong hệ:
  · Journey A (Copilot): tổng hợp chi tiêu theo tháng/nhóm, dự báo dòng tiền,
    sinh insight và gợi ý sản phẩm SAVINGS/INVESTMENT.
  · Journey B (chống rủi ro): là nguồn duy nhất tính baseline hành vi
    (`/transactions/{id}/baseline-metrics`) và là nơi ghi nhận giao dịch mới,
    khóa tạm hoặc hủy giao dịch theo quyết định của Guardian.

Hai cột `is_fraud` và `fraud_case_id` chỉ phục vụ kiểm thử engine; hàm `_tx_payload`
loại chúng khỏi mọi response nên chúng không bao giờ rò ra API hay prompt LLM.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    db_health,
    execute_returning,
    mask_free_text,
    now_vn,
    num,
    openapi_description,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
    tx_datetime,
    ymd_to_iso,
)

SERVICE_NAME = "transaction-service"
OWNS_TABLES = [
    "transaction_history",
    "spending_insight",
    "product",
    "product_recommendation",
]

CATEGORIES = [
    "FOOD", "TRANSPORT", "SHOPPING", "BILLS", "RENT", "HEALTH",
    "FAMILY_SUPPORT", "INVESTMENT", "TRANSFER_P2P", "OTHER",
]

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": 'transaction',
        "description": (
            'Truy vấn và ghi nhận giao dịch. Guardian dùng `PATCH .../status` để chuyển giao dịch sang PENDING khi khóa tạm hoặc CANCELLED khi hủy.'
        ),
    },
    {
        "name": 'baseline',
        "description": (
            'Tính 18 chỉ số hành vi từ lịch sử. Chỉ lấy giao dịch OUT / POSTED / không gian lận — để hành vi lừa đảo không trở thành chuẩn mực bình thường.'
        ),
    },
    {
        "name": 'insight',
        "description": (
            'Phân tích chi tiêu theo kỳ, tính sẵn và lưu lại. Số liệu do service tính; LLM chỉ viết phần diễn giải, không được tự đưa ra con số.'
        ),
    },
    {
        "name": 'product',
        "description": (
            'Danh mục sản phẩm và gợi ý tích lũy. Chỉ gợi ý SAVINGS và INVESTMENT — phạm vi demo không gợi ý sản phẩm vay.'
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary='Lịch sử giao dịch, phân tích chi tiêu, dự báo dòng tiền, danh mục và gợi ý sản phẩm.',
    description=openapi_description(SERVICE_NAME, 'Lịch sử giao dịch, phân tích chi tiêu, dự báo dòng tiền, danh mục và gợi ý sản phẩm.', OWNS_TABLES),
    openapi_tags=OPENAPI_TAGS,
    contact={"name": "MSB AI Financial Guardian — xem manifest tại GET /agent/tools"},
    docs_url="/docs",
    redoc_url="/redoc",
)
setup_docs(app, SERVICE_NAME)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class TransactionCreate(BaseModel):
    transaction_id: int | None = Field(
        None, description="Bỏ trống để service tự cấp số tiếp theo"
    )
    customer_id: int
    account_id: int
    direction: Literal["OUT", "IN"] = "OUT"
    amount: float
    beneficiary_id: int | None = None
    beneficiary_bank_code: str | None = None
    beneficiary_account_masked: str | None = None
    currency: str = "VND"
    balance_after: float | None = None
    transaction_type: Literal["FT", "BILL", "QR", "ATM", "SALARY"] = "FT"
    category: str = "TRANSFER_P2P"
    transaction_description: str | None = None
    channel: Literal["MOBILE", "INTERNET", "BRANCH", "ATM"] = "MOBILE"
    transaction_date: str | None = Field(None, description="YYYYMMDD, bỏ trống = hôm nay")
    transaction_time: str | None = Field(None, description="HHMMSS, bỏ trống = bây giờ")
    status: Literal["POSTED", "PENDING", "CANCELLED", "REVERSED"] = "POSTED"
    risk_decision_id: str | None = None


class TransactionStatusUpdate(BaseModel):
    status: Literal["POSTED", "PENDING", "CANCELLED", "REVERSED"]
    risk_decision_id: str | None = None


class RecommendationStatusUpdate(BaseModel):
    status: Literal["SHOWN", "ACCEPTED", "DISMISSED"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tx_payload(row: dict) -> dict:
    """Ánh xạ an toàn: bỏ is_fraud / fraud_case_id, cast tiền sang số, mask nội dung CK."""
    amount = float(num(row.get("amount")))
    return {
        "transaction_id": row["transaction_id"],
        "customer_id": row.get("customer_id"),
        "account_id": row.get("account_id"),
        "direction": row.get("direction"),
        "amount": amount,
        "signed_amount": amount if row.get("direction") == "IN" else -amount,
        "currency": row.get("currency"),
        "balance_after": float(num(row.get("balance_after"))) if row.get("balance_after") else None,
        "beneficiary_id": row.get("beneficiary_id"),
        "beneficiary_bank_code": row.get("beneficiary_bank_code"),
        "beneficiary_account_masked": row.get("beneficiary_account_masked"),
        "transaction_type": row.get("transaction_type"),
        "category": row.get("category"),
        "description": mask_free_text(row.get("transaction_description")),
        "channel": row.get("channel"),
        "date": ymd_to_iso(row.get("transaction_date")),
        "time": row.get("transaction_time"),
        "transaction_date": row.get("transaction_date"),
        "status": row.get("status"),
        "risk_decision_id": row.get("risk_decision_id"),
    }


def _require_transactions(customer_id: int) -> None:
    exists = query_one(
        "SELECT 1 AS x FROM transaction_history WHERE customer_id = %s LIMIT 1",
        (customer_id,),
    )
    if exists is None:
        raise HTTPException(404, f"không có giao dịch nào của customer {customer_id}")


def _percentile(values: list[float], pct: float) -> float:
    """Nội suy tuyến tính; tránh phụ thuộc numpy cho image nhẹ."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


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
    return service_info(
        SERVICE_NAME,
        "Lịch sử giao dịch, phân tích chi tiêu theo tháng/nhóm, dự báo dòng tiền, "
        "số liệu baseline hành vi, danh mục và gợi ý sản phẩm.",
        OWNS_TABLES,
        AGENT_TOOLS,
    )


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    return agent_tools_payload(SERVICE_NAME, "TRANSACTION_SERVICE_URL", AGENT_TOOLS)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
@app.get("/transactions/{customer_id}", tags=["transaction"])
def get_transactions(
    customer_id: int,
    date_from: str | None = Query(None, description="YYYYMMDD"),
    date_to: str | None = Query(None, description="YYYYMMDD"),
    direction: Literal["OUT", "IN"] | None = None,
    category: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    sql = "SELECT * FROM transaction_history WHERE customer_id = %s"
    params: list = [customer_id]
    if date_from:
        sql += " AND transaction_date >= %s"
        params.append(date_from)
    if date_to:
        sql += " AND transaction_date <= %s"
        params.append(date_to)
    if direction:
        sql += " AND direction = %s"
        params.append(direction)
    if category:
        sql += " AND category = %s"
        params.append(category.upper())
    if status:
        sql += " AND status = %s"
        params.append(status.upper())
    sql += " ORDER BY transaction_date DESC, transaction_time DESC LIMIT %s OFFSET %s"
    params += [limit, offset]
    rows = query(sql, params)
    return {
        "customer_id": customer_id,
        "count": len(rows),
        "transactions": [_tx_payload(r) for r in rows],
    }


@app.get("/transactions/{customer_id}/monthly-summary", tags=["transaction"])
def monthly_summary(
    customer_id: int, months: int = Query(6, ge=1, le=24)
):
    """Thu/chi/net theo tháng kèm phân rã theo nhóm chi tiêu — Scene 1 của Copilot."""
    _require_transactions(customer_id)
    start = (now_vn() - timedelta(days=31 * months)).strftime("%Y%m%d")
    rows = query(
        """
        SELECT * FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s AND status = 'POSTED'
        ORDER BY transaction_date
        """,
        (customer_id, start),
    )

    buckets: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "expense": 0.0, "count": 0, "by_category": defaultdict(float)}
    )
    for r in rows:
        period = r["transaction_date"][:6]  # YYYYMM
        amount = float(num(r.get("amount")))
        b = buckets[period]
        b["count"] += 1
        if r.get("direction") == "IN":
            b["income"] += amount
        else:
            b["expense"] += amount
            b["by_category"][r.get("category") or "OTHER"] += amount

    summary = []
    for period in sorted(buckets):
        b = buckets[period]
        by_cat = sorted(b["by_category"].items(), key=lambda kv: kv[1], reverse=True)
        summary.append(
            {
                "period": period,
                "month": f"{period[:4]}-{period[4:]}",
                "income": round(b["income"]),
                "expense": round(b["expense"]),
                "net": round(b["income"] - b["expense"]),
                "count": b["count"],
                "top_categories": [
                    {"category": c, "amount": round(v), "rank": i + 1}
                    for i, (c, v) in enumerate(by_cat[:5])
                ],
            }
        )
    return {"customer_id": customer_id, "months": len(summary), "summary": summary}


# Chuyển khoản đi KHÔNG phải chi tiêu: nó là tiền chuyển chỗ, không phải tiền
# tiêu mất. Gộp chung thì một lệnh chuyển lớn nuốt trọn biểu đồ và các nhóm chi
# tiêu thật bị ép về gần 0%.
TRANSFER_CATEGORIES = ("TRANSFER_P2P", "TRANSFER")


def _quarter_of(transaction_date: str) -> tuple[int, int]:
    """(năm, quý) từ chuỗi YYYYMMDD."""
    year, month = int(transaction_date[:4]), int(transaction_date[4:6])
    return year, (month - 1) // 3 + 1


@app.get("/transactions/{customer_id}/quarterly-summary", tags=["transaction"])
def quarterly_summary(
    customer_id: int,
    quarters: int = Query(8, ge=1, le=20, description="Số quý gần nhất cần lấy"),
    include_transfers: bool = Query(
        False, description="Tính cả chuyển khoản đi vào phần chi tiêu"
    ),
):
    """Thu/chi/net theo QUÝ kèm phân rã theo nhóm chi tiêu.

    Khác monthly-summary ở chỗ quý gom đủ dữ liệu để so sánh có nghĩa: một tháng
    lẻ có thể chỉ có vài giao dịch, còn xu hướng theo quý thì đọc được.

    Mỗi nhóm kèm `delta_vs_prev_pct` — thay đổi so với chính nhóm đó ở quý liền
    trước. Đây là con số trả lời thẳng câu hỏi "quý này tôi tiêu khác gì quý
    trước", thay vì bắt bên gọi tự trừ hai danh sách.
    """
    _require_transactions(customer_id)
    # Lấy dư một quý rồi cắt: quý cũ nhất trong cửa sổ thường bị cắt giữa chừng
    # nên delta của nó vô nghĩa, nhưng vẫn cần nó làm mốc so sánh cho quý kế.
    start = (now_vn() - timedelta(days=92 * (quarters + 1))).strftime("%Y%m%d")
    rows = query(
        """
        SELECT * FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s AND status = 'POSTED'
        ORDER BY transaction_date
        """,
        (customer_id, start),
    )

    excluded = () if include_transfers else TRANSFER_CATEGORIES
    buckets: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "expense": 0.0, "count": 0,
                 "by_category": defaultdict(float), "excluded_amount": 0.0}
    )
    for r in rows:
        date = r.get("transaction_date") or ""
        if len(date) < 6:
            continue
        year, quarter = _quarter_of(date)
        b = buckets[f"{year}Q{quarter}"]
        amount = float(num(r.get("amount")))
        b["count"] += 1
        if r.get("direction") == "IN":
            b["income"] += amount
            continue
        category = r.get("category") or "OTHER"
        if category in excluded:
            # Vẫn cộng vào một sổ riêng để bên gọi biết đã bỏ qua bao nhiêu,
            # thay vì im lặng làm số liệu không khớp sao kê.
            b["excluded_amount"] += amount
            continue
        b["expense"] += amount
        b["by_category"][category] += amount

    periods = sorted(buckets)
    summary = []
    for i, period in enumerate(periods):
        b = buckets[period]
        total = b["expense"] or 1
        truoc = buckets[periods[i - 1]]["by_category"] if i > 0 else {}
        by_cat = sorted(b["by_category"].items(), key=lambda kv: kv[1], reverse=True)
        year, quarter = int(period[:4]), int(period[-1])
        summary.append({
            "period": period,
            "year": year,
            "quarter": quarter,
            "label": f"Quý {quarter}/{year}",
            "income": round(b["income"]),
            "expense": round(b["expense"]),
            "net": round(b["income"] - b["expense"]),
            "count": b["count"],
            "excluded_transfer_amount": round(b["excluded_amount"]),
            "by_category": [
                {
                    "category": c,
                    "amount": round(v),
                    "pct": round(v * 100 / total),
                    "rank": j + 1,
                    # None ở quý đầu tiên và ở nhóm chưa từng xuất hiện: chưa có
                    # mốc để so thì nói là chưa có, không trả 0 như thể không đổi.
                    "delta_vs_prev_pct": (
                        round((v - truoc[c]) * 100 / truoc[c], 1)
                        if truoc.get(c) else None
                    ),
                }
                for j, (c, v) in enumerate(by_cat)
            ],
        })

    # Cắt bỏ quý dư đã lấy thêm để làm mốc so sánh.
    summary = summary[-quarters:]
    totals: dict[str, float] = defaultdict(float)
    for q in summary:
        for c in q["by_category"]:
            totals[c["category"]] += c["amount"]
    grand = sum(totals.values()) or 1

    return {
        "customer_id": customer_id,
        "quarters": len(summary),
        "include_transfers": include_transfers,
        "excluded_categories": list(excluded),
        "summary": summary,
        "category_totals": [
            {"category": c, "amount": round(v), "pct": round(v * 100 / grand)}
            for c, v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


_MONTH_NAMES_VI = (
    "", "Tháng 1", "Tháng 2", "Tháng 3", "Tháng 4", "Tháng 5", "Tháng 6",
    "Tháng 7", "Tháng 8", "Tháng 9", "Tháng 10", "Tháng 11", "Tháng 12",
)


@app.get("/transactions/{customer_id}/monthly-comparison", tags=["transaction"])
def monthly_comparison(
    customer_id: int,
    months: int = Query(6, ge=2, le=24, description="Số tháng gần nhất cần so sánh"),
    include_transfers: bool = Query(
        False, description="Tính cả chuyển khoản đi vào phần chi tiêu"
    ),
):
    """So sánh chi tiêu GIỮA CÁC THÁNG, kèm mức thay đổi so với tháng liền trước.

    Khác monthly-summary (chỉ liệt kê từng tháng độc lập) ở chỗ endpoint này trả
    thẳng con số so sánh: mỗi tháng có `delta_vs_prev_pct` cho tổng chi, và mỗi
    nhóm có `delta_vs_prev_pct` so với chính nhóm đó ở tháng trước. Đây là thứ
    trả lời câu "tháng này so với tháng trước thế nào" mà không bắt bên gọi tự
    trừ hai danh sách — cùng khuôn với quarterly-summary, chỉ đổi đơn vị sang
    tháng.
    """
    _require_transactions(customer_id)
    # Lấy dư một tháng rồi cắt: tháng cũ nhất trong cửa sổ dùng làm mốc so sánh
    # cho tháng kế, bản thân delta của nó thì bỏ.
    start = (now_vn() - timedelta(days=31 * (months + 1))).strftime("%Y%m%d")
    rows = query(
        """
        SELECT * FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s AND status = 'POSTED'
        ORDER BY transaction_date
        """,
        (customer_id, start),
    )

    excluded = () if include_transfers else TRANSFER_CATEGORIES
    buckets: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "expense": 0.0, "count": 0,
                 "by_category": defaultdict(float), "excluded_amount": 0.0}
    )
    for r in rows:
        date = r.get("transaction_date") or ""
        if len(date) < 6:
            continue
        b = buckets[date[:6]]  # YYYYMM
        amount = float(num(r.get("amount")))
        b["count"] += 1
        if r.get("direction") == "IN":
            b["income"] += amount
            continue
        category = r.get("category") or "OTHER"
        if category in excluded:
            b["excluded_amount"] += amount
            continue
        b["expense"] += amount
        b["by_category"][category] += amount

    periods = sorted(buckets)
    summary = []
    for i, period in enumerate(periods):
        b = buckets[period]
        total = b["expense"] or 1
        prev = buckets[periods[i - 1]] if i > 0 else None
        truoc = prev["by_category"] if prev else {}
        by_cat = sorted(b["by_category"].items(), key=lambda kv: kv[1], reverse=True)
        year, month = int(period[:4]), int(period[4:6])
        summary.append({
            "period": period,
            "year": year,
            "month": month,
            "label": f"{_MONTH_NAMES_VI[month]}/{year}",
            "income": round(b["income"]),
            "expense": round(b["expense"]),
            "net": round(b["income"] - b["expense"]),
            "count": b["count"],
            "excluded_transfer_amount": round(b["excluded_amount"]),
            # Thay đổi TỔNG chi so với tháng trước — con số cho bảng so sánh tháng.
            "delta_vs_prev_pct": (
                round((b["expense"] - prev["expense"]) * 100 / prev["expense"], 1)
                if prev and prev["expense"] else None
            ),
            "by_category": [
                {
                    "category": c,
                    "amount": round(v),
                    "pct": round(v * 100 / total),
                    "rank": j + 1,
                    "delta_vs_prev_pct": (
                        round((v - truoc[c]) * 100 / truoc[c], 1)
                        if truoc.get(c) else None
                    ),
                }
                for j, (c, v) in enumerate(by_cat)
            ],
        })

    # Cắt bỏ tháng dư đã lấy thêm làm mốc.
    summary = summary[-months:]
    totals: dict[str, float] = defaultdict(float)
    for m in summary:
        for c in m["by_category"]:
            totals[c["category"]] += c["amount"]
    grand = sum(totals.values()) or 1

    return {
        "customer_id": customer_id,
        "months": len(summary),
        "include_transfers": include_transfers,
        "excluded_categories": list(excluded),
        "summary": summary,
        "category_totals": [
            {"category": c, "amount": round(v), "pct": round(v * 100 / grand)}
            for c, v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


@app.get("/transactions/{customer_id}/cashflow-forecast", tags=["transaction"])
def cashflow_forecast(customer_id: int, horizon: int = Query(3, ge=1, le=12)):
    """Dự báo net theo trung bình có trọng số: 3 tháng gần nhất nặng hơn các tháng cũ.

    Chỉ lấy các tháng có dữ liệu trọn vẹn. Tháng đầu cửa sổ thường bị cắt giữa chừng
    và tháng hiện tại thì chưa hết, nên nếu tính cả hai vào trung bình thì một khách
    hàng thực tế đang dư tiền vẫn có thể bị dự báo âm — sai lệch đủ để Copilot đưa ra
    lời khuyên ngược hẳn với tình hình thật.
    """
    _require_transactions(customer_id)
    data = monthly_summary(customer_id, months=7)["summary"]
    if not data:
        raise HTTPException(404, f"không đủ dữ liệu dự báo cho customer {customer_id}")

    current_period = now_vn().strftime("%Y%m")
    complete = [m for m in data if m["period"] != current_period]
    if len(complete) > 2:
        complete = complete[1:]   # bỏ tháng đầu vì cửa sổ cắt vào giữa tháng
    excluded = [m["month"] for m in data if m not in complete]
    if not complete:
        complete = data
        excluded = []

    nets = [m["net"] for m in complete]
    recent = nets[-3:] if len(nets) >= 3 else nets
    weighted = round(0.6 * (sum(recent) / len(recent)) + 0.4 * (sum(nets) / len(nets)))

    last = complete[-1]["period"]
    year, mon = int(last[:4]), int(last[4:])
    forecast = []
    for _ in range(horizon):
        mon += 1
        if mon > 12:
            mon, year = 1, year + 1
        forecast.append({"month": f"{year:04d}-{mon:02d}", "projected_net": weighted})

    avg_expense = round(sum(m["expense"] for m in complete) / len(complete))
    return {
        "customer_id": customer_id,
        "avg_monthly_net": weighted,
        "avg_monthly_expense": avg_expense,
        "months_used": [m["month"] for m in complete],
        "months_excluded_incomplete": excluded,
        "months_of_buffer_needed": 3,
        "recommended_emergency_fund": avg_expense * 3,
        "forecast": forecast,
    }


@app.get("/transactions/{customer_id}/baseline-metrics", tags=["baseline"])
def baseline_metrics(customer_id: int, window_days: int = Query(90, ge=30, le=365)):
    """Tính đủ 18 chỉ số của behavior_profile từ lịch sử giao dịch.

    Baseline chỉ tính trên direction = OUT, status = POSTED, is_fraud = 'N' —
    nếu để giao dịch gian lận lọt vào baseline thì chính hành vi lừa đảo sẽ
    trở thành "bình thường" và engine mất khả năng phát hiện.
    customer-profile-service gọi endpoint này rồi ghi vào behavior_profile.
    """
    start_dt = now_vn() - timedelta(days=window_days)
    start = start_dt.strftime("%Y%m%d")

    out_rows = query(
        """
        SELECT * FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s
          AND direction = 'OUT' AND status = 'POSTED' AND is_fraud = 'N'
        ORDER BY transaction_date, transaction_time
        """,
        (customer_id, start),
    )
    in_rows = query(
        """
        SELECT amount, transaction_date FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s
          AND direction = 'IN' AND status = 'POSTED' AND is_fraud = 'N'
        """,
        (customer_id, start),
    )
    if not out_rows:
        raise HTTPException(
            404,
            f"không đủ giao dịch OUT trong {window_days} ngày để tính baseline "
            f"cho customer {customer_id}",
        )

    amounts = [float(num(r["amount"])) for r in out_rows]
    months = max(1.0, window_days / 30.0)
    weeks = max(1.0, window_days / 7.0)

    # Phân bố theo giờ trong ngày + tỷ lệ giao dịch đêm (23:00–05:59)
    hours = [0.0] * 24
    night = 0
    weekend = 0
    per_day: dict[str, int] = defaultdict(int)
    for r in out_rows:
        dt = tx_datetime(r["transaction_date"], r["transaction_time"])
        if dt is None:
            continue
        hours[dt.hour] += 1
        if dt.hour >= 23 or dt.hour < 6:
            night += 1
        if dt.weekday() >= 5:
            weekend += 1
        per_day[r["transaction_date"]] += 1
    total = max(1, sum(int(h) for h in hours))
    active_hours = [round(h / total, 4) for h in hours]

    # Người nhận: số người đã biết, tốc độ thêm mới, tỷ trọng tiền chảy về người mới
    benef_ids = {r["beneficiary_id"] for r in out_rows if r.get("beneficiary_id")}
    benef_first_tx: dict[int, str] = {}
    for r in out_rows:
        bid = r.get("beneficiary_id")
        if bid and bid not in benef_first_tx:
            benef_first_tx[bid] = r["transaction_date"]
    new_benef_cutoff = (now_vn() - timedelta(days=30)).strftime("%Y%m%d")
    new_benefs = {b for b, d in benef_first_tx.items() if d >= new_benef_cutoff}
    to_new = sum(
        float(num(r["amount"]))
        for r in out_rows
        if r.get("beneficiary_id") in new_benefs
    )
    total_out = sum(amounts)

    # Tổng dồn tối đa về một người nhận trong 14 ngày — bắt mẫu chuỗi đầu tư/romance
    max_cum_14d = 0.0
    by_benef: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    for r in out_rows:
        bid = r.get("beneficiary_id")
        dt = tx_datetime(r["transaction_date"], r["transaction_time"])
        if bid and dt:
            by_benef[bid].append((dt, float(num(r["amount"]))))
    for items in by_benef.values():
        items.sort()
        left = 0
        window_sum = 0.0
        for right, (dt, amt) in enumerate(items):
            window_sum += amt
            while items[left][0] < dt - timedelta(days=14):
                window_sum -= items[left][1]
                left += 1
            max_cum_14d = max(max_cum_14d, window_sum)

    # Số dư và tỷ lệ "vét sạch" cao nhất từng xảy ra
    balances = [float(num(r["balance_after"])) for r in out_rows if r.get("balance_after")]
    max_drain = 0.0
    for r in out_rows:
        bal_after = float(num(r.get("balance_after"))) if r.get("balance_after") else None
        amt = float(num(r["amount"]))
        if bal_after is not None and (bal_after + amt) > 0:
            max_drain = max(max_drain, amt / (bal_after + amt))

    metrics = {
        "window_days": window_days,
        "out_median": round(statistics.median(amounts)),
        "out_p90": round(_percentile(amounts, 0.90)),
        "out_p99": round(_percentile(amounts, 0.99)),
        "out_max": round(max(amounts)),
        "monthly_out_avg": round(total_out / months),
        "monthly_in_avg": round(
            sum(float(num(r["amount"])) for r in in_rows) / months
        ),
        "known_beneficiaries": len(benef_ids),
        "new_benef_per_30d": float(len(new_benefs)),
        "share_to_new_benef": round(to_new / total_out, 4) if total_out else 0.0,
        "active_hours": active_hours,
        "night_tx_ratio": round(night / len(out_rows), 4),
        "weekend_tx_ratio": round(weekend / len(out_rows), 4),
        "tx_per_week": round(len(out_rows) / weeks, 2),
        "max_tx_per_day": max(per_day.values()) if per_day else 0,
        "max_cum_to_one_benef_14d": round(max_cum_14d),
        "balance_median": round(statistics.median(balances)) if balances else 0,
        "max_drain_ratio_90d": round(min(max_drain, 1.0), 4),
    }
    return {
        "customer_id": customer_id,
        "sample_size": len(out_rows),
        "metrics": metrics,
    }


@app.post("/transactions", tags=["transaction"], status_code=201)
def create_transaction(req: TransactionCreate):
    """Ghi nhận giao dịch. Guardian tạo bản ghi PENDING khi quyết định khóa tạm."""
    tx_id = req.transaction_id
    if tx_id is None:
        row = query_one("SELECT COALESCE(max(transaction_id), 0) + 1 AS nid FROM transaction_history")
        tx_id = int(row["nid"])
    now = now_vn()
    row = execute_returning(
        """
        INSERT INTO transaction_history (
          transaction_id, customer_id, account_id, direction, beneficiary_id,
          beneficiary_bank_code, beneficiary_account_masked, currency, amount,
          balance_after, transaction_type, category, transaction_description,
          channel, transaction_date, transaction_time, status, risk_decision_id
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            tx_id, req.customer_id, req.account_id, req.direction, req.beneficiary_id,
            req.beneficiary_bank_code, req.beneficiary_account_masked, req.currency,
            str(int(req.amount)),
            str(int(req.balance_after)) if req.balance_after is not None else None,
            req.transaction_type, req.category,
            mask_free_text(req.transaction_description), req.channel,
            req.transaction_date or now.strftime("%Y%m%d"),
            req.transaction_time or now.strftime("%H%M%S"),
            req.status, req.risk_decision_id,
        ),
    )
    return _tx_payload(row)


@app.patch("/transactions/{transaction_id}/status", tags=["transaction"])
def update_transaction_status(transaction_id: int, req: TransactionStatusUpdate):
    """Guardian dùng để chuyển giao dịch sang PENDING (khóa tạm 24h) hoặc CANCELLED."""
    row = execute_returning(
        """
        UPDATE transaction_history
        SET status = %s,
            risk_decision_id = COALESCE(%s::uuid, risk_decision_id)
        WHERE transaction_id = %s
        RETURNING *
        """,
        (req.status, req.risk_decision_id, transaction_id),
    )
    if row is None:
        raise HTTPException(404, f"transaction {transaction_id} không tồn tại")
    return _tx_payload(row)


# ---------------------------------------------------------------------------
# Spending insight (Journey A)
# ---------------------------------------------------------------------------
@app.get("/customers/{customer_id}/insights", tags=["insight"])
def get_insights(
    customer_id: int, period: str | None = Query(None, description="YYYYMM")
):
    sql = "SELECT * FROM spending_insight WHERE customer_id = %s"
    params: list = [customer_id]
    if period:
        sql += " AND period = %s"
        params.append(period)
    sql += " ORDER BY period DESC, rank_in_period NULLS LAST"
    rows = query(sql, params)
    return {"customer_id": customer_id, "count": len(rows), "insights": rows}


@app.post("/customers/{customer_id}/insights/generate", tags=["insight"])
def generate_insights(
    customer_id: int, period: str | None = Query(None, description="YYYYMM, mặc định tháng trước")
):
    """Tính và cache insight cho một kỳ. Số liệu do service tính; LLM chỉ viết
    `insight_text` sau đó, không được tự nghĩ ra con số."""
    if period is None:
        first = now_vn().replace(day=1)
        period = (first - timedelta(days=1)).strftime("%Y%m")
    prev_year, prev_mon = int(period[:4]), int(period[4:])
    prev_mon -= 1
    if prev_mon == 0:
        prev_mon, prev_year = 12, prev_year - 1
    prev_period = f"{prev_year:04d}{prev_mon:02d}"

    def totals(p: str) -> dict[str, float]:
        rows = query(
            """
            SELECT category, sum(amount::numeric) AS total
            FROM transaction_history
            WHERE customer_id = %s AND direction = 'OUT' AND status = 'POSTED'
              AND substring(transaction_date, 1, 6) = %s
            GROUP BY category
            """,
            (customer_id, p),
        )
        return {r["category"] or "OTHER": float(r["total"]) for r in rows}

    current, previous = totals(period), totals(prev_period)
    if not current:
        raise HTTPException(404, f"không có chi tiêu kỳ {period} của customer {customer_id}")

    ranked = sorted(current.items(), key=lambda kv: kv[1], reverse=True)
    rows_out = [("TOTAL", sum(current.values()), None)] + [
        (cat, amt, idx + 1) for idx, (cat, amt) in enumerate(ranked)
    ]
    prev_total = sum(previous.values())

    saved = []
    for category, amount, rank in rows_out:
        base = prev_total if category == "TOTAL" else previous.get(category, 0.0)
        delta = round((amount - base) / base * 100, 2) if base else None
        row = execute_returning(
            """
            INSERT INTO spending_insight
              (customer_id, period, category, amount, delta_vs_prev, rank_in_period)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (customer_id, period, category) DO UPDATE SET
              amount = EXCLUDED.amount,
              delta_vs_prev = EXCLUDED.delta_vs_prev,
              rank_in_period = EXCLUDED.rank_in_period,
              generated_at = now()
            RETURNING *
            """,
            (customer_id, period, category, round(amount), delta, rank),
        )
        saved.append(row)
    return {
        "customer_id": customer_id,
        "period": period,
        "compared_to": prev_period,
        "count": len(saved),
        "insights": saved,
    }


# ---------------------------------------------------------------------------
# Product & recommendation (Journey A)
# ---------------------------------------------------------------------------
@app.get("/products", tags=["product"])
def list_products(
    product_group: str | None = Query(None, description="SAVINGS | INVESTMENT | CARD | INSURANCE | LOAN"),
    status: str = Query("ACTIVE"),
):
    sql = "SELECT * FROM product WHERE product_status = %s"
    params: list = [status.upper()]
    if product_group:
        sql += " AND product_group = %s"
        params.append(product_group.upper())
    sql += " ORDER BY product_group, product_id"
    rows = query(sql, params)
    return {"count": len(rows), "products": rows}


@app.get("/customers/{customer_id}/recommendations", tags=["product"])
def get_recommendations(customer_id: int, status: str | None = None):
    sql = """
        SELECT r.*, p.product_name, p.product_group, p.product_interest
        FROM product_recommendation r
        JOIN product p ON p.product_id = r.product_id
        WHERE r.customer_id = %s
    """
    params: list = [customer_id]
    if status:
        sql += " AND r.status = %s"
        params.append(status.upper())
    sql += " ORDER BY r.shown_at DESC"
    rows = query(sql, params)
    return {"customer_id": customer_id, "count": len(rows), "recommendations": rows}


@app.post("/customers/{customer_id}/recommendations/generate", tags=["product"])
def generate_recommendations(customer_id: int, limit: int = Query(3, ge=1, le=5)):
    """Sinh gợi ý dựa trên dòng tiền dư thực tế.

    Chỉ gợi ý SAVINGS và INVESTMENT — schema quy định không gợi ý LOAN trong
    phạm vi demo. `estimated_benefit` do service tính từ lãi suất, LLM chỉ được
    diễn giải lại, không được tự đưa ra con số.
    """
    forecast = cashflow_forecast(customer_id)
    surplus = max(0, forecast["avg_monthly_net"])
    if surplus <= 0:
        return {
            "customer_id": customer_id,
            "count": 0,
            "recommendations": [],
            "note": "Dòng tiền chưa dư, không gợi ý sản phẩm tích lũy.",
        }

    latest = query_one(
        """
        SELECT insight_id FROM spending_insight
        WHERE customer_id = %s AND category = 'TOTAL'
        ORDER BY period DESC LIMIT 1
        """,
        (customer_id,),
    )
    insight_id = latest["insight_id"] if latest else None

    products = query(
        """
        SELECT * FROM product
        WHERE product_status = 'ACTIVE' AND product_group IN ('SAVINGS','INVESTMENT')
        ORDER BY product_group, product_id LIMIT %s
        """,
        (limit,),
    )
    if not products:
        raise HTTPException(404, "không có sản phẩm SAVINGS/INVESTMENT nào đang ACTIVE")

    out = []
    for p in products:
        rate = float(num(p.get("product_interest"), 0)) if p.get("product_interest") else 0.0
        if rate == 0.0:
            # product_interest là chuỗi hiển thị (vd "5.2%/năm") → tách phần số
            digits = "".join(ch for ch in str(p.get("product_interest") or "") if ch.isdigit() or ch == ".")
            rate = float(digits) if digits.replace(".", "", 1).isdigit() else 4.5
        annual = round(surplus * 12 * rate / 100)
        row = execute_returning(
            """
            INSERT INTO product_recommendation
              (customer_id, product_id, insight_id, reason, estimated_benefit, status)
            VALUES (%s,%s,%s,%s,%s,'SHOWN')
            RETURNING *
            """,
            (
                customer_id, p["product_id"], insight_id,
                f"Dòng tiền dư trung bình {surplus:,.0f} VND/tháng, "
                f"đủ để tích lũy vào {p['product_name']}.",
                annual,
            ),
        )
        row["product_name"] = p["product_name"]
        row["product_group"] = p["product_group"]
        out.append(row)
    return {
        "customer_id": customer_id,
        "monthly_surplus": surplus,
        "count": len(out),
        "recommendations": out,
    }


@app.patch("/recommendations/{recommendation_id}", tags=["product"])
def update_recommendation(recommendation_id: int, req: RecommendationStatusUpdate):
    row = execute_returning(
        """
        UPDATE product_recommendation
        SET status = %s, responded_at = now()
        WHERE recommendation_id = %s RETURNING *
        """,
        (req.status, recommendation_id),
    )
    if row is None:
        raise HTTPException(404, f"recommendation {recommendation_id} không tồn tại")
    return row


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------
AGENT_TOOLS = [
    tool(
        "get_transactions",
        "Lịch sử giao dịch của khách hàng, lọc theo khoảng ngày, chiều tiền, "
        "nhóm chi tiêu và trạng thái.",
        "GET", "/transactions/{customer_id}",
        params={
            "customer_id": "int", "date_from": "YYYYMMDD", "date_to": "YYYYMMDD",
            "direction": "OUT|IN", "category": "FOOD|BILLS|...", "status": "POSTED|PENDING|...",
            "limit": "int", "offset": "int",
        },
        returns="Danh sách giao dịch (không kèm cờ gian lận nội bộ).",
    ),
    tool(
        "get_monthly_summary",
        "Thu / chi / net theo từng tháng kèm top 5 nhóm chi tiêu. Dùng để trả lời "
        "'tháng này tôi tiêu vào đâu nhiều nhất'.",
        "GET", "/transactions/{customer_id}/monthly-summary",
        params={"customer_id": "int", "months": "int, mặc định 6"},
        returns="Mảng tổng hợp theo tháng.",
    ),
    tool(
        "get_quarterly_summary",
        "Thu/chi/net theo QUÝ kèm phân rã theo nhóm chi tiêu và mức thay đổi so với quý trước. "
        "Dùng khi khách hỏi về xu hướng chi tiêu dài hơn một tháng.",
        "GET", "/transactions/{customer_id}/quarterly-summary",
        params={"customer_id": "int", "quarters": "int, mặc định 8",
                "include_transfers": "bool, mặc định false"},
        returns="summary[] theo quý với by_category[] kèm delta_vs_prev_pct, và category_totals[].",
    ),
    tool(
        "get_monthly_comparison",
        "So sánh chi tiêu GIỮA CÁC THÁNG kèm mức thay đổi so với tháng liền trước "
        "(cả tổng chi lẫn từng nhóm). Dùng khi khách hỏi 'tháng này so với tháng "
        "trước/tháng 8 thế nào' hoặc muốn bảng so sánh nhiều tháng.",
        "GET", "/transactions/{customer_id}/monthly-comparison",
        params={"customer_id": "int", "months": "int, mặc định 6",
                "include_transfers": "bool, mặc định false"},
        returns="summary[] theo tháng, mỗi tháng có delta_vs_prev_pct (tổng) và "
                "by_category[] kèm delta_vs_prev_pct; và category_totals[].",
    ),
    tool(
        "get_cashflow_forecast",
        "Dự báo dòng tiền các tháng tới và mức quỹ dự phòng khuyến nghị.",
        "GET", "/transactions/{customer_id}/cashflow-forecast",
        params={"customer_id": "int", "horizon": "int, số tháng dự báo"},
        returns="avg_monthly_net, recommended_emergency_fund, forecast.",
    ),
    tool(
        "get_baseline_metrics",
        "Tính 18 chỉ số hành vi từ lịch sử giao dịch (chỉ OUT/POSTED/không gian lận). "
        "Dùng để dựng hoặc làm mới digital twin.",
        "GET", "/transactions/{customer_id}/baseline-metrics",
        params={"customer_id": "int", "window_days": "int, mặc định 90"},
        returns="metrics khớp đúng các cột behavior_profile.",
    ),
    tool(
        "create_transaction",
        "Ghi nhận một giao dịch mới. Đặt status = PENDING khi Guardian khóa tạm.",
        "POST", "/transactions",
        body={
            "customer_id": "int", "account_id": "int", "direction": "OUT|IN",
            "amount": "number", "beneficiary_id": "int (tùy chọn)",
            "category": "str", "channel": "MOBILE|INTERNET|BRANCH|ATM",
            "status": "POSTED|PENDING|CANCELLED|REVERSED",
        },
        returns="Giao dịch vừa tạo.",
    ),
    tool(
        "update_transaction_status",
        "Đổi trạng thái giao dịch: PENDING để khóa tạm 24h, CANCELLED để hủy theo "
        "yêu cầu khách, REVERSED khi hoàn tiền.",
        "PATCH", "/transactions/{transaction_id}/status",
        params={"transaction_id": "int"},
        body={"status": "POSTED|PENDING|CANCELLED|REVERSED", "risk_decision_id": "uuid (tùy chọn)"},
        returns="Giao dịch sau cập nhật.",
    ),
    tool(
        "get_insights",
        "Đọc insight chi tiêu đã tính sẵn của một kỳ (YYYYMM).",
        "GET", "/customers/{customer_id}/insights",
        params={"customer_id": "int", "period": "YYYYMM (tùy chọn)"},
        returns="Danh sách spending_insight.",
    ),
    tool(
        "generate_insights",
        "Tính lại insight chi tiêu cho một kỳ, kèm % thay đổi so kỳ trước và thứ hạng nhóm.",
        "POST", "/customers/{customer_id}/insights/generate",
        params={"customer_id": "int", "period": "YYYYMM, mặc định tháng trước"},
        returns="Insight vừa tính.",
    ),
    tool(
        "list_products",
        "Danh mục sản phẩm MSB đang mở bán.",
        "GET", "/products",
        params={"product_group": "SAVINGS|INVESTMENT|CARD|INSURANCE|LOAN", "status": "ACTIVE"},
        returns="Danh sách sản phẩm.",
    ),
    tool(
        "generate_recommendations",
        "Gợi ý sản phẩm tích lũy dựa trên dòng tiền dư thực tế. Chỉ gợi ý SAVINGS và "
        "INVESTMENT; estimated_benefit do service tính, LLM không được tự đặt con số.",
        "POST", "/customers/{customer_id}/recommendations/generate",
        params={"customer_id": "int", "limit": "int, tối đa 5"},
        returns="Danh sách gợi ý kèm lợi ích ước tính theo năm.",
    ),
    tool(
        "get_recommendations",
        "Đọc các gợi ý đã hiển thị cho khách hàng.",
        "GET", "/customers/{customer_id}/recommendations",
        params={"customer_id": "int", "status": "SHOWN|ACCEPTED|DISMISSED"},
        returns="Danh sách gợi ý.",
    ),
    tool(
        "update_recommendation",
        "Ghi nhận khách chấp nhận hay bỏ qua một gợi ý (số liệu cross-sell).",
        "PATCH", "/recommendations/{recommendation_id}",
        params={"recommendation_id": "int"},
        body={"status": "SHOWN|ACCEPTED|DISMISSED"},
        returns="Gợi ý sau cập nhật.",
    ),
]
