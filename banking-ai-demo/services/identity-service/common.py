"""Lớp dùng chung cho 5 service của banking-ai-demo.

Nguồn sự thật đặt tại `services/_common/common.py`; `make sync-common` copy file này
vào từng service vì Docker build context là `services/<name>` nên không thấy được
thư mục cha. KHÔNG sửa trực tiếp bản copy trong service.

Gồm 3 nhóm:
  1. Kết nối PostgreSQL (pool lazy, cast varchar-core sang số) và dịch lỗi
     cơ sở dữ liệu thành mã trạng thái có nghĩa.
  2. Mask PII — schema quy định full_name / legal_id / phone_no / email / street /
     date_of_birth không được ra API cho agent hay vào prompt LLM.
  3. Helper dựng manifest `GET /agent/tools` để agent tự discover endpoint.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------------------
# 1. Kết nối DB
# ---------------------------------------------------------------------------
VN_TZ = timezone(timedelta(hours=7))


def dsn() -> str:
    """DSN ưu tiên DATABASE_URL, fallback ghép từ các biến PG* chuẩn libpq."""
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "anhnv20")
    password = os.getenv("PGPASSWORD", "")
    database = os.getenv("PGDATABASE", "ea-hackathon")
    sslmode = os.getenv("PGSSLMODE", "prefer")
    return (
        f"host={host} port={port} user={user} password={password} "
        f"dbname={database} sslmode={sslmode}"
    )


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Pool khởi tạo lười: service vẫn start được khi DB chưa sẵn sàng, nhờ đó
    livenessProbe `/health` (shallow) không giết pod trong lúc DB khởi động."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn(),
            min_size=int(os.getenv("DB_POOL_MIN", "1")),
            max_size=int(os.getenv("DB_POOL_MAX", "5")),
            timeout=float(os.getenv("DB_POOL_TIMEOUT", "10")),
            kwargs={"row_factory": dict_row, "autocommit": True},
            open=True,
            check=ConnectionPool.check_connection,
        )
    return _pool


@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    """Chạy câu lệnh ghi, trả về số dòng bị ảnh hưởng."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def execute_returning(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


@contextmanager
def transaction():
    """Transaction tường minh cho luồng ghi nhiều bảng (pool đang autocommit)."""
    with pool().connection() as conn:
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True


def db_health() -> dict:
    try:
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
        return {"database": "ok"}
    except Exception as exc:  # noqa: BLE001 - trả nguyên nhân cho ops, không raise
        return {"database": "error", "detail": type(exc).__name__ + ": " + str(exc)[:200]}


# ---------------------------------------------------------------------------
# 1b. Lỗi cơ sở dữ liệu → mã trạng thái nói được điều gì đó
# ---------------------------------------------------------------------------
# Không có lớp này thì một id sai của người gọi đi thẳng xuống Postgres, vỡ ràng
# buộc, và FastAPI trả `500 Internal Server Error`. Trợ lý đọc nguyên văn thân
# phản hồi đó, nên nó không phân biệt được "tra sai id, nên tra lại" với "hệ
# thống hỏng, nên dừng" — và điều phối sai. Ở đây dịch lỗi của Postgres thành mã
# trạng thái và một câu tiếng Việt.
#
# Nhận diện bằng sqlstate chứ không bằng tên lớp: sqlstate là mã chuẩn của
# Postgres, còn cây thừa kế của driver thì có thể đổi giữa các phiên bản.
_KEY_THIEU_CHA = re.compile(
    r'Key \((?P<cot>[^)]+)\)=\((?P<gia_tri>.*)\) is not present in table "(?P<bang>[^"]+)"', re.S
)
_KEY_CON_THAM_CHIEU = re.compile(
    r'Key \((?P<cot>[^)]+)\)=\((?P<gia_tri>.*)\) is still referenced from table "(?P<bang>[^"]+)"', re.S
)
_KEY_TRUNG = re.compile(
    r'Key \((?P<cot>[^)]+)\)=\((?P<gia_tri>.*)\) already exists', re.S
)


# `legal_id` kết thúc bằng `_id` nhưng là số giấy tờ — schema cấm nó ra API, nên
# phải loại tay khỏi heuristic bên dưới.
_COT_ID_NHUNG_LA_PII = {"legal_id"}


