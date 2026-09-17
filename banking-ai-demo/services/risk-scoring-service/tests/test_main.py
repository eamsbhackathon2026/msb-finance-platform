"""Test cho risk-scoring-service — trọng tâm là engine 6 yếu tố.

Các hàm chấm yếu tố là hàm thuần, không chạm DB hay service khác, nên test được
trực tiếp. Đây cũng chính là lý do engine được viết tất định: nếu điểm số phụ thuộc
vào LLM thì không có cách nào khẳng định hôm nay và ngày mai nó chấm giống nhau.
"""
from datetime import datetime, timedelta, timezone

import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)
VN = timezone(timedelta(hours=7))

# Baseline của một khách hàng làm công ăn lương điển hình
BASELINE = {
    "out_median": 300_000, "out_p90": 4_000_000, "out_p99": 6_000_000,
    "out_max": 12_000_000, "max_drain_ratio_90d": 0.35,
    "night_tx_ratio": 0.05, "max_cum_to_one_benef_14d": 8_000_000,
}
QUEN = {
    "known": True, "is_new": False, "age_days": 400, "tx_count": 30,
    "relationship": "FAMILY", "total_in": 0, "total_out": 50_000_000, "status": "ACTIVE",
}
MOI = {
    "known": False, "is_new": True, "age_days": 0, "tx_count": 0,
    "relationship": "UNKNOWN", "total_in": 0, "total_out": 0, "status": "ACTIVE",
}
TRUA = datetime(2026, 9, 15, 14, 30, tzinfo=VN)
DEM = datetime(2026, 9, 15, 2, 15, tzinfo=VN)


def cham(amount, when=TRUA, baseline=BASELINE, benef=QUEN, balance=50_000_000,
         events=None, flags=None):
    return main.score_transfer(amount, when, baseline, benef, balance,
                               events or [], flags or {})


# ---------------------------------------------------------------------------
# Ngưỡng phân mức
# ---------------------------------------------------------------------------
def test_giao_dich_binh_thuong_cho_di_qua():
    r = cham(2_000_000)
    assert r["level"] == "pass"
    assert r["score"] < main.LEVEL_SOFT_WARN


def test_nguong_phan_muc_dung_theo_thiet_ke():
    assert main.LEVEL_SOFT_WARN == 40 and main.LEVEL_INTERVENE == 75


def test_diem_khong_bao_gio_vuot_100():
    """Tổng trần 6 yếu tố là 110 nên điểm thô phải bị chặn ở 100."""
    r = cham(900_000_000, when=DEM, benef=MOI, balance=1_000_000,
             events=[{"event_type": "SAVINGS_CLOSED"}, {"event_type": "NEW_DEVICE_LOGIN"}],
             flags={"screen_sharing": True, "remote_app": True, "on_call": True,
                    "new_device": True, "accessibility_service": True})
    assert r["score"] == 100
    assert r["level"] == "intervene"


# ---------------------------------------------------------------------------
# Từng yếu tố
# ---------------------------------------------------------------------------
def test_nguoi_nhan_moi_bi_cham_diem_toi_da():
    f = main._factor_new_beneficiary(MOI)
    assert f["score"] == main.FACTOR_CAPS["new_beneficiary"]


def test_nguoi_nhan_quen_thuoc_khong_bi_tru_diem():
    assert main._factor_new_beneficiary(QUEN)["score"] == 0


def test_giao_dich_dem_chi_dang_ngo_voi_nguoi_khong_quen_giao_dich_dem():
    ban_ngay = main._factor_time_of_day(DEM, BASELINE)
    cu_dem = main._factor_time_of_day(DEM, {**BASELINE, "night_tx_ratio": 0.3})
    assert ban_ngay["score"] == main.FACTOR_CAPS["time_of_day"]
    assert cu_dem["score"] < ban_ngay["score"], (
        "người vốn hay giao dịch đêm không nên bị phạt như người không bao giờ làm vậy"
    )


def test_ty_le_vet_bi_chan_tran_khi_vuot_so_du():
    """Số tiền vượt số dư từng cho ra 'vét 1953% số dư' — vô nghĩa với khách."""
    f = main._factor_amount_deviation(560_000_000, BASELINE, balance=28_000_000)
    assert f["ratio"] is not None and f["ratio"] <= 1.0
    assert "vượt quá số dư" in f["detail"]


def test_vuot_nhieu_lan_p99_nang_hon_vuot_nhe():
    nhe = main._factor_amount_deviation(13_000_000, BASELINE, balance=100_000_000)
    nang = main._factor_amount_deviation(60_000_000, BASELINE, balance=100_000_000)
    assert nang["score"] > nhe["score"]


def test_co_chiem_quyen_thiet_bi_lam_lech_hanh_vi():
    f = main._factor_behavior_drift(1_000_000, BASELINE, QUEN,
                                    {"screen_sharing": True, "remote_app": True})
    assert f["score"] >= 12


def test_nguoi_nhan_bi_nghi_ngo_cham_diem_toi_da():
    f = main._factor_relationship_history({**QUEN, "status": "SUSPECTED"}, 1_000_000)
    assert f["score"] == main.FACTOR_CAPS["relationship_history"]


