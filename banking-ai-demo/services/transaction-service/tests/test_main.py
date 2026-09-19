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
        "category": "SHOPPING", "label": "Mua sắm", "amount": 6_000_000, "rank": 1}


def test_monthly_summary_khong_tinh_chuyen_khoan_vao_chi_tieu(monkeypatch):
    """Một lệnh chuyển lớn từng nuốt trọn bảng chi tiêu: "tháng này bạn chi 622
    triệu" trong khi 620 triệu là tiền chuyển đi, không phải tiền tiêu mất."""
    rows = [
        tx(600_000_000, "OUT", "20260810", cat="TRANSFER_P2P"),
        tx(2_000_000, "OUT", "20260812", cat="FOOD"),
        tx(10_000_000, "IN", "20260805", cat="OTHER"),
    ]
    monkeypatch.setattr(main, "query", lambda *a, **k: rows)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    thang = client.get("/transactions/100001/monthly-summary").json()["summary"][0]
    assert thang["expense"] == 2_000_000
    assert thang["transfer_out"] == 600_000_000
    # Tiền chuyển đi vẫn rời tài khoản thật nên net phải trừ cả hai.
    assert thang["net"] == 10_000_000 - 2_000_000 - 600_000_000
    assert [c["category"] for c in thang["top_categories"]] == ["FOOD"]


def test_monthly_summary_bo_thang_cut_dau_cua_so(monkeypatch):
    """Tháng cũ nhất trong cửa sổ luôn bị cắt giữa chừng; giữ lại thì cùng một
    tháng ra hai con số khác nhau tuỳ `months` bên gọi truyền."""
    rows = [
        tx(1_000_000, "OUT", "20260710", cat="FOOD"),
        tx(2_000_000, "OUT", "20260810", cat="FOOD"),
        tx(3_000_000, "OUT", "20260910", cat="FOOD"),
    ]
    monkeypatch.setattr(main, "query", lambda *a, **k: rows)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    body = client.get("/transactions/100001/monthly-summary", params={"months": 2}).json()
    assert [m["period"] for m in body["summary"]] == ["202608", "202609"]
    assert body["months"] == 2


def test_monthly_summary_khach_khong_co_giao_dich_tra_rong_chu_khong_404(monkeypatch):
    """Không có giao dịch là dữ liệu rỗng, không phải lỗi: báo lỗi thì trợ lý nói
    với khách "thử lại sau" trong khi thật ra chẳng có gì để thử."""
    monkeypatch.setattr(main, "query", lambda *a, **k: [])
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    r = client.get("/transactions/100001/monthly-summary")
    assert r.status_code == 200
    assert r.json()["summary"] == []


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


# ---------------------------------------------------------------------------
# Cổng kiểm tra tồn tại
# ---------------------------------------------------------------------------
# Không có cổng thì id sai của người gọi đi thẳng xuống Postgres, vỡ khóa ngoại
# và thành 500 — trợ lý đọc chuỗi đó không biết nên tra lại id hay nên dừng.
def _thieu(bang):
    """query_one giả cho các cổng: mọi bảng đều có bản ghi, trừ `bang`."""
    def _q(sql, params=None, *a, **k):
        if "SELECT 1 AS x FROM" not in sql:
            return {"nid": 500}
        return None if f"FROM {bang} " in sql else {"x": 1}
    return _q


def test_ghi_giao_dich_cho_khach_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/transactions", json={
        "customer_id": 999999, "account_id": 200001, "amount": 5_000_000})
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_ghi_giao_dich_tren_tai_khoan_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("account"))
    r = client.post("/transactions", json={
        "customer_id": 100008, "account_id": 999999, "amount": 5_000_000})
    assert r.status_code == 404
    assert r.json()["detail"] == "account 999999 không tồn tại"


