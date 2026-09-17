"""Test lớp dịch domain → hợp đồng FE.

Các hàm trong mappers.py là hàm thuần nên kiểm tra được trực tiếp, không cần 5
service domain chạy. Dữ liệu mẫu dưới đây chép từ response THẬT của cluster
(rút gọn), nên khi domain đổi hợp đồng thì test này hỏng — đúng điều mong muốn.
"""
import mappers

# Chép từ GET /customers/100008 và /customers/100008/portfolio
CUSTOMER = {"customer_id": 100008, "name_masked": "NGUYEN THI B***", "persona": "SENIOR", "age": 68}
PORTFOLIO = {
    "customer_id": 100008,
    "persona": "SENIOR",
    "summary": {"total_working_balance": 28679000.0, "total_deposit": 770000000.0},
    "accounts": [{"account_id": 5011, "account_name": "TK huu tri", "working_balance": 28679000.0}],
}

# Chép từ POST /transfer/precheck
PRECHECK = {
    "decision_id": "cab99d05-c621-4552-b57c-f644ba1f67de",
    "customer_id": 100008,
    "persona": "SENIOR",
    "score": 56,
    "level": "intervene",
    "factors": {
        "amount_deviation": {"score": 25, "detail": "vượt mức chuyển lớn nhất từng thực hiện"},
        "new_beneficiary": {"score": 20, "detail": "người nhận hoàn toàn mới, chưa từng chuyển"},
        "time_of_day": {"score": 0, "detail": "17h — trong khung giờ thường ngày"},
        "behavior_drift": {"score": 0, "detail": "nhịp giao dịch bình thường"},
        "relationship_history": {"score": 11, "detail": "chưa xác định quan hệ với người nhận"},
        "recent_context": {"score": 0, "detail": "không có sự kiện bất thường", "events": []},
        "scenario_escalation": {"score": 0, "detail": "Nâng từ soft_warn lên intervene: khớp kịch bản S01"},
    },
    "template_text": "Giao dịch 560,000,000 VND có rủi ro cao (điểm 56/100).",
}

SCENARIO = {
    "scenario_id": "S01",
    "scenario_name": "Giả danh công an, yêu cầu chuyển vào 'tài khoản an toàn'",
    "advice_title": "Công an không bao giờ yêu cầu chuyển tiền",
    "advice_body": "Cơ quan công an KHÔNG BAO GIỜ làm việc qua điện thoại.",
}


def test_khach_hang():
    c = mappers.map_customer(CUSTOMER, PORTFOLIO)
    assert c.id == "100008"
    assert c.name == "NGUYEN THI B***"
    # Số tài khoản thật không bao giờ được domain trả ra; đuôi dùng account_id.
    assert c.masked_account == "**** 5011"
    assert c.balance == 28_679_000


def test_khach_hang_khong_co_tai_khoan_van_khong_vo():
    c = mappers.map_customer(CUSTOMER, {"summary": {}, "accounts": []})
    assert c.masked_account == "**** ----"
    assert c.balance == 0


def test_muc_do_doi_dung_tu_engine_sang_fe():
    # Engine dùng pass/soft_warn/intervene, FE khai báo low/medium/high.
    assert mappers.map_assessment({**PRECHECK, "level": "pass"}, None).level == "low"
    assert mappers.map_assessment({**PRECHECK, "level": "soft_warn"}, None).level == "medium"
    assert mappers.map_assessment(PRECHECK, None).level == "high"


def test_chi_lay_yeu_to_co_diem():
    a = mappers.map_assessment(PRECHECK, SCENARIO)
    ids = [s.id for s in a.signals]
    assert ids == ["amount_deviation", "new_beneficiary", "relationship_history"]
    # Yếu tố 0 điểm không phải tín hiệu rủi ro, đưa lên chỉ làm loãng giải thích.
    assert "time_of_day" not in ids
    assert "scenario_escalation" not in ids


