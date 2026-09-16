"""Test cho identity-service — trọng tâm là ba hàng rào dữ liệu.

Service này là nơi duy nhất trong hệ thống chạm tới mật khẩu và PII của người
dùng ứng dụng, nên phần lớn test ở đây không kiểm tra "chạy có ra kết quả không"
mà kiểm tra "có rò thứ không được phép rò ra không".
"""
import bcrypt
import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)

MAT_KHAU = "MatKhau@123"
HASH = bcrypt.hashpw(MAT_KHAU.encode(), bcrypt.gensalt(rounds=4)).decode()

ADMIN = {
    "user_id": 1, "username": "admin", "role": "ADMIN", "customer_id": None,
    "full_name": "Nguyen Van Quan Tri", "email": "quantri@msb.com.vn",
    "phone_no": "0912345678", "user_status": "ACTIVE", "failed_login_count": 0,
    "last_login_at": None, "created_at": "2026-09-01T10:00:00+07:00",
}
KHACH = {
    "user_id": 6, "username": "kh100001", "role": "CUSTOMER", "customer_id": 100001,
    "full_name": "Tran Thi Khach Hang", "email": "khach@example.com",
    "phone_no": "0987654321", "user_status": "ACTIVE", "failed_login_count": 0,
    "last_login_at": None, "created_at": "2026-09-01T10:00:00+07:00",
}
BI_KHOA = {**KHACH, "user_id": 7, "username": "kh100002", "user_status": "LOCKED", "failed_login_count": 5}

VAI_TRO_ADMIN = {
    "role_code": "ADMIN", "role_name": "Quản trị hệ thống", "role_scope": "OPS",
    "permissions": ["ops.read", "ops.decide"], "role_status": "ACTIVE",
    "created_at": "2026-09-16T10:00:00+07:00",
}


# ---------------------------------------------------------------------------
# Hàng rào 1: password_hash không bao giờ ra ngoài
# ---------------------------------------------------------------------------
def test_danh_sach_khong_chua_password_hash(monkeypatch):
    # Cố tình trả về cả password_hash để chứng minh lớp che chặn được nó.
    monkeypatch.setattr(main, "query", lambda *a, **k: [{**ADMIN, "password_hash": HASH}])
    body = client.get("/users").json()
    assert "password_hash" not in str(body)
    assert HASH not in str(body)


def test_chi_tiet_khong_chua_password_hash(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**ADMIN, "password_hash": HASH})
    assert "password_hash" not in str(client.get("/users/1").json())


def test_mask_user_dung_danh_sach_trang(monkeypatch):
    """Thêm cột vào bảng thì cột đó KHÔNG được tự động lọt ra response."""
    ket_qua = main.mask_user({**ADMIN, "password_hash": HASH, "cot_moi_bi_mat": "KHONG_DUOC_RO"})
    assert "cot_moi_bi_mat" not in ket_qua
    assert "password_hash" not in ket_qua


def test_xac_thuc_dung_van_khong_tra_hash(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**ADMIN, "password_hash": HASH})
    body = client.post("/auth/verify", json={"username": "admin", "password": MAT_KHAU}).json()
    assert body["authenticated"] is True
    assert "password_hash" not in str(body)


# ---------------------------------------------------------------------------
# Hàng rào 2: PII bị che
# ---------------------------------------------------------------------------
def test_pii_bi_che(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(ADMIN))
    body = client.get("/users/1").json()
    assert body["full_name_masked"] != ADMIN["full_name"]
    assert "quantri@msb.com.vn" not in str(body)
    assert "0912345678" not in str(body)
    # Tên trường nói rõ đã che, để bên gọi không nhầm là giá trị thật.
    assert set(body) >= {"full_name_masked", "email_masked", "phone_masked"}


def test_khong_co_pii_thi_tra_null_khong_phai_chuoi_rong(monkeypatch):
    """50 tài khoản khách hàng trong bảng đều để trống full_name.

    Chuỗi rỗng dễ bị hiểu thành "tên rỗng"; null nói đúng là chưa có dữ liệu.
    """
    trong = {**KHACH, "full_name": None, "email": None, "phone_no": None}
    monkeypatch.setattr(main, "query_one", lambda *a, **k: trong)
    body = client.get("/users/6").json()
    assert body["full_name_masked"] is None
    assert body["email_masked"] is None
    assert body["phone_masked"] is None


# ---------------------------------------------------------------------------
# Xác thực
# ---------------------------------------------------------------------------
def test_sai_mat_khau(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**ADMIN, "password_hash": HASH})
    body = client.post("/auth/verify", json={"username": "admin", "password": "sai"}).json()
    assert body == {"authenticated": False, "reason": "invalid_credentials"}


