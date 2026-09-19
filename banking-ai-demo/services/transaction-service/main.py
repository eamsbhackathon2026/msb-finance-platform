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
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    db_health,
    execute_returning,
    install_db_error_handlers,
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
    "interest_rate",
    "interest_rate_term",
]

CATEGORIES = [
    "FOOD", "TRANSPORT", "SHOPPING", "BILLS", "RENT", "HEALTH",
    "FAMILY_SUPPORT", "INVESTMENT", "TRANSFER_P2P", "OTHER",
]

# Mã nhóm → nhãn tiếng Việt. Trả kèm trong payload thay vì để bên gọi tự dịch:
# trợ lý dịch lại mỗi lượt một kiểu ("FAMILY_SUPPORT" ra "chuyển tiền cho người
# khác" ở câu này, "cho người thân" ở câu sau) nên cùng một nhóm hiện hai tên
# trong cùng một cuộc trò chuyện.
CATEGORY_LABELS_VI = {
    "FOOD": "Ăn uống",
    "TRANSPORT": "Di chuyển",
    "SHOPPING": "Mua sắm",
    "BILLS": "Hoá đơn tiện ích",
    "RENT": "Thuê nhà",
    "HEALTH": "Sức khoẻ",
    "FAMILY_SUPPORT": "Hỗ trợ gia đình",
    "INVESTMENT": "Đầu tư",
    "TRANSFER_P2P": "Chuyển khoản đi",
    "TRANSFER": "Chuyển khoản đi",
    "OTHER": "Khác",
}


def _nhan_nhom(category: str) -> str:
    return CATEGORY_LABELS_VI.get(category, category)


def _tien_vi(amount: float) -> str:
    """1234000 → "1.234.000 ₫"."""
    return f"{round(amount):,.0f}".replace(",", ".") + " ₫"

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
# Lỗi Postgres do id sai của người gọi phải ra 404/409/422 chứ không phải
# 500, vì trợ lý đọc thẳng thân phản hồi này để quyết bước tiếp theo.
install_db_error_handlers(app)


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


# Khóa ngoại là thứ chặn cuối cùng; để nó vỡ thì thông điệp trả về là của
# Postgres chứ không phải của nghiệp vụ. Tra trước và nêu đúng thực thể thì trợ
# lý đọc là biết phải đi tra lại id nào. Cột cho phép NULL chỉ kiểm tra khi có
# giá trị — không được biến khóa ngoại tùy chọn thành bắt buộc.
def _require_customer(customer_id: int) -> None:
    if query_one("SELECT 1 AS x FROM customer WHERE customer_id = %s", (customer_id,)) is None:
        raise HTTPException(404, f"customer {customer_id} không tồn tại")


def _require_account(account_id: int) -> None:
    if query_one("SELECT 1 AS x FROM account WHERE account_id = %s", (account_id,)) is None:
        raise HTTPException(404, f"account {account_id} không tồn tại")


def _require_beneficiary(beneficiary_id: int | None) -> None:
    if beneficiary_id is None:
        return
    row = query_one(
        "SELECT 1 AS x FROM beneficiary WHERE beneficiary_id = %s", (beneficiary_id,)
    )
    if row is None:
        raise HTTPException(404, f"beneficiary {beneficiary_id} không tồn tại")


def _require_decision(decision_id: str | None) -> None:
    if decision_id is None:
        return
    row = query_one(
        "SELECT 1 AS x FROM risk_decision WHERE decision_id = %s::uuid", (decision_id,)
    )
    if row is None:
        raise HTTPException(404, f"decision {decision_id} không tồn tại")


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


# Chuyển khoản đi KHÔNG phải chi tiêu: nó là tiền chuyển chỗ, không phải tiền
# tiêu mất. Gộp chung thì một lệnh chuyển lớn nuốt trọn biểu đồ và các nhóm chi
# tiêu thật bị ép về gần 0%.
TRANSFER_CATEGORIES = ("TRANSFER_P2P", "TRANSFER")


@app.get("/transactions/{customer_id}/monthly-summary", tags=["transaction"])
def monthly_summary(
    customer_id: int,
    months: int = Query(6, ge=1, le=24),
    include_transfers: bool = Query(
        False, description="Tính cả chuyển khoản đi vào phần chi tiêu"
    ),
):
    """Thu/chi/net theo tháng kèm phân rã theo nhóm chi tiêu — Scene 1 của Copilot.

    `expense` là chi tiêu THẬT, không gồm chuyển khoản đi — cùng định nghĩa với
    quarterly-summary và monthly-comparison. Trước đây endpoint này gộp chung
    nên một lệnh chuyển 620 triệu biến thành "tháng này bạn chi tiêu 622 triệu".
    Tiền chuyển đi vẫn rời tài khoản thật nên `net` trừ cả hai, và `transfer_out`
    giữ riêng cho bên nào cần tổng dòng tiền ra.

    Khách không có giao dịch nào là chuyện bình thường, không phải lỗi: trả danh
    sách rỗng. Chỉ mã khách không tồn tại mới là 404 để bên gọi biết đi tra lại id.
    """
    _require_customer(customer_id)
    # Lấy dư một tháng rồi cắt: tháng cũ nhất trong cửa sổ luôn bị cắt giữa
    # chừng, gắn nhãn tháng đầy đủ cho nó thì cùng một tháng ra hai con số khác
    # nhau tuỳ `months` bên gọi truyền vào.
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
        lambda: {"income": 0.0, "expense": 0.0, "transfer_out": 0.0, "count": 0,
                 "by_category": defaultdict(float)}
    )
    for r in rows:
        period = r["transaction_date"][:6]  # YYYYMM
        amount = float(num(r.get("amount")))
        b = buckets[period]
        b["count"] += 1
        if r.get("direction") == "IN":
            b["income"] += amount
            continue
        category = r.get("category") or "OTHER"
        if category in excluded:
            b["transfer_out"] += amount
            continue
        b["expense"] += amount
        b["by_category"][category] += amount

    ky_hien_tai = now_vn().strftime("%Y%m")
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
                "transfer_out": round(b["transfer_out"]),
                "net": round(b["income"] - b["expense"] - b["transfer_out"]),
                "count": b["count"],
                # Tháng đang chạy chưa đủ ngày để so với tháng đã đóng sổ.
                "partial": period == ky_hien_tai,
                "top_categories": [
                    {"category": c, "label": _nhan_nhom(c), "amount": round(v), "rank": i + 1}
                    for i, (c, v) in enumerate(by_cat[:5])
                ],
            }
        )
    # Bỏ tháng dư đã lấy thêm để cắt cửa sổ cho tròn tháng lịch.
    summary = summary[-months:]
    return {
        "customer_id": customer_id,
        "months": len(summary),
        "include_transfers": include_transfers,
        "excluded_categories": list(excluded),
        "summary": summary,
    }


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
    _require_customer(customer_id)
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
                    "label": _nhan_nhom(c),
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
            {"category": c, "label": _nhan_nhom(c), "amount": round(v),
             "pct": round(v * 100 / grand)}
            for c, v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


