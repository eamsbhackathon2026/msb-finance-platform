"""Dịch dữ liệu của 5 service domain sang hợp đồng mà web app đang chờ.

Hai bên nói hai ngôn ngữ khác nhau: domain phơi ra dữ liệu ở mức bản ghi
(risk_decision, tx_snapshot, insight, beneficiary), còn FE cần payload đã gộp
sẵn theo màn hình. Toàn bộ phần dịch nằm ở đây, tách khỏi main.py, để khi domain
đổi hợp đồng thì chỉ phải sửa một chỗ.

Chỗ nào domain KHÔNG có dữ liệu tương ứng đều được chú thích rõ là suy ra từ
đâu — để không ai nhầm giá trị suy diễn với giá trị lấy thẳng từ database.
"""
from __future__ import annotations

import re
from datetime import datetime

from models import (
    Beneficiary,
    CategoryTotal,
    QuarterCategory,
    QuarterSummary,
    QuarterlyReport,
    SafetyHistoryItem,
    TimelineEvent,
    BudgetSummary,
    CopilotOverview,
    Customer,
    HomeContent,
    HourlyAlertPoint,
    Insight,
    KpiDelta,
    OpsDashboard,
    OpsDeltas,
    OpsMetrics,
    RiskAssessment,
    RiskSignal,
    ScamAlert,
    ScenarioCount,
    SpendingCategory,
)

# Nhãn tiếng Việt cho 6 yếu tố của engine cộng cơ chế nâng mức.
FACTOR_LABELS = {
    "amount_deviation": "Số tiền lệch xa thói quen",
    "new_beneficiary": "Người nhận mới",
    "time_of_day": "Thời điểm giao dịch",
    "behavior_drift": "Hành vi bất thường",
    "relationship_history": "Quan hệ với người nhận",
    "recent_context": "Sự kiện ngay trước giao dịch",
    "scenario_escalation": "Khớp kịch bản lừa đảo",
}

CATEGORY_LABELS = {
    "FOOD": "Ăn uống",
    "SHOPPING": "Mua sắm",
    "BILLS": "Hoá đơn",
    "TRANSPORT": "Di chuyển",
    "HEALTH": "Y tế",
    "FAMILY_SUPPORT": "Hỗ trợ gia đình",
    "EDUCATION": "Giáo dục",
    "ENTERTAINMENT": "Giải trí",
    "TRANSFER": "Chuyển khoản",
    "OTHER": "Khác",
}

# Engine trả pass/soft_warn/intervene; FE khai báo low/medium/high.
LEVEL_MAP = {"pass": "low", "soft_warn": "medium", "intervene": "high"}

# Trạng thái case bên action-feedback so với trạng thái cảnh báo bên FE.
CASE_STATUS_MAP = {
    "OPEN": "pending",
    "IN_PROGRESS": "investigating",
    "CONFIRMED_FRAUD": "confirmed",
    "DISMISSED": "dismissed",
    "CLOSED": "dismissed",
}

# persona của domain so với hạng sản phẩm hiển thị trên Home.
PERSONA_TIER = {"HNW": "M-FIRST PLATINUM", "SALARY": "M-FIRST GOLD", "SENIOR": "M-FIRST GOLD"}


def _vi_category(code: str) -> str:
    return CATEGORY_LABELS.get(code, code.replace("_", " ").capitalize())


def _severity(weight: int) -> str:
    if weight >= 20:
        return "high"
    return "med" if weight >= 10 else "low"


def _masked_account(pf: dict | None) -> str:
    """Số tài khoản hiển thị.

    Domain không trả số tài khoản thật (có chủ đích — không endpoint nào trả
    PII). Dùng account_id làm phần đuôi để mỗi khách vẫn có một chuỗi ổn định
    và khác nhau, thay vì bịa một số.
    """
    accounts = (pf or {}).get("accounts") or []
    return f"**** {accounts[0]['account_id']}" if accounts else "**** ----"


