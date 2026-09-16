"""Test cho transaction-service."""
import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)


def tx(amount, direction="OUT", date="20260815", time="143000", **kw):
    row = {
        "transaction_id": kw.get("tid", 1), "customer_id": 100001, "account_id": 5001,
        "direction": direction, "amount": str(amount), "balance_after": "50000000",
        "currency": "VND", "beneficiary_id": kw.get("ben", 300001),
        "beneficiary_bank_code": "VCB", "beneficiary_account_masked": "0301 ****",
        "transaction_type": "FT", "category": kw.get("cat", "FOOD"),
        "transaction_description": kw.get("desc", "THANH TOAN"), "channel": "MOBILE",
        "transaction_date": date, "transaction_time": time, "status": "POSTED",
        "risk_decision_id": None, "is_fraud": "Y", "fraud_case_id": "F01",
    }
    return row


# ---------------------------------------------------------------------------
# Cột kiểm thử nội bộ không được rò ra API
# ---------------------------------------------------------------------------
def test_payload_loai_bo_co_gian_lan():
    """is_fraud và fraud_case_id là đáp án của bộ kiểm thử. Lọt ra API thì vừa lộ
    nhãn cho bên ngoài, vừa có nguy cơ chui vào prompt LLM."""
    out = main._tx_payload(tx(1_000_000))
    assert "is_fraud" not in out and "fraud_case_id" not in out


def test_payload_mask_sdt_trong_noi_dung_ck():
    out = main._tx_payload(tx(1_000_000, desc="CHUYEN TIEN LH 0912345678"))
    assert "0912345678" not in out["description"]


def test_payload_cast_tien_tu_varchar_sang_so():
    """Cột tiền để varchar theo core T24; API phải trả số để agent tính được."""
    out = main._tx_payload(tx(1_500_000))
    assert out["amount"] == 1_500_000.0
    assert isinstance(out["amount"], float)


def test_signed_amount_theo_chieu_tien():
    assert main._tx_payload(tx(500_000, "OUT"))["signed_amount"] == -500_000
    assert main._tx_payload(tx(500_000, "IN"))["signed_amount"] == 500_000


# ---------------------------------------------------------------------------
# Thống kê
# ---------------------------------------------------------------------------
def test_percentile_noi_suy_tuyen_tinh():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert main._percentile(vals, 0.0) == 1
    assert main._percentile(vals, 1.0) == 10
    assert main._percentile(vals, 0.5) == pytest.approx(5.5)


def test_percentile_chiu_duoc_danh_sach_rong_va_mot_phan_tu():
    assert main._percentile([], 0.9) == 0.0
    assert main._percentile([42], 0.9) == 42


def test_monthly_summary_tach_thu_chi_va_xep_hang_nhom(monkeypatch):
    rows = (
        [tx(20_000_000, "IN", "20260805", cat="OTHER")]
        + [tx(3_000_000, "OUT", "20260810", cat="SHOPPING")] * 2
        + [tx(500_000, "OUT", "20260812", cat="FOOD")] * 4
    )
    monkeypatch.setattr(main, "query", lambda *a, **k: rows)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    body = client.get("/transactions/100001/monthly-summary").json()
    thang = body["summary"][0]
    assert thang["income"] == 20_000_000
    assert thang["expense"] == 8_000_000
    assert thang["net"] == 12_000_000
    assert thang["top_categories"][0] == {
        "category": "SHOPPING", "amount": 6_000_000, "rank": 1}