def test_tin_hieu_sap_theo_trong_so_giam_dan():
    a = mappers.map_assessment(PRECHECK, SCENARIO)
    assert [s.weight for s in a.signals] == [25, 20, 11]
    assert a.score == 56


def test_muc_nghiem_trong_theo_trong_so():
    a = mappers.map_assessment(PRECHECK, SCENARIO)
    sev = {s.id: s.severity for s in a.signals}
    assert sev["amount_deviation"] == "high"      # 25
    assert sev["new_beneficiary"] == "high"       # 20
    assert sev["relationship_history"] == "med"   # 11


def test_khuyen_cao_lay_tu_playbook_khong_phai_tu_engine():
    a = mappers.map_assessment(PRECHECK, SCENARIO)
    assert a.scenario_name == SCENARIO["scenario_name"]
    assert a.recommendations[0] == SCENARIO["advice_title"]
    # Không có playbook thì mới dùng câu mô tả của engine.
    b = mappers.map_assessment(PRECHECK, None)
    assert b.recommendations == [PRECHECK["template_text"]]


def test_rut_ma_kich_ban_tu_cau_nang_muc():
    import main
    detail = PRECHECK["factors"]["scenario_escalation"]["detail"]
    assert main._scenario_id_in(detail) == "S01"
    assert main._scenario_id_in("không có mã nào") is None


def test_nguoi_nhan_khop_danh_sach_quen():
    known = [{"account_masked": "3028 ****", "bank_code": "VCB", "name_masked": "MAI VAN S***", "age_days": 430}]
    b = mappers.map_beneficiary({"beneficiary_masked": "3028 ****", "bank_code": "VCB"}, known)
    assert b.holder_name == "MAI VAN S***" and b.account_age_days == 430


def test_nguoi_nhan_khong_khop_thi_la_moi():
    b = mappers.map_beneficiary({"beneficiary_masked": "0899 ****", "bank_code": "VPB"}, [])
    assert b.holder_name == "Người nhận mới"
    assert b.account_age_days == 0


def test_chi_so_ops():
    summary = {"summary": {"suspicious_total": 25, "prevented_total": 8, "proceeded_after_intervene": 1}}
    decisions = [
        {"level": "intervene", "outcome": "prevented", "tx_snapshot": {"amount": 560000000.0}},
        {"level": "soft_warn", "outcome": None, "tx_snapshot": {"amount": 95000000.0}},
    ]
    m = mappers.map_metrics(summary, decisions)
    assert m.alerts_fired == 25
    assert m.scanned_today == 2
    assert m.cancel_rate_pct == 89           # 8 / (8+1)
    # Giá trị đã bảo vệ chỉ cộng các ca thực sự bị chặn.
    assert m.protected_value_vnd == 560_000_000


def test_gia_tri_da_bao_ve_khong_dem_ca_khach_van_chuyen():
    """Trên dữ liệu thật, cộng theo mức thay vì theo kết cục cho ra 24,2 tỷ
    trong khi tiền thật sự giữ lại được chỉ 4,1 tỷ."""
    summary = {"summary": {"suspicious_total": 3, "prevented_total": 1, "proceeded_after_intervene": 1}}
    decisions = [
        {"level": "intervene", "outcome": "prevented", "tx_snapshot": {"amount": 100_000_000}},
        {"level": "intervene", "outcome": "held", "tx_snapshot": {"amount": 50_000_000}},
        # Cảnh báo rồi nhưng khách vẫn chuyển: không bảo vệ được đồng nào.
        {"level": "intervene", "outcome": "proceeded", "tx_snapshot": {"amount": 900_000_000}},
        # Chưa xử lý xong thì chưa tính.
        {"level": "intervene", "outcome": None, "tx_snapshot": {"amount": 700_000_000}},
    ]
    assert mappers.map_metrics(summary, decisions).protected_value_vnd == 150_000_000