# ---- Lộ trình tiết kiệm cho mục tiêu lớn ------------------------------------
#
# Ba công cụ dưới đây phục vụ đúng một câu hỏi: "giúp tôi tiết kiệm N tiền trong
# M năm tới". Trợ lý gọi lần lượt review quý → khả năng tiết kiệm → gói sản phẩm.
# Tách làm ba chứ không gộp một vì mỗi bước trả lời được một câu hỏi riêng, và
# vì trợ lý cần ĐỌC ĐƯỢC số ở bước trước rồi mới quyết định tham số bước sau.
#
# Ba rổ chi tiêu. Ranh giới này quyết định câu "bạn có thể cắt bớt ở đâu", nên
# đặt ở service thay vì để mô hình tự phân loại mỗi lượt một kiểu:
#   · THIẾT YẾU  — cắt là ảnh hưởng sinh hoạt, coi như sàn.
#   · CAM KẾT    — không phải muốn cắt là cắt được (tiền gửi về gia đình, đầu tư
#                  định kỳ), nhưng có thể thương lượng lại nên vẫn tách riêng.
#   · CO GIÃN    — phần thực sự nằm trong tay khách.
CHI_THIET_YEU = ("BILLS", "RENT", "HEALTH", "FOOD", "TRANSPORT")
CHI_CAM_KET = ("FAMILY_SUPPORT", "INVESTMENT")
CHI_CO_GIAN = ("SHOPPING", "OTHER")
NHAN_RO = {"essential": "Thiết yếu", "committed": "Cam kết", "flexible": "Có thể co giãn"}


def _ro_chi(category: str) -> str:
    if category in CHI_THIET_YEU:
        return "essential"
    if category in CHI_CAM_KET:
        return "committed"
    return "flexible"


def _ky_hien_tai() -> str:
    return now_vn().strftime("%Y%m")


def _trung_vi(values: list[float]) -> float:
    """Trung vị, rỗng thì 0. Dùng thay trung bình ở mọi chỗ nói "một tháng điển hình"."""
    return statistics.median(values) if values else 0.0


def _thang_trong_ky(customer_id: int, months: int) -> list[dict]:
    """Thu / chi / net từng tháng trong `months` tháng gần nhất.

    Chuyển khoản đi KHÔNG tính là chi tiêu: chuyển sang tài khoản tiết kiệm của
    chính mình mà bị tính thành chi thì càng để dành nhiều càng bị chấm là tiêu
    hoang, đúng ngược với việc công cụ này đang đo.
    """
    start = (now_vn() - timedelta(days=31 * months)).strftime("%Y%m%d")
    rows = query(
        """
        SELECT transaction_date, direction, amount, category
        FROM transaction_history
        WHERE customer_id = %s AND transaction_date >= %s AND status = 'POSTED'
        ORDER BY transaction_date
        """,
        (customer_id, start),
    )
    theo_thang: dict[str, dict] = defaultdict(
        lambda: {"income": 0.0, "expense": 0.0, "by_category": defaultdict(float), "count": 0}
    )
    for r in rows:
        date = r.get("transaction_date") or ""
        if len(date) < 6:
            continue
        b = theo_thang[date[:6]]
        amount = float(num(r.get("amount")))
        b["count"] += 1
        if r.get("direction") == "IN":
            b["income"] += amount
            continue
        category = r.get("category") or "OTHER"
        if category in TRANSFER_CATEGORIES:
            continue
        b["expense"] += amount
        b["by_category"][category] += amount
    return [
        {"period": k, "income": v["income"], "expense": v["expense"],
         "net": v["income"] - v["expense"], "count": v["count"],
         "by_category": dict(v["by_category"])}
        for k, v in sorted(theo_thang.items())
    ]