def test_du_bao_loai_thang_cut(monkeypatch):
    """Tháng đầu cửa sổ bị cắt giữa chừng và tháng hiện tại chưa hết; tính cả hai
    vào trung bình có thể biến một khách đang dư tiền thành dự báo âm."""
    ky = main.now_vn().strftime("%Y%m")
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "monthly_summary", lambda cid, months=6: {"summary": [
        {"period": "202604", "month": "2026-04", "income": 5, "expense": 90, "net": -85,
         "count": 1, "top_categories": []},
        {"period": "202605", "month": "2026-05", "income": 30, "expense": 20, "net": 10,
         "count": 1, "top_categories": []},
        {"period": "202606", "month": "2026-06", "income": 30, "expense": 18, "net": 12,
         "count": 1, "top_categories": []},
        {"period": "202607", "month": "2026-07", "income": 30, "expense": 22, "net": 8,
         "count": 1, "top_categories": []},
        {"period": ky, "month": "hien-tai", "income": 2, "expense": 70, "net": -68,
         "count": 1, "top_categories": []},
    ]})
    body = client.get("/transactions/100001/cashflow-forecast").json()
    assert body["avg_monthly_net"] > 0, "tháng cụt vẫn đang kéo dự báo xuống âm"
    assert "2026-04" in body["months_excluded_incomplete"]
    assert "hien-tai" in body["months_excluded_incomplete"]
    assert body["months_used"] == ["2026-05", "2026-06", "2026-07"]


def test_baseline_metrics_bao_loi_khi_khong_du_du_lieu(monkeypatch):
    monkeypatch.setattr(main, "query", lambda *a, **k: [])
    assert client.get("/transactions/100001/baseline-metrics").status_code == 404


def test_baseline_metrics_tra_du_18_chi_so(monkeypatch):
    rows = [tx(1_000_000 * i, date=f"202608{10+i:02d}", time=f"{8+i:02d}3000",
               tid=i, ben=300000 + (i % 3)) for i in range(1, 12)]
    monkeypatch.setattr(main, "query", lambda sql, p=None: rows if "OUT" in sql else [])
    body = client.get("/transactions/100001/baseline-metrics").json()
    m = body["metrics"]
    for col in ("out_median", "out_p90", "out_p99", "out_max", "monthly_out_avg",
                "monthly_in_avg", "known_beneficiaries", "new_benef_per_30d",
                "share_to_new_benef", "active_hours", "night_tx_ratio",
                "weekend_tx_ratio", "tx_per_week", "max_tx_per_day",
                "max_cum_to_one_benef_14d", "balance_median", "max_drain_ratio_90d"):
        assert col in m, f"thiếu chỉ số {col}"
    assert len(m["active_hours"]) == 24
    assert m["out_max"] >= m["out_p99"] >= m["out_p90"] >= m["out_median"]


def test_health_va_agent_tools():
    assert client.get("/health").json()["status"] == "ok"
    names = {t["name"] for t in client.get("/agent/tools").json()["tools"]}
    assert {"get_monthly_summary", "get_baseline_metrics", "generate_recommendations"} <= names


def test_openapi_hop_le():
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "transaction-service"
    assert {"transaction", "insight", "product", "baseline"} <= {
        t["name"] for t in spec["tags"]}


