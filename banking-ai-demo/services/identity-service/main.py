"""identity-service

Sở hữu: app_role + app_user.

Ai được vào hệ thống, với vai trò nào, và quyền của vai trò đó gồm những gì.
Hai bảng này là nền cho phần xác thực và phân quyền mà các service khác chưa có.

BA HÀNG RÀO, theo đúng nguyên tắc chung của repo:

1. `password_hash` KHÔNG BAO GIỜ rời khỏi service này. Không endpoint nào trả ra
   nó, kể cả đã che. Nó chỉ được đọc trong bộ nhớ khi so khớp mật khẩu ở
   POST /auth/verify.

2. Mọi PII đều bị che: họ tên, email, số điện thoại. Response an toàn để đưa
   thẳng vào prompt LLM.

3. Chỉ chạm hai bảng của chính mình. Thông tin khách hàng lấy qua HTTP tới
   customer-profile-service, không truy vấn chéo database.

Lưu ý về dữ liệu hiện có: `app_role` đang TRỐNG trong khi `app_user.role` đã có
giá trị ADMIN và CUSTOMER. Vì vậy quyền của một người dùng có thể chưa định
nghĩa; endpoint quyền nói rõ điều đó bằng cờ `role_defined` thay vì trả mảng
rỗng như thể vai trò không có quyền nào.
"""
from __future__ import annotations

import os
import re
from typing import Literal

import bcrypt
import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    agent_tools_payload,
    db_health,
    execute_returning,
    mask_email,
    mask_name,
    mask_phone,
    openapi_description,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
)

SERVICE_NAME = "identity-service"
OWNS_TABLES = ["app_role", "app_user"]

CUSTOMER_PROFILE_URL = os.getenv("CUSTOMER_PROFILE_SERVICE_URL", "http://customer-profile-service")
PEER_TIMEOUT = float(os.getenv("PEER_TIMEOUT_SECONDS", "5"))

# Số lần sai mật khẩu liên tiếp trước khi khoá tài khoản. Khoá là trạng thái
# LOCKED chứ không phải DISABLED: DISABLED là quyết định của quản trị viên, còn
# LOCKED là hệ quả tự động và mở lại được.
MAX_FAILED_LOGIN = int(os.getenv("MAX_FAILED_LOGIN", "5"))

PURPOSE = (
    "Danh tính và phân quyền của người dùng ứng dụng: tra cứu tài khoản đăng nhập, "
    "vai trò, quyền theo vai trò, và xác thực mật khẩu."
)

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": "users",
        "description": (
            "Tài khoản đăng nhập. Mọi response đều đã che PII và KHÔNG BAO GIỜ chứa "
            "password_hash."
        ),
    },
    {
        "name": "roles",
        "description": (
            "Vai trò và quyền. Bảng app_role hiện đang trống — quyền được khai báo ở "
            "đây chứ không nằm rải trong code từng service."
        ),
    },
    {
        "name": "auth",
        "description": (
            "Xác thực mật khẩu. Đây là nơi duy nhất trong toàn hệ thống đọc "
            "password_hash."
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary=PURPOSE,
    description=openapi_description(SERVICE_NAME, PURPOSE, OWNS_TABLES),
    openapi_tags=OPENAPI_TAGS,
    contact={"name": "MSB AI Financial Guardian — xem manifest tại GET /agent/tools"},
    docs_url="/docs",
    redoc_url="/redoc",
)
setup_docs(app, SERVICE_NAME)

# Các giá trị hợp lệ lấy từ CHECK constraint đang có trên database, không phải
# từ suy đoán. Khai báo lại ở đây để request sai trả 422 kèm danh sách giá trị
# đúng, thay vì để psycopg ném CheckViolation ra thành 500 không đọc được:
#
#   ck_user_status   ACTIVE | LOCKED | DISABLED
#   ck_user_role     CUSTOMER | ADMIN
#   ck_role_scope    APP | BACKOFFICE
#   ck_role_status   ACTIVE | INACTIVE
#   ck_user_customer CUSTOMER phải có customer_id, ADMIN phải để trống
USER_STATUS = Literal["ACTIVE", "DISABLED", "LOCKED"]
USER_ROLE = Literal["CUSTOMER", "ADMIN"]
ROLE_SCOPE = Literal["APP", "BACKOFFICE"]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class RoleUpsert(BaseModel):
    role_name: str = Field(..., min_length=1)
    role_scope: ROLE_SCOPE = Field(..., description="APP cho ứng dụng khách, BACKOFFICE cho vận hành nội bộ")
    permissions: list[str] = Field(default_factory=list)
    role_status: Literal["ACTIVE", "INACTIVE"] = "ACTIVE"


class StatusUpdate(BaseModel):
    user_status: USER_STATUS


class VerifyRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginAttempt(BaseModel):
    success: bool


# ---------------------------------------------------------------------------
# Che dữ liệu
# ---------------------------------------------------------------------------
def mask_user(row: dict) -> dict:
    """Bỏ password_hash và che toàn bộ PII.

    Hàm này là cửa duy nhất mà một dòng app_user được phép đi qua để ra ngoài.
    Viết theo kiểu dựng dict mới thay vì xoá khoá khỏi dict cũ: thêm cột vào
    bảng thì cột đó KHÔNG tự động lọt ra response.
    """
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
        "customer_id": row.get("customer_id"),
        # Không có dữ liệu thì trả null, không trả chuỗi rỗng: chuỗi rỗng dễ bị
        # hiểu thành "tên rỗng" thay vì "chưa có tên". 50 tài khoản khách hàng
        # trong bảng đều để trống full_name nên đây là trường hợp thường gặp.
        "full_name_masked": mask_name(row["full_name"]) if row.get("full_name") else None,
        "email_masked": mask_email(row["email"]) if row.get("email") else None,
        "phone_masked": mask_phone(row["phone_no"]) if row.get("phone_no") else None,
        "user_status": row["user_status"],
        "failed_login_count": row["failed_login_count"],
        "last_login_at": row.get("last_login_at"),
        "created_at": row.get("created_at"),
    }