def map_customer(cust: dict, pf: dict | None) -> Customer:
    summary = (pf or {}).get("summary") or {}
    return Customer(
        id=str(cust["customer_id"]),
        name=cust.get("name_masked") or "—",
        masked_account=_masked_account(pf),
        balance=int(summary.get("total_working_balance") or 0),
    )


def map_home(cust: dict, ins: dict | None, now: datetime) -> HomeContent:
    hour = now.hour
    greeting = "Chào buổi sáng" if hour < 12 else ("Chào buổi chiều" if hour < 18 else "Chào buổi tối")
    # Gợi ý của trợ lý lấy insight đứng đầu kỳ gần nhất, đúng thứ Copilot sẽ nói.
    items = sorted(
        (ins or {}).get("insights") or [],
        # .get(k, default) chỉ dùng default khi THIẾU khoá; giá trị None vẫn trả
        # None. Domain có trả rank_in_period = null nên phải chặn bằng "or".
        key=lambda i: (i.get("period") or "", -(i.get("rank_in_period") or 99)),
        reverse=True,
    )
    hint = items[0]["insight_text"] if items else "Xem phân tích chi tiêu tháng này của bạn."
    return HomeContent(
        greeting=greeting,
        customer_name=cust.get("name_masked") or "—",
        product_tier=PERSONA_TIER.get(cust.get("persona", ""), "M-FIRST"),
        assistant_hint=hint,
    )


def map_overview(monthly: dict, ins: dict | None) -> CopilotOverview | None:
    periods = (monthly or {}).get("summary") or []
    if not periods:
        return None
    latest = periods[-1]

    # Chuyển khoản đi KHÔNG phải chi tiêu. Màn Copilot nói về thói quen tiêu
    # dùng; để nguyên thì một lệnh chuyển 620 triệu chiếm 100% biểu đồ và các
    # nhóm thật bị ép về 0%, màn hình mất hết ý nghĩa.
    cats = [c for c in (latest.get("top_categories") or [])
            if not str(c.get("category", "")).startswith("TRANSFER")]
    total = sum(int(c.get("amount") or 0) for c in cats) or 1
    expense = total

    # delta_vs_prev của insight cùng kỳ, cùng nhóm — dùng làm xu hướng.
    rows = sorted(((ins or {}).get("insights") or []), key=lambda i: i.get("period") or "")
    deltas = {(i.get("period"), i.get("category")): i.get("delta_vs_prev") for i in rows}
    # Kỳ mới nhất thường chưa có insight (chúng được sinh theo kỳ đã đóng), nên
    # thiếu thì lùi về xu hướng gần nhất của chính nhóm đó thay vì hiện 0%.
    latest_by_cat = {i.get("category"): i.get("delta_vs_prev") for i in rows}
    period = latest.get("period")

    categories = [
        SpendingCategory(
            key=str(c["category"]).lower().replace("_", "-"),
            label_vi=_vi_category(c["category"]),
            amount=int(c.get("amount") or 0),
            pct=round(int(c.get("amount") or 0) * 100 / total),
            trend_pct=round(
                deltas.get((period, c["category"]))
                or latest_by_cat.get(c["category"])
                or 0
            ),
        )
        for c in cats
    ]

    # Domain KHÔNG có khái niệm ngân sách — đây là giá trị suy ra, không phải số
    # lấy từ database. Từng thử dùng thu nhập của kỳ, nhưng thu nhập gồm cả tiền
    # tất toán sổ tiết kiệm: tháng 09 ra ngân sách 527 triệu trong khi chi tiêu
    # thật chỉ 2,4 triệu, thanh tiến độ gần như bằng không. Lấy 120% chi tiêu thì
    # luôn có tỷ lệ đọc được.
    budget = round(expense * 1.2)

    insights = [
        Insight(
            id=f"ins-{i['insight_id']}",
            kind="info",
            title=f"Nhóm {_vi_category(i['category'])} kỳ {i['period']}",
            body=i.get("insight_text") or "",
        )
        for i in ((ins or {}).get("insights") or [])[:3]
    ]

    month = latest.get("month") or ""
    label = f"Tháng {month[5:7]}/{month[:4]}" if len(month) >= 7 else str(latest.get("period", ""))
    return CopilotOverview(
        budget=BudgetSummary(month_label=label, spent_vnd=expense, budget_vnd=budget),
        categories=categories,
        insights=insights,
    )