# ---------------------------------------------------------------------------
# Tổng hợp theo quý
# ---------------------------------------------------------------------------
def quarterly(monkeypatch, rows, **params):
    monkeypatch.setattr(main, "query", lambda *a, **k: rows)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    r = client.get("/transactions/100001/quarterly-summary", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_gom_dung_thang_vao_quy(monkeypatch):
    # Ranh giới quý: tháng 3 thuộc Q1, tháng 4 thuộc Q2, tháng 12 thuộc Q4.
    rows = [
        tx(1_000_000, date="20260331"), tx(2_000_000, date="20260401"),
        tx(3_000_000, date="20261231"),
    ]
    d = quarterly(monkeypatch, rows, quarters=20)
    ky = {q["period"]: q["expense"] for q in d["summary"]}
    assert ky["2026Q1"] == 1_000_000
    assert ky["2026Q2"] == 2_000_000
    assert ky["2026Q4"] == 3_000_000


def test_nhan_quy_doc_duoc(monkeypatch):
    d = quarterly(monkeypatch, [tx(1_000_000, date="20260715")])
    q = d["summary"][-1]
    assert q["period"] == "2026Q3" and q["label"] == "Quý 3/2026"
    assert q["year"] == 2026 and q["quarter"] == 3


def test_chuyen_khoan_khong_tinh_la_chi_tieu(monkeypatch):
    """Chuyển khoản là tiền đổi chỗ, không phải tiền tiêu mất.

    Gộp chung thì một lệnh chuyển lớn nuốt trọn biểu đồ nhóm chi tiêu.
    """
    rows = [tx(1_000_000, date="20260715", cat="FOOD"),
            tx(500_000_000, date="20260715", cat="TRANSFER_P2P")]
    d = quarterly(monkeypatch, rows)
    q = d["summary"][-1]
    assert q["expense"] == 1_000_000
    # Nhưng vẫn cho biết đã bỏ qua bao nhiêu, để số liệu đối chiếu được sao kê.
    assert q["excluded_transfer_amount"] == 500_000_000
    assert [c["category"] for c in q["by_category"]] == ["FOOD"]


def test_co_the_yeu_cau_tinh_ca_chuyen_khoan(monkeypatch):
    rows = [tx(1_000_000, date="20260715", cat="FOOD"),
            tx(500_000_000, date="20260715", cat="TRANSFER_P2P")]
    d = quarterly(monkeypatch, rows, include_transfers="true")
    assert d["summary"][-1]["expense"] == 501_000_000
    assert d["excluded_categories"] == []


def test_ty_trong_nhom_cong_lai_bang_100(monkeypatch):
    rows = [tx(6_000_000, date="20260715", cat="FOOD"),
            tx(3_000_000, date="20260716", cat="BILLS"),
            tx(1_000_000, date="20260717", cat="HEALTH")]
    q = quarterly(monkeypatch, rows)["summary"][-1]
    assert [c["pct"] for c in q["by_category"]] == [60, 30, 10]
    assert [c["rank"] for c in q["by_category"]] == [1, 2, 3]


def test_thay_doi_so_voi_quy_truoc(monkeypatch):
    rows = [tx(2_000_000, date="20260415", cat="FOOD"),
            tx(3_000_000, date="20260715", cat="FOOD")]
    d = quarterly(monkeypatch, rows)
    q3 = [q for q in d["summary"] if q["period"] == "2026Q3"][0]
    assert q3["by_category"][0]["delta_vs_prev_pct"] == 50.0


def test_quy_dau_tien_khong_co_moc_so_sanh(monkeypatch):
    """Chưa có mốc thì trả null, không trả 0 như thể không đổi."""
    d = quarterly(monkeypatch, [tx(2_000_000, date="20260415", cat="FOOD")])
    assert d["summary"][0]["by_category"][0]["delta_vs_prev_pct"] is None


def test_nhom_moi_xuat_hien_cung_khong_co_moc(monkeypatch):
    rows = [tx(2_000_000, date="20260415", cat="FOOD"),
            tx(1_000_000, date="20260715", cat="HEALTH")]
    d = quarterly(monkeypatch, rows)
    q3 = [q for q in d["summary"] if q["period"] == "2026Q3"][0]
    assert q3["by_category"][0]["category"] == "HEALTH"
    assert q3["by_category"][0]["delta_vs_prev_pct"] is None


def test_tien_vao_khong_bi_tinh_thanh_chi_tieu(monkeypatch):
    rows = [tx(20_000_000, direction="IN", date="20260715", cat="OTHER"),
            tx(1_000_000, date="20260715", cat="FOOD")]
    q = quarterly(monkeypatch, rows)["summary"][-1]
    assert q["income"] == 20_000_000
    assert q["expense"] == 1_000_000
    assert q["net"] == 19_000_000
    assert [c["category"] for c in q["by_category"]] == ["FOOD"]


def test_tong_ca_ky_theo_nhom(monkeypatch):
    rows = [tx(2_000_000, date="20260415", cat="FOOD"),
            tx(3_000_000, date="20260715", cat="FOOD"),
            tx(1_000_000, date="20260716", cat="BILLS")]
    d = quarterly(monkeypatch, rows)
    tong = {c["category"]: c["amount"] for c in d["category_totals"]}
    assert tong["FOOD"] == 5_000_000 and tong["BILLS"] == 1_000_000
    assert d["category_totals"][0]["category"] == "FOOD"


def test_gioi_han_so_quy_tra_ve(monkeypatch):
    rows = [tx(1_000_000, date=f"2026{m:02d}15") for m in (1, 4, 7)]
    d = quarterly(monkeypatch, rows, quarters=2)
    assert d["quarters"] == 2
    assert [q["period"] for q in d["summary"]] == ["2026Q2", "2026Q3"]


def test_ngay_hong_dinh_dang_bi_bo_qua_khong_lam_vo(monkeypatch):
    rows = [tx(1_000_000, date="20260715"), tx(9_000_000, date="")]
    q = quarterly(monkeypatch, rows)["summary"][-1]
    assert q["expense"] == 1_000_000


def test_quarterly_co_trong_manifest_agent():
    tools = client.get("/agent/tools").json()["tools"]
    assert any(t["name"] == "get_quarterly_summary" for t in tools)


# ---------------------------------------------------------------------------
# So sánh theo tháng (monthly-comparison)
# ---------------------------------------------------------------------------
def monthly_cmp(monkeypatch, rows, **params):
    monkeypatch.setattr(main, "query", lambda *a, **k: rows)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    r = client.get("/transactions/100001/monthly-comparison", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_mc_gom_dung_thang(monkeypatch):
    rows = [tx(1_000_000, date="20260615"), tx(2_000_000, date="20260715"),
            tx(3_000_000, date="20260815")]
    d = monthly_cmp(monkeypatch, rows, months=24)
    ky = {m["period"]: m["expense"] for m in d["summary"]}
    assert ky == {"202606": 1_000_000, "202607": 2_000_000, "202608": 3_000_000}


def test_mc_nhan_thang_doc_duoc(monkeypatch):
    m = monthly_cmp(monkeypatch, [tx(1_000_000, date="20260815")])["summary"][-1]
    assert m["period"] == "202608" and m["label"] == "Tháng 8/2026"
    assert m["year"] == 2026 and m["month"] == 8


def test_mc_delta_tong_chi_so_thang_truoc(monkeypatch):
    # Tổng chi tháng 8 (3tr) so tháng 7 (2tr) = +50%; nhóm FOOD cũng +50%.
    rows = [tx(2_000_000, date="20260715", cat="FOOD"),
            tx(3_000_000, date="20260815", cat="FOOD")]
    d = monthly_cmp(monkeypatch, rows)
    m8 = [m for m in d["summary"] if m["period"] == "202608"][0]
    assert m8["delta_vs_prev_pct"] == 50.0
    assert m8["by_category"][0]["delta_vs_prev_pct"] == 50.0


def test_mc_thang_dau_khong_co_moc(monkeypatch):
    d = monthly_cmp(monkeypatch, [tx(2_000_000, date="20260715", cat="FOOD")])
    assert d["summary"][0]["delta_vs_prev_pct"] is None
    assert d["summary"][0]["by_category"][0]["delta_vs_prev_pct"] is None


def test_mc_loai_chuyen_khoan_khoi_chi_tieu(monkeypatch):
    rows = [tx(1_000_000, date="20260815", cat="FOOD"),
            tx(9_000_000, date="20260815", cat="TRANSFER_P2P")]
    m = monthly_cmp(monkeypatch, rows)["summary"][-1]
    assert m["expense"] == 1_000_000
    assert m["excluded_transfer_amount"] == 9_000_000
    assert [c["category"] for c in m["by_category"]] == ["FOOD"]


def test_mc_ty_trong_cong_100_va_xep_hang(monkeypatch):
    rows = [tx(6_000_000, date="20260815", cat="FOOD"),
            tx(3_000_000, date="20260816", cat="BILLS"),
            tx(1_000_000, date="20260817", cat="HEALTH")]
    m = monthly_cmp(monkeypatch, rows)["summary"][-1]
    assert [c["pct"] for c in m["by_category"]] == [60, 30, 10]
    assert [c["rank"] for c in m["by_category"]] == [1, 2, 3]


def test_mc_agent_tool_dang_ky(monkeypatch):
    names = {t["name"] for t in client.get("/agent/tools").json()["tools"]}
    assert "get_monthly_comparison" in names