def test_khong_co_tai_khoan_tra_giong_het_sai_mat_khau(monkeypatch):
    """Không được để lộ tên đăng nhập nào tồn tại.

    Khác biệt nhỏ nhất giữa hai câu trả lời cũng đủ để dò ra danh sách người dùng.
    """
    monkeypatch.setattr(main, "query_one", lambda *a, **k: None)
    khong_co = client.post("/auth/verify", json={"username": "khong-ton-tai", "password": "x"}).json()
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**ADMIN, "password_hash": HASH})
    sai_pass = client.post("/auth/verify", json={"username": "admin", "password": "sai"}).json()
    assert khong_co == sai_pass


def test_hash_hong_dinh_dang_khong_lam_vo_service(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**ADMIN, "password_hash": "khong-phai-bcrypt"})
    r = client.post("/auth/verify", json={"username": "admin", "password": MAT_KHAU})
    assert r.status_code == 200 and r.json()["authenticated"] is False


def test_dung_mat_khau_nhung_tai_khoan_khoa(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: {**BI_KHOA, "password_hash": HASH})
    body = client.post("/auth/verify", json={"username": "kh100002", "password": MAT_KHAU}).json()
    # Nói rõ lý do vì người gọi đã chứng minh họ là chủ tài khoản.
    assert body["authenticated"] is False and body["reason"] == "locked"
    assert "password_hash" not in str(body)


# ---------------------------------------------------------------------------
# Quyền
# ---------------------------------------------------------------------------
def test_vai_tro_chua_khai_bao_noi_ro_chu_khong_tra_mang_rong(monkeypatch):
    """app_role đang trống nên đây là trường hợp thường gặp.

    'Chưa định nghĩa' khác hẳn 'không có quyền nào'; nhầm hai thứ này sẽ dẫn tới
    chặn nhầm người dùng hợp lệ.
    """
    goi = iter([dict(ADMIN), None])
    monkeypatch.setattr(main, "query_one", lambda *a, **k: next(goi))
    body = client.get("/users/1/permissions").json()
    assert body["role_defined"] is False
    assert body["effective_permissions"] == []
    assert "chưa được khai báo" in body["note"]


def test_quyen_co_hieu_luc_khi_ca_hai_cung_active(monkeypatch):
    goi = iter([dict(ADMIN), dict(VAI_TRO_ADMIN)])
    monkeypatch.setattr(main, "query_one", lambda *a, **k: next(goi))
    body = client.get("/users/1/permissions").json()
    assert body["role_defined"] is True
    assert body["effective_permissions"] == ["ops.read", "ops.decide"]


def test_tai_khoan_khoa_thi_quyen_het_hieu_luc(monkeypatch):
    goi = iter([dict(BI_KHOA), dict(VAI_TRO_ADMIN)])
    monkeypatch.setattr(main, "query_one", lambda *a, **k: next(goi))
    body = client.get("/users/7/permissions").json()
    # Vẫn cho biết vai trò khai báo gì, nhưng hiệu lực là rỗng.
    assert body["effective_permissions"] == []
    assert body["declared_permissions"] == ["ops.read", "ops.decide"]


def test_vai_tro_ngung_hoat_dong_thi_quyen_het_hieu_luc(monkeypatch):
    goi = iter([dict(ADMIN), {**VAI_TRO_ADMIN, "role_status": "INACTIVE"}])
    monkeypatch.setattr(main, "query_one", lambda *a, **k: next(goi))
    assert client.get("/users/1/permissions").json()["effective_permissions"] == []


def test_danh_sach_vai_tro_chi_ra_vai_tro_dang_dung_ma_chua_khai_bao(monkeypatch):
    monkeypatch.setattr(main, "query", lambda sql, *a, **k: (
        [dict(VAI_TRO_ADMIN)] if "app_role" in sql else [{"role": "ADMIN"}, {"role": "CUSTOMER"}]
    ))
    body = client.get("/roles").json()
    assert body["undeclared_roles_in_use"] == ["CUSTOMER"]


# ---------------------------------------------------------------------------
# Đăng nhập sai nhiều lần
# ---------------------------------------------------------------------------
def test_dang_nhap_thanh_cong_dat_lai_bo_dem(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(KHACH))
    ghi = {}

    def fake_update(sql, params):
        ghi["sql"] = sql
        return {**KHACH, "failed_login_count": 0, "last_login_at": "2026-09-16T18:00:00+07:00"}

    monkeypatch.setattr(main, "execute_returning", fake_update)
    body = client.post("/users/6/login-attempt", json={"success": True}).json()
    assert body["locked"] is False
    assert "failed_login_count = 0" in ghi["sql"]
    assert "last_login_at = now()" in ghi["sql"]