def map_assessment(pre: dict, scen: dict | None) -> RiskAssessment:
    """Đổi kết quả engine 6 yếu tố sang RiskAssessment của FE.

    Chỉ lấy các yếu tố có điểm > 0: yếu tố 0 điểm không phải tín hiệu rủi ro,
    đưa lên màn hình chỉ làm loãng phần giải thích.
    """
    factors: dict = pre.get("factors") or {}
    signals = [
        RiskSignal(
            id=key,
            label=FACTOR_LABELS.get(key, key),
            detail=value.get("detail") or "",
            weight=int(value.get("score") or 0),
            severity=_severity(int(value.get("score") or 0)),
        )
        for key, value in factors.items()
        if int(value.get("score") or 0) > 0
    ]
    signals.sort(key=lambda s: s.weight, reverse=True)

    recommendations: list[str] = []
    if scen:
        if scen.get("advice_title"):
            recommendations.append(scen["advice_title"])
        if scen.get("advice_body"):
            recommendations.append(scen["advice_body"])
    if not recommendations and pre.get("template_text"):
        recommendations.append(pre["template_text"])

    return RiskAssessment(
        score=int(pre.get("score") or 0),
        level=LEVEL_MAP.get(pre.get("level", ""), "medium"),
        scenario_name=(scen or {}).get("scenario_name") or "Chưa khớp kịch bản nào",
        signals=signals,
        recommendations=recommendations,
    )


def map_beneficiary(snapshot: dict, known: list[dict]) -> Beneficiary:
    """Người nhận của một giao dịch.

    risk_decision chỉ lưu số tài khoản đã che và mã ngân hàng. Đối chiếu với
    danh sách người nhận của khách để lấy thêm tên và tuổi tài khoản; không khớp
    nghĩa là người nhận hoàn toàn mới — đúng tình huống đáng ngờ nhất.
    """
    masked = snapshot.get("beneficiary_masked") or "—"
    bank = snapshot.get("bank_code") or "—"
    for b in known:
        if b.get("account_masked") == masked:
            return Beneficiary(
                account_no=masked,
                bank_name=b.get("bank_code") or bank,
                holder_name=b.get("name_masked") or "—",
                account_age_days=int(b.get("age_days") or 0),
                report_count=0,  # domain chưa có đếm báo cáo cộng đồng
            )
    return Beneficiary(
        account_no=masked,
        bank_name=bank,
        holder_name="Người nhận mới",
        account_age_days=0,
        report_count=0,
    )


_SCENARIO_RE = re.compile(r"\bS\d{2}\b")


def scenario_id_of(dec: dict) -> str | None:
    """Mã kịch bản của một quyết định.

    Bản ghi risk_decision không có trường scenario_id — chỉ view /ops/decisions
    mới có. Nhưng câu giải thích của cơ chế nâng mức luôn nhắc tên mã, nên rút
    từ đó ra thay vì phải gọi thêm một endpoint nữa cho mỗi dòng.
    """
    if dec.get("scenario_id"):
        return dec["scenario_id"]
    detail = ((dec.get("factors") or {}).get("scenario_escalation") or {}).get("detail") or ""
    m = _SCENARIO_RE.search(detail)
    return m.group(0) if m else None