def test_chi_so_ops_khong_chia_cho_khong():
    m = mappers.map_metrics({"summary": {}}, [])
    assert m.cancel_rate_pct == 0


def test_dashboard_gom_theo_gio_va_kich_ban():
    decisions = [
        {"scenario_name": "Giả danh công an", "tx_snapshot": {"tx_time": "2026-09-16T17:39:42+07:00"}},
        {"scenario_name": "Giả danh công an", "tx_snapshot": {"tx_time": "2026-09-16T17:05:00+07:00"}},
        {"scenario_name": "Đầu tư ảo", "tx_snapshot": {"tx_time": "2026-09-16T09:00:00+07:00"}},
    ]
    d = mappers.map_dashboard(decisions, ["a", "b"])
    assert len(d.hourly_alerts) == 24
    theo_gio = {h.hour: h.count for h in d.hourly_alerts}
    assert theo_gio["17h"] == 2 and theo_gio["09h"] == 1 and theo_gio["03h"] == 0
    assert d.scenario_counts[0].name == "Giả danh công an"
    assert d.scenario_counts[0].count == 2


def test_dong_thoi_gian_su_kien_ket_thuc_bang_giao_dich_dang_giu():
    events = [
        {"event_id": 17, "event_type": "NEW_DEVICE_LOGIN", "event_time": "2026-04-26T09:57:00+07:00", "amount": None},
        {"event_id": 23, "event_type": "SAVINGS_CLOSED", "event_time": "2026-09-11T14:52:00+07:00", "amount": 520000000.0},
    ]
    t = mappers.map_event_timeline(events, 560_000_000)
    # Cũ trước, mới sau, và mốc cuối luôn là giao dịch đang bị giữ.
    assert t[0].label == "Đăng nhập từ thiết bị mới"
    assert t[1].label == "Tất toán sổ tiết kiệm trước hạn" and t[1].tone == "danger"
    assert "560.000.000" in t[-1].label and t[-1].tone == "danger"


def test_dong_thoi_gian_rong_thi_tra_rong():
    # Rỗng để phần gọi biết mà dùng dữ liệu tạm, thay vì hiện mỗi một mốc trơ trọi.
    assert mappers.map_event_timeline([], 1_000) == []


def test_lich_su_an_toan_rut_so_tien_tu_narrative():
    cases = [{
        "case_id": "CASE-2026-0006", "status": "OPEN", "scenario_id": "S09",
        "created_at": "2026-09-16T12:02:12+07:00",
        "narrative": "Giao dịch 95,000,000 VND có rủi ro cao (điểm 76/100).",
    }]
    h = mappers.map_safety_history(cases)
    assert h[0].amount == 95_000_000
    assert h[0].date == "2026-09-16"
    assert h[0].status == "processing"


# ---- Thống kê theo quý -------------------------------------------------------

# Chép từ GET /transactions/{id}/quarterly-summary
QUARTERLY = {
    "customer_id": 100008,
    "quarters": 2,
    "include_transfers": False,
    "excluded_categories": ["TRANSFER_P2P", "TRANSFER"],
    "summary": [
        {
            "period": "2026Q2", "year": 2026, "quarter": 2, "label": "Quý 2/2026",
            "income": 7600000, "expense": 4000000, "net": 3600000, "count": 13,
            "excluded_transfer_amount": 0,
            "by_category": [
                {"category": "FAMILY_SUPPORT", "amount": 3000000, "pct": 75, "rank": 1, "delta_vs_prev_pct": None},
                {"category": "FOOD", "amount": 1000000, "pct": 25, "rank": 2, "delta_vs_prev_pct": None},
            ],
        },
        {
            "period": "2026Q3", "year": 2026, "quarter": 3, "label": "Quý 3/2026",
            "income": 7600000, "expense": 5000000, "net": 2600000, "count": 18,
            "excluded_transfer_amount": 620000000,
            "by_category": [
                {"category": "FAMILY_SUPPORT", "amount": 3500000, "pct": 70, "rank": 1, "delta_vs_prev_pct": 16.7},
                {"category": "HEALTH", "amount": 1500000, "pct": 30, "rank": 2, "delta_vs_prev_pct": None},
            ],
        },
    ],
    "category_totals": [
        {"category": "FAMILY_SUPPORT", "amount": 6500000, "pct": 72},
        {"category": "FOOD", "amount": 1000000, "pct": 11},
        {"category": "HEALTH", "amount": 1500000, "pct": 17},
    ],
}