def test_tien_moi_bi_phat_hien():
    """Nhận 5 triệu rồi chuyển đi 100 triệu là mẫu tiền mồi của bẫy đầu tư."""
    f = main._factor_relationship_history(
        {**MOI, "known": True, "total_in": 5_000_000}, 100_000_000)
    assert f["score"] > 0 and "mồi" in f["detail"]


def test_ngu_canh_gan_day_lay_su_kien_nang_nhat_khong_cong_don():
    f = main._factor_recent_context(
        [{"event_type": "SAVINGS_CLOSED"}, {"event_type": "INBOUND_UNKNOWN"}], TRUA)
    assert f["score"] <= main.FACTOR_CAPS["recent_context"]
    assert "SAVINGS_CLOSED" in f["events"]


def test_khong_co_su_kien_thi_khong_cong_diem():
    assert main._factor_recent_context([], TRUA)["score"] == 0


# ---------------------------------------------------------------------------
# Kịch bản kinh điển: tất toán sổ rồi chuyển cho người lạ
# ---------------------------------------------------------------------------
def test_tat_toan_so_roi_chuyen_cho_nguoi_la_phai_can_thiep():
    r = cham(500_000_000, benef=MOI, balance=520_000_000,
             events=[{"event_type": "SAVINGS_CLOSED"}], flags={"on_call": True})
    assert r["level"] == "intervene"
    assert "recent_context" in r["top_factors"]


def test_cung_so_tien_nhung_nguoi_nhan_quen_thi_khong_can_thiep():
    """Đối chứng: chặn nhầm một giao dịch hợp lệ tệ hơn là bỏ lọt một vụ lừa đảo."""
    r = cham(500_000_000, benef=QUEN, balance=520_000_000, baseline={
        **BASELINE, "out_p99": 400_000_000, "out_max": 600_000_000,
        "max_drain_ratio_90d": 0.95,
    })
    assert r["level"] != "intervene"


def test_khong_co_baseline_thi_cham_diem_trung_tinh():
    """Khách mới chưa có lịch sử không được mặc định coi là rủi ro cao."""
    f = main._factor_amount_deviation(50_000_000, None, 100_000_000)
    assert 0 < f["score"] < main.FACTOR_CAPS["amount_deviation"]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
def test_health_khong_cham_database():
    assert client.get("/health").json()["status"] == "ok"


def test_endpoint_cu_van_hoat_dong():
    """Hợp đồng /risk-score cũ phải giữ nguyên để client cũ không gãy."""
    r = client.post("/risk-score", json={
        "customer_id": "C001", "amount": 50_000_000,
        "new_beneficiary": True, "unusual_time": True, "geo_anomaly": True,
    }).json()
    assert r["score"] == 75 and r["level"] == "HIGH"
    assert {f["name"] for f in r["factors"]} == {
        "new_beneficiary", "unusual_time", "geo_anomaly"}


def test_agent_tools_va_openapi():
    names = {t["name"] for t in client.get("/agent/tools").json()["tools"]}
    assert {"precheck_transfer", "record_intervention", "take_action"} <= names
    spec = client.get("/openapi.json").json()
    assert "/transfer/precheck" in spec["paths"]


def test_info_cong_bo_trong_so_va_nguong():
    body = client.get("/info").json()
    assert body["factor_caps"] == main.FACTOR_CAPS
    assert set(body["levels"]) == {"pass", "soft_warn", "intervene"}


# ---------------------------------------------------------------------------
# Cổng kiểm tra tồn tại
# ---------------------------------------------------------------------------
def test_chot_hanh_dong_voi_giao_dich_khong_ton_tai_thanh_404(monkeypatch):
    """`risk_decision.transaction_id` là khóa ngoại; để Postgres từ chối thì
    trợ lý chỉ nhận được 500 và không biết là mình truyền sai số giao dịch."""
    def _q(sql, params=None, *a, **k):
        if "SELECT 1 AS x FROM transaction_history" in sql:
            return None
        return {"decision_id": "11111111-2222-3333-4444-555555555555"}
    monkeypatch.setattr(main, "query_one", _q)
    r = client.post("/transfer/action", json={
        "decision_id": "11111111-2222-3333-4444-555555555555",
        "action_taken": "hold", "transaction_id": 999999})
    assert r.status_code == 404
    assert r.json()["detail"] == "transaction 999999 không tồn tại"


def test_chot_hanh_dong_khong_kem_giao_dich_van_di_qua(monkeypatch):
    """`transaction_id` cho phép NULL — cổng không được biến nó thành bắt buộc."""
    goi = {"n": 0}

    def _q(sql, params=None, *a, **k):
        assert "FROM transaction_history" not in sql
        goi["n"] += 1
        return {"decision_id": "11111111-2222-3333-4444-555555555555"}
    monkeypatch.setattr(main, "query_one", _q)
    monkeypatch.setattr(main, "execute_returning", lambda *a, **k: {
        "decision_id": "11111111-2222-3333-4444-555555555555",
        "action_taken": "cancel", "outcome": "prevented",
        "score": 80, "level": "intervene"})
    r = client.post("/transfer/action", json={
        "decision_id": "11111111-2222-3333-4444-555555555555",
        "action_taken": "cancel"})
    assert r.status_code == 200
    assert goi["n"] >= 1