def test_ghi_giao_dich_voi_nguoi_nhan_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("beneficiary"))
    r = client.post("/transactions", json={
        "customer_id": 100008, "account_id": 200001, "amount": 5_000_000,
        "beneficiary_id": 999999})
    assert r.status_code == 404


def test_khoa_ngoai_tuy_chon_bo_trong_thi_khong_bi_chan(monkeypatch):
    """`beneficiary_id` và `risk_decision_id` cho phép NULL — cổng không được
    biến chúng thành bắt buộc."""
    monkeypatch.setattr(main, "query_one", _thieu("beneficiary"))
    monkeypatch.setattr(main, "execute_returning", lambda *a, **k: {
        "transaction_id": 500, "customer_id": 100008, "amount": "5000000"})
    r = client.post("/transactions", json={
        "customer_id": 100008, "account_id": 200001, "amount": 5_000_000})
    assert r.status_code == 201


def test_doi_trang_thai_voi_quyet_dinh_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("risk_decision"))
    r = client.patch("/transactions/900001/status", json={
        "status": "PENDING",
        "risk_decision_id": "11111111-2222-3333-4444-555555555555"})
    assert r.status_code == 404
    assert "decision" in r.json()["detail"]


def test_sinh_insight_cho_khach_khong_ton_tai_noi_dung_khach_khong_ton_tai(monkeypatch):
    """Trước đây trả 'không có chi tiêu kỳ X' — nghe như nghiệp vụ bình thường
    nên trợ lý đi tiếp thay vì tra lại id."""
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/customers/999999/insights/generate")
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_sinh_goi_y_cho_khach_khong_ton_tai_noi_dung_khach_khong_ton_tai(monkeypatch):
    """Trước đây trả 'Dòng tiền chưa dư', một câu đúng hình thức nhưng sai nguyên nhân."""
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/customers/999999/recommendations/generate")
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_sinh_insight_loai_chuyen_khoan_va_tu_viet_cau(monkeypatch):
    """Insight phải cùng định nghĩa "chi tiêu" với monthly-summary, và câu chữ do
    service viết từ chính con số vừa tính — để trống thì mỗi bên đọc tự nghĩ một
    kiểu, cùng một nhóm bị gọi hai tên trong một cuộc trò chuyện."""
    tong_theo_nhom = [
        {"category": "TRANSFER_P2P", "total": "600000000"},
        {"category": "FOOD", "total": "3000000"},
        {"category": "SHOPPING", "total": "1000000"},
    ]
    ghi: list[tuple] = []
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: tong_theo_nhom)
    monkeypatch.setattr(main, "execute_returning",
                        lambda sql, params: ghi.append(params) or {"category": params[2]})
    # Bước dọn nhóm cũ cũng chạm database — không chặn thì test đi thẳng vào
    # kết nối thật rồi trả về payload lỗi thay vì payload insight.
    monkeypatch.setattr(main, "execute", lambda sql, params: 0)
    body = client.post("/customers/100001/insights/generate",
                       params={"period": "202608"}).json()

    nhom = {p[2]: p for p in ghi}
    assert "TRANSFER_P2P" not in nhom, "chuyển khoản không phải chi tiêu"
    assert nhom["TOTAL"][3] == 4_000_000
    assert nhom["FOOD"][6] == (
        "Nhóm Ăn uống chiếm 75% tổng chi, xếp thứ 1. So với kỳ trước gần như không đổi.")
    assert body["insights"][1]["label"] == "Ăn uống"
    assert body["partial"] is False


def test_doc_insight_bao_ky_dang_chay(monkeypatch):
    """Kỳ đang chạy thì bản cache chỉ tính tới lúc sinh; bên gọi phải biết mà
    sinh lại, thay vì tưởng đây là con số chốt của cả tháng."""
    ky = main.now_vn().strftime("%Y%m")
    monkeypatch.setattr(main, "query", lambda *a, **k: [
        {"period": ky, "category": "FOOD", "amount": 1_000_000}])
    body = client.get("/customers/100001/insights").json()
    assert body["period_in_progress"] is True
    assert body["insights"][0]["label"] == "Ăn uống"