def test_quy_giu_nguyen_nhan_va_so_lieu_cua_domain():
    r = mappers.map_quarterly(QUARTERLY)
    assert [q.period for q in r.quarters] == ["2026Q2", "2026Q3"]
    assert r.quarters[1].label == "Quý 3/2026"
    assert r.quarters[1].expense == 5_000_000
    assert r.quarters[1].net == 2_600_000


def test_ma_nhom_duoc_gan_nhan_tieng_viet():
    r = mappers.map_quarterly(QUARTERLY)
    nhan = {c.category: c.label_vi for c in r.quarters[1].by_category}
    assert nhan["FAMILY_SUPPORT"] == "Hỗ trợ gia đình"
    assert nhan["HEALTH"] == "Y tế"


def test_ma_nhom_la_thay_van_hien_duoc():
    """Domain thêm nhóm mới thì màn hình vẫn đọc được, không hiện rỗng."""
    payload = {**QUARTERLY, "summary": [{
        **QUARTERLY["summary"][0],
        "by_category": [{"category": "PET_CARE", "amount": 100, "pct": 100, "rank": 1, "delta_vs_prev_pct": None}],
    }]}
    assert mappers.map_quarterly(payload).quarters[0].by_category[0].label_vi == "Pet care"


def test_chua_co_moc_so_sanh_thi_giu_null():
    """None và 0 khác nhau: chưa có mốc, chứ không phải không đổi."""
    r = mappers.map_quarterly(QUARTERLY)
    assert r.quarters[0].by_category[0].delta_vs_prev_pct is None
    assert r.quarters[1].by_category[0].delta_vs_prev_pct == 16.7


def test_gateway_khong_tinh_lai_ty_trong():
    """pct lấy nguyên của domain.

    Hai chỗ cùng tính một con số là hai chỗ có thể lệch nhau; số liệu trên màn
    hình phải khớp số liệu agent đọc được từ cùng endpoint.
    """
    r = mappers.map_quarterly(QUARTERLY)
    assert [c.pct for c in r.quarters[1].by_category] == [70, 30]
    assert [c.pct for c in r.category_totals] == [72, 11, 17]


def test_khong_co_quy_nao_thi_tra_none():
    # Để phần gọi biết mà dùng dữ liệu tạm, thay vì hiện màn hình trống.
    assert mappers.map_quarterly({"summary": [], "category_totals": []}) is None


# ---- Bốn màn vận hành phụ ----------------------------------------------------

# Chép từ GET /cases của action-feedback-service
CASE_ROWS = [
    {"case_id": "CASE-2026-0007", "decision_id": "cab99d05-c621-4552-b57c-f644ba1f67de",
     "customer_id": 100008, "scenario_id": "S05", "status": "OPEN",
     "narrative": "Giao dịch 95,000,000 VND toi nguoi nhan moi.",
     "created_at": "2026-09-15T09:41:20+07:00", "closed_at": None},
    {"case_id": "CASE-2026-0003", "decision_id": "aaa11111-2222-3333-4444-555566667777",
     "customer_id": 100008, "scenario_id": "S09", "status": "CLOSED_FRAUD",
     "narrative": "Khach huy giao dich sau canh bao.",
     "created_at": "2026-09-14T21:30:41+07:00", "closed_at": "2026-09-15T07:05:00+07:00"},
]

