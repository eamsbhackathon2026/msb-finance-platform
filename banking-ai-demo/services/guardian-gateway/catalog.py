"""Dữ liệu chuẩn của demo — bản đối chiếu của `src/data/demo-scenarios.ts`.

Giữ đúng từng con số bên FE (Nguyễn Minh Anh · **** 4821 · 47.820.000 ₫ ·
risk 87 · 85.000.000 ₫) để chuyển giữa demo mode và live mode không làm đổi nội
dung màn hình. Khác biệt duy nhất nhìn thấy được là header X-Guardian-Data-Source
và log trong Console.

TOÀN BỘ dữ liệu ở đây là tạm. Khi gateway nối vào 5 service domain, từng hằng số
sẽ được thay bằng lời gọi thật; cấu trúc file giữ nguyên để lúc đó chỉ phải đổi
nguồn chứ không phải đổi hợp đồng.
"""
from __future__ import annotations

from models import (
    Beneficiary,
    BudgetSummary,
    CopilotOverview,
    Insight,
    OpsMetrics,
    RiskAssessment,
    RiskSignal,
    ScamAlert,
    SpendingCategory,
)

SCAM_AMOUNT = 85_000_000
MONTH_BUDGET = 18_000_000

CATEGORIES = [
    SpendingCategory(key="an-uong", label_vi="Ăn uống", amount=4_230_000, pct=34, trend_pct=34),
    SpendingCategory(key="mua-sam", label_vi="Mua sắm", amount=3_120_000, pct=25, trend_pct=12),
    SpendingCategory(key="hoa-don", label_vi="Hoá đơn", amount=2_410_000, pct=19, trend_pct=-5),
    SpendingCategory(key="di-chuyen", label_vi="Di chuyển", amount=1_850_000, pct=15, trend_pct=-8),
    SpendingCategory(key="khac", label_vi="Khác", amount=850_000, pct=7, trend_pct=3),
]

SPENT_THIS_MONTH = sum(c.amount for c in CATEGORIES)  # 12.460.000

INSIGHTS = [
    Insight(
        id="ins-1",
        kind="warning",
        title="Chi tiêu Ăn uống tăng 34% so với tháng 8",
        body=(
            "Bạn đã chi 4.230.000 ₫ cho Ăn uống trong nửa đầu tháng, cao hơn hẳn nhịp chi "
            "thường lệ. Ba khoản lớn nhất đến từ GrabFood và cà phê sáng."
        ),
    ),
    Insight(
        id="ins-2",
        kind="info",
        title="Hoá đơn điện vào mùa cao điểm",
        body=(
            "Tiền điện EVN Hà Nội kỳ này là 1.240.000 ₫, cao hơn 18% trung bình 3 tháng "
            "gần nhất — phù hợp xu hướng mùa nóng."
        ),
    ),
    Insight(
        id="ins-3",
        kind="action",
        title="Bạn có thể tiết kiệm thêm trong tháng này",
        body=(
            "Với nhịp chi hiện tại, dự kiến cuối tháng bạn dư khoảng 6.500.000 ₫. Trích một "
            "phần sang tiết kiệm sẽ không ảnh hưởng chi tiêu."
        ),
        cta_label="Chuyển 3.000.000 ₫ vào Tiết kiệm",
    ),
]

COPILOT_OVERVIEW = CopilotOverview(
    budget=BudgetSummary(month_label="Tháng 9/2026", spent_vnd=SPENT_THIS_MONTH, budget_vnd=MONTH_BUDGET),
    categories=CATEGORIES,
    insights=INSIGHTS,
)

MAIN_BENEFICIARY = Beneficiary(
    account_no="0899 552 617",
    bank_name="VPBank",
    holder_name="TRAN VAN KHOA",
    account_age_days=2,
    report_count=12,
)