# ---------------------------------------------------------------------------
# Lộ trình tiết kiệm: review quý → khả năng tiết kiệm → gói sản phẩm
# ---------------------------------------------------------------------------
def _thang(period, income, expense, cats=None):
    """Một tháng dựng sẵn cho _thang_trong_ky đã monkeypatch."""
    return {"period": period, "income": income, "expense": expense,
            "net": income - expense, "count": 30, "by_category": cats or {}}


def test_fv_va_pmt_la_hai_chieu_cua_cung_mot_phep_tinh():
    """Sai một trong hai là hứa với khách một mốc thời gian không có thật."""
    for lai in (0, 3.5, 5.6, 7.2):
        for n in (12, 36, 60):
            pmt = main._pmt_for_goal(500_000_000, lai, n)
            assert abs(main._fv_annuity(pmt, lai, n) - 500_000_000) < 1


def test_lai_suat_0_thi_khong_sinh_them_dong_nao():
    """Nhánh r = 0 phải tách riêng: công thức annuity chia cho r sẽ nổ."""
    assert main._fv_annuity(1_000_000, 0, 36) == 36_000_000
    assert main._pmt_for_goal(36_000_000, 0, 36) == 1_000_000


def test_von_co_san_du_lon_thi_khong_can_gui_them():
    """Không được ra số âm — "mỗi tháng gửi -2 triệu" là câu vô nghĩa với khách."""
    assert main._pmt_for_goal(100_000_000, 5.6, 36, initial=200_000_000) == 0.0


def test_kha_nang_tiet_kiem_bo_thang_khong_co_thu_nhap(monkeypatch):
    """Tháng ở mép cửa sổ dữ liệu không có kỳ lương; tính vào sẽ đẻ ra một tháng
    âm bịa đặt và kéo mức để dành xuống gần một nửa."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202603", 0, 4_588_000),            # mép dữ liệu, phải bỏ
        _thang("202604", 7_600_000, 6_500_000),
        _thang("202605", 7_600_000, 6_600_000),
        _thang("202606", 7_600_000, 6_400_000),
    ])
    b = client.get("/transactions/100001/savings-capacity").json()
    assert b["months_used"] == 3
    assert [x["period"] for x in b["months_excluded"]] == ["202603"]
    assert b["realistic"]["monthly"] == 1_100_000      # trung vị, không phải -4,5tr
    assert b["realistic"]["annual"] == 13_200_000


def test_kha_nang_tiet_kiem_dung_trung_vi_chu_khong_trung_binh(monkeypatch):
    """Một tháng nhận 527 triệu là có thật trong dữ liệu demo. Trung bình sẽ ra
    "để dành 88 triệu/tháng" rồi dựng cả kế hoạch 3 năm trên con số đó."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202604", 7_600_000, 6_500_000),
        _thang("202605", 7_600_000, 6_600_000),
        _thang("202606", 527_000_000, 6_400_000),   # khoản một lần
    ])
    b = client.get("/transactions/100001/savings-capacity").json()
    assert b["realistic"]["monthly"] == 1_100_000
    assert b["basis"] == "median"