def _neu_duoc_gia_tri(cot: str, gia_tri: str) -> str | None:
    """Chỉ nêu giá trị khi mọi cột trong khóa đều là cột id, và không phải PII.

    Giá trị lấy từ thông điệp của Postgres có thể là bất cứ thứ gì người gọi
    gửi lên. Schema cấm full_name/legal_id/phone_no/email ra API, nên cột không
    đạt điều kiện thì chỉ nêu tên cột, giấu giá trị."""
    cot_list = [c.strip() for c in cot.split(",")]
    la_id = all(
        (c == "id" or c.endswith("_id")) and c not in _COT_ID_NHUNG_LA_PII
        for c in cot_list
    )
    if la_id and len(gia_tri) <= 80:
        return gia_tri
    return None


def db_error_status(exc: psycopg.Error) -> tuple[int, str] | None:
    """(mã trạng thái, câu tiếng Việt) cho lỗi do dữ liệu người gọi gây ra.

    Trả `None` khi lỗi không phải lỗi của người gọi — SQL sai cú pháp chẳng hạn.
    Chỗ gọi phải để lỗi đó nổ tiếp thành 500, vì đó là bug của service và che nó
    đi thì không ai biết mà sửa."""
    state = exc.sqlstate or ""
    diag = getattr(exc, "diag", None)
    chi_tiet = (getattr(diag, "message_detail", None) or "") if diag else ""
    bang = (getattr(diag, "table_name", None) or "") if diag else ""
    cot = (getattr(diag, "column_name", None) or "") if diag else ""
    rang_buoc = (getattr(diag, "constraint_name", None) or "") if diag else ""

    if state == "23503":  # foreign_key_violation
        m = _KEY_THIEU_CHA.search(chi_tiet)
        if m:
            gia_tri = _neu_duoc_gia_tri(m.group("cot"), m.group("gia_tri"))
            if gia_tri:
                return 404, f"{m.group('bang')} {gia_tri} không tồn tại"
            return 404, f"{m.group('cot')} tham chiếu tới {m.group('bang')} không tồn tại"
        m = _KEY_CON_THAM_CHIEU.search(chi_tiet)
        if m:
            gia_tri = _neu_duoc_gia_tri(m.group("cot"), m.group("gia_tri"))
            chu_the = f"{bang} {gia_tri}" if bang and gia_tri else (bang or "bản ghi")
            return 409, f"không xóa được {chu_the} vì còn bản ghi {m.group('bang')} tham chiếu"
        return 404, f"tham chiếu tới bản ghi không tồn tại ({rang_buoc or 'khóa ngoại'})"

    if state == "23505":  # unique_violation
        m = _KEY_TRUNG.search(chi_tiet)
        if m:
            gia_tri = _neu_duoc_gia_tri(m.group("cot"), m.group("gia_tri"))
            mo_ta = f"{m.group('cot')} {gia_tri}" if gia_tri else m.group("cot")
            return 409, f"{bang or 'bản ghi'} với {mo_ta} đã tồn tại"
        return 409, f"bản ghi đã tồn tại ({rang_buoc or 'khóa duy nhất'})"

    if state == "23502":  # not_null_violation
        return 422, f"thiếu giá trị bắt buộc cho {cot or 'một cột'} của {bang or 'bản ghi'}"

    if state == "23514":  # check_violation
        return 422, f"giá trị không hợp lệ theo ràng buộc {rang_buoc or 'kiểm tra'}"

    if state.startswith("22"):  # data_exception, gồm 22P02 uuid sai định dạng
        # Khác các nhánh trên, chuỗi này chứa nguyên văn giá trị người gọi gửi lên
        # chứ không phải một id đã lọc, nên vừa cắt ngắn vừa mask trước khi trả ra.
        primary = (getattr(diag, "message_primary", None) or str(exc)) if diag else str(exc)
        primary = mask_free_text(primary.strip()) or ""
        if len(primary) > 120:
            primary = primary[:120] + "…"
        return 422, f"giá trị không đúng định dạng: {primary}"

    # 08xxx mất kết nối; 53xxx hết tài nguyên (hết connection, hết đĩa, hết bộ nhớ);
    # 57P01/02/03 Postgres đang tắt hoặc đang khởi động. Cả ba nhóm đều là "lát nữa
    # thử lại", khác hẳn 500 vốn bảo người gọi rằng dừng lại đi.
    if (
        state.startswith("08")
        or state.startswith("53")
        or state in {"57P01", "57P02", "57P03"}
        or (not state and isinstance(exc, psycopg.OperationalError))
    ):
        # Gồm cả psycopg_pool.PoolTimeout, vốn kế thừa OperationalError.
        return 503, "cơ sở dữ liệu tạm thời không truy cập được, thử lại sau"

    return None