# Chép từ GET /scams của scam-knowledge-service
SCENARIO_ROWS = [
    {"scenario_id": "S09", "scenario_name": "Chiem quyen dieu khien thiet bi",
     "group_code": "G5", "pattern": "TAKEOVER", "agent_can_ask": "N",
     "advice_title": "Tam giu lenh", "advice_body": "Xac thuc lai bang sinh trac.",
     "recommended_action": "hold", "priority": 0},
    {"scenario_id": "S05", "scenario_name": "Mao danh co quan cong an",
     "group_code": "G1", "pattern": "DRAIN", "agent_can_ask": "Y",
     "advice_title": "Cong an khong yeu cau chuyen tien",
     "advice_body": "Co quan cong an khong lam viec qua dien thoai.",
     "recommended_action": "cancel", "priority": 1},
]

# Chép từ GET /info của risk-scoring-service
RISK_INFO = {
    "service": "risk-scoring-service",
    "factor_caps": {"amount_deviation": 25, "new_beneficiary": 20, "time_of_day": 10,
                    "behavior_drift": 20, "relationship_history": 15, "recent_context": 20},
    "levels": {"pass": "< 40", "soft_warn": "40 - 74", "intervene": ">= 75"},
}

# Chép từ GET /llm-traces và /llm-traces/stats của action-feedback-service
TRACE_ROWS = [
    {"trace_id": 1042, "decision_id": "cab99d05-c621-4552-b57c-f644ba1f67de",
     "agent": "shield_explain", "model": "z-ai/glm-5.2-hackathon", "status": "ok",
     "latency_ms": 1080, "created_at": "2026-09-15T09:41:03+07:00"},
    {"trace_id": 1041, "decision_id": None, "agent": "copilot",
     "model": "z-ai/glm-5.2-hackathon", "status": "timeout", "latency_ms": 6000,
     "created_at": "2026-09-15T09:38:50+07:00"},
]
TRACE_STATS = {
    "total_calls": 10,
    "fallback_rate": 0.2,
    "breakdown": [
        {"agent": "copilot", "status": "ok", "n": 6, "avg_latency_ms": 800},
        {"agent": "copilot", "status": "timeout", "n": 2, "avg_latency_ms": 6000},
        {"agent": "shield_explain", "status": "ok", "n": 2, "avg_latency_ms": 1100},
    ],
}


def test_case_moi_nhat_len_dau_va_co_nhan_tieng_viet():
    rows = mappers.map_ops_cases(CASE_ROWS, {100008: "NGUYEN THI B***"}, {"S05": "Mao danh"})
    assert [c.id for c in rows] == ["CASE-2026-0007", "CASE-2026-0003"]
    assert rows[0].status == "open" and rows[0].status_label == "Đang mở"
    assert rows[1].status == "closedFraud"
    assert rows[0].customer == "NGUYEN THI B***"


def test_case_khong_khop_kich_ban_van_co_chu_de_doc():
    # S09 không có trong bảng tên truyền vào: không để ô trống, cũng không ném
    # mã trần ra bảng cho chuyên viên đọc.
    rows = mappers.map_ops_cases(CASE_ROWS, {}, {"S05": "Mao danh"})
    assert rows[1].scenario_name == "Kịch bản S09"
    assert rows[0].customer == "—"


def test_kich_ban_sap_theo_do_uu_tien_va_gan_so_vu_hom_nay():
    from models import ScenarioCount
    rows = mappers.map_ops_scenarios(
        SCENARIO_ROWS, [ScenarioCount(name="Mao danh co quan cong an", count=48)]
    )
    # priority 0 là TAKEOVER — luôn đứng đầu danh sách.
    assert rows[0].id == "S09"
    assert rows[0].can_ask is False
    assert rows[0].pattern_label == "Chiếm quyền điều khiển thiết bị"
    assert rows[1].alerts_today == 48
    assert rows[1].action_label == "Khuyên huỷ giao dịch"