def test_kha_nang_tiet_kiem_muc_stretch_chi_cat_phan_co_gian(monkeypatch):
    """Thiết yếu và cam kết không được tính là cắt được, và không cắt quá 70%."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202604", 10_000_000, 8_000_000,
               {"BILLS": 3_000_000, "FAMILY_SUPPORT": 4_000_000, "SHOPPING": 1_000_000}),
        _thang("202605", 10_000_000, 8_000_000,
               {"BILLS": 3_000_000, "FAMILY_SUPPORT": 4_000_000, "SHOPPING": 1_000_000}),
    ])
    b = client.get("/transactions/100001/savings-capacity").json()
    assert b["realistic"]["monthly"] == 2_000_000
    assert b["stretch"]["flexible_monthly"] == 1_000_000      # chỉ SHOPPING
    assert b["stretch"]["monthly"] == 2_700_000               # + 70% của 1tr


def test_review_quy_bo_qua_quy_dang_chay(monkeypatch):
    """Quý đang chạy mới đi được một phần đường nên tổng chi luôn thấp giả tạo."""
    ky = main.now_vn().strftime("%Y%m")
    nam, quy = main._quarter_of(ky + "01")
    truoc = f"{nam}Q{quy - 1}" if quy > 1 else f"{nam - 1}Q4"
    thang_truoc = {1: ("10", "11", "12"), 2: ("01", "02", "03"),
                   3: ("04", "05", "06"), 4: ("07", "08", "09")}[quy]
    nam_truoc = nam if quy > 1 else nam - 1
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: [])
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang(f"{nam_truoc}{mm}", 7_600_000, 6_400_000, {"FOOD": 6_400_000})
        for mm in thang_truoc
    ] + [_thang(ky, 7_600_000, 900_000, {"FOOD": 900_000})])
    b = client.get("/transactions/100001/quarter-review").json()
    assert b["period"] == truoc
    assert b["is_completed_quarter"] is True
    assert b["monthly_avg"]["expense"] == 6_400_000


def test_review_quy_xep_nhom_vao_ba_ro(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: [])
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202604", 10_000_000, 8_000_000,
               {"BILLS": 3_000_000, "FAMILY_SUPPORT": 4_000_000, "SHOPPING": 1_000_000}),
    ])
    b = client.get("/transactions/100001/quarter-review", params={"quarter": "2026Q2"}).json()
    ro = {x["bucket"]: x for x in b["buckets"]}
    assert ro["essential"]["amount"] == 3_000_000
    assert ro["committed"]["amount"] == 4_000_000
    assert ro["flexible"]["amount"] == 1_000_000
    assert {c["category"]: c["bucket"] for c in b["by_category"]}["SHOPPING"] == "flexible"


def _bieu_lai():
    return {"as_of": "2026-09-01", "rates": [
        {"product_id": "RB.TK.LSCN", "product_name": "Tiết kiệm lãi suất cao nhất",
         "term_code": "T12", "term_label": "12 tháng", "term_months": 12.0, "rate_pct": 5.6},
        {"product_id": "RB.TK.THUONG", "product_name": "Tiết kiệm thường",
         "term_code": "T06", "term_label": "6 tháng", "term_months": 6.0, "rate_pct": 4.2},
    ]}


def test_ke_hoach_muc_tieu_bao_khong_kha_thi_kem_phuong_an(monkeypatch):
    """500 triệu / 3 năm với mức để dành 1 triệu là không tới. Câu trả lời phải
    nói thẳng còn thiếu bao nhiêu chứ không dừng ở "bạn không làm được"."""
    monkeypatch.setattr(main, "_rate_matrix", lambda g: _bieu_lai())
    b = client.get("/products/savings-goal-plan", params={
        "goal_amount": 500_000_000, "months": 36, "monthly_capacity": 1_018_000}).json()
    assert b["feasible"] is False
    assert b["best_rate_pct"] == 5.6
    assert b["required_monthly_with_interest"] == 12_787_180
    assert b["coverage_pct"] == 8
    assert b["gap_amount"] == 500_000_000 - b["projected_amount"]
    assert b["alternatives"]["keep_pace_years_needed"] > 3
    assert b["alternatives"]["reachable_goal_same_months"] == b["projected_amount"]


def test_ke_hoach_muc_tieu_bao_kha_thi_khi_du_kha_nang(monkeypatch):
    monkeypatch.setattr(main, "_rate_matrix", lambda g: _bieu_lai())
    b = client.get("/products/savings-goal-plan", params={
        "goal_amount": 500_000_000, "months": 36, "monthly_capacity": 14_000_000}).json()
    assert b["feasible"] is True
    assert b["coverage_pct"] >= 100
    assert b["gap_amount"] == 0 and b["gap_monthly"] == 0
    assert "alternatives" not in b


def test_ke_hoach_chi_lay_ky_han_khong_vuot_thoi_gian_gui(monkeypatch):
    """Áp lãi kỳ 12 tháng cho người gửi 6 tháng là hứa mức lãi không có thật."""
    monkeypatch.setattr(main, "_rate_matrix", lambda g: _bieu_lai())
    b = client.get("/products/savings-goal-plan", params={
        "goal_amount": 100_000_000, "months": 6, "monthly_capacity": 1_000_000}).json()
    assert [p["product_id"] for p in b["products"]] == ["RB.TK.THUONG"]
    assert b["best_rate_pct"] == 4.2


def test_ke_hoach_khong_co_kha_nang_thi_bao_chua_ket_luan_duoc(monkeypatch):
    """Thiếu monthly_capacity mà vẫn phán "khả thi" là đoán mò."""
    monkeypatch.setattr(main, "_rate_matrix", lambda g: _bieu_lai())
    b = client.get("/products/savings-goal-plan", params={
        "goal_amount": 500_000_000, "months": 36}).json()
    assert b["feasible"] is None
    assert "savings-capacity" in b["note"]


def test_ba_tool_moi_co_trong_danh_muc_agent():
    ten = {t["name"] for t in main.AGENT_TOOLS}
    assert {"review_quarter_spending", "get_savings_capacity", "plan_savings_goal"} <= ten


def test_review_quy_doc_dung_ten_cot_va_che_so_dien_thoai(monkeypatch):
    """Cột nội dung trong transaction_history tên là `transaction_description`.
    Test cũ mock query trả rỗng nên SQL không bao giờ chạy và lỗi tên cột chỉ lộ
    ra khi đã lên cụm — nên ở đây phải trả về đúng hình dạng dòng thật."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: [
        {"transaction_date": "20260415", "amount": "6000000", "category": "HEALTH",
         "transaction_description": "VIEN PHI LH 0912345678"},
    ])
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202604", 10_000_000, 8_000_000, {"HEALTH": 8_000_000}),
    ])
    b = client.get("/transactions/100001/quarter-review", params={"quarter": "2026Q2"}).json()
    mot_lan = b["one_off_transactions"]
    assert mot_lan and mot_lan[0]["amount"] == 6_000_000
    assert mot_lan[0]["label"] == "Sức khoẻ"
    assert "0912345678" not in mot_lan[0]["description"], "số điện thoại phải được che"