def install_db_error_handlers(app) -> None:
    """Dịch lỗi Postgres thành 4xx/5xx có nghĩa cho mọi endpoint của service.

    Đây là lưới đỡ chứ không thay cho cổng kiểm tra tại endpoint: cổng cho câu
    trả lời đúng giọng nghiệp vụ, còn lưới bảo đảm endpoint viết sau này cũng
    không rơi về 500 trống nghĩa."""
    from fastapi.responses import JSONResponse

    @app.exception_handler(psycopg.Error)
    def _loi_co_so_du_lieu(_request, exc: psycopg.Error):
        ket_qua = db_error_status(exc)
        if ket_qua is None:
            raise exc  # bug của service — phải là 500 để còn thấy mà sửa
        ma, cau = ket_qua
        # KHÔNG mask lại ở đây: `_ID_RE` thay mọi dãy 9-12 chữ số bằng `***`, nên
        # nó băm nát chính cái id mà người gọi cần để tra lại — `transaction 800012345
        # không tồn tại` thành `transaction *** không tồn tại`. Giá trị đi tới đây đã
        # qua `_neu_duoc_gia_tri`, còn văn bản tự do của server thì mask ngay tại nhánh 22xxx.
        return JSONResponse(status_code=ma, content={"detail": cau})


# ---------------------------------------------------------------------------
# 2. Cast varchar-core sang số / ngày giờ
# ---------------------------------------------------------------------------
def num(value: Any, default: Decimal | int = 0) -> Decimal:
    """Các cột tiền/lãi suất để varchar theo core T24 → cast trước khi tính."""
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal(default)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(num(value, default))
    except (InvalidOperation, ValueError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(num(value, Decimal(str(default))))
    except (InvalidOperation, ValueError):
        return default


def ymd_to_iso(ymd: str | None) -> str | None:
    """'20260315' -> '2026-03-15'."""
    if not ymd or len(ymd) != 8 or not ymd.isdigit():
        return None
    return f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"


def hms_to_iso(hms: str | None) -> str | None:
    """'193045' -> '19:30:45'."""
    if not hms or len(hms) != 6 or not hms.isdigit():
        return None
    return f"{hms[0:2]}:{hms[2:4]}:{hms[4:6]}"


def tx_datetime(date_ymd: str, time_hms: str) -> datetime | None:
    iso_d, iso_t = ymd_to_iso(date_ymd), hms_to_iso(time_hms)
    if not iso_d or not iso_t:
        return None
    return datetime.fromisoformat(f"{iso_d}T{iso_t}").replace(tzinfo=VN_TZ)


def age_from_dob(dob_ymd: str | None, today: datetime | None = None) -> int | None:
    iso = ymd_to_iso(dob_ymd)
    if not iso:
        return None
    born = datetime.fromisoformat(iso).date()
    ref = (today or datetime.now(VN_TZ)).date()
    return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


# ---------------------------------------------------------------------------
# 3. Persona
# ---------------------------------------------------------------------------
TARGET_TO_PERSONA = {"1": "SALARY", "2": "HNW", "3": "SENIOR"}
PERSONAS = ("SALARY", "HNW", "SENIOR")


def persona_of(target: str | None, date_of_birth: str | None = None) -> str:
    """target T24 quyết định persona; tuổi >= 60 luôn nâng lên SENIOR."""
    age = age_from_dob(date_of_birth)
    if age is not None and age >= 60:
        return "SENIOR"
    return TARGET_TO_PERSONA.get((target or "").strip(), "SALARY")


# ---------------------------------------------------------------------------
# 4. Mask PII
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(r"(?<!\d)(0\d{8,10}|\+84\d{8,10})(?!\d)")
_ID_RE = re.compile(r"(?<!\d)\d{9,12}(?!\d)")


def mask_name(full_name: str | None) -> str:
    """'NGUYEN VAN MINH' -> 'NGUYEN VAN M***'."""
    if not full_name:
        return ""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0][:1] + "***"
    return " ".join(parts[:-1] + [parts[-1][:1] + "***"])