def test_nguong_rut_tu_chuoi_mo_ta_cua_engine():
    cfg = mappers.map_ops_model(RISK_INFO, ["Lịch sử giao dịch 12 tháng"])
    assert (cfg.soft_warn_min, cfg.intervene_min) == (40, 75)
    # Tổng trần điểm 6 yếu tố vượt 100 — engine cộng rồi mới chặn trên.
    assert cfg.max_score == 110
    assert len(cfg.factors) == 6
    assert all(f.label != f.key for f in cfg.factors)


def test_ty_le_du_phong_chi_dem_timeout_va_error():
    log = mappers.map_ops_audit(TRACE_ROWS, TRACE_STATS)
    assert log.total_calls == 10
    assert log.fallback_rate_pct == 20
    copilot = [a for a in log.per_agent if a.agent_label == "Trợ lý tài chính"][0]
    assert (copilot.calls, copilot.fallback_calls) == (8, 2)
    # cache không phải lỗi nên không tính vào tỷ lệ dự phòng.
    shield = [a for a in log.per_agent if a.agent_label.startswith("Scam Shield")][0]
    assert shield.fallback_calls == 0


def test_trace_khong_co_decision_thi_bo_han_field():
    log = mappers.map_ops_audit(TRACE_ROWS, TRACE_STATS)
    assert log.traces[0].decision_id == "cab99d05-c621-4552-b57c-f644ba1f67de"
    assert log.traces[1].decision_id is None
    assert log.traces[1].status_label.startswith("Quá hạn")


def test_do_tre_bo_qua_nhom_chua_do_duoc_thoi_gian():
    """avg_latency_ms = NULL nghĩa là chưa đo, không phải 0 ms.

    Cộng như 0 sẽ báo hệ thống nhanh gấp đôi thực tế — đúng con số mà buổi
    trình bày sẽ bị hỏi lại.
    """
    stats = {
        "total_calls": 4,
        "fallback_rate": 0.0,
        "breakdown": [
            {"agent": "copilot", "status": "ok", "n": 2, "avg_latency_ms": 1000},
            {"agent": "copilot", "status": "cache", "n": 2, "avg_latency_ms": None},
        ],
    }
    log = mappers.map_ops_audit([], stats)
    assert log.avg_latency_ms == 1000
    assert log.per_agent[0].calls == 4


def test_chua_co_ban_ghi_nao_thi_khong_bia_ra_mot_luot_goi():
    # Upstream trả total_calls = 1 khi bảng rỗng để khỏi chia cho 0.
    log = mappers.map_ops_audit([], {"total_calls": 1, "fallback_rate": 0.0, "breakdown": []})
    assert (log.total_calls, log.fallback_rate_pct, log.avg_latency_ms) == (0, 0, 0)


def test_trang_thai_la_vao_nhom_loi_va_nhan_di_kem_dung_nhom_do():
    log = mappers.map_ops_audit(
        [{"trace_id": 9, "agent": "copilot", "model": "m", "status": "weird",
          "latency_ms": 10, "created_at": "2026-09-15T09:00:00+07:00"}],
        {"total_calls": 1, "fallback_rate": 0.0, "breakdown": [{"agent": "copilot", "status": "ok", "n": 1, "avg_latency_ms": 10}]},
    )
    trace = log.traces[0]
    assert trace.status == "error"
    assert trace.status_label == mappers.TRACE_STATUS["error"]


# ---- Chi tiết case và dòng thời gian ------------------------------------------
#
# Ba endpoint này từng trả hằng số cho mọi case. Dữ liệu mẫu dưới đây chép theo
# đúng hình dạng bản ghi thật của risk_decision, guardian_case và llm_trace.

from datetime import datetime, timedelta, timezone

import catalog

VN = timezone(timedelta(hours=7))
NOW = datetime(2026, 9, 15, 10, 30, tzinfo=VN)