# Bốn tín hiệu rủi ro với trọng số 32/28/18/9 — cộng lại đúng 87.
BASE_SIGNALS = [
    RiskSignal(
        id="sig-1",
        label="Tài khoản nhận mới mở 2 ngày",
        detail="Tài khoản người nhận vừa được mở ngày 13/09/2026, chưa có lịch sử giao dịch đáng tin cậy.",
        weight=32,
        severity="high",
        confidence_pct=96,
    ),
    RiskSignal(
        id="sig-2",
        label="Nằm trong danh sách cảnh báo cộng đồng",
        detail="12 người dùng đã báo cáo số tài khoản này trong 48 giờ qua trên hệ thống cảnh báo liên ngân hàng.",
        weight=28,
        severity="high",
        confidence_pct=92,
    ),
    RiskSignal(
        id="sig-3",
        label="Khớp mẫu hành vi mạo danh công an",
        detail="Chuỗi hành vi cuộc gọi lạ kéo dài → chuyển gần hết số dư tới tài khoản mới khớp 92% kịch bản đã ghi nhận.",
        weight=18,
        severity="med",
        confidence_pct=84,
    ),
    RiskSignal(
        id="sig-4",
        label="Giao dịch bất thường so với thói quen",
        detail="Số tiền gấp 9 lần giao dịch trung bình của bạn và được tạo chỉ 18 phút sau một cuộc gọi từ số lạ.",
        weight=9,
        severity="low",
        confidence_pct=71,
    ),
]

RECOMMENDATIONS = [
    "Gọi tổng đài MSB 1800 6083 để xác minh trước khi thực hiện bất kỳ giao dịch nào.",
    "Cơ quan công an không bao giờ yêu cầu chuyển tiền qua điện thoại. Hãy đến trực tiếp công an phường nơi cư trú để xác nhận.",
]

MAIN_SCENARIO = "Mạo danh cơ quan công an"


def level_of(score: int) -> str:
    """Ngưỡng phân mức, giữ đúng như FE đang dùng trong buildAssessment."""
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def build_assessment(score: int, scenario_name: str) -> RiskAssessment:
    """Phân rã điểm theo đúng tỷ trọng 32/28/18/9 của case chính.

    Trọng số cuối lấy phần dư thay vì làm tròn riêng, để tổng luôn cộng khớp
    với `score` — màn giải thích rủi ro bên FE cộng lại các weight và hiển thị,
    lệch một điểm là người xem nhìn ra ngay.
    """
    ratios = [32 / 87, 28 / 87, 18 / 87, 9 / 87]
    weights = [round(score * r) for r in ratios]
    weights[3] = score - weights[0] - weights[1] - weights[2]
    return RiskAssessment(
        score=score,
        level=level_of(score),
        scenario_name=scenario_name,
        signals=[s.model_copy(update={"weight": w}) for s, w in zip(BASE_SIGNALS, weights)],
        recommendations=RECOMMENDATIONS,
    )


MAIN_ASSESSMENT = RiskAssessment(
    score=87,
    level="high",
    scenario_name=MAIN_SCENARIO,
    signals=BASE_SIGNALS,
    recommendations=RECOMMENDATIONS,
)

OPS_METRICS = OpsMetrics(
    scanned_today=24_817,
    alerts_fired=142,
    cancel_rate_pct=78,
    protected_value_vnd=8_400_000_000,
)