def mask_phone(phone: str | None) -> str | None:
    """'0912345678' -> '09** *** 678'."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 5:
        return "*" * len(digits)
    return f"{digits[:2]}** *** {digits[-3:]}"


def mask_account(account_no: str | int | None) -> str | None:
    """'0301234567' -> '0301 ****'."""
    if account_no is None:
        return None
    s = str(account_no)
    return f"{s[:4]} ****" if len(s) > 4 else "****"


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


def mask_legal_id(_legal_id: str | None) -> str | None:
    """CCCD/hộ chiếu là PII nhạy cảm nhất: mask toàn bộ, chỉ báo có hay không."""
    return "************" if _legal_id else None


def mask_free_text(text: str | None) -> str | None:
    """Mask SĐT/CCCD lẫn trong nội dung CK hoặc free text khách nhập."""
    if not text:
        return text
    masked = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)) or "***", text)
    return _ID_RE.sub("***", masked)


def mask_customer_row(row: dict) -> dict:
    """Chuyển 1 dòng `customer` thành payload an toàn cho agent/LLM.

    Các cột PII gốc (full_name, street, legal_id, date_of_birth, email, phone_no)
    bị loại bỏ hoàn toàn, chỉ giữ bản đã mask và các trường dẫn xuất.
    """
    return {
        "customer_id": row["customer_id"],
        "name_masked": mask_name(row.get("full_name")),
        "persona": persona_of(row.get("target"), row.get("date_of_birth")),
        "target": row.get("target"),
        "age": age_from_dob(row.get("date_of_birth")),
        "nationality": row.get("nationality"),
        "legal_type": row.get("legal_type"),
        "legal_id_masked": mask_legal_id(row.get("legal_id")),
        "phone_masked": mask_phone(row.get("phone_no")),
        "email_masked": mask_email(row.get("email")),
        "co_code": row.get("co_code"),
    }


def mask_beneficiary_row(row: dict, include_full: bool = False) -> dict:
    """Mặc định chỉ `*_masked` được ra ngoài; beneficiary_account_no /
    beneficiary_name thì không (đường dữ liệu đưa vào LLM/agent).

    include_full=True trả kèm `name` + `account_no` đầy đủ — CHỈ dùng cho màn
    danh bạ trên app khách hàng (khách xem danh bạ của chính mình), tuyệt đối
    không bật ở các đường gọi phục vụ ngữ cảnh agent."""
    out = {
        "beneficiary_id": row["beneficiary_id"],
        "customer_id": row.get("customer_id"),
        "bank_code": row.get("beneficiary_bank_code"),
        "account_masked": row.get("beneficiary_account_masked"),
        "name_masked": row.get("beneficiary_name_masked"),
        "type": row.get("beneficiary_type"),
        "relationship": row.get("beneficiary_relationship"),
        "first_seen_at": row.get("beneficiary_first_seen_at"),
        "last_tx_at": row.get("beneficiary_last_tx_at"),
        "tx_count": row.get("beneficiary_tx_count"),
        "total_out": row.get("beneficiary_total_out"),
        "total_in": row.get("beneficiary_total_in"),
        "avg_amount": row.get("beneficiary_avg_amount"),
        "age_days": row.get("beneficiary_age_days"),
        "status": row.get("beneficiary_status"),
        "is_new": row.get("beneficiary_first_seen_at") is None,
    }
    if include_full:
        out["name"] = row.get("beneficiary_name")
        out["account_no"] = row.get("beneficiary_account_no")
    return out


# ---------------------------------------------------------------------------
# 5. Manifest cho agent
# ---------------------------------------------------------------------------
def tool(
    name: str,
    description: str,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    returns: str = "",
) -> dict:
    """Một tool definition cho agent: đủ để agent sinh lời gọi HTTP mà không cần đọc /docs."""
    return {
        "name": name,
        "description": description,
        "method": method.upper(),
        "path": path,
        "parameters": params or {},
        "body_schema": body or {},
        "returns": returns,
    }


def agent_tools_payload(service: str, base_url_env: str, tools: Iterable[dict]) -> dict:
    return {
        "service": service,
        "base_url": os.getenv(base_url_env, f"http://{service}"),
        "protocol": "rest+openapi",
        "openapi": "/openapi.json",
        "pii_policy": (
            "Mọi response đã mask PII. Không có trường full_name/legal_id/phone_no/"
            "email/street/date_of_birth, và không có is_fraud/fraud_case_id. "
            "Agent được phép đưa thẳng response vào prompt LLM."
        ),
        "tools": list(tools),
    }


def service_info(service: str, description: str, tables: list[str], tools: list[dict]) -> dict:
    return {
        "service": service,
        "description": description,
        "owns_tables": tables,
        "database": os.getenv("PGDATABASE", "ea-hackathon"),
        "endpoints": [f"{t['method']} {t['path']}" for t in tools],
    }


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


# ---------------------------------------------------------------------------
# 6. Swagger / OpenAPI
# ---------------------------------------------------------------------------
# FastAPI đã tự phục vụ /docs, /redoc và /openapi.json. Phần dưới bổ sung những
# thứ FastAPI không tự suy ra được: mô tả cho từng nhóm endpoint, địa chỉ thật của
# service khi đã deploy (để nút "Try it out" gọi đúng chỗ thay vì gọi localhost),
# và một trang gốc chỉ đường.
#
# Lưu ý khi chạy trong cluster không có đường ra Internet: Swagger UI mặc định tải
# JS/CSS từ CDN jsdelivr. Đặt SWAGGER_JS_URL và SWAGGER_CSS_URL trỏ tới bản tự host
# nếu môi trường chặn egress, còn /openapi.json thì luôn dùng được.

COMMON_TAGS = [
    {
        "name": "meta",
        "description": "Kiểm tra sức khỏe, thông tin service và manifest cho agent. "
        "`/health` cố tình không chạm database để sự cố DB không làm Kubernetes "
        "khởi động lại pod; dùng `/health/db` khi cần biết trạng thái kết nối.",
    },
]


def openapi_description(service: str, purpose: str, tables: list[str]) -> str:
    """Phần mô tả hiển thị ngay đầu trang Swagger của service."""
    return f"""{purpose}

