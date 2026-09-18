"""Test cho action-feedback-service."""
import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)

DECISION = "11111111-2222-3333-4444-555555555555"
CASE = {
    "case_id": "CASE-2026-0001", "decision_id": DECISION, "customer_id": 100008,
    "scenario_id": "S01", "status": "OPEN", "narrative": "Tóm tắt case.",
    "callback_phone_masked": "09** *** 678", "created_at": None, "closed_at": None,
}


# ---------------------------------------------------------------------------
# Hành động
# ---------------------------------------------------------------------------
def test_hanh_dong_khong_hop_le_bi_tu_choi():
    r = client.post("/actions", json={
        "customer_id": 100008, "action": "DELETE_EVERYTHING"})
    assert r.status_code == 400


def test_hanh_dong_hop_le_duoc_chap_nhan(monkeypatch):
    # Cổng kiểm tra tồn tại tra `customer` trước khi ghi, nên khách phải có thật.
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "execute_returning",
                        lambda *a, **k: {"notification_id": 7, "decision_id": None})
    body = client.post("/actions", json={
        "customer_id": 100008, "action": "ALERT", "reason": "Canh bao thu"}).json()
    assert body["action"] == "ALERT"


def test_ly_do_hanh_dong_duoc_mask_sdt(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "execute_returning",
                        lambda *a, **k: {"notification_id": 7, "decision_id": None})
    body = client.post("/actions", json={
        "customer_id": 100008, "action": "ALERT",
        "reason": "Khach bao so 0912345678 goi den"}).json()
    assert "0912345678" not in body["reason"]


# ---------------------------------------------------------------------------
# Case
# ---------------------------------------------------------------------------
def test_mo_case_lan_hai_tra_case_cu_thay_vi_bao_loi(monkeypatch):
    """Quan hệ với risk_decision là 1-1. Agent gọi lại sau timeout không nên
    nhận lỗi trùng khóa — nếu không, mọi luồng retry đều gãy."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(CASE))
    body = client.post("/cases", json={
        "decision_id": DECISION, "customer_id": 100008}).json()
    assert body["already_existed"] is True
    assert body["case_id"] == "CASE-2026-0001"


def test_dong_case_gian_lan_tu_sinh_feedback_ops(monkeypatch):
    """Kết luận của đội vận hành là nhãn huấn luyện đáng tin nhất hệ thống có."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(CASE))
    ghi = {}

    def fake_returning(sql, params=None):
        if "UPDATE guardian_case" in sql:
            return {**CASE, "status": "CLOSED_FRAUD"}
        ghi["feedback"] = params
        return {"feedback_id": 1, "decision_id": DECISION, "label": params[2],
                "source": params[3]}

    monkeypatch.setattr(main, "execute_returning", fake_returning)
    body = client.patch("/cases/CASE-2026-0001", json={"status": "CLOSED_FRAUD"}).json()
    assert body["feedback"]["label"] == "fraud"
    assert body["feedback"]["source"] == "ops"


def test_dong_case_hop_le_sinh_nhan_legit(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(CASE))
    monkeypatch.setattr(main, "execute_returning", lambda sql, params=None: (
        {**CASE, "status": "CLOSED_LEGIT"} if "UPDATE guardian_case" in sql
        else {"feedback_id": 2, "decision_id": DECISION,
              "label": params[2], "source": params[3]}))
    body = client.patch("/cases/CASE-2026-0001", json={"status": "CLOSED_LEGIT"}).json()
    assert body["feedback"]["label"] == "legit"


def test_case_khong_ton_tai_tra_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: None)
    assert client.get("/cases/CASE-2026-9999").status_code == 404


