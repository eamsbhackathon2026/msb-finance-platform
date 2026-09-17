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
from datetime import datetime, timedelta

from models import (
    AuditAgentStat,
    AuditTrace,
    Beneficiary,
    CaseCustomerProfile,
    CaseDetail,
    CaseModelInfo,
    CaseTimelineStep,
    CaseTransaction,
    CategoryTotal,
    InvestRates,
    MaturingDeposit,
    MaturingDeposits,
    RateCell,
    RateProduct,
    RateRow,
    RateTerm,
    MonthCategory,
    MonthSummary,
    MonthlyReport,
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
    OpsAuditLog,
    OpsCase,
    OpsDashboard,
    OpsDeltas,
    OpsMetrics,
    OpsModelConfig,
    OpsModelFactor,
    OpsScenario,
    RiskAssessment,
    RiskSignal,
    ScamAlert,
    SystemStatusRow,
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
# Trạng thái case (ck_case_status của bảng guardian_case) so với trạng thái cảnh
# báo hiển thị bên Ops. Bốn khoá này là TOÀN BỘ giá trị hợp lệ của cột; bảng cũ
# dùng tên khác (IN_PROGRESS, CONFIRMED_FRAUD, CLOSED) nên mọi case đã đóng đều
# rơi về "pending" — cảnh báo đã xử lý xong vẫn hiện là chờ xử lý.
CASE_STATUS_MAP = {
    "OPEN": "pending",
    "CALLBACK_DONE": "investigating",
    "CLOSED_FRAUD": "confirmed",
    "CLOSED_LEGIT": "dismissed",
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


# Giới hạn một lần lấy quyết định. risk-scoring chặn trên ở 200 và không có
# endpoint đếm tổng, nên hai chỉ số phải cộng từ danh sách sẽ dừng ở đây khi
# database vượt 200 dòng. Hai chỉ số còn lại đọc view ops_summary nên luôn đúng
# toàn bảng.
DECISION_FETCH_LIMIT = 200

# Kết cục nghĩa là tiền không rời khỏi tài khoản khách.
PROTECTED_OUTCOMES = ("prevented", "held")


def map_metrics(summary: dict, decisions: list[dict]) -> OpsMetrics:
    s = summary.get("summary") or {}
    prevented = int(s.get("prevented_total") or 0)
    proceeded = int(s.get("proceeded_after_intervene") or 0)
    denom = prevented + proceeded
    # "Giá trị đã bảo vệ" là tiền GIỮ LẠI được, nên chỉ cộng ca có kết cục chặn
    # hoặc tạm giữ. Cộng mọi ca mức intervene như trước là cộng cả những ca khách
    # vẫn chuyển đi — trên dữ liệu thật, 24,2 tỷ thay vì 4,1 tỷ.
    protected = sum(
        int((d.get("tx_snapshot") or {}).get("amount") or 0)
        for d in decisions
        if d.get("outcome") in PROTECTED_OUTCOMES
    )
    return OpsMetrics(
        # Không phải "hôm nay": đây là số quyết định lấy về được, tối đa
        # DECISION_FETCH_LIMIT. Nhãn trên màn hình nói đúng như vậy.
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
            scanned_today=KpiDelta(value_label="các quyết định gần nhất", up=True),
            alerts_fired=KpiDelta(value_label="tổng tích luỹ", up=True),
            cancel_rate_pct=KpiDelta(value_label="trên số ca đã can thiệp", up=True),
            protected_value_vnd=KpiDelta(value_label="tiền giữ lại được", up=True),
        ),
        hourly_alerts=[HourlyAlertPoint(hour=h, count=c) for h, c in buckets.items()],
        scenario_counts=[
            ScenarioCount(name=n, count=c)
            for n, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
        ],
        model_inputs=model_inputs,
    )


# ---- Bốn màn vận hành phụ -----------------------------------------------------
#
# Bốn bảng nhãn dưới đây là chỗ duy nhất dịch mã nội bộ sang tiếng Việt. Mã thô
# (OPEN, TAKEOVER, shield_explain…) không được rò ra màn hình: chuyên viên vận
# hành không đọc mã của kỹ sư.

CASE_STATUS = {
    "OPEN": ("open", "Đang mở"),
    "CALLBACK_DONE": ("callbackDone", "Đã gọi lại khách"),
    "CLOSED_FRAUD": ("closedFraud", "Đã đóng — xác nhận lừa đảo"),
    "CLOSED_LEGIT": ("closedLegit", "Đã đóng — giao dịch hợp lệ"),
}

SCENARIO_GROUPS = {
    "G1": "Mạo danh",
    "G2": "Deepfake",
    "G3": "Đầu tư & việc làm",
    "G4": "Mua bán",
    "G5": "Chiếm quyền thiết bị",
}

SCENARIO_PATTERNS = {
    "SINGLE": "Một lệnh đơn lẻ",
    "SERIES": "Chuỗi nhiều lệnh",
    "DRAIN": "Rút sạch tài khoản",
    "TAKEOVER": "Chiếm quyền điều khiển thiết bị",
    "RECEIVER": "Tài khoản nhận đáng ngờ",
}

SCENARIO_ACTIONS = {
    "cancel": "Khuyên huỷ giao dịch",
    "hold": "Tạm giữ lệnh",
    "contact": "Gọi lại khách hàng",
    "continue": "Cho đi tiếp",
}

AGENT_LABELS = {
    "copilot": "Trợ lý tài chính",
    "shield_explain": "Scam Shield — giải thích rủi ro",
    "shield_interview": "Scam Shield — hỏi khách",
    "shield_advice": "Scam Shield — khuyến cáo",
}

TRACE_STATUS = {
    "ok": "Thành công",
    "cache": "Dùng lại kết quả cũ",
    "timeout": "Quá hạn — đã dùng bản dự phòng",
    "error": "Lỗi — đã dùng bản dự phòng",
}

# timeout và error là hai trạng thái phải rơi về câu trả lời viết sẵn; cache thì
# không, nên tỷ lệ dự phòng chỉ đếm hai cái đầu (giống /llm-traces/stats).
FALLBACK_STATUSES = ("timeout", "error")


def map_ops_cases(cases: list[dict], names: dict[int, str],
                  scenario_names: dict[str, str]) -> list[OpsCase]:
    out: list[OpsCase] = []
    for c in sorted(cases, key=lambda x: x.get("created_at") or "", reverse=True):
        key, label = CASE_STATUS.get(c.get("status") or "", ("open", "Đang mở"))
        sid = c.get("scenario_id") or ""
        out.append(OpsCase(
            id=c.get("case_id") or "",
            decision_id=str(c.get("decision_id") or ""),
            customer=names.get(int(c.get("customer_id") or 0), "—"),
            # Chưa tra được tên nhưng vẫn có mã: nói rõ là kịch bản nào thay vì
            # ném mã trần "S09" vào ô, và cũng không nói dối là "chưa khớp".
            scenario_name=scenario_names.get(sid) or (f"Kịch bản {sid}" if sid else "Chưa khớp kịch bản"),
            status=key,
            status_label=label,
            narrative=c.get("narrative") or "",
            opened_at=c.get("created_at") or "",
            closed_at=c.get("closed_at"),
        ))
    return out


def map_ops_scenarios(scenarios: list[dict], counts: list[ScenarioCount]) -> list[OpsScenario]:
    today = {c.name: c.count for c in counts}
    out: list[OpsScenario] = []
    # priority 0 là nhóm chiếm quyền thiết bị và phải đứng đầu, nên không dùng
    # `or` để đặt mặc định — 0 là giá trị thật, không phải giá trị trống.
    def _rank(row: dict) -> tuple[int, str]:
        pr = row.get("priority")
        return (99 if pr is None else int(pr), row.get("scenario_id") or "")

    for s in sorted(scenarios, key=_rank):
        name = s.get("scenario_name") or s.get("scenario_id") or ""
        out.append(OpsScenario(
            id=s.get("scenario_id") or "",
            name=name,
            group_label=SCENARIO_GROUPS.get(s.get("group_code") or "", "Khác"),
            pattern_label=SCENARIO_PATTERNS.get(s.get("pattern") or "", "Chưa phân loại"),
            action_label=SCENARIO_ACTIONS.get(s.get("recommended_action") or "", "Chưa đặt"),
            # agent_can_ask = 'N' ở nhóm chiếm quyền thiết bị: kẻ gian đang nhìn
            # thấy màn hình nên hỏi khách là tự lộ.
            can_ask=(s.get("agent_can_ask") or "Y") == "Y",
            advice_title=s.get("advice_title") or "",
            advice_body=s.get("advice_body") or "",
            priority=int(s.get("priority") or 0),
            alerts_today=today.get(name, 0),
        ))
    return out


def map_ops_model(info: dict, inputs: list[str]) -> OpsModelConfig:
    caps = info.get("factor_caps") or {}
    levels = info.get("levels") or {}
    factors = [
        OpsModelFactor(key=k, label=MODEL_FACTOR_LABELS.get(k, k), max_score=int(v))
        for k, v in caps.items()
    ]
    return OpsModelConfig(
        # Ngưỡng nằm trong chuỗi mô tả ("40 - 74", ">= 75"); rút số đầu tiên ra.
        soft_warn_min=_first_int(levels.get("soft_warn"), 40),
        intervene_min=_first_int(levels.get("intervene"), 75),
        max_score=sum(f.max_score for f in factors) or 100,
        factors=factors,
        inputs=inputs,
    )


MODEL_FACTOR_LABELS = {
    "amount_deviation": "Số tiền lệch thói quen",
    "new_beneficiary": "Người nhận mới",
    "time_of_day": "Giờ giao dịch bất thường",
    "behavior_drift": "Hành vi đổi khác",
    "relationship_history": "Chưa từng giao dịch với người nhận",
    "recent_context": "Sự kiện đáng ngờ ngay trước đó",
}


def _first_int(text: object, default: int) -> int:
    m = re.search(r"\d+", str(text or ""))
    return int(m.group(0)) if m else default


def map_ops_audit(traces: list[dict], stats: dict) -> OpsAuditLog:
    rows = []
    for t in traces:
        # Trạng thái và nhãn phải lấy từ cùng một khoá: tra hai lần với hai mặc
        # định khác nhau sinh ra cặp vô nghĩa như "error" kèm chữ "Không rõ".
        raw = t.get("status") or ""
        status = raw if raw in TRACE_STATUS else "error"
        rows.append(AuditTrace(
            id=str(t.get("trace_id") if t.get("trace_id") is not None else ""),
            time=t.get("created_at") or "",
            # Khoá agent là dữ liệu kiểm toán, giữ nguyên khi chưa có nhãn thay
            # vì đổi thành "—": người đọc nhật ký cần biết chính xác ai đã gọi.
            agent_label=AGENT_LABELS.get(t.get("agent") or "", t.get("agent") or "—"),
            model=t.get("model") or "—",
            status=status,
            status_label=TRACE_STATUS[status],
            latency_ms=int(t["latency_ms"]) if t.get("latency_ms") is not None else None,
            decision_id=str(t["decision_id"]) if t.get("decision_id") else None,
        ))

    breakdown = stats.get("breakdown") or []
    per_agent: dict[str, dict[str, int]] = {}
    weighted = 0
    timed_calls = 0
    for row in breakdown:
        label = AGENT_LABELS.get(row.get("agent") or "", row.get("agent") or "—")
        n = int(row.get("n") or 0)
        acc = per_agent.setdefault(label, {"calls": 0, "fallback": 0, "latency": 0, "timed": 0})
        acc["calls"] += n
        if row.get("status") in FALLBACK_STATUSES:
            acc["fallback"] += n
        # avg_latency_ms là NULL khi nhóm đó chưa đo được thời gian. Cộng như 0
        # sẽ kéo trung bình xuống và báo hệ thống nhanh hơn thực tế.
        if row.get("avg_latency_ms") is None:
            continue
        latency = int(row["avg_latency_ms"])
        acc["latency"] += latency * n
        acc["timed"] += n
        weighted += latency * n
        timed_calls += n

    # Upstream trả total_calls = 1 khi chưa có bản ghi nào (nó tự thay 0 bằng 1
    # để khỏi chia cho 0). Màn chứng minh "AI kiểm toán được" không được hiện
    # một lượt gọi không tồn tại.
    total = int(stats.get("total_calls") or 0) if breakdown else 0
    return OpsAuditLog(
        total_calls=total,
        fallback_rate_pct=round(float(stats.get("fallback_rate") or 0) * 100) if breakdown else 0,
        avg_latency_ms=round(weighted / timed_calls) if timed_calls else 0,
        per_agent=[
            AuditAgentStat(
                agent_label=label,
                calls=v["calls"],
                fallback_calls=v["fallback"],
                avg_latency_ms=round(v["latency"] / v["timed"]) if v["timed"] else 0,
            )
            for label, v in sorted(per_agent.items())
        ],
        traces=rows,
    )


def filter_audit(log: OpsAuditLog, status: str | None = None) -> OpsAuditLog:
    """Lọc nhật ký đã dựng sẵn — dùng cho nhánh dữ liệu dự phòng.

    Ba con số thống kê giữ nguyên vì chúng là số của toàn bộ nhật ký, không phải
    của phần đang lọc; màn hình nói rõ điều đó.
    """
    if not status:
        return log
    return log.model_copy(update={"traces": [t for t in log.traces if t.status == status]})


# ---- Chi tiết case, dòng thời gian và tình trạng hệ thống ---------------------

# Cờ phiên mà risk-scoring đọc; chúng nói lên bối cảnh thiết bị lúc chuyển tiền,
# nên chuyên viên cần thấy chúng ở dòng "Kênh" chứ không phải chỉ "Mobile".
SESSION_FLAG_LABELS = {
    "screen_sharing": "đang chia sẻ màn hình",
    "remote_app": "có ứng dụng điều khiển từ xa",
    "accessibility_service": "bật dịch vụ trợ năng",
    "new_device": "thiết bị mới",
    "on_call": "đang trong cuộc gọi",
}

PERSONA_SEGMENTS = {"SALARY": "Lương", "HNW": "Ưu tiên", "SENIOR": "Cao tuổi"}

HOLD_BY_CASE = {
    "OPEN": "Tạm giữ bởi Scam Shield",
    "CALLBACK_DONE": "Đã gọi lại khách, chờ kết luận",
    "CLOSED_FRAUD": "Đã chặn — xác nhận lừa đảo",
    "CLOSED_LEGIT": "Đã cho đi tiếp — giao dịch hợp lệ",
}

# Chưa mở case thì suy từ hành động đã ghi trên chính quyết định.
HOLD_BY_ACTION = {
    "cancel": "Khách đã huỷ lệnh",
    "hold": "Tạm giữ bởi Scam Shield",
    "contact": "Chờ liên hệ khách",
    "continue": "Đã cho đi tiếp",
}

ACTION_STEPS = {
    "cancel": "Khách hàng huỷ giao dịch",
    "hold": "Hệ thống tạm giữ lệnh",
    "contact": "Chuyên viên liên hệ khách hàng",
    "continue": "Khách hàng tiếp tục chuyển tiền",
}

LEVEL_WORDS = {"pass": "cho đi tiếp", "soft_warn": "cảnh báo mềm", "intervene": "can thiệp"}

# Thời hạn xử lý một case, tính từ lúc mở.
SLA_MINUTES = 60

# Cửa sổ thống kê "cảnh báo gần đây" trên hồ sơ khách.
ALERT_WINDOW_DAYS = 90


def _parse_time(stamp: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(stamp))
    except Exception:
        return None


def _hms(stamp: object) -> str:
    parsed = _parse_time(stamp)
    return parsed.strftime("%H:%M:%S") if parsed else "—"


def _amount_of(dec: dict) -> int:
    return int((dec.get("tx_snapshot") or {}).get("amount") or 0)


def map_channel(snapshot: dict) -> str:
    """Kênh giao dịch kèm bối cảnh phiên.

    Cờ phiên là thứ phân biệt "khách tự bấm" với "có người đang nhìn màn hình
    khách", nên nó thuộc về dòng đầu tiên chuyên viên đọc.
    """
    flags = snapshot.get("session_flags") or {}
    notes = [label for key, label in SESSION_FLAG_LABELS.items() if flags.get(key)]
    return "Mobile Banking" + (f" · {', '.join(notes)}" if notes else "")


def _sla_left(case_row: dict | None, dec: dict, now: datetime) -> int:
    """Số phút còn lại của case. Case đã đóng thì không còn hạn nào."""
    if case_row and case_row.get("status") in ("CLOSED_FRAUD", "CLOSED_LEGIT"):
        return 0
    opened = _parse_time((case_row or {}).get("created_at") or dec.get("created_at"))
    if opened is None:
        return SLA_MINUTES
    elapsed = (now - opened).total_seconds() / 60
    return max(0, round(SLA_MINUTES - elapsed))


def map_case_detail(dec: dict, cust: dict | None, case_row: dict | None, info: dict | None,
                    history: list[dict], defaults: CaseDetail, now: datetime) -> CaseDetail:
    """Chi tiết một case từ dữ liệu thật.

    `defaults` là bản hằng số trong catalog: vài trường không có nguồn thật nào
    (khách hàng từ năm nào, phiên bản mô hình, thời gian chấm điểm, độ tin cậy),
    nên lấy từ đó thay vì bịa số. Bảng `customer` không có ngày mở tài khoản, và
    risk-scoring không trả thời gian chấm điểm của từng lượt.
    """
    snapshot = dec.get("tx_snapshot") or {}
    levels = (info or {}).get("levels") or {}
    intervene = _first_int(levels.get("intervene"), defaults.model.intervene_threshold)

    # Cảnh báo gần đây: chỉ tính lượt có can thiệp, trong cửa sổ đã nêu trên màn.
    window_start = now - timedelta(days=ALERT_WINDOW_DAYS)
    recent = [
        d for d in history
        if d.get("level") != "pass"
        and (_parse_time(d.get("created_at")) or now) >= window_start
        and d.get("decision_id") != dec.get("decision_id")
    ]
    amounts = [_amount_of(d) for d in history if _amount_of(d) > 0]

    return CaseDetail(
        transaction=CaseTransaction(
            channel=map_channel(snapshot),
            content=snapshot.get("memo_masked") or "—",
            hold_status=(
                HOLD_BY_CASE.get((case_row or {}).get("status") or "")
                or HOLD_BY_ACTION.get(dec.get("action_taken") or "")
                or "Chờ quyết định xử lý"
            ),
            sla_minutes=_sla_left(case_row, dec, now),
        ),
        customer_profile=CaseCustomerProfile(
            customer_since=defaults.customer_profile.customer_since,
            segment=PERSONA_SEGMENTS.get((cust or {}).get("persona") or "", "Chưa phân khúc"),
            avg_transfer_vnd=round(sum(amounts) / len(amounts)) if amounts else 0,
            recent_alerts_window_days=ALERT_WINDOW_DAYS,
            recent_alerts_count=len(recent),
            recent_alerts_top_score=max((int(d.get("score") or 0) for d in recent), default=0),
        ),
        model=CaseModelInfo(
            version=defaults.model.version,
            method=defaults.model.method,
            scoring_ms=defaults.model.scoring_ms,
            confidence_pct=defaults.model.confidence_pct,
            # Ba ngưỡng đọc từ risk-scoring /info nên màn case và màn "Mô hình &
            # ngưỡng" không thể nói hai con số khác nhau.
            intervene_threshold=intervene,
            soft_warn_min=_first_int(levels.get("soft_warn"), defaults.model.soft_warn_min),
            soft_warn_max=intervene - 1,
        ),
        note_chips=defaults.note_chips,
    )


def map_case_timeline(dec: dict, case_row: dict | None, traces: list[dict],
                      extra: list[CaseTimelineStep] | None = None) -> list[CaseTimelineStep]:
    """Dòng thời gian dựng từ các mốc thời gian thật của một quyết định.

    Mốc nào chưa xảy ra thì không có dòng — dài ngắn theo sự thật, thay vì luôn
    đúng năm bước như bản hằng số cũ. Bước cuối "chờ quyết định xử lý" chỉ xuất
    hiện khi case thật sự chưa được xử lý, và luôn nằm cuối vì nó chưa hoàn tất.
    """
    amount = _amount_of(dec)
    steps: list[tuple[str, str]] = [
        (dec.get("created_at") or "", f"Khách hàng khởi tạo lệnh chuyển {amount:,} ₫".replace(",", ".")),
        (dec.get("created_at") or "",
         f"Risk Engine chấm điểm {dec.get('score')}/100 — mức {LEVEL_WORDS.get(dec.get('level') or '', dec.get('level') or '')}"),
    ]
    for t in traces:
        label = AGENT_LABELS.get(t.get("agent") or "", t.get("agent") or "Trợ lý")
        latency = f" trong {int(t['latency_ms']):,} ms".replace(",", ".") if t.get("latency_ms") else ""
        fallback = " — đã dùng bản dự phòng" if t.get("status") in FALLBACK_STATUSES else ""
        steps.append((t.get("created_at") or "", f"{label} phản hồi{latency}{fallback}"))
    if dec.get("intervened_at"):
        steps.append((dec["intervened_at"], "Hiển thị cảnh báo Scam Shield cho khách hàng"))
    if case_row and case_row.get("created_at"):
        steps.append((case_row["created_at"], f"Mở case {case_row.get('case_id')} cho chuyên viên vận hành"))
    if dec.get("actioned_at"):
        steps.append((dec["actioned_at"], ACTION_STEPS.get(dec.get("action_taken") or "", "Đã xử lý")))
    if case_row and case_row.get("closed_at"):
        steps.append((case_row["closed_at"],
                      f"Đóng case — {CASE_STATUS.get(case_row.get('status') or '', ('', 'đã đóng'))[1]}"))

    steps.sort(key=lambda pair: str(pair[0]))
    out = [
        CaseTimelineStep(id=f"ct-{i + 1}", time=_hms(stamp), label=label, done=True)
        for i, (stamp, label) in enumerate(steps)
    ]
    out.extend(extra or [])
    # "Chờ quyết định xử lý" là chờ CHUYÊN VIÊN, không phải chờ khách. Một case
    # có thể đã ghi action_taken=hold (hệ thống tạm giữ) mà vẫn đang mở, chờ
    # người xử lý — suy theo actioned_at sẽ bỏ mất bước đang chờ của chính nó.
    waiting = (case_row.get("status") in ("OPEN", "CALLBACK_DONE")
               if case_row else not dec.get("actioned_at"))
    if waiting:
        out.append(CaseTimelineStep(id=f"ct-{len(out) + 1}", time="—",
                                    label="Chờ quyết định xử lý", done=False))
    return out


def map_system_status(services: dict[str, bool], agent_ready: bool) -> list[SystemStatusRow]:
    """Tình trạng thật của từng mắt xích, thay cho ba dòng cứng."""
    rows = [SystemStatusRow(label="API Gateway", value="OK", tone="ok")]
    rows.extend(
        SystemStatusRow(label=label, value="OK" if ok else "Không gọi được",
                        tone="ok" if ok else "danger")
        for label, ok in services.items()
    )
    rows.append(SystemStatusRow(
        label="Trợ lý AI",
        value="Sẵn sàng" if agent_ready else "Chưa cấu hình",
        tone="ok" if agent_ready else "warn",
    ))
    return rows


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


def map_monthly(payload: dict) -> MonthlyReport | None:
    """So sánh theo tháng từ transaction-service.

    Giống map_quarterly: chỉ gắn nhãn tiếng Việt cho mã nhóm, không tính lại con
    số nào — delta và pct đã do domain tính. `label` của tháng ("Tháng 8/2026")
    thì domain trả sẵn nên giữ nguyên.
    """
    rows = payload.get("summary") or []
    if not rows:
        return None
    months = [
        MonthSummary(
            period=m["period"],
            label=m["label"],
            year=int(m.get("year") or 0),
            month=int(m.get("month") or 0),
            income=int(m.get("income") or 0),
            expense=int(m.get("expense") or 0),
            net=int(m.get("net") or 0),
            count=int(m.get("count") or 0),
            delta_vs_prev_pct=m.get("delta_vs_prev_pct"),
            by_category=[
                MonthCategory(
                    category=c["category"],
                    label_vi=_vi_category(c["category"]),
                    amount=int(c.get("amount") or 0),
                    pct=int(c.get("pct") or 0),
                    rank=int(c.get("rank") or 0),
                    delta_vs_prev_pct=c.get("delta_vs_prev_pct"),
                )
                for c in (m.get("by_category") or [])
            ],
        )
        for m in rows
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
    return MonthlyReport(months=months, category_totals=totals)


def map_invest_rates(raw: dict) -> InvestRates:
    """Xoay payload phẳng của transaction-service thành bảng theo kỳ hạn.

    Service trả một dòng cho mỗi (sản phẩm, kỳ hạn); FE cần nhóm sẵn theo kỳ hạn
    để vẽ bảng biểu lãi suất và biểu đồ so sánh cùng kỳ hạn mà không phải tự gộp.
    """
    rates = raw.get("rates") or []
    product_names: dict[str, str] = {}
    terms: dict[str, RateTerm] = {}
    cells: dict[str, list[RateCell]] = {}
    for r in rates:
        pid = str(r["product_id"])
        product_names.setdefault(pid, r["product_name"])
        code = r["term_code"]
        terms.setdefault(code, RateTerm(code=code, months=float(r["term_months"]), label=r["term_label"]))
        cells.setdefault(code, []).append(RateCell(product_id=pid, rate_pct=float(r["rate_pct"])))
    return InvestRates(
        as_of=str(raw.get("as_of") or ""),
        products=[RateProduct(id=pid, name=name) for pid, name in sorted(product_names.items())],
        rows=[
            RateRow(term=terms[code], rates=cells[code])
            for code in sorted(terms, key=lambda c: terms[c].months)
        ],
    )


def _core_ymd_to_iso(ymd) -> str:
    """'20270410' (định dạng T24) → '2027-04-10'; giá trị lạ trả nguyên văn."""
    s = str(ymd or "")
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def map_maturing_deposits(raw: dict) -> MaturingDeposits:
    """Sổ đến hạn từ customer-profile-service → hợp đồng FE (ngày sang ISO)."""
    return MaturingDeposits(
        as_of=_core_ymd_to_iso(raw.get("as_of")),
        count=int(raw.get("count") or 0),
        deposits=[
            MaturingDeposit(
                deposit_id=int(d["deposit_id"]),
                product_name=d.get("product_name"),
                amount=int(float(d.get("amount") or 0)),
                rate_pct=float(d.get("interest_rate") or 0),
                term_months=d.get("term_months"),
                maturity_date=_core_ymd_to_iso(d.get("maturity_date")),
                due_today=bool(d.get("due_today")),
                overdue=bool(d.get("overdue")),
            )
            for d in (raw.get("deposits") or [])
        ],
    )