@app.get("/transactions/{customer_id}/quarter-review", tags=["savings-goal"])
def quarter_review(
    customer_id: int,
    quarter: str | None = Query(
        None, description='Quý cần xem, dạng "2026Q2". Bỏ trống = quý GẦN NHẤT ĐÃ KẾT THÚC.'
    ),
):
    """BƯỚC 1 của lộ trình tiết kiệm: mổ xẻ chi tiêu một quý.

    Mặc định lấy quý gần nhất ĐÃ KẾT THÚC, không lấy quý đang chạy. Quý đang
    chạy mới đi được một phần đường nên tổng chi của nó luôn thấp giả tạo; lấy
    nó làm gốc cho kế hoạch ba năm sẽ hứa với khách một mức để dành không có
    thật.

    Mỗi nhóm chi được xếp vào một trong ba rổ (thiết yếu / cam kết / co giãn) để
    bước sau biết phần nào thực sự cắt được. Các khoản một lần bất thường được
    tách ra thay vì trộn vào trung bình — một lần viện phí 6 triệu không phải là
    nhịp chi hằng tháng.
    """
    _require_customer(customer_id)
    months = _thang_trong_ky(customer_id, 24)
    if not months:
        return {"customer_id": customer_id, "found": False,
                "note": "Chưa có giao dịch nào trong 24 tháng gần đây."}

    theo_quy: dict[str, list[dict]] = defaultdict(list)
    for m in months:
        year, q = _quarter_of(m["period"] + "01")
        theo_quy[f"{year}Q{q}"].append(m)

    ky_nay = _ky_hien_tai()
    nam_nay, quy_nay = _quarter_of(ky_nay + "01")
    quy_dang_chay = f"{nam_nay}Q{quy_nay}"
    da_xong = [k for k in sorted(theo_quy) if k != quy_dang_chay]
    chon = quarter or (da_xong[-1] if da_xong else sorted(theo_quy)[-1])
    if chon not in theo_quy:
        raise HTTPException(404, f"không có dữ liệu cho quý {chon}")

    trong_quy = theo_quy[chon]
    thu = sum(m["income"] for m in trong_quy)
    chi = sum(m["expense"] for m in trong_quy)
    so_thang = len(trong_quy) or 1

    gop: dict[str, float] = defaultdict(float)
    for m in trong_quy:
        for c, v in m["by_category"].items():
            gop[c] += v
    tong = chi or 1

    ro = {k: {"amount": 0.0, "categories": []} for k in NHAN_RO}
    by_category = []
    for c, v in sorted(gop.items(), key=lambda kv: kv[1], reverse=True):
        r = _ro_chi(c)
        ro[r]["amount"] += v
        ro[r]["categories"].append(c)
        by_category.append({
            "category": c, "label": _nhan_nhom(c), "bucket": r,
            "bucket_label": NHAN_RO[r], "amount": round(v),
            "pct": round(v * 100 / tong), "monthly_avg": round(v / so_thang),
        })

    # Khoản một lần: giao dịch đơn lẻ lớn hơn 40% tổng chi của chính nhóm đó
    # trong quý. Ngưỡng theo nhóm chứ không theo tổng, vì 3 triệu là bất thường
    # với nhóm Ăn uống nhưng bình thường với nhóm Sức khoẻ.
    dau, cuoi = trong_quy[0]["period"] + "01", trong_quy[-1]["period"] + "31"
    lon = query(
        """
        SELECT transaction_date, amount, category, transaction_description
        FROM transaction_history
        WHERE customer_id = %s AND transaction_date BETWEEN %s AND %s
          AND status = 'POSTED' AND direction = 'OUT'
        ORDER BY amount DESC LIMIT 20
        """,
        (customer_id, dau, cuoi),
    )
    mot_lan = [
        {"date": r["transaction_date"], "category": r.get("category"),
         "label": _nhan_nhom(r.get("category") or "OTHER"),
         "amount": round(float(num(r.get("amount")))),
         "description": mask_free_text(r.get("transaction_description")) or ""}
        for r in lon
        if r.get("category") not in TRANSFER_CATEGORIES
        and float(num(r.get("amount"))) > 0.4 * gop.get(r.get("category") or "OTHER", 0)
    ][:5]

    truoc = da_xong[da_xong.index(chon) - 1] if chon in da_xong and da_xong.index(chon) > 0 else None
    # Quý liền trước chỉ dùng làm mốc khi nó ĐỦ ba tháng và có thu nhập. Quý nằm
    # ở mép cửa sổ dữ liệu chỉ ghi được vài giao dịch: so với nó ra "+321% so
    # với quý trước", một con số đúng về số học nhưng vô nghĩa, và trợ lý sẽ
    # đọc nguyên nó cho khách.
    quy_truoc = theo_quy.get(truoc) or []
    du_tin = len(quy_truoc) == 3 and sum(m["income"] for m in quy_truoc) > 0
    chi_truoc = sum(m["expense"] for m in quy_truoc) if du_tin else 0

    return {
        "customer_id": customer_id,
        "period": chon,
        "label": f"Quý {chon[-1]}/{chon[:4]}",
        "is_completed_quarter": chon != quy_dang_chay,
        "months": [m["period"] for m in trong_quy],
        "income": round(thu),
        "expense": round(chi),
        "net": round(thu - chi),
        "monthly_avg": {"income": round(thu / so_thang), "expense": round(chi / so_thang),
                        "net": round((thu - chi) / so_thang)},
        "by_category": by_category,
        "buckets": [
            {"bucket": k, "label": NHAN_RO[k], "amount": round(v["amount"]),
             "pct": round(v["amount"] * 100 / tong),
             "monthly_avg": round(v["amount"] / so_thang),
             "categories": v["categories"]}
            for k, v in ro.items()
        ],
        "one_off_transactions": mot_lan,
        "prev_period": truoc if du_tin else None,
        "delta_expense_vs_prev_pct": round((chi - chi_truoc) * 100 / chi_truoc) if chi_truoc else None,
        "prev_period_skipped": (
            f"Bỏ qua so sánh với {truoc}: quý đó nằm ở mép dữ liệu, không đủ ba tháng "
            "hoặc không có thu nhập nên mức tăng giảm tính ra sẽ sai lệch."
            if truoc and not du_tin else None
        ),
        "note": ("Quý gần nhất đã kết thúc — dùng làm gốc cho kế hoạch tiết kiệm."
                 if chon != quy_dang_chay else
                 "CẢNH BÁO: đây là quý đang chạy, tổng chi chưa đủ nên đừng dùng làm gốc kế hoạch."),
    }