# ---------------------------------------------------------------------------
# Nhật ký LLM
# ---------------------------------------------------------------------------
def test_ghi_trace_mask_pii_con_sot_trong_prompt(monkeypatch):
    ghi = {}

    def fake(sql, params=None):
        ghi["params"] = params
        return {"trace_id": 1, "decision_id": None, "prompt_masked": params[5]}

    monkeypatch.setattr(main, "execute_returning", fake)
    client.post("/llm-traces", json={
        "agent": "shield_explain", "model": "claude-sonnet-5",
        "prompt_key": "shield_explain@v3",
        "prompt_masked": "Khach hang so 0912345678 chuyen tien",
        "status": "ok", "latency_ms": 800})
    assert "0912345678" not in ghi["params"][5]


def _fake_stats_query(sql, params=None):
    """Hai truy vấn thống kê dùng chung một hàm `query`, phân biệt theo nội dung SQL."""
    if "GROUP BY customer_id" in sql:
        return [{"customer_id": 100008, "n": 7}, {"customer_id": 100002, "n": 3}]
    return [
        {"agent": "shield_explain", "status": "ok", "n": 8, "avg_latency_ms": 900},
        {"agent": "shield_explain", "status": "timeout", "n": 2, "avg_latency_ms": 3000},
    ]


def test_thong_ke_tinh_ca_luot_that_bai(monkeypatch):
    """Bản ghi timeout và error phải được đếm, nếu không tỷ lệ fallback bị tô hồng."""
    monkeypatch.setattr(main, "query", _fake_stats_query)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"latest_at": "2026-09-15T09:41:03+07:00"})
    body = client.get("/llm-traces/stats").json()
    assert body["total_calls"] == 10
    assert body["fallback_rate"] == 0.2


def test_thong_ke_kem_danh_sach_khach_va_moc_moi_nhat(monkeypatch):
    """Màn vận hành cần hai thứ KHÔNG đổi theo bộ lọc: danh sách khách để chọn,
    và một mốc thời gian cố định để tính các khoảng nhanh."""
    monkeypatch.setattr(main, "query", _fake_stats_query)
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"latest_at": "2026-09-15T09:41:03+07:00"})
    body = client.get("/llm-traces/stats").json()
    assert [c["customer_id"] for c in body["customers"]] == [100008, 100002]
    assert body["latest_at"].startswith("2026-09-15")


def test_loc_nhat_ky_theo_khach_va_khoang_thoi_gian(monkeypatch):
    """Khoảng nửa mở [since, until): bản ghi đúng mốc `until` KHÔNG được tính,
    nếu không một lượt gọi nằm ở hai ngày liền nhau."""
    ghi = {}

    def fake(sql, params=None):
        ghi["sql"] = sql
        ghi["params"] = params
        return []

    monkeypatch.setattr(main, "query", fake)
    client.get("/llm-traces?customer_id=100008"
               "&since=2026-09-12T17:00:00%2B00:00&until=2026-09-13T17:00:00%2B00:00")
    assert "AND customer_id = %s" in ghi["sql"]
    assert "created_at >= %s::timestamptz" in ghi["sql"]
    assert "created_at < %s::timestamptz" in ghi["sql"]
    assert ghi["params"][:3] == [100008, "2026-09-12T17:00:00+00:00", "2026-09-13T17:00:00+00:00"]


def test_health_va_agent_tools():
    assert client.get("/health").json()["status"] == "ok"
    names = {t["name"] for t in client.get("/agent/tools").json()["tools"]}
    assert {"record_action", "submit_feedback", "apply_feedback", "log_llm_call"} <= names


def test_info_liet_ke_hanh_dong_cho_phep():
    assert set(client.get("/info").json()["allowed_actions"]) == main.ALLOWED_ACTIONS


def test_openapi_hop_le():
    spec = client.get("/openapi.json").json()
    assert "/feedback/apply" in spec["paths"]
    assert {"case", "feedback", "audit", "notification"} <= {
        t["name"] for t in spec["tags"]}