def map_alert(dec: dict, names: dict[int, str], known: list[dict],
              statuses: dict[str, str], scen: dict | None) -> ScamAlert:
    snapshot = dec.get("tx_snapshot") or {}
    return ScamAlert(
        id=dec["decision_id"],
        timestamp=snapshot.get("tx_time") or dec.get("created_at") or "",
        customer=names.get(int(dec.get("customer_id") or 0), "—"),
        amount=int(snapshot.get("amount") or 0),
        beneficiary=map_beneficiary(snapshot, known),
        assessment=map_assessment(dec, scen),
        status=statuses.get(dec["decision_id"], "pending"),
    )


def map_metrics(summary: dict, decisions: list[dict]) -> OpsMetrics:
    s = summary.get("summary") or {}
    prevented = int(s.get("prevented_total") or 0)
    proceeded = int(s.get("proceeded_after_intervene") or 0)
    denom = prevented + proceeded
    # Giá trị đã bảo vệ = tổng số tiền của các giao dịch bị chặn. ops_summary
    # không có sẵn con số này nên cộng từ danh sách quyết định mức intervene.
    protected = sum(
        int((d.get("tx_snapshot") or {}).get("amount") or 0)
        for d in decisions
        if d.get("level") == "intervene"
    )
    return OpsMetrics(
        scanned_today=len(decisions),
        alerts_fired=int(s.get("suspicious_total") or 0),
        cancel_rate_pct=round(prevented * 100 / denom) if denom else 0,
        protected_value_vnd=protected,
    )