@app.get("/transactions/{customer_id}/savings-capacity", tags=["savings-goal"])
def savings_capacity(
    customer_id: int,
    months: int = Query(12, ge=3, le=36, description="Cửa sổ dữ liệu để tính, tính bằng tháng"),
):
    """BƯỚC 2 của lộ trình tiết kiệm: một tháng khách để dành được bao nhiêu.

    Lấy TRUNG VỊ chứ không lấy trung bình. Dữ liệu thật của khách demo có một
    tháng nhận hơn 500 triệu; trung bình sẽ ra "để dành được 88 triệu/tháng",
    sai hoàn toàn so với nhịp sống thường và sẽ đẻ ra một kế hoạch ba năm dựa
    trên con số không bao giờ lặp lại.

    Bỏ hai loại tháng không đại diện cho một chu kỳ sống, và trả lại danh sách
    đã bỏ kèm lý do để bên gọi giải thích được với khách:
      · Tháng đang chạy — chưa đủ ngày nên chưa đủ chi.
      · Tháng không có đồng thu nhập nào — đó là tháng nằm ở mép cửa sổ dữ liệu
        (bắt đầu từ giữa tháng, chưa kịp có kỳ lương). Tính vào sẽ thành một
        tháng "âm mấy triệu" bịa ra, kéo mức để dành xuống gần một nửa.

    Trả về hai mức: `realistic` là nhịp hiện tại, `stretch` là khi cắt phần chi
    co giãn. Hai mức chứ không một, vì câu hỏi tiếp theo của khách luôn là
    "thế nếu tôi tiêu tiết kiệm hơn thì sao".
    """
    _require_customer(customer_id)
    tat_ca = _thang_trong_ky(customer_id, months)
    ky_nay = _ky_hien_tai()

    bo = []
    dung = []
    for m in tat_ca:
        if m["period"] == ky_nay:
            bo.append({"period": m["period"], "reason": "tháng đang chạy, chưa đủ ngày"})
        elif m["income"] <= 0:
            bo.append({"period": m["period"], "reason": "không ghi nhận thu nhập, nằm ở mép dữ liệu"})
        else:
            dung.append(m)

    if not dung:
        return {"customer_id": customer_id, "found": False, "months_used": 0,
                "months_excluded": bo,
                "note": "Không đủ tháng đầy đủ để tính khả năng tiết kiệm."}

    thu = _trung_vi([m["income"] for m in dung])
    chi = _trung_vi([m["expense"] for m in dung])
    du = _trung_vi([m["net"] for m in dung])

    # Phần co giãn lấy trung vị theo tháng, không lấy tổng chia đều: một tháng
    # mua sắm lớn không được phép nâng mức "cắt được" của mọi tháng còn lại.
    co_gian = _trung_vi([
        sum(v for c, v in m["by_category"].items() if _ro_chi(c) == "flexible") for m in dung
    ])
    # Cắt tối đa 70% phần co giãn. Giả định cắt sạch là giả định không ai sống
    # được, và một kế hoạch dựng trên đó sẽ vỡ ngay tháng thứ hai.
    them = co_gian * 0.7
    ty_le = round(du * 100 / thu) if thu else 0

    return {
        "customer_id": customer_id,
        "found": True,
        "basis": "median",
        "window_months": months,
        "months_used": len(dung),
        "periods_used": [m["period"] for m in dung],
        "months_excluded": bo,
        "monthly": {"income": round(thu), "expense": round(chi), "net": round(du)},
        "savings_rate_pct": ty_le,
        "realistic": {
            "monthly": round(du), "quarterly": round(du * 3), "annual": round(du * 12),
            "label": "Giữ nguyên nhịp chi hiện tại",
        },
        "stretch": {
            "monthly": round(du + them), "quarterly": round((du + them) * 3),
            "annual": round((du + them) * 12),
            "flexible_monthly": round(co_gian),
            "cut_assumption_pct": 70,
            "label": "Cắt 70% phần chi co giãn (mua sắm, khoản khác)",
        },
        "note": (f"Tính trên {len(dung)} tháng đầy đủ, dùng trung vị. "
                 f"Đã bỏ {len(bo)} tháng không đại diện." if bo else
                 f"Tính trên {len(dung)} tháng đầy đủ, dùng trung vị."),
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
    _require_customer(customer_id)
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
                    "label": _nhan_nhom(c),
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
            {"category": c, "label": _nhan_nhom(c), "amount": round(v),
             "pct": round(v * 100 / grand)}
            for c, v in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


@app.get("/transactions/{customer_id}/cashflow-forecast", tags=["transaction"])
def cashflow_forecast(customer_id: int, horizon: Annotated[int, Query(ge=1, le=12)] = 3):
    """Dự báo net theo trung bình có trọng số: 3 tháng gần nhất nặng hơn các tháng cũ.

    `horizon` khai bằng Annotated để GIÁ TRỊ MẶC ĐỊNH là số 3 thật. Viết
    `horizon: int = Query(3, ...)` thì mặc định là một đối tượng Query của
    FastAPI — qua HTTP không sao vì FastAPI thay nó, nhưng gọi thẳng hàm này từ
    trong Python (generate_recommendations vẫn gọi) thì `range(horizon)` nổ
    TypeError và endpoint trả 500 với mọi khách hàng.


    Chỉ lấy các tháng có dữ liệu trọn vẹn. Tháng đầu cửa sổ thường bị cắt giữa chừng
    và tháng hiện tại thì chưa hết, nên nếu tính cả hai vào trung bình thì một khách
    hàng thực tế đang dư tiền vẫn có thể bị dự báo âm — sai lệch đủ để Copilot đưa ra
    lời khuyên ngược hẳn với tình hình thật.
    """
    _require_customer(customer_id)
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
    _require_customer(req.customer_id)
    _require_account(req.account_id)
    _require_beneficiary(req.beneficiary_id)
    _require_decision(req.risk_decision_id)

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
    _require_decision(req.risk_decision_id)
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
    for r in rows:
        r["label"] = _nhan_nhom(r.get("category") or "OTHER")
    # Insight là số đã cache. Kỳ đang chạy thì bản cache chỉ phản ánh tới lúc
    # sinh, nên bên gọi phải biết mà sinh lại trước khi đọc, thay vì tưởng đây
    # là con số chốt của cả tháng.
    ky_hien_tai = now_vn().strftime("%Y%m")
    return {
        "customer_id": customer_id,
        "count": len(rows),
        "period_in_progress": any(r.get("period") == ky_hien_tai for r in rows),
        "insights": rows,
    }


def _cau_insight(category: str, amount: float, rank: int | None,
                 tong_ky: float, delta: float | None) -> str:
    """Câu mô tả cho một dòng insight, viết từ chính con số vừa tính.

    Trước đây cột này để trống và bên đọc (thẻ insight của app, trợ lý) phải tự
    nghĩ chữ — mỗi lần một kiểu, và nhóm chi tiêu bị gọi hai tên khác nhau trong
    cùng một cuộc trò chuyện.
    """
    if category == "TOTAL":
        cau = f"Tổng chi kỳ này là {_tien_vi(amount)}."
    else:
        pct = round(amount * 100 / tong_ky)
        cau = f"Nhóm {_nhan_nhom(category)} chiếm {pct}% tổng chi, xếp thứ {rank}."
    if delta is None:
        return cau
    if round(delta) == 0:
        return cau + " So với kỳ trước gần như không đổi."
    chieu = "tăng" if delta > 0 else "giảm"
    return cau + f" So với kỳ trước {chieu} {abs(delta):.0f}%."


@app.post("/customers/{customer_id}/insights/generate", tags=["insight"])
def generate_insights(
    customer_id: int,
    period: str | None = Query(None, description="YYYYMM, mặc định tháng trước"),
    include_transfers: bool = Query(
        False, description="Tính cả chuyển khoản đi vào phần chi tiêu"
    ),
):
    """Tính và cache insight cho một kỳ. Số liệu do service tính; câu chữ cũng
    do service viết từ chính con số đó — LLM không được tự nghĩ ra con số.

    Chi tiêu KHÔNG gồm chuyển khoản đi, cùng định nghĩa với monthly-summary.

    Sinh cho kỳ ĐANG CHẠY vẫn được (khách hỏi "tháng này tôi tiêu gì" là câu
    hợp lệ) nhưng kết quả trả `partial: true`: bản cache chỉ tính tới thời điểm
    sinh, ai đọc lại ngày hôm sau mà không sinh lại sẽ nhận số cũ."""
    # Không có cổng này thì khách không tồn tại sẽ nhận "không có chi tiêu kỳ X"
    # — nghe như nghiệp vụ bình thường, nên trợ lý đi tiếp thay vì tra lại id.
    _require_customer(customer_id)

    if period is None:
        first = now_vn().replace(day=1)
        period = (first - timedelta(days=1)).strftime("%Y%m")
    prev_year, prev_mon = int(period[:4]), int(period[4:])
    prev_mon -= 1
    if prev_mon == 0:
        prev_mon, prev_year = 12, prev_year - 1
    prev_period = f"{prev_year:04d}{prev_mon:02d}"

    excluded = () if include_transfers else TRANSFER_CATEGORIES

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
        return {
            r["category"] or "OTHER": float(r["total"])
            for r in rows
            if (r["category"] or "OTHER") not in excluded
        }

    current, previous = totals(period), totals(prev_period)
    if not current:
        raise HTTPException(404, f"không có chi tiêu kỳ {period} của customer {customer_id}")

    ranked = sorted(current.items(), key=lambda kv: kv[1], reverse=True)
    rows_out = [("TOTAL", sum(current.values()), None)] + [
        (cat, amt, idx + 1) for idx, (cat, amt) in enumerate(ranked)
    ]
    prev_total = sum(previous.values())

    tong_ky = sum(current.values()) or 1
    saved = []
    for category, amount, rank in rows_out:
        base = prev_total if category == "TOTAL" else previous.get(category, 0.0)
        delta = round((amount - base) / base * 100, 2) if base else None
        text = _cau_insight(category, amount, rank, tong_ky, delta)
        row = execute_returning(
            """
            INSERT INTO spending_insight
              (customer_id, period, category, amount, delta_vs_prev, rank_in_period, insight_text)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (customer_id, period, category) DO UPDATE SET
              amount = EXCLUDED.amount,
              delta_vs_prev = EXCLUDED.delta_vs_prev,
              rank_in_period = EXCLUDED.rank_in_period,
              insight_text = EXCLUDED.insight_text,
              generated_at = now()
            RETURNING *
            """,
            (customer_id, period, category, round(amount), delta, rank, text),
        )
        if row is not None:
            row["label"] = _nhan_nhom(category)
        saved.append(row)
    return {
        "customer_id": customer_id,
        "period": period,
        "compared_to": prev_period,
        "include_transfers": include_transfers,
        "partial": period == now_vn().strftime("%Y%m"),
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


def _rate_matrix(product_group: str) -> dict:
    """Biểu lãi suất hiện hành của một nhóm sản phẩm (SAVINGS hoặc LOAN).

    Đọc view v_interest_rate — nó đã lọc sẵn rate_status = ACTIVE và nối
    product × interest_rate × interest_rate_term. Lưu ý con số lãi nằm ở
    interest_rate_term.rate theo interest_code, KHÔNG phải trên interest_rate:
    một sản phẩm có một biểu lãi, mỗi kỳ hạn một dòng.

    term_months là NUMERIC vì có kỳ hạn ngắn hơn tháng ("1 tuần" = 0.25), nên
    giữ float — ép int sẽ biến 1 tuần thành 0 và đụng với "không kỳ hạn".
    """
    rows = query(
        """
        SELECT product_id, product_name, term_code, term_label, term_months,
               rate, seq, effective_from
        FROM v_interest_rate
        WHERE product_group = %s
        ORDER BY seq, term_months, product_id
        """,
        (product_group.upper(),),
    )
    products: list[dict] = []
    terms: list[dict] = []
    seen_p: set[str] = set()
    seen_t: set[str] = set()
    for r in rows:
        pid = str(r["product_id"])
        if pid not in seen_p:
            seen_p.add(pid)
            products.append({"product_id": pid, "product_name": r["product_name"]})
        code = str(r["term_code"])
        if code not in seen_t:
            seen_t.add(code)
            terms.append({
                "term_code": code,
                "term_label": r["term_label"],
                "term_months": float(num(r["term_months"])),
                "seq": int(r["seq"]),
            })
    terms.sort(key=lambda t: (t["seq"], t["term_months"]))
    rates = [
        {
            "product_id": str(r["product_id"]),
            "product_name": r["product_name"],
            "term_code": str(r["term_code"]),
            "term_label": r["term_label"],
            "term_months": float(num(r["term_months"])),
            "rate_pct": float(num(r["rate"])),
        }
        for r in rows
    ]
    as_of = max((str(r["effective_from"]) for r in rows), default=None)
    return {
        "count": len(rates), "as_of": as_of, "product_group": product_group.upper(),
        "products": products, "terms": terms, "rates": rates,
    }


@app.get("/products/rates", tags=["product"])
def product_rates(
    product_group: str = Query("SAVINGS", description="SAVINGS | LOAN — nhóm cần tra biểu lãi suất"),
):
    """Biểu lãi suất theo (sản phẩm × kỳ hạn) cho cả tiết kiệm lẫn vay."""
    return _rate_matrix(product_group)


@app.get("/products/savings/rates", tags=["product"])
def savings_rates(
    product_group: str = Query("SAVINGS", description="Nhóm sản phẩm cần tra biểu lãi suất"),
):
    """Giữ nguyên đường dẫn cũ cho màn Biểu lãi suất; nội dung do _rate_matrix dựng."""
    return _rate_matrix(product_group)


def _pick_term(terms: list[dict], months: float) -> dict | None:
    """Kỳ hạn niêm yết khớp nhất với số tháng khách muốn.

    Lấy kỳ hạn ngắn nhất mà vẫn ĐỦ DÀI cho nhu cầu — vay 30 tháng thì áp lãi kỳ
    36 tháng chứ không phải 12, vì kỳ hạn dài luôn có lãi cao hơn và lấy kỳ ngắn
    sẽ báo một mức trả góp rẻ hơn thực tế. Nhu cầu vượt mọi kỳ thì dùng kỳ dài nhất.
    """
    du_dai = [t for t in terms if t["term_months"] >= months]
    if du_dai:
        return min(du_dai, key=lambda t: t["term_months"])
    return max(terms, key=lambda t: t["term_months"]) if terms else None


def _monthly_payment(principal: float, annual_pct: float, months: int) -> float:
    """Tiền trả hàng tháng theo dư nợ giảm dần, trả góp đều (annuity)."""
    r = annual_pct / 100 / 12
    if r <= 0:
        return principal / months
    return principal * r / (1 - (1 + r) ** -months)


@app.get("/products/loan-options", tags=["product"])
def loan_options(
    amount: int = Query(..., gt=0, description="Số tiền muốn vay (VND)"),
    months: int = Query(..., ge=1, le=360, description="Số tháng muốn vay"),
    limit: int = Query(5, ge=1, le=20),
):
    """Các gói vay khả dụng cho (số tiền, kỳ hạn), kèm tiền trả hàng tháng.

    Tiền trả hàng tháng và tổng lãi do SERVICE tính bằng công thức trả góp đều —
    LLM chỉ diễn giải lại. Để LLM tự tính khoản vay là cách nhanh nhất có một
    con số sai trên màn hình mà không ai kiểm chứng được.
    """
    matrix = _rate_matrix("LOAN")
    theo_sp: dict[str, list[dict]] = {}
    for r in matrix["rates"]:
        theo_sp.setdefault(r["product_id"], []).append(r)

    out = []
    for pid, ky_han in theo_sp.items():
        t = _pick_term(ky_han, months)
        if not t:
            continue
        rate = t["rate_pct"]
        tra_thang = _monthly_payment(amount, rate, months)
        tong_tra = tra_thang * months
        out.append({
            "product_id": pid,
            "product_name": t["product_name"],
            "term_code": t["term_code"],
            "term_label": t["term_label"],
            "rate_pct": rate,
            "monthly_payment": round(tra_thang),
            "total_payment": round(tong_tra),
            "total_interest": round(tong_tra - amount),
        })
    out.sort(key=lambda o: o["rate_pct"])
    return {
        "amount": amount, "months": months, "as_of": matrix["as_of"],
        "count": len(out[:limit]), "options": out[:limit],
    }


@app.get("/products/savings-options", tags=["product"])
def savings_options(
    amount: int = Query(..., gt=0, description="Số tiền muốn gửi (VND)"),
    months: int = Query(..., ge=1, le=120, description="Số tháng muốn gửi"),
    limit: int = Query(5, ge=1, le=20),
):
    """Các gói tiết kiệm cho (số tiền, kỳ hạn), kèm tiền lãi dự kiến khi đáo hạn.

    Tiết kiệm có kỳ hạn trả lãi đơn trên số ngày gửi thực tế, nên lãi = gốc ×
    lãi suất năm × số tháng / 12. Số do service tính, LLM không tự nhân chia.
    """
    matrix = _rate_matrix("SAVINGS")
    theo_sp: dict[str, list[dict]] = {}
    for r in matrix["rates"]:
        theo_sp.setdefault(r["product_id"], []).append(r)

    out = []
    for pid, ky_han in theo_sp.items():
        # Gửi tiết kiệm thì lấy kỳ hạn dài nhất KHÔNG vượt thời gian khách gửi:
        # gửi 6 tháng mà áp lãi kỳ 12 tháng là hứa mức lãi khách không được nhận.
        vua_du = [t for t in ky_han if t["term_months"] <= months]
        t = max(vua_du, key=lambda x: x["term_months"]) if vua_du else None
        if not t:
            continue
        rate = t["rate_pct"]
        lai = amount * rate / 100 * months / 12
        out.append({
            "product_id": pid,
            "product_name": t["product_name"],
            "term_code": t["term_code"],
            "term_label": t["term_label"],
            "rate_pct": rate,
            "interest_amount": round(lai),
            "maturity_amount": round(amount + lai),
        })
    out.sort(key=lambda o: o["rate_pct"], reverse=True)
    return {
        "amount": amount, "months": months, "as_of": matrix["as_of"],
        "count": len(out[:limit]), "options": out[:limit],
    }


def _fv_annuity(monthly: float, annual_pct: float, months: int) -> float:
    """Giá trị tương lai của việc gửi đều `monthly` mỗi tháng trong `months` tháng.

    Gửi cuối kỳ (ordinary annuity) chứ không phải đầu kỳ: khoản của tháng cuối
    chưa kịp sinh lãi. Chọn hướng thận trọng vì đây là con số dùng để hứa với
    khách bao giờ đạt mục tiêu.
    """
    r = annual_pct / 100 / 12
    if r <= 0:
        return monthly * months
    return monthly * (((1 + r) ** months - 1) / r)


def _pmt_for_goal(goal: float, annual_pct: float, months: int, initial: float = 0.0) -> float:
    """Mỗi tháng phải gửi bao nhiêu để sau `months` tháng có đủ `goal`."""
    r = annual_pct / 100 / 12
    con_lai = goal - initial * ((1 + r) ** months if r > 0 else 1)
    if con_lai <= 0:
        return 0.0
    if r <= 0:
        return con_lai / months
    return con_lai * r / ((1 + r) ** months - 1)


@app.get("/products/savings-goal-plan", tags=["savings-goal"])
def savings_goal_plan(
    goal_amount: int = Query(..., gt=0, description="Số tiền mục tiêu (VND)"),
    months: int = Query(..., ge=1, le=360, description="Số tháng muốn đạt mục tiêu"),
    monthly_capacity: int = Query(
        0, ge=0, description="Mỗi tháng khách để dành được bao nhiêu — lấy từ savings-capacity"
    ),
    initial_amount: int = Query(0, ge=0, description="Số vốn có sẵn để gửi ngay"),
    limit: int = Query(3, ge=1, le=10),
):
    """BƯỚC 3 của lộ trình tiết kiệm: mục tiêu này có với tới được không, bằng gói nào.

    Lãi suất lấy từ biểu niêm yết thật (v_interest_rate), kỳ hạn dài nhất không
    vượt thời gian khách gửi — áp lãi kỳ 12 tháng cho người gửi 6 tháng là hứa
    mức lãi khách không bao giờ nhận được.

    Service tính hết phần số học và trả về `feasible` cùng `gap_*`. Mô hình chỉ
    việc diễn đạt: giao cho LLM tự nhân lãi kép 36 kỳ là cách chắc chắn nhất để
    có một kế hoạch tài chính sai số.

    Khi không khả thi, phần `alternatives` đưa ba hướng đã tính sẵn — kéo dài
    thời gian, hạ mục tiêu, hoặc nâng mức để dành — để câu trả lời không dừng ở
    "bạn không làm được".
    """
    matrix = _rate_matrix("SAVINGS")
    theo_sp: dict[str, list[dict]] = {}
    for r in matrix["rates"]:
        theo_sp.setdefault(r["product_id"], []).append(r)

    goi = []
    for pid, ky_han in theo_sp.items():
        vua_du = [k for k in ky_han if k["term_months"] <= months]
        k = max(vua_du, key=lambda x: x["term_months"]) if vua_du else None
        if not k:
            continue
        rate = k["rate_pct"]
        goi.append({
            "product_id": pid, "product_name": k["product_name"],
            "term_code": k["term_code"], "term_label": k["term_label"],
            "rate_pct": rate,
            "required_monthly": round(_pmt_for_goal(goal_amount, rate, months, initial_amount)),
            "projected_amount": (
                round(_fv_annuity(monthly_capacity, rate, months)
                      + initial_amount * ((1 + rate / 100 / 12) ** months))
                if monthly_capacity or initial_amount else None
            ),
        })
    goi.sort(key=lambda g: g["rate_pct"], reverse=True)
    goi = goi[:limit]

    tot = goi[0] if goi else None
    lai = tot["rate_pct"] if tot else 0.0
    can_moi_thang = _pmt_for_goal(goal_amount, lai, months, initial_amount)
    khong_lai = (goal_amount - initial_amount) / months

    ra = {
        "goal_amount": goal_amount,
        "months": months,
        "years": round(months / 12, 1),
        "initial_amount": initial_amount,
        "as_of": matrix["as_of"],
        "best_rate_pct": lai,
        "required_monthly_no_interest": round(khong_lai),
        "required_monthly_with_interest": round(can_moi_thang),
        "interest_saves_monthly": round(khong_lai - can_moi_thang),
        "products": goi,
    }

    if not monthly_capacity:
        ra["feasible"] = None
        ra["note"] = ("Chưa có monthly_capacity nên chỉ tính được mức cần gửi. "
                      "Gọi savings-capacity trước rồi truyền vào để biết có khả thi không.")
        return ra

    du_kien = _fv_annuity(monthly_capacity, lai, months) + initial_amount * ((1 + lai / 100 / 12) ** months)
    thieu = goal_amount - du_kien
    ra.update({
        "monthly_capacity": monthly_capacity,
        "projected_amount": round(du_kien),
        "feasible": thieu <= 0,
        "coverage_pct": round(du_kien * 100 / goal_amount),
        "gap_amount": round(max(thieu, 0)),
        "gap_monthly": round(max(can_moi_thang - monthly_capacity, 0)),
        "capacity_multiple_needed": round(can_moi_thang / monthly_capacity, 1) if monthly_capacity else None,
    })

    if thieu > 0:
        # Bao nhiêu tháng thì tới đích nếu giữ nguyên mức để dành. Cộng dồn từng
        # tháng thay vì giải log: vòng lặp có trần rõ ràng và đọc ra ngay là
        # "quá 50 năm thì đừng nói tiếp".
        r = lai / 100 / 12
        so_du, n = float(initial_amount), 0
        while so_du < goal_amount and n < 600:
            so_du = so_du * (1 + r) + monthly_capacity
            n += 1
        ra["alternatives"] = {
            "keep_pace_months_needed": n if n < 600 else None,
            "keep_pace_years_needed": round(n / 12, 1) if n < 600 else None,
            "reachable_goal_same_months": round(du_kien),
            "needed_monthly_to_hit_goal": round(can_moi_thang),
        }
    return ra


def _best_rate(product_id: str, months: int = 12) -> float:
    """Lãi suất niêm yết của sản phẩm ở kỳ hạn gần nhất với `months`."""
    row = query_one(
        """
        SELECT rate FROM v_interest_rate
        WHERE product_id = %s
        ORDER BY ABS(term_months - %s), term_months DESC
        LIMIT 1
        """,
        (product_id, months),
    )
    return float(num(row["rate"])) if row else 0.0


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
    # Khách không tồn tại mà không chặn ở đây thì kết cục là "Dòng tiền chưa dư",
    # một câu trả lời nghe hợp lý nhưng sai nguyên nhân.
    _require_customer(customer_id)

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
        # product_interest là MÃ biểu lãi ("RB.TK.LSCN.INT"), không phải con số:
        # tách chữ số ra khỏi mã sẽ cho ra rác. Lãi thật nằm ở interest_rate_term.
        rate = _best_rate(str(p["product_id"]), 12)
        if rate <= 0:
            continue  # sản phẩm chưa có biểu lãi thì không hứa lợi ích
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
    # Ba công cụ của lộ trình tiết kiệm. Mô tả cố ý nói rõ THỨ TỰ GỌI: trợ lý chỉ
    # biết công cụ qua mô tả, không đọc được code, nên nếu không viết ra thì nó
    # sẽ gọi tool 3 trước với một con số tự bịa cho monthly_capacity.
    tool(
        "review_quarter_spending",
        "BƯỚC 1 khi khách đặt mục tiêu tiết kiệm: mổ xẻ chi tiêu của quý GẦN NHẤT "
        "ĐÃ KẾT THÚC — thu, chi, từng nhóm, và xếp mỗi nhóm vào ba rổ thiết yếu / "
        "cam kết / co giãn để biết phần nào cắt được. Tách sẵn các khoản chi một "
        "lần bất thường. Gọi công cụ này TRƯỚC khi nói bất cứ điều gì về khả năng "
        "tiết kiệm của khách.",
        "GET", "/transactions/{customer_id}/quarter-review",
        params={"customer_id": "int", "quarter": 'str tùy chọn, dạng "2026Q2"; bỏ trống = quý gần nhất đã kết thúc'},
        returns="period, income, expense, monthly_avg, by_category[] kèm bucket, "
                "buckets[] (thiết yếu/cam kết/co giãn), one_off_transactions[], delta_expense_vs_prev_pct.",
    ),
    tool(
        "get_savings_capacity",
        "BƯỚC 2: mỗi tháng / quý / năm khách thực sự để dành được bao nhiêu. Dùng "
        "TRUNG VỊ và tự loại tháng đang chạy cùng tháng không có thu nhập, nên con "
        "số này đáng tin hơn mọi phép chia trung bình. Trả về hai mức: realistic "
        "(giữ nhịp hiện tại) và stretch (cắt 70% chi co giãn). Lấy số "
        "realistic.monthly ở đây rồi truyền vào bước 3, ĐỪNG tự ước lượng.",
        "GET", "/transactions/{customer_id}/savings-capacity",
        params={"customer_id": "int", "months": "int, cửa sổ dữ liệu, mặc định 12"},
        returns="monthly{income,expense,net}, savings_rate_pct, realistic{monthly,quarterly,annual}, "
                "stretch{...}, months_used, months_excluded[] kèm lý do.",
    ),
    tool(
        "plan_savings_goal",
        "BƯỚC 3: mục tiêu của khách có đạt được không và bằng gói tiết kiệm nào. "
        "Truyền monthly_capacity lấy từ bước 2. Service tính sẵn lãi kép, mức cần "
        "gửi mỗi tháng, số tiền dự kiến đạt được, phần còn thiếu và các phương án "
        "thay thế khi không khả thi — TUYỆT ĐỐI không tự nhân chia lãi suất, hãy "
        "đọc thẳng các con số trả về.",
        "GET", "/products/savings-goal-plan",
        params={"goal_amount": "int, số tiền mục tiêu VND", "months": "int, số tháng",
                "monthly_capacity": "int, lấy từ get_savings_capacity",
                "initial_amount": "int, vốn có sẵn, mặc định 0", "limit": "int, mặc định 3"},
        returns="feasible, required_monthly_with_interest, projected_amount, coverage_pct, "
                "gap_amount, gap_monthly, products[] kèm rate_pct thật, alternatives{}.",
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
        "get_product_rates",
        "Biểu lãi suất hiện hành theo (sản phẩm × kỳ hạn). product_group=SAVINGS cho "
        "gói tiết kiệm, =LOAN cho gói vay. Dùng khi khách hỏi 'lãi suất gửi/vay bao nhiêu'.",
        "GET", "/products/rates",
        params={"product_group": "SAVINGS|LOAN"},
        returns="products[], terms[] và rates[] (mỗi dòng một cặp sản phẩm-kỳ hạn) kèm as_of.",
    ),
    tool(
        "compare_loan_options",
        "So sánh các gói VAY cho một số tiền và số tháng cụ thể, kèm tiền trả hàng "
        "tháng, tổng lãi và tổng phải trả. Dùng khi khách hỏi 'muốn vay X trong Y "
        "tháng thì gói nào'. Tiền trả hàng tháng do service tính theo dư nợ giảm "
        "dần — chép nguyên, KHÔNG tự tính lại.",
        "GET", "/products/loan-options",
        params={"amount": "int, số tiền vay (VND)", "months": "int, số tháng", "limit": "int, mặc định 5"},
        returns="options[] xếp theo lãi suất tăng dần, kèm monthly_payment và total_interest.",
    ),
    tool(
        "compare_savings_options",
        "So sánh các gói TIẾT KIỆM cho một số tiền và số tháng gửi, kèm tiền lãi dự "
        "kiến khi đáo hạn. Dùng khi khách hỏi 'gửi X trong Y tháng thì gói nào lợi "
        "nhất'. Số lãi do service tính — chép nguyên, KHÔNG tự tính lại.",
        "GET", "/products/savings-options",
        params={"amount": "int, số tiền gửi (VND)", "months": "int, số tháng", "limit": "int, mặc định 5"},
        returns="options[] xếp theo lãi suất giảm dần, kèm interest_amount và maturity_amount.",
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