# ---------------------------------------------------------------------------
# Lưới lỗi cơ sở dữ liệu
# ---------------------------------------------------------------------------
# Trợ lý đọc nguyên văn thân phản hồi của tool, nên `500 Internal Server Error`
# không cho nó biết nên tra lại id hay nên dừng. Các test dưới đây khẳng định
# từng lớp lỗi của Postgres ra một mã trạng thái nói được điều gì đó.
#
# Chuỗi trong `message_detail` lấy nguyên văn từ database thật `ea-hackathon`,
# không phải bịa: đó là thứ psycopg trả về khi khóa ngoại vỡ.
from types import SimpleNamespace  # noqa: E402

import common  # noqa: E402
import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _loi(lop, **diag):
    """Dựng lỗi psycopg kèm diag như server Postgres trả về.

    `sqlstate` là thuộc tính lớp của psycopg nên tự đúng theo `lop`; chỉ `diag`
    là thứ phải giả, vì bình thường nó do server điền."""
    lop_gia = type(lop.__name__ + "Gia", (lop,), {
        "diag": property(lambda self: SimpleNamespace(
            message_detail=diag.get("message_detail"),
            message_primary=diag.get("message_primary"),
            table_name=diag.get("table_name"),
            column_name=diag.get("column_name"),
            constraint_name=diag.get("constraint_name"),
        )),
    })
    return lop_gia("thông điệp của server")


def _bat_loi_khi_ghi(monkeypatch, exc):
    """Cho endpoint ghi ném lỗi DB, trả về phản hồi HTTP tương ứng."""
    def _no(*a, **k):
        raise exc
    # Cổng kiểm tra cho đi qua, để lỗi rơi đúng vào câu ghi — đây là test của
    # lưới đỡ, không phải của cổng.
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "execute_returning", _no)
    return client.post("/feedback", json={
        "decision_id": DECISION, "customer_id": 999999,
        "label": "legit", "source": "ops"})


def test_khoa_ngoai_thieu_ban_ghi_cha_thanh_404(monkeypatch):
    r = _bat_loi_khi_ghi(monkeypatch, _loi(
        psycopg.errors.ForeignKeyViolation,
        message_detail='Key (customer_id)=(999999) is not present in table "customer".',
        table_name="feedback", constraint_name="feedback_customer_id_fkey"))
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_ghi_trung_ban_ghi_mot_mot_thanh_409(monkeypatch):
    r = _bat_loi_khi_ghi(monkeypatch, _loi(
        psycopg.errors.UniqueViolation,
        message_detail='Key (decision_id)=(11111111-2222-3333-4444-555555555555) already exists.',
        table_name="guardian_case", constraint_name="guardian_case_decision_id_key"))
    assert r.status_code == 409
    # So nguyên câu chứ không chỉ tìm "đã tồn tại": đúng phần id mới là thứ trợ lý
    # cần để tra lại, và cũng là phần dễ hỏng nhất.
    assert r.json()["detail"] == (
        "guardian_case với decision_id 11111111-2222-3333-4444-555555555555 đã tồn tại")


def test_uuid_sai_dinh_dang_thanh_422(monkeypatch):
    r = _bat_loi_khi_ghi(monkeypatch, _loi(
        psycopg.errors.InvalidTextRepresentation,
        message_primary='invalid input syntax for type uuid: "khong-phai-uuid"'))
    assert r.status_code == 422
    assert "không đúng định dạng" in r.json()["detail"]


def test_mat_ket_noi_database_thanh_503(monkeypatch):
    r = _bat_loi_khi_ghi(monkeypatch, psycopg.OperationalError("connection refused"))
    assert r.status_code == 503


def test_sql_sai_cu_phap_van_la_500(monkeypatch):
    """Lưới chỉ dịch lỗi do người gọi. Bug của chính service phải nổ ra 500,
    che nó đi thì không ai biết mà sửa."""
    def _no(*a, **k):
        raise psycopg.errors.SyntaxError("lỗi cú pháp SQL")
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(main, "execute_returning", _no)
    khong_nem = TestClient(main.app, raise_server_exceptions=False)
    r = khong_nem.post("/feedback", json={
        "decision_id": DECISION, "customer_id": 100008,
        "label": "legit", "source": "ops"})
    assert r.status_code == 500