def map_dashboard(decisions: list[dict], model_inputs: list[str]) -> OpsDashboard:
    buckets = {f"{h:02d}h": 0 for h in range(24)}
    for d in decisions:
        stamp = (d.get("tx_snapshot") or {}).get("tx_time") or d.get("created_at") or ""
        try:
            buckets[f"{datetime.fromisoformat(stamp).hour:02d}h"] += 1
        except Exception:
            continue

    counts: dict[str, int] = {}
    for d in decisions:
        name = d.get("scenario_name") or "Chưa khớp kịch bản"
        counts[name] = counts.get(name, 0) + 1

    return OpsDashboard(
        # Domain chưa lưu số liệu của ngày hôm trước nên không tính được delta
        # thật. Để nhãn trung tính thay vì bịa một tỷ lệ phần trăm.
        deltas=OpsDeltas(
            scanned_today=KpiDelta(value_label="trong 24 giờ gần nhất", up=True),
            alerts_fired=KpiDelta(value_label="tổng tích luỹ", up=True),
            cancel_rate_pct=KpiDelta(value_label="trên số ca đã can thiệp", up=True),
            protected_value_vnd=KpiDelta(value_label="tổng giá trị đã chặn", up=True),
        ),
        hourly_alerts=[HourlyAlertPoint(hour=h, count=c) for h, c in buckets.items()],
        scenario_counts=[
            ScenarioCount(name=n, count=c)
            for n, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
        model_inputs=model_inputs,
    )


# Sự kiện tài khoản: nhãn tiếng Việt và mức độ đáng ngại.
EVENT_LABELS = {
    "SAVINGS_CLOSED": ("Tất toán sổ tiết kiệm trước hạn", "danger"),
    "NEW_DEVICE_LOGIN": ("Đăng nhập từ thiết bị mới", "warning"),
    "LIMIT_INCREASED": ("Nâng hạn mức chuyển tiền", "warning"),
    "PASSWORD_CHANGED": ("Đổi mật khẩu", "warning"),
    "INBOUND_UNKNOWN": ("Nhận tiền từ nguồn lạ", "warning"),
}


def _vi_datetime(stamp: str) -> str:
    try:
        d = datetime.fromisoformat(stamp)
        return d.strftime("%d/%m/%Y · %H:%M")
    except Exception:
        return stamp


def map_event_timeline(events: list[dict], amount: int) -> list[TimelineEvent]:
    """Dòng thời gian dẫn tới cảnh báo.

    Đây chính là những sự kiện mà yếu tố recent_context của engine dựa vào để
    cộng điểm, nên đưa đúng chúng lên màn hình giữ cho lời giải thích và điểm số
    nói cùng một câu chuyện. Sắp xếp cũ trước để đọc như một mạch thời gian.
    """
    rows = sorted(events, key=lambda e: e.get("event_time") or "")
    out = [
        TimelineEvent(
            id=f"ev-{e['event_id']}",
            time=_vi_datetime(e.get("event_time") or ""),
            label=EVENT_LABELS.get(e.get("event_type", ""), (e.get("event_type", "Sự kiện"), "neutral"))[0],
            detail=f"{int(e['amount']):,} ₫".replace(",", ".") if e.get("amount") else None,
            tone=EVENT_LABELS.get(e.get("event_type", ""), ("", "neutral"))[1],
        )
        for e in rows
    ]
    if out:
        out.append(TimelineEvent(
            id="ev-now",
            time="Hôm nay",
            label=f"Giao dịch {amount:,} ₫ được tạm giữ".replace(",", "."),
            tone="danger",
        ))
    return out


# Kết cục của case so với trạng thái hiển thị trong Trung tâm an toàn.
_HISTORY_STATUS = {
    "CONFIRMED_FRAUD": "blocked",
    "DISMISSED": "ignored",
    "CLOSED": "blocked",
    "OPEN": "processing",
    "IN_PROGRESS": "processing",
}


def map_safety_history(cases: list[dict]) -> list[SafetyHistoryItem]:
    """Lịch sử cảnh báo của chính khách hàng, lấy từ case đã mở cho họ."""
    rows = sorted(cases, key=lambda c: c.get("created_at") or "", reverse=True)
    out: list[SafetyHistoryItem] = []
    for c in rows[:6]:
        # narrative mở đầu bằng "Giao dịch 95,000,000 VND ..." — lấy số tiền ở đó
        # vì bản ghi case không có trường amount riêng.
        m = re.search(r"([\d,\.]+)\s*VND", c.get("narrative") or "")
        amount = int(m.group(1).replace(",", "").replace(".", "")) if m else 0
        stamp = (c.get("created_at") or "")[:10]
        out.append(SafetyHistoryItem(
            id=c.get("case_id") or f"case-{len(out)}",
            date=stamp,
            amount=amount,
            scenario_name=c.get("scenario_id") or "—",
            status=_HISTORY_STATUS.get(c.get("status", ""), "processing"),
        ))
    return out


def map_quarterly(payload: dict) -> QuarterlyReport | None:
    """Thống kê theo quý từ transaction-service.

    Việc duy nhất phải làm ở đây là gắn nhãn tiếng Việt cho mã nhóm: domain trả
    FOOD, FAMILY_SUPPORT còn màn hình cần "Ăn uống", "Hỗ trợ gia đình". Phần
    tính toán đã nằm ở domain, gateway không tính lại — hai chỗ cùng tính một
    con số là hai chỗ có thể lệch nhau.
    """
    rows = payload.get("summary") or []
    if not rows:
        return None
    quarters = [
        QuarterSummary(
            period=q["period"],
            label=q["label"],
            income=int(q.get("income") or 0),
            expense=int(q.get("expense") or 0),
            net=int(q.get("net") or 0),
            count=int(q.get("count") or 0),
            by_category=[
                QuarterCategory(
                    category=c["category"],
                    label_vi=_vi_category(c["category"]),
                    amount=int(c.get("amount") or 0),
                    pct=int(c.get("pct") or 0),
                    rank=int(c.get("rank") or 0),
                    delta_vs_prev_pct=c.get("delta_vs_prev_pct"),
                )
                for c in (q.get("by_category") or [])
            ],
        )
        for q in rows
    ]
    totals = [
        CategoryTotal(
            category=t["category"],
            label_vi=_vi_category(t["category"]),
            amount=int(t.get("amount") or 0),
            pct=int(t.get("pct") or 0),
        )
        for t in (payload.get("category_totals") or [])
    ]
    return QuarterlyReport(quarters=quarters, category_totals=totals)
