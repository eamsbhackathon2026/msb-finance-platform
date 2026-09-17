"""Test cho customer-profile-service.

Không cần PostgreSQL: `common.pool()` khởi tạo lười nên chỉ cần thay `common.query`
và `common.query_one` là chạy được toàn bộ endpoint.
"""
import common
import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)

CUSTOMER = {
    "customer_id": 100008,
    "full_name": "NGUYEN THI BICH",
    "street": "19 Bach Mai, Hai Ba Trung, Ha Noi",
    "target": "3",
    "nationality": "VN",
    "legal_id": "038158001234",
    "legal_type": "CCCD",
    "date_of_birth": "19580207",
    "email": "nguyen.thi.bich@example.com",
    "phone_no": "0912345678",
    "co_code": "MSB",
}


# ---------------------------------------------------------------------------
# Mask PII — ràng buộc quan trọng nhất của service này
# ---------------------------------------------------------------------------
def test_mask_customer_row_khong_lam_lo_pii():
    """Không một cột PII thô nào được phép xuất hiện trong payload trả ra."""
    masked = common.mask_customer_row(CUSTOMER)
    payload = str(masked)
    for leak in ("NGUYEN THI BICH", "038158001234", "0912345678",
                 "Bach Mai", "19580207", "nguyen.thi.bich@example.com"):
        assert leak not in payload, f"rò rỉ PII: {leak}"
    assert masked["name_masked"] == "NGUYEN THI B***"
    assert masked["phone_masked"] == "09** *** 678"
    assert masked["legal_id_masked"] == "************"


def test_persona_tu_target_va_tuoi():
    assert common.persona_of("1", "19920314") == "SALARY"
    assert common.persona_of("2", "19780923") == "HNW"
    assert common.persona_of("3", "19580207") == "SENIOR"


def test_tuoi_tu_60_luon_la_senior_du_target_khac():
    """Người 60 tuổi trở lên cần được hỏi bằng giọng khác, bất kể phân khúc T24."""
    assert common.persona_of("1", "19550101") == "SENIOR"
    assert common.persona_of("2", "19500101") == "SENIOR"


def test_mask_free_text_che_sdt_va_cccd():
    text = "Ho goi tu so 0912345678 va doc CCCD 038158001234"
    masked = common.mask_free_text(text)
    assert "0912345678" not in masked
    assert "038158001234" not in masked


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
def test_health_khong_cham_database():
    """`/health` phải trả lời được cả khi database sập, nếu không Kubernetes sẽ
    khởi động lại pod mỗi lần DB chập chờn."""
    def no_db(*_a, **_k):
        raise RuntimeError("database không khả dụng")

    main.query = no_db
    main.query_one = no_db
    assert client.get("/health").json() == {
        "status": "ok", "service": "customer-profile-service"
    }


def test_get_customer_tra_ho_so_da_mask(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda sql, p=None: (
        CUSTOMER if "FROM customer" in sql
        else {"n": 2} if "count(" in sql else {"x": 1}
    ))
    body = client.get("/customers/100008").json()
    assert body["customer_id"] == 100008
    assert body["persona"] == "SENIOR"
    assert "full_name" not in body
    assert body["name_masked"] == "NGUYEN THI B***"


def test_customer_khong_ton_tai_tra_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda sql, p=None: None)
    assert client.get("/customers/999999").status_code == 404


def test_resolve_nguoi_nhan_la_tra_ve_is_new(monkeypatch):
    """Người nhận chưa có trong sổ phải trả is_new = True — đây là đầu vào của
    yếu tố rủi ro 'người nhận mới'."""
    monkeypatch.setattr(main, "query_one", lambda sql, p=None: (
        CUSTOMER if "FROM customer" in sql else None))
    body = client.post(
        "/customers/100008/beneficiaries/resolve",
        json={"bank_code": "ACB", "account_no": "5270384262"},
    ).json()
    assert body["known"] is False
    assert body["is_new"] is True
    assert body["relationship"] == "UNKNOWN"
    assert "5270384262" not in str(body), "số tài khoản thô bị trả ngược ra"
    assert body["account_masked"] == "5270 ****"


def test_su_kien_khong_lo_khoa_kiem_thu_noi_bo(monkeypatch):
    """Cột meta của account_event chứa lẫn cả khóa phục vụ sinh dữ liệu. Trả nguyên
    dòng sẽ đẩy mã fraud case ra API — đúng thứ có thể lọt vào prompt LLM."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: CUSTOMER)
    monkeypatch.setattr(main, "query", lambda *a, **k: [{
        "event_id": 1, "customer_id": 100008, "account_id": 5011,
        "event_type": "SAVINGS_CLOSED", "amount": 520_000_000,
        "event_time": "2026-09-11T14:52:00+07:00",
        "source_ref": "F01",
        "meta": {"fraud_case_id": "F01", "injected": True, "channel": "MOBILE"},
    }])
    body = client.get("/customers/100008/events?within_minutes=60").json()
    payload = str(body)
    assert "F01" not in payload, "mã fraud case bị rò ra API"
    assert "injected" not in payload
    assert "source_ref" not in payload
    assert body["events"][0]["event_type"] == "SAVINGS_CLOSED"
    assert body["events"][0]["meta"] == {"channel": "MOBILE"}, (
        "phần meta nghiệp vụ phải được giữ lại"
    )


def test_agent_tools_liet_ke_du_tool():
    body = client.get("/agent/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert {"get_baseline", "resolve_beneficiary", "list_recent_events",
            "get_portfolio"} <= names
    for t in body["tools"]:
        assert t["description"] and t["method"] and t["path"]


def test_openapi_co_day_du_metadata():
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "customer-profile-service"
    assert spec["info"]["description"]
    tags = {t["name"] for t in spec.get("tags", [])}
    assert {"meta", "customer", "baseline", "beneficiary", "event"} <= tags


# ---------------------------------------------------------------------------
# Cổng kiểm tra tồn tại
# ---------------------------------------------------------------------------
def test_ghi_su_kien_tren_tai_khoan_khong_ton_tai_thanh_404(monkeypatch):
    """Khách có thật nhưng tài khoản thì không: trước đây vỡ khóa ngoại thành 500."""
    def _q(sql, params=None, *a, **k):
        if "SELECT 1 AS x FROM account" in sql:
            return None
        return {"customer_id": 100008}
    monkeypatch.setattr(main, "query_one", _q)
    r = client.post("/customers/100008/events", json={
        "event_type": "NEW_DEVICE_LOGIN", "account_id": 999999})
    assert r.status_code == 404
    assert r.json()["detail"] == "account 999999 không tồn tại"


def test_ghi_su_kien_cap_khach_hang_khong_can_tai_khoan(monkeypatch):
    """`account_event.account_id` cho phép NULL với sự kiện cấp khách hàng."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {"customer_id": 100008})
    monkeypatch.setattr(main, "execute_returning", lambda *a, **k: {
        "event_id": 1, "customer_id": 100008, "account_id": None,
        "event_type": "PASSWORD_RESET", "amount": None, "event_time": None,
        "source_ref": None, "meta": {}})
    r = client.post("/customers/100008/events", json={"event_type": "PASSWORD_RESET"})
    assert r.status_code == 201