def test_xoa_ban_ghi_con_duoc_tham_chieu_thanh_409():
    ma, cau = common.db_error_status(_loi(
        psycopg.errors.ForeignKeyViolation,
        message_detail='Key (customer_id)=(100008) is still referenced from table "transaction_history".',
        table_name="customer", constraint_name="transaction_history_customer_id_fkey"))
    assert ma == 409
    assert cau == ("không xóa được customer 100008 vì còn bản ghi "
                   "transaction_history tham chiếu")


def test_gia_tri_cot_khong_phai_id_thi_khong_lo_ra_ngoai():
    """Giá trị trong thông điệp của Postgres là thứ người gọi gửi lên, có thể là
    PII. Cột không phải id thì chỉ nêu tên cột."""
    _, cau = common.db_error_status(_loi(
        psycopg.errors.UniqueViolation,
        message_detail='Key (phone_no)=(0912345678) already exists.',
        table_name="app_user", constraint_name="app_user_phone_no_key"))
    assert "0912345678" not in cau
    assert "phone_no" in cau


# ---------------------------------------------------------------------------
# Cổng kiểm tra tồn tại
# ---------------------------------------------------------------------------
def _thieu(bang):
    """query_one giả cho các cổng kiểm tra: mọi bảng đều có bản ghi, trừ `bang`.

    Tra cứu khác (case cũ trong nhánh idempotent chẳng hạn) trả `None` để luồng
    đi tới chỗ ghi, vì cổng nào cũng hỏi bằng đúng dạng `SELECT 1 AS x FROM`."""
    def _q(sql, params=None, *a, **k):
        if "SELECT 1 AS x FROM" not in sql:
            return None
        return None if f"FROM {bang} " in sql else {"x": 1}
    return _q


def test_mo_case_cho_khach_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/cases", json={
        "decision_id": DECISION, "customer_id": 999999, "trigger": "hold"})
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_mo_case_tren_quyet_dinh_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("risk_decision"))
    r = client.post("/cases", json={
        "decision_id": DECISION, "customer_id": 100008, "trigger": "hold"})
    assert r.status_code == 404
    assert "decision" in r.json()["detail"]


def test_mo_case_voi_kich_ban_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("scam_scenario"))
    r = client.post("/cases", json={
        "decision_id": DECISION, "customer_id": 100008,
        "scenario_id": "S99", "trigger": "hold"})
    assert r.status_code == 404
    assert "S99" in r.json()["detail"]


def test_gui_phan_hoi_cho_khach_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/feedback", json={
        "decision_id": DECISION, "customer_id": 999999,
        "label": "legit", "source": "ops"})
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_gui_thong_bao_cho_khach_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("customer"))
    r = client.post("/notifications", json={
        "customer_id": 999999, "channel": "push", "template_key": "hold_confirmed"})
    assert r.status_code == 404
    assert r.json()["detail"] == "customer 999999 không tồn tại"


def test_ghi_nhat_ky_llm_cho_quyet_dinh_khong_ton_tai_thanh_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", _thieu("risk_decision"))
    r = client.post("/llm-traces", json={
        "decision_id": DECISION, "agent": "shield_explain", "model": "gemini",
        "prompt_key": "shield_explain@v3", "prompt_masked": "Giải thích rủi ro",
        "status": "ok"})
    assert r.status_code == 404