def test_sai_du_nguong_thi_khoa(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(KHACH))
    monkeypatch.setattr(main, "execute_returning",
                        lambda *a, **k: {**KHACH, "failed_login_count": 5, "user_status": "LOCKED"})
    assert client.post("/users/6/login-attempt", json={"success": False}).json()["locked"] is True


def test_mo_khoa_dat_lai_bo_dem(monkeypatch):
    """Không đặt lại thì lần sai tiếp theo khoá ngay lập tức."""
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(BI_KHOA))
    ghi = {}

    def fake_update(sql, params):
        ghi["sql"] = sql
        return {**BI_KHOA, "user_status": "ACTIVE", "failed_login_count": 0}

    monkeypatch.setattr(main, "execute_returning", fake_update)
    client.patch("/users/7/status", json={"user_status": "ACTIVE"})
    assert "failed_login_count = 0" in ghi["sql"]


def test_khoa_tay_thi_khong_dat_lai_bo_dem(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: dict(KHACH))
    ghi = {}

    def fake_update(sql, params):
        ghi["sql"] = sql
        return {**KHACH, "user_status": "DISABLED"}

    monkeypatch.setattr(main, "execute_returning", fake_update)
    client.patch("/users/6/status", json={"user_status": "DISABLED"})
    assert "failed_login_count = 0" not in ghi["sql"]


# ---------------------------------------------------------------------------
# Định tuyến và lỗi
# ---------------------------------------------------------------------------
def test_summary_khong_bi_bat_lam_user_id(monkeypatch):
    """/users/summary phải khớp trước /users/{user_id}, nếu không sẽ ra 422."""
    monkeypatch.setattr(main, "query", lambda *a, **k: [{"role": "ADMIN", "user_status": "ACTIVE", "total": 4}])
    r = client.get("/users/summary")
    assert r.status_code == 200
    assert r.json()["total"] == 4


def test_khong_tim_thay_tra_404(monkeypatch):
    monkeypatch.setattr(main, "query_one", lambda *a, **k: None)
    assert client.get("/users/999").status_code == 404
    assert client.get("/users/by-username/khong-co").status_code == 404
    assert client.get("/customers/999999/user").status_code == 404
    assert client.get("/roles/KHONG-CO").status_code == 404


def test_trang_thai_khong_hop_le_bi_tu_choi():
    assert client.patch("/users/1/status", json={"user_status": "XOA_LUON"}).status_code == 422


def test_health_khong_cham_database():
    assert client.get("/health").json() == {"status": "ok", "service": "identity-service"}


def test_manifest_agent_va_info():
    tools = client.get("/agent/tools").json()
    assert len(tools["tools"]) == 6
    info = client.get("/info").json()
    assert info["owns_tables"] == ["app_role", "app_user"]


# ---------------------------------------------------------------------------
# Ràng buộc của database được chặn ngay ở tầng API
# ---------------------------------------------------------------------------
def test_role_scope_ngoai_danh_sach_bi_tu_choi_bang_422():
    """Database có ck_role_scope chỉ nhận APP hoặc BACKOFFICE.

    Không chặn ở đây thì psycopg ném CheckViolation và bên gọi nhận 500 kèm
    "Internal Server Error" — không biết giá trị nào mới đúng.
    """
    r = client.put("/roles/ADMIN", json={
        "role_name": "Quản trị", "role_scope": "OPS", "permissions": [],
    })
    assert r.status_code == 422
    assert "APP" in str(r.json()) and "BACKOFFICE" in str(r.json())


def test_role_scope_hop_le_duoc_chap_nhan(monkeypatch):
    monkeypatch.setattr(main, "execute_returning", lambda *a, **k: {**VAI_TRO_ADMIN, "role_scope": "BACKOFFICE"})
    for scope in ("APP", "BACKOFFICE"):
        r = client.put("/roles/X", json={"role_name": "X", "role_scope": scope, "permissions": []})
        assert r.status_code == 200, scope


def test_role_status_ngoai_danh_sach_bi_tu_choi():
    r = client.put("/roles/ADMIN", json={
        "role_name": "Quản trị", "role_scope": "APP", "permissions": [], "role_status": "XOA",
    })
    assert r.status_code == 422


def test_loc_theo_vai_tro_ngoai_danh_sach_bi_tu_choi():
    assert client.get("/users?role=SIEU_NHAN").status_code == 422