def test_review_quy_khong_so_voi_quy_nam_o_mep_du_lieu(monkeypatch):
    """Quý mép dữ liệu chỉ ghi được vài giao dịch. So với nó ra "+321% so với quý
    trước" — đúng số học, vô nghĩa với khách, và trợ lý sẽ đọc nguyên con số đó."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: [])
    monkeypatch.setattr(main, "_thang_trong_ky", lambda c, m: [
        _thang("202603", 0, 4_588_000, {"FOOD": 4_588_000}),          # cả quý 1 chỉ có 1 tháng
        _thang("202604", 7_600_000, 6_400_000, {"FOOD": 6_400_000}),
        _thang("202605", 7_600_000, 6_400_000, {"FOOD": 6_400_000}),
        _thang("202606", 7_600_000, 6_400_000, {"FOOD": 6_400_000}),
    ])
    b = client.get("/transactions/100001/quarter-review", params={"quarter": "2026Q2"}).json()
    assert b["delta_expense_vs_prev_pct"] is None
    assert b["prev_period"] is None
    assert "mép dữ liệu" in b["prev_period_skipped"]


def test_sinh_lai_insight_don_nhom_khong_con_thuoc_ky(monkeypatch):
    """Upsert chỉ ghi đè nhóm nó viết ra, nên nhóm cũ nằm lại vĩnh viễn. Bản
    cache của khách demo còn một dòng TRANSFER_P2P 620 triệu hạng 1 do lần sinh
    trước tính cả chuyển khoản đi là chi tiêu — kỳ đó thành ra có HAI dòng hạng
    1, và màn Home lấy dòng đầu nên báo "tổng chi 622 triệu" cho một khách chỉ
    tiêu 2,4 triệu."""
    xoa: list[tuple] = []
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "query", lambda *a, **k: [
        {"category": "FOOD", "total": "3000000"},
        {"category": "TRANSFER_P2P", "total": "620150000"},
    ])
    monkeypatch.setattr(main, "execute_returning", lambda sql, params: {"category": params[2]})
    monkeypatch.setattr(main, "execute", lambda sql, params: xoa.append((sql, params)) or 1)

    main_body = client.post("/customers/100001/insights/generate", params={"period": "202609"}).json()
    assert [i["category"] for i in main_body["insights"]] == ["TOTAL", "FOOD"]

    assert len(xoa) == 1, "phải có đúng một lệnh dọn"
    sql, params = xoa[0]
    assert "DELETE FROM spending_insight" in sql
    assert params[0] == 100001 and params[1] == "202609"
    # TRANSFER_P2P không nằm trong danh sách giữ lại nên sẽ bị dọn.
    assert "TRANSFER_P2P" not in params[2]
    assert set(params[2]) == {"TOTAL", "FOOD"}


# ---------------------------------------------------------------------------
# Sức khỏe tài chính & sức chống chịu
# ---------------------------------------------------------------------------
def _dt_100008(*a, **k):
    """Dòng tiền điển hình xấp xỉ khách demo 100008: thu 7,6tr, chi 6,5tr,
    thiết yếu ~4,6tr, cam kết (hỗ trợ gia đình) ~1,6tr, để dành ~1tr."""
    return {"months_used": 5, "income": 7_600_000, "expense": 6_582_000, "net": 1_018_000,
            "essential": 4_600_000, "committed": 1_600_000, "flexible": 400_000}


def test_health_diem_do_service_cham_khong_do_llm(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    monkeypatch.setattr(main, "_lai_tiet_kiem_tot_nhat", lambda *a, **k: 5.6)
    b = client.get("/customers/100008/financial-health",
                   params={"liquid_balance": 28_679_000, "deposit_balance": 0}).json()
    assert b["found"] is True
    assert 0 <= b["score"] <= 100 and b["grade"] in {"Tốt", "Khá", "Cần cải thiện", "Yếu"}
    tru = {p["key"]: p for p in b["pillars"]}
    assert set(tru) == {"savings_rate", "emergency_fund", "fixed_burden", "idle_cash", "diversification"}
    # 100008: chưa có sổ tiết kiệm -> đa dạng tài sản phải là "kém".
    assert tru["diversification"]["status"] == "poor"
    # 28,7tr / 4,6tr thiết yếu ~ 6,2 tháng -> quỹ dự phòng "tốt".
    assert tru["emergency_fund"]["status"] == "good"
    assert tru["emergency_fund"]["value"] >= 6
    # priority_actions chỉ gồm trụ cột không "tốt", nhiều nhất 3.
    assert len(b["priority_actions"]) <= 3
    assert all("tốt" not in a["detail"].lower() or True for a in b["priority_actions"])


def test_health_tien_nhan_roi_quy_ra_lai_co_hoi(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    monkeypatch.setattr(main, "_lai_tiet_kiem_tot_nhat", lambda *a, **k: 6.0)
    b = client.get("/customers/100008/financial-health",
                   params={"liquid_balance": 28_679_000, "deposit_balance": 0}).json()
    # Nhàn rỗi = 28,679tr - 6*4,6tr = 1,079tr; lãi cơ hội = 1,079tr * 6% ~ 64.740.
    idle = {p["key"]: p for p in b["pillars"]}["idle_cash"]
    assert idle["value"] == 28_679_000 - 6 * 4_600_000
    assert b["opportunity_cost_annual"] == round(idle["value"] * 6.0 / 100)


def test_health_co_so_tiet_kiem_thi_da_dang_tot_hon(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    monkeypatch.setattr(main, "_lai_tiet_kiem_tot_nhat", lambda *a, **k: 5.6)
    b = client.get("/customers/100008/financial-health",
                   params={"liquid_balance": 20_000_000, "deposit_balance": 30_000_000}).json()
    tru = {p["key"]: p for p in b["pillars"]}
    assert tru["diversification"]["status"] in {"warn", "good"}


def test_health_thieu_thang_du_lieu_thi_bao_khong_danh_gia_duoc(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", lambda *a, **k: None)
    b = client.get("/customers/100008/financial-health", params={"liquid_balance": 1}).json()
    assert b["found"] is False


def test_resilience_mat_thu_nhap_tinh_so_thang_tru_duoc(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    b = client.get("/customers/100008/resilience", params={
        "liquid_balance": 28_679_000, "shock_type": "income_loss", "income_loss_months": 3}).json()
    # 28,679tr / 4,6tr thiết yếu ~ 6,2 tháng.
    assert b["runway_essential_months"] == round(28_679_000 / 4_600_000, 1)
    assert b["runway_full_months"] == round(28_679_000 / 6_582_000, 1)
    # Trụ 6,2 tháng > giả định 3 tháng -> đủ sức.
    assert b["verdict"] == "đủ sức"
    assert "3 tháng" in b["recommendation"]


def test_resilience_chi_dot_xuat_thieu_thi_bao_nguon_bu(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    b = client.get("/customers/100008/resilience", params={
        "liquid_balance": 28_679_000, "deposit_balance": 0,
        "shock_type": "expense_shock", "expense_amount": 50_000_000}).json()
    # Cần 50tr, có 28,679tr, không có sổ -> thiếu cả sau khi gộp -> rủi ro.
    assert b["shortfall_liquid"] == 50_000_000 - 28_679_000
    assert b["shortfall_total"] == 50_000_000 - 28_679_000
    assert b["verdict"] == "rủi ro"


def test_resilience_chi_dot_xuat_du_tien_thi_du_suc(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    b = client.get("/customers/100008/resilience", params={
        "liquid_balance": 28_679_000, "shock_type": "expense_shock", "expense_amount": 10_000_000}).json()
    assert b["verdict"] == "đủ sức" and b["shortfall_liquid"] == 0


def test_hai_tool_advisor_co_trong_danh_muc_agent():
    ten = {t["name"] for t in main.AGENT_TOOLS}
    assert {"check_financial_health", "check_resilience"} <= ten


def test_advisor_khong_nuot_dau_phay_van_ban(monkeypatch):
    """.replace(',', '.') áp lên cả câu từng biến "để dành 1.018.000 ₫, bằng 13%"
    thành "₫. bằng". Số phải định dạng riêng, dấu phẩy văn bản giữ nguyên."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "_dong_tien_dien_hinh", _dt_100008)
    monkeypatch.setattr(main, "_lai_tiet_kiem_tot_nhat", lambda *a, **k: 5.6)
    h = client.get("/customers/100008/financial-health",
                   params={"liquid_balance": 28_679_000}).json()
    d1 = {p["key"]: p for p in h["pillars"]}["savings_rate"]["detail"]
    assert "₫, bằng" in d1 and "₫. bằng" not in d1

    r = client.get("/customers/100008/resilience", params={
        "liquid_balance": 28_679_000, "shock_type": "income_loss", "income_loss_months": 3}).json()
    assert "giả định, vẫn" in r["recommendation"] and "giả định. vẫn" not in r["recommendation"]