DECISION = {
    "decision_id": "cab99d05-c621-4552-b57c-f644ba1f67de",
    "customer_id": 100008,
    "tx_snapshot": {
        "amount": 95_000_000, "bank_code": "VCB", "memo_masked": "Nop tien xac minh",
        "tx_time": "2026-09-15T09:41:02+07:00",
        "session_flags": {"new_device": True, "on_call": True, "screen_sharing": False},
    },
    "score": 87, "level": "intervene", "scenario_id": "S05",
    "action_taken": None, "outcome": None,
    "created_at": "2026-09-15T09:41:02+07:00",
    "intervened_at": "2026-09-15T09:41:03+07:00",
    "actioned_at": None,
}

CASE_ROW = {
    "case_id": "CASE-2026-0007", "decision_id": DECISION["decision_id"],
    "customer_id": 100008, "scenario_id": "S05", "status": "OPEN",
    "narrative": "Giao dich 95,000,000 VND toi nguoi nhan moi.",
    "created_at": "2026-09-15T09:41:20+07:00", "closed_at": None,
}

CUSTOMER_ROW = {"customer_id": 100008, "name_masked": "NGUYEN THI B***", "persona": "SENIOR", "age": 68}

HISTORY = [
    DECISION,
    {"decision_id": "old-1", "level": "intervene", "score": 58,
     "tx_snapshot": {"amount": 10_000_000}, "created_at": "2026-08-30T10:00:00+07:00"},
    {"decision_id": "old-2", "level": "pass", "score": 12,
     "tx_snapshot": {"amount": 3_000_000}, "created_at": "2026-09-01T10:00:00+07:00"},
    # Ngoài cửa sổ 90 ngày: không được tính vào "cảnh báo gần đây".
    {"decision_id": "old-3", "level": "intervene", "score": 99,
     "tx_snapshot": {"amount": 1_000_000}, "created_at": "2026-01-01T10:00:00+07:00"},
]


def test_kenh_giao_dich_noi_ca_boi_canh_phien():
    channel = mappers.map_channel(DECISION["tx_snapshot"])
    # Cờ bật mới được nhắc; cờ tắt thì không.
    assert "thiết bị mới" in channel and "đang trong cuộc gọi" in channel
    assert "chia sẻ màn hình" not in channel


def test_chi_tiet_case_lay_so_that_cua_khach():
    detail = mappers.map_case_detail(DECISION, CUSTOMER_ROW, CASE_ROW, RISK_INFO,
                                     HISTORY, catalog.CASE_DETAIL, NOW)
    profile = detail.customer_profile
    # Chỉ đếm lượt có can thiệp, trong 90 ngày, và không đếm chính case đang mở.
    assert profile.recent_alerts_count == 1
    assert profile.recent_alerts_top_score == 58
    assert profile.segment == "Cao tuổi"
    assert profile.avg_transfer_vnd == round((95_000_000 + 10_000_000 + 3_000_000 + 1_000_000) / 4)


def test_chi_tiet_case_lay_nguong_tu_engine_khong_chep_tay():
    detail = mappers.map_case_detail(DECISION, CUSTOMER_ROW, CASE_ROW, RISK_INFO,
                                     HISTORY, catalog.CASE_DETAIL, NOW)
    assert (detail.model.soft_warn_min, detail.model.soft_warn_max) == (40, 74)
    assert detail.model.intervene_threshold == 75


def test_sla_dem_nguoc_tu_luc_mo_case_va_case_dong_thi_het_han():
    detail = mappers.map_case_detail(DECISION, CUSTOMER_ROW, CASE_ROW, RISK_INFO,
                                     HISTORY, catalog.CASE_DETAIL, NOW)
    # Mở 09:41, hiện 10:30 → đã dùng 49 phút trong 60.
    assert detail.transaction.sla_minutes == 11
    closed = {**CASE_ROW, "status": "CLOSED_FRAUD", "closed_at": "2026-09-15T10:00:00+07:00"}
    done = mappers.map_case_detail(DECISION, CUSTOMER_ROW, closed, RISK_INFO,
                                   HISTORY, catalog.CASE_DETAIL, NOW)
    assert done.transaction.sla_minutes == 0
    assert done.transaction.hold_status == "Đã chặn — xác nhận lừa đảo"