USER_COLUMNS = (
    "user_id, username, role, customer_id, full_name, email, phone_no, "
    "user_status, failed_login_count, last_login_at, created_at"
)


def _fetch_user(user_id: int) -> dict:
    row = query_one(f"select {USER_COLUMNS} from app_user where user_id = %s", (user_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Không có người dùng {user_id}")
    return row


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@app.get("/users", tags=["users"], summary="Danh sách tài khoản đăng nhập")
def list_users(
    role: USER_ROLE | None = Query(None, description="Lọc theo vai trò"),
    user_status: USER_STATUS | None = Query(None, description="Lọc theo trạng thái"),
    q: str | None = Query(None, description="Tìm theo tên đăng nhập, khớp tiền tố"),
    limit: int = Query(50, ge=1, le=200),
):
    where, params = ["1 = 1"], []
    if role:
        where.append("role = %s")
        params.append(role)
    if user_status:
        where.append("user_status = %s")
        params.append(user_status)
    if q:
        where.append("username like %s")
        params.append(f"{q}%")
    params.append(limit)
    rows = query(
        f"select {USER_COLUMNS} from app_user where {' and '.join(where)} "
        f"order by user_id limit %s",
        tuple(params),
    )
    return {"count": len(rows), "users": [mask_user(r) for r in rows]}


@app.get("/users/summary", tags=["users"], summary="Thống kê theo vai trò và trạng thái")
def users_summary():
    # Đặt TRƯỚC /users/{user_id}: FastAPI khớp route theo thứ tự khai báo, để sau
    # thì "summary" bị bắt làm user_id và trả 422.
    rows = query(
        "select role, user_status, count(*) as total from app_user "
        "group by role, user_status order by role, user_status"
    )
    return {
        "total": sum(r["total"] for r in rows),
        "breakdown": rows,
    }


@app.get("/users/by-username/{username}", tags=["users"], summary="Tra cứu theo tên đăng nhập")
def user_by_username(username: str):
    row = query_one(f"select {USER_COLUMNS} from app_user where username = %s", (username,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"Không có tài khoản '{username}'")
    return mask_user(row)


@app.get("/users/{user_id}", tags=["users"], summary="Chi tiết một tài khoản")
def get_user(user_id: int):
    return mask_user(_fetch_user(user_id))


@app.get("/users/{user_id}/permissions", tags=["users"], summary="Quyền hiệu lực của một tài khoản")
def user_permissions(user_id: int):
    user = _fetch_user(user_id)
    role = query_one(
        "select role_code, role_name, role_scope, permissions, role_status "
        "from app_role where role_code = %s",
        (user["role"],),
    )
    if role is None:
        # Nói rõ là CHƯA ĐỊNH NGHĨA, khác hẳn với "vai trò không có quyền nào".
        # app_role đang trống nên đây là trường hợp thường gặp, và nhầm hai thứ
        # này sẽ dẫn tới việc chặn nhầm người dùng hợp lệ.
        return {
            "user_id": user_id,
            "role": user["role"],
            "role_defined": False,
            "effective_permissions": [],
            "note": f"Vai trò '{user['role']}' chưa được khai báo trong app_role.",
        }
    active = role["role_status"] == "ACTIVE" and user["user_status"] == "ACTIVE"
    return {
        "user_id": user_id,
        "role": user["role"],
        "role_defined": True,
        "role_status": role["role_status"],
        "user_status": user["user_status"],
        # Vai trò ngừng hoạt động hoặc tài khoản bị khoá thì quyền không còn
        # hiệu lực, dù bảng vẫn ghi danh sách quyền.
        "effective_permissions": role["permissions"] if active else [],
        "declared_permissions": role["permissions"],
    }


@app.get("/customers/{customer_id}/user", tags=["users"], summary="Tài khoản đăng nhập của một khách hàng")
def user_of_customer(customer_id: int):
    row = query_one(
        f"select {USER_COLUMNS} from app_user where customer_id = %s order by user_id limit 1",
        (customer_id,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Khách hàng {customer_id} chưa có tài khoản đăng nhập")
    return mask_user(row)


@app.patch("/users/{user_id}/status", tags=["users"], summary="Đổi trạng thái tài khoản")
def update_status(user_id: int, payload: StatusUpdate):
    _fetch_user(user_id)
    # Mở khoá thì đặt lại bộ đếm: không đặt lại thì lần sai tiếp theo khoá ngay.
    reset = ", failed_login_count = 0" if payload.user_status == "ACTIVE" else ""
    row = execute_returning(
        f"update app_user set user_status = %s{reset}, updated_at = now() "
        f"where user_id = %s returning {USER_COLUMNS}",
        (payload.user_status, user_id),
    )
    return mask_user(row)


@app.post("/users/{user_id}/login-attempt", tags=["users"], summary="Ghi nhận một lần đăng nhập")
def record_login(user_id: int, payload: LoginAttempt):
    """Ghi kết quả đăng nhập và tự khoá khi sai quá nhiều lần.

    Tách khỏi /auth/verify để bên gọi có thể ghi nhận cả những lần đăng nhập đi
    qua kênh khác (OTP, sinh trắc học) mà service này không xác thực.
    """
    _fetch_user(user_id)
    if payload.success:
        row = execute_returning(
            f"update app_user set failed_login_count = 0, last_login_at = now(), "
            f"updated_at = now() where user_id = %s returning {USER_COLUMNS}",
            (user_id,),
        )
    else:
        row = execute_returning(
            f"update app_user set failed_login_count = failed_login_count + 1, "
            f"user_status = case when failed_login_count + 1 >= %s and user_status = 'ACTIVE' "
            f"then 'LOCKED' else user_status end, updated_at = now() "
            f"where user_id = %s returning {USER_COLUMNS}",
            (MAX_FAILED_LOGIN, user_id),
        )
    return {"locked": row["user_status"] == "LOCKED", "user": mask_user(row)}


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
@app.get("/roles", tags=["roles"], summary="Danh sách vai trò")
def list_roles():
    rows = query(
        "select role_code, role_name, role_scope, permissions, role_status, created_at "
        "from app_role order by role_code"
    )
    # Vai trò đang được dùng trong app_user nhưng chưa khai báo — chỉ ra ngay ở
    # đây thay vì để người dùng tự đối chiếu hai danh sách.
    used = {r["role"] for r in query("select distinct role from app_user")}
    return {
        "count": len(rows),
        "roles": rows,
        "undeclared_roles_in_use": sorted(used - {r["role_code"] for r in rows}),
    }


@app.get("/roles/{role_code}", tags=["roles"], summary="Chi tiết một vai trò")
def get_role(role_code: str):
    row = query_one(
        "select role_code, role_name, role_scope, permissions, role_status, created_at "
        "from app_role where role_code = %s",
        (role_code,),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Không có vai trò '{role_code}'")
    return row


@app.put("/roles/{role_code}", tags=["roles"], summary="Tạo hoặc cập nhật vai trò")
def upsert_role(role_code: str, payload: RoleUpsert):
    """Idempotent: gọi lại với cùng nội dung không tạo bản ghi thứ hai.

    Chọn PUT thay vì POST vì role_code do bên gọi đặt, không phải service sinh.
    """
    import json

    row = execute_returning(
        "insert into app_role (role_code, role_name, role_scope, permissions, role_status) "
        "values (%s, %s, %s, %s::jsonb, %s) "
        "on conflict (role_code) do update set role_name = excluded.role_name, "
        "role_scope = excluded.role_scope, permissions = excluded.permissions, "
        "role_status = excluded.role_status "
        "returning role_code, role_name, role_scope, permissions, role_status, created_at",
        (role_code, payload.role_name, payload.role_scope,
         json.dumps(payload.permissions), payload.role_status),
    )
    return row


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/auth/verify", tags=["auth"], summary="Xác thực tên đăng nhập và mật khẩu")
def verify(payload: VerifyRequest):
    """So khớp mật khẩu với password_hash.

    Đây là nơi DUY NHẤT đọc password_hash, và nó không bao giờ đi ra response.

    Sai mật khẩu và không có tài khoản đều trả về cùng một câu trả lời: biết
    được tên đăng nhập nào tồn tại là đã đủ để dò danh sách người dùng.
    """
    row = query_one(
        f"select {USER_COLUMNS}, password_hash from app_user where username = %s",
        (payload.username,),
    )
    if row is None:
        return {"authenticated": False, "reason": "invalid_credentials"}

    stored = row.pop("password_hash")
    try:
        ok = bcrypt.checkpw(payload.password.encode(), stored.encode())
    except ValueError:
        # Hash sai định dạng là lỗi dữ liệu, không phải sai mật khẩu.
        ok = False

    if not ok:
        return {"authenticated": False, "reason": "invalid_credentials"}
    if row["user_status"] != "ACTIVE":
        # Đúng mật khẩu nhưng tài khoản khoá: nói rõ để giao diện hướng dẫn được,
        # vì lúc này người gọi đã chứng minh họ là chủ tài khoản.
        return {"authenticated": False, "reason": row["user_status"].lower(), "user": mask_user(row)}
    return {"authenticated": True, "user": mask_user(row)}


# ---------------------------------------------------------------------------
# Common endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/db", tags=["meta"])
def health_db():
    return {"service": SERVICE_NAME, **db_health()}


AGENT_TOOLS = [
    tool("list_users", "Danh sách tài khoản đăng nhập, lọc theo vai trò hoặc trạng thái",
         "GET", "/users", params={"role": "str", "user_status": "str", "q": "str", "limit": "int"},
         returns="count, users[] đã che PII"),
    tool("get_user", "Chi tiết một tài khoản", "GET", "/users/{user_id}",
         returns="tài khoản đã che PII"),
    tool("user_permissions", "Quyền hiệu lực của một tài khoản", "GET", "/users/{user_id}/permissions",
         returns="role_defined, effective_permissions[]"),
    tool("user_of_customer", "Tài khoản đăng nhập của một khách hàng", "GET", "/customers/{customer_id}/user",
         returns="tài khoản đã che PII"),
    tool("list_roles", "Danh sách vai trò và quyền", "GET", "/roles",
         returns="roles[], undeclared_roles_in_use[]"),
    tool("users_summary", "Thống kê tài khoản theo vai trò và trạng thái", "GET", "/users/summary",
         returns="total, breakdown[]"),
]


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    return agent_tools_payload(SERVICE_NAME, "IDENTITY_SERVICE_URL", AGENT_TOOLS)


@app.get("/info", tags=["meta"])
def info():
    return service_info(SERVICE_NAME, PURPOSE, OWNS_TABLES, AGENT_TOOLS)