def test_mo_lai_case_da_co_van_tra_ve_case_cu(monkeypatch):
    """Cổng mới đặt sau nhánh idempotent, nếu không luồng retry của agent sẽ gãy."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(CASE))
    body = client.post("/cases", json={
        "decision_id": DECISION, "customer_id": 100008, "trigger": "hold"}).json()
    assert body["already_existed"] is True


def test_id_trong_thong_diep_khong_bi_mask_hong(monkeypatch):
    """Hồi quy: `mask_free_text` thay mọi dãy 9-12 chữ số bằng `***`, nên nếu áp
    lên câu đã ghép thì `transaction 800012345 không tồn tại` thành
    `transaction *** không tồn tại` — trợ lý mất đúng cái id nó cần tra lại."""
    ma, cau = common.db_error_status(_loi(
        psycopg.errors.ForeignKeyViolation,
        message_detail='Key (transaction_id)=(800012345) is not present in table "transaction_history".'))
    assert ma == 404
    assert cau == "transaction_history 800012345 không tồn tại"

    _, cau_uuid = common.db_error_status(_loi(
        psycopg.errors.UniqueViolation,
        message_detail='Key (decision_id)=(29e847b4-c299-58d0-a34d-585030306acc) already exists.',
        table_name="guardian_case"))
    assert "585030306acc" in cau_uuid


def test_so_giay_to_khong_lo_ra_du_ket_thuc_bang_id():
    """`legal_id` kết thúc bằng `_id` nhưng schema cấm nó ra API."""
    _, cau = common.db_error_status(_loi(
        psycopg.errors.UniqueViolation,
        message_detail='Key (legal_id)=(001234567890) already exists.',
        table_name="customer"))
    assert "001234567890" not in cau
    assert "legal_id" in cau


def test_database_het_tai_nguyen_hoac_dang_tat_thanh_503():
    """Không chỉ mất kết nối (08xxx): hết connection hay Postgres đang khởi động
    lại cũng là "lát nữa thử lại", không phải "hệ thống hỏng, dừng"."""
    for lop in (psycopg.errors.TooManyConnections, psycopg.errors.CannotConnectNow,
                psycopg.errors.AdminShutdown, psycopg.errors.CrashShutdown):
        assert common.db_error_status(lop("x"))[0] == 503, lop.__name__


def test_loi_lap_trinh_van_khong_duoc_nhan_dien():
    """Đối trọng của test trên: lỗi của chính service phải rơi về 500."""
    for lop in (psycopg.errors.SyntaxError, psycopg.errors.UndefinedColumn,
                psycopg.errors.QueryCanceled):
        assert common.db_error_status(lop("x")) is None, lop.__name__


def test_thieu_cot_bat_buoc_thanh_422():
    ma, cau = common.db_error_status(_loi(
        psycopg.errors.NotNullViolation, table_name="feedback", column_name="customer_id"))
    assert ma == 422
    assert "customer_id" in cau


def test_sai_rang_buoc_check_thanh_422():
    ma, cau = common.db_error_status(_loi(
        psycopg.errors.CheckViolation, table_name="app_user", constraint_name="ck_user_role"))
    assert ma == 422
    assert "ck_user_role" in cau


def test_khoa_nhieu_cot_lan_pii_thi_giau_gia_tri():
    """Khóa gồm một cột không phải id thì cả giá trị bị giấu, chỉ nêu tên cột."""
    ma, cau = common.db_error_status(_loi(
        psycopg.errors.ForeignKeyViolation,
        message_detail='Key (customer_id, phone_no)=(100008, 0912345678) is not present in table "customer".'))
    assert ma == 404
    assert "0912345678" not in cau
    assert "customer" in cau


def test_van_ban_tu_do_cua_server_bi_cat_ngan():
    """Nhánh 22xxx trả nguyên văn chuỗi người gọi gửi lên; không cắt thì đó là một
    cửa nhồi văn bản vào ngữ cảnh mô hình."""
    _, cau = common.db_error_status(_loi(
        psycopg.errors.InvalidTextRepresentation,
        message_primary='invalid input syntax for type uuid: "' + "A" * 500 + '"'))
    assert len(cau) < 200
    assert cau.endswith("…")