**Sở hữu dữ liệu:** {', '.join(f'`{t}`' for t in tables)}

Service này chỉ đọc và ghi các bảng của chính nó; dữ liệu thuộc service khác được
lấy qua HTTP, không truy vấn chéo database.

### Dành cho agent
Gọi `GET /agent/tools` để nhận danh sách tool kèm mô tả, tham số và kiểu trả về —
agent không cần đọc trang này hay hardcode đường dẫn.

### Về dữ liệu cá nhân
Mọi response đã loại bỏ hoặc che thông tin định danh (họ tên đầy đủ, số giấy tờ,
số điện thoại, email, địa chỉ, ngày sinh) và hai cột kiểm thử `is_fraud`,
`fraud_case_id`. Kết quả trả về an toàn để đưa thẳng vào prompt LLM.
"""


def setup_docs(app, service: str) -> None:
    """Gắn địa chỉ thật của service vào OpenAPI và thêm trang gốc chỉ đường."""
    from fastapi.responses import JSONResponse

    public_url = os.getenv("PUBLIC_BASE_URL")

    if public_url:
        # Không có dòng này thì nút "Try it out" trên Swagger gọi vào host của trang
        # thay vì địa chỉ thật của service khi đứng sau ingress hoặc reverse proxy.
        app.servers = [
            {"url": public_url, "description": "Môi trường đang chạy"},
            {"url": "/", "description": "Tương đối theo host hiện tại"},
        ]

    @app.get("/", include_in_schema=False)
    def _root():
        return JSONResponse(
            {
                "service": service,
                "docs": "/docs",
                "redoc": "/redoc",
                "openapi": "/openapi.json",
                "agent_tools": "/agent/tools",
                "health": "/health",
            }
        )
