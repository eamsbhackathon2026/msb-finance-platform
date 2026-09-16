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
        {"level": "intervene", "tx_snapshot": {"amount": 560000000.0}},
        {"level": "soft_warn", "tx_snapshot": {"amount": 95000000.0}},
    ]
    m = mappers.map_metrics(summary, decisions)
    assert m.alerts_fired == 25
    assert m.scanned_today == 2
    assert m.cancel_rate_pct == 89           # 8 / (8+1)
    # Giá trị đã bảo vệ chỉ cộng các ca thực sự bị chặn.
    assert m.protected_value_vnd == 560_000_000


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