OPS_ALERTS = [
    ScamAlert(
        id="ALT-4092",
        timestamp="2026-09-15T09:41:00",
        customer="Nguyễn Minh Anh",
        amount=SCAM_AMOUNT,
        beneficiary=MAIN_BENEFICIARY,
        assessment=MAIN_ASSESSMENT,
        status="pending",
    ),
    ScamAlert(
        id="ALT-4091",
        timestamp="2026-09-15T09:12:00",
        customer="Trần Quốc Bảo",
        amount=42_500_000,
        beneficiary=Beneficiary(account_no="1902 664 130", bank_name="Techcombank", holder_name="NGUYEN HUU PHUC", account_age_days=5, report_count=7),
        assessment=build_assessment(81, "Đầu tư ảo"),
        status="investigating",
    ),
    ScamAlert(
        id="ALT-4090",
        timestamp="2026-09-15T08:56:00",
        customer="Lê Thu Hà",
        amount=12_000_000,
        beneficiary=Beneficiary(account_no="0071 220 954", bank_name="Sacombank", holder_name="DO THI KIM NGAN", account_age_days=9, report_count=4),
        assessment=build_assessment(74, "Trúng thưởng giả"),
        status="pending",
    ),
    ScamAlert(
        id="ALT-4089",
        timestamp="2026-09-15T08:31:00",
        customer="Phạm Văn Long",
        amount=156_000_000,
        beneficiary=Beneficiary(account_no="8833 107 462", bank_name="VIB", holder_name="LUU DINH TRONG", account_age_days=1, report_count=19),
        assessment=build_assessment(91, "Giả nhân viên ngân hàng"),
        status="confirmed",
    ),
    ScamAlert(
        id="ALT-4088",
        timestamp="2026-09-15T08:02:00",
        customer="Đỗ Ngọc Lan",
        amount=8_400_000,
        beneficiary=Beneficiary(account_no="2210 458 771", bank_name="BIDV", holder_name="HOANG MINH TUAN", account_age_days=34, report_count=1),
        assessment=build_assessment(58, "Giả mạo người thân"),
        status="dismissed",
    ),
    ScamAlert(
        id="ALT-4087",
        timestamp="2026-09-15T07:45:00",
        customer="Vũ Minh Châu",
        amount=27_900_000,
        beneficiary=Beneficiary(account_no="5504 913 286", bank_name="MB Bank", holder_name="PHAN VAN DUC", account_age_days=3, report_count=8),
        assessment=build_assessment(69, MAIN_SCENARIO),
        status="pending",
    ),
]

SPENDING_CHART = {
    "type": "bar",
    "title": "Chi tiêu theo nhóm · Tháng 9/2026",
    "data": [{"label": c.label_vi, "value": c.amount} for c in CATEGORIES],
}

# Câu trả lời viết sẵn cho chat. Thứ tự có ý nghĩa: mẫu khớp đầu tiên thắng.
SCRIPTED_REPLIES = [
    (
        r"tiêu nhiều nhất|tiêu bao nhiêu|chi tiêu",
        "Từ 01/09 đến 15/09 bạn đã chi 12.460.000 ₫, bằng 69% ngân sách tháng. Nhóm lớn nhất "
        "là Ăn uống với 4.230.000 ₫ — tăng 34% so với tháng 8, chủ yếu từ GrabFood và cà phê "
        "sáng. Dưới đây là bức tranh đầy đủ theo nhóm:",
    ),
    (
        r"tiết kiệm",
        "Với nhịp chi hiện tại, dự kiến cuối tháng bạn dư khoảng 6.500.000 ₫ sau khi trừ các "
        "hoá đơn định kỳ. Tôi gợi ý trích 3.000.000 ₫ vào Tiết kiệm mục tiêu ngay hôm nay — "
        "phần còn lại vẫn đủ thoải mái cho 2 tuần cuối tháng.",
    ),
    (
        r"dự báo|số dư",
        "Số dư hiện tại là 47.820.000 ₫. Sau khi trừ các khoản chi dự kiến (hoá đơn nước, di "
        "chuyển và ăn uống theo thói quen ~5.500.000 ₫), số dư ngày 30/09 ước tính khoảng "
        "42.300.000 ₫. Không có hoá đơn lớn nào đến hạn trong 2 tuần tới.",
    ),
]

FALLBACK_REPLY = (
    "Tôi có thể giúp bạn phân tích chi tiêu, dự báo dòng tiền và gợi ý tiết kiệm dựa trên "
    'giao dịch của bạn tại MSB. Bạn thử hỏi: "Tháng này tôi tiêu nhiều nhất vào đâu?" nhé.'
)