def test_dong_thoi_gian_dung_theo_moc_that():
    traces = [
        {"trace_id": 1, "agent": "shield_explain", "status": "ok", "latency_ms": 1080,
         "created_at": "2026-09-15T09:41:04+07:00"},
        {"trace_id": 2, "agent": "copilot", "status": "timeout", "latency_ms": 6000,
         "created_at": "2026-09-15T09:41:10+07:00"},
    ]
    steps = mappers.map_case_timeline(DECISION, CASE_ROW, traces)
    labels = [s.label for s in steps]
    assert labels[0].startswith("Khách hàng khởi tạo lệnh chuyển 95.000.000")
    assert any("87/100" in l and "can thiệp" in l for l in labels)
    assert any("đã dùng bản dự phòng" in l for l in labels)
    assert any("Mở case CASE-2026-0007" in l for l in labels)
    # Chưa xử lý xong nên bước cuối là mốc chưa hoàn thành.
    assert steps[-1].done is False and steps[-1].label == "Chờ quyết định xử lý"
    assert [s.time for s in steps[:-1]] == sorted(s.time for s in steps[:-1])


def test_case_da_xu_ly_thi_khong_con_buoc_cho():
    done_decision = {**DECISION, "action_taken": "cancel",
                     "actioned_at": "2026-09-15T09:45:00+07:00"}
    closed_case = {**CASE_ROW, "status": "CLOSED_FRAUD", "closed_at": "2026-09-15T10:00:00+07:00"}
    steps = mappers.map_case_timeline(done_decision, closed_case, [])
    assert all(s.done for s in steps)
    assert steps[-1].label.startswith("Đóng case")
    assert any("Khách hàng huỷ giao dịch" == s.label for s in steps)


def test_tinh_trang_he_thong_noi_dung_service_nao_chet():
    rows = mappers.map_system_status({"Risk Engine": True, "Tri thức lừa đảo": False}, False)
    by_label = {r.label: r for r in rows}
    assert by_label["Risk Engine"].tone == "ok"
    assert by_label["Tri thức lừa đảo"].tone == "danger"
    # Chưa cấu hình agent là cảnh báo, không phải lỗi — demo vẫn chạy được.
    assert by_label["Trợ lý AI"].tone == "warn"


def test_trang_thai_case_dong_khong_con_bi_hieu_la_cho_xu_ly():
    # Bảng cũ dùng CONFIRMED_FRAUD/DISMISSED, không phải giá trị thật của cột,
    # nên mọi case đã đóng đều hiện "chờ xử lý".
    assert mappers.CASE_STATUS_MAP["CLOSED_FRAUD"] == "confirmed"
    assert mappers.CASE_STATUS_MAP["CLOSED_LEGIT"] == "dismissed"
    assert mappers.CASE_STATUS_MAP["CALLBACK_DONE"] == "investigating"


def test_case_con_mo_thi_van_con_buoc_cho_du_khach_da_hanh_dong():
    """Bắt được nhờ chạy trên case thật CASE-2026-0006: hệ thống đã tạm giữ lệnh
    (action_taken=hold) nhưng case vẫn OPEN, tức vẫn chờ chuyên viên quyết định."""
    held = {**DECISION, "action_taken": "hold", "actioned_at": "2026-09-15T09:42:00+07:00"}
    steps = mappers.map_case_timeline(held, CASE_ROW, [])
    assert steps[-1].label == "Chờ quyết định xử lý"
    assert steps[-1].done is False
