#!/usr/bin/env python3
"""Sinh seed.sql cho MSB AI Financial Guardian.

Chạy: python3 db/generate_seed.py > db/seed.sql

Tạo ra 10 khách hàng phủ đủ ba phân khúc của T24 (target 1 = SALARY, 2 = HNW,
3 = SENIOR) cùng 6 tháng lịch sử thanh toán, baseline hành vi tính từ chính lịch
sử đó, và 10 fraud case dùng làm bộ kiểm thử engine.

Ba điểm đáng lưu ý về cách sinh dữ liệu:

  · Dữ liệu tất định. random.seed cố định nên chạy lại cho ra đúng cùng một bộ
    dữ liệu; khi engine đổi điểm số, ta biết chắc là do engine đổi chứ không phải
    do dữ liệu đổi.

  · behavior_profile được tính từ chính các giao dịch vừa sinh, bằng đúng công
    thức mà transaction-service dùng, và chỉ tính trên giao dịch sạch
    (OUT / POSTED / is_fraud = 'N'). Nếu để giao dịch gian lận lọt vào baseline
    thì chính hành vi lừa đảo sẽ trở thành "bình thường".

  · Các giao dịch gian lận được gắn is_fraud = 'Y' và fraud_case_id. Hai cột này
    chỉ dùng cho kiểm thử, service đã loại chúng khỏi mọi response.

Thứ tự INSERT tuân theo ràng buộc khóa ngoại; riêng cặp vòng giữa risk_decision
và transaction_history được xử lý bằng cách chèn risk_decision với transaction_id
NULL trước, rồi UPDATE lại ở cuối.
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

SEED = 20260915
random.seed(SEED)

TODAY = date(2026, 9, 15)
HISTORY_DAYS = 184           # ~6 tháng lịch sử thanh toán
BASELINE_WINDOW = 90         # behavior_profile.window_days
START = TODAY - timedelta(days=HISTORY_DAYS)


# ---------------------------------------------------------------------------
# Tiện ích sinh SQL
# ---------------------------------------------------------------------------
def sql_value(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def insert(table: str, columns: list[str], rows: list[tuple]) -> str:
    if not rows:
        return f"-- (không có dòng nào cho {table})\n"
    head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES\n"
    body = ",\n".join("  (" + ", ".join(sql_value(v) for v in row) + ")" for row in rows)
    return head + body + ";\n\n"


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def hms(h: int, m: int, s: int) -> str:
    return f"{h:02d}{m:02d}{s:02d}"


def ts(d: date, h: int, m: int = 0, s: int = 0) -> str:
    return f"{d.isoformat()} {h:02d}:{m:02d}:{s:02d}+07"


def mask_name(name: str) -> str:
    parts = name.split()
    return " ".join(parts[:-1] + [parts[-1][:1] + "***"])


def mask_acct(no: str) -> str:
    return f"{no[:4]} ****"


def rnd_amount(lo: int, hi: int, step: int = 1000) -> int:
    return random.randrange(lo // step, hi // step + 1) * step


# ---------------------------------------------------------------------------
# 1. Danh mục sản phẩm
# ---------------------------------------------------------------------------
PRODUCTS = [
    (1,  "Tiết kiệm Măng Non 6 tháng",        "SAVINGS",    "VND", "ACTIVE",      "5.2"),
    (2,  "Tiết kiệm Online 12 tháng",          "SAVINGS",    "VND", "ACTIVE",      "5.8"),
    (3,  "Tiết kiệm Linh hoạt không kỳ hạn",   "SAVINGS",    "VND", "ACTIVE",      "0.5"),
    (4,  "Tiết kiệm Tích lũy An nhàn",         "SAVINGS",    "VND", "ACTIVE",      "6.1"),
    (5,  "Chứng chỉ Quỹ Trái phiếu MSB",       "INVESTMENT", "VND", "ACTIVE",      "7.4"),
    (6,  "Quỹ Cân bằng MSB Balanced",          "INVESTMENT", "VND", "ACTIVE",      "9.2"),
    (7,  "Tài khoản Thanh toán Chuẩn",         "SAVINGS",    "VND", "ACTIVE",      "0.1"),
    (8,  "Vay Tiêu dùng Tín chấp",             "LOAN",       "VND", "ACTIVE",      "12.5"),
    (9,  "Vay Mua nhà An cư",                  "LOAN",       "VND", "ACTIVE",      "8.5"),
    (10, "Vay Mua ô tô",                       "LOAN",       "VND", "ACTIVE",      "9.0"),
    (11, "Thẻ tín dụng MSB Visa Platinum",     "CARD",       "VND", "ACTIVE",      "24.0"),
    (12, "Bảo hiểm Nhân thọ An Tâm",           "INSURANCE",  "VND", "ACTIVE",      None),
    (13, "Quỹ Cổ phiếu Tăng trưởng",           "INVESTMENT", "VND", "COMING_SOON", "11.0"),
]

# Kỳ hạn gửi tiết kiệm — term_months=0 là không kỳ hạn, dùng để xếp cột biểu lãi suất.
RATE_TERMS = [
    ("KKH", 0,  "Không kỳ hạn"),
    ("T01", 1,  "1 tháng"),
    ("T03", 3,  "3 tháng"),
    ("T06", 6,  "6 tháng"),
    ("T09", 9,  "9 tháng"),
    ("T12", 12, "12 tháng"),
    ("T18", 18, "18 tháng"),
    ("T24", 24, "24 tháng"),
]

# Lãi suất %/năm theo (product_id → {term_code: rate}) cho hai đợt hiệu lực:
# đợt cũ 2026-06-01 và đợt hiện hành 2026-09-01 (thấp hơn/cao hơn ~0.2 điểm) —
# để endpoint "lãi suất hiện tại" thực sự phải chọn MAX(effective_from).
# Sản phẩm 3 (linh hoạt) và 7 (tài khoản thanh toán) chỉ có lãi không kỳ hạn.
CURRENT_RATES = {
    1: {"T01": 3.6, "T03": 3.9, "T06": 5.2, "T09": 5.3, "T12": 5.5, "T18": 5.6, "T24": 5.6},
    2: {"T01": 3.9, "T03": 4.2, "T06": 5.5, "T09": 5.7, "T12": 5.8, "T18": 6.0, "T24": 6.1},
    4: {"T03": 4.0, "T06": 5.3, "T09": 5.6, "T12": 6.1, "T18": 6.2, "T24": 6.3},
    3: {"KKH": 0.5},
    7: {"KKH": 0.1},
}
RATE_EFFECTIVE_OLD = date(2026, 6, 1)
RATE_EFFECTIVE_CURRENT = date(2026, 9, 1)

interest_rate_rows: list[tuple] = []
_rate_id = 0
for effective, delta in ((RATE_EFFECTIVE_OLD, -0.2), (RATE_EFFECTIVE_CURRENT, 0.0)):
    for pid in sorted(CURRENT_RATES):
        for term_code, rate in CURRENT_RATES[pid].items():
            _rate_id += 1
            interest_rate_rows.append(
                (_rate_id, pid, term_code, round(max(rate + delta, 0.1), 2), effective.isoformat())
            )

# ---------------------------------------------------------------------------
# 2. Mười khách hàng — phủ đủ ba phân khúc
# ---------------------------------------------------------------------------
# (id, tên, target, ngày sinh, giới thiệu ngắn về hồ sơ tài chính)
CUSTOMERS = [
    # SALARY — người làm công ăn lương, thu nhập đều, chi tiêu nhiều giao dịch nhỏ
    (100001, "NGUYEN VAN MINH",   "1", "19920314", "Kỹ sư phần mềm, lương 32tr, có vay tiêu dùng"),
    (100002, "TRAN THI THU HA",   "1", "19950821", "Nhân viên marketing, lương 19tr, thuê nhà"),
    (100003, "LE HOANG NAM",      "1", "19880105", "Trưởng phòng, lương 45tr, vay mua nhà"),
    (100004, "PHAM THI LAN ANH",  "1", "19970612", "Nhân viên ngân hàng, lương 21tr, mới đi làm"),
    # HNW — khách ưu tiên, giao dịch lớn, nhiều người nhận mới mỗi tháng
    (100005, "VU QUANG DUNG",     "2", "19780923", "Chủ doanh nghiệp xây dựng, dòng tiền lớn"),
    (100006, "DANG THI MY LINH",  "2", "19831130", "Chủ chuỗi nhà hàng, giao dịch nhà cung cấp dày"),
    (100007, "HOANG MINH TUAN",   "2", "19750418", "Nhà đầu tư bất động sản, chuyển khoản giá trị cao"),
    # SENIOR — người cao tuổi, ít giao dịch, mỗi giao dịch đều đặn và nhỏ
    (100008, "NGUYEN THI BICH",   "3", "19580207", "Về hưu, lương hưu 8tr, sống cùng con gái"),
    (100009, "TRAN VAN HUNG",     "3", "19551019", "Về hưu quân đội, có sổ tiết kiệm lớn"),
    (100010, "LY THI KIM CUC",    "3", "19620525", "Về hưu giáo viên, hay gửi tiền cho cháu"),
]

SEGMENT_OF = {c[0]: {"1": "SALARY", "2": "HNW", "3": "SENIOR"}[c[2]] for c in CUSTOMERS}

STREETS = [
    "12 Lang Ha, Ba Dinh, Ha Noi", "45 Nguyen Trai, Thanh Xuan, Ha Noi",
    "88 Tran Hung Dao, Hoan Kiem, Ha Noi", "23 Le Loi, Quan 1, TP HCM",
    "156 Cach Mang Thang 8, Quan 3, TP HCM", "7 Hai Ba Trung, Hoan Kiem, Ha Noi",
    "301 Nguyen Van Cu, Long Bien, Ha Noi", "19 Bach Mai, Hai Ba Trung, Ha Noi",
    "64 Doi Can, Ba Dinh, Ha Noi", "210 Xa Dan, Dong Da, Ha Noi",
]
BRANCH = ["CA Ha Noi", "CA TP HCM", "CA Da Nang"]

customer_rows = []
for i, (cid, name, target, dob, _note) in enumerate(CUSTOMERS):
    customer_rows.append((
        cid, name, STREETS[i], target, "VN",
        f"0{random.randrange(10**10, 10**11)}"[:12],
        "CCCD", random.choice(BRANCH), dob,
        name.lower().replace(" ", ".") + "@example.com",
        "09" + str(random.randrange(10**7, 10**8)),
        "MSB",
    ))

# ---------------------------------------------------------------------------
# 3. Tài khoản, sổ tiết kiệm, khoản vay
# ---------------------------------------------------------------------------
# Số dư và cấu trúc tài sản khác hẳn nhau giữa ba phân khúc — đây là điều khiến
# cùng một số tiền chuyển đi lại mang ý nghĩa rủi ro rất khác nhau.
SEGMENT_PROFILE = {
    "SALARY": {
        "balance": (25_000_000, 90_000_000),
        "income": (19_000_000, 45_000_000),
        "tx_per_month": (38, 52),
        "benef_count": (7, 11),
        "hours": [7, 8, 9, 11, 12, 13, 18, 19, 20, 21, 22],
        "night_rate": 0.06,
    },
    "HNW": {
        "balance": (800_000_000, 2_500_000_000),
        "income": (180_000_000, 420_000_000),
        "tx_per_month": (62, 88),
        "benef_count": (14, 20),
        "hours": [7, 8, 9, 10, 11, 13, 14, 15, 16, 17, 19, 20, 21, 22],
        "night_rate": 0.04,
    },
    "SENIOR": {
        "balance": (40_000_000, 180_000_000),
        "income": (6_500_000, 12_000_000),
        "tx_per_month": (9, 15),
        "benef_count": (3, 5),
        "hours": [8, 9, 10, 11, 14, 15, 16, 17],
        "night_rate": 0.005,
    },
}

account_rows, deposit_rows, loan_rows = [], [], []
accounts_of: dict[int, list[dict]] = defaultdict(list)

acct_seq = 5000
dep_seq = 7000
loan_seq = 9000

for cid, name, target, dob, _ in CUSTOMERS:
    seg = SEGMENT_OF[cid]
    prof = SEGMENT_PROFILE[seg]

    acct_seq += 1
    main_id = acct_seq
    main_balance = rnd_amount(*prof["balance"], step=100_000)
    group = "ACCOUNT_SALARY" if seg == "SALARY" else (
        "ACCOUNT_BUSINESS" if seg == "HNW" else "ACCOUNT_CURRENT"
    )
    acct_name = {"SALARY": "TK luong", "HNW": "TK kinh doanh", "SENIOR": "TK huu tri"}[seg]
    account_rows.append((
        main_id, group, 7, cid, acct_name, "VND",
        str(main_balance), str(main_balance), "MSB",
    ))
    accounts_of[cid].append({"account_id": main_id, "balance": main_balance, "primary": True})

    # Khách HNW luôn có thêm tài khoản chi tiêu tách khỏi tài khoản kinh doanh
    if seg == "HNW":
        acct_seq += 1
        sub_balance = rnd_amount(120_000_000, 400_000_000, step=100_000)
        account_rows.append((
            acct_seq, "ACCOUNT_CURRENT", 7, cid, "TK chi tieu", "VND",
            str(sub_balance), str(sub_balance), "MSB",
        ))
        accounts_of[cid].append({"account_id": acct_seq, "balance": sub_balance, "primary": False})

    # Sổ tiết kiệm: người cao tuổi và khách ưu tiên gửi nhiều, người đi làm ít hơn
    n_dep = {"SALARY": random.randint(0, 1), "HNW": random.randint(2, 3), "SENIOR": random.randint(1, 2)}[seg]
    for _ in range(n_dep):
        dep_seq += 1
        term = random.choice([6, 12, 12, 24])
        start = TODAY - timedelta(days=random.randint(30, 300))
        maturity = start + timedelta(days=term * 30)
        amount = {
            "SALARY": rnd_amount(50_000_000, 200_000_000, 1_000_000),
            "HNW": rnd_amount(500_000_000, 3_000_000_000, 10_000_000),
            "SENIOR": rnd_amount(200_000_000, 900_000_000, 10_000_000),
        }[seg]
        pid = 2 if term >= 12 else 1
        deposit_rows.append((
            dep_seq, cid, pid, "VND", str(amount),
            PRODUCTS[pid - 1][5], "0.3", str(term),
            random.choice(["PRINCIPAL", "PRINCIPAL_INTEREST"]),
            random.choice(["MOBILE", "ONLINE", "BRANCH"]),
            ymd(start), ymd(maturity), main_id,
            "DEPOSIT_ONLINE" if term >= 12 else "DEPOSIT_TERM",
            str(main_id), str(main_id), "MSB",
        ))

    # Khoản vay: người đi làm vay tiêu dùng/mua nhà, khách ưu tiên vay kinh doanh
    if seg == "SALARY" and random.random() < 0.75:
        loan_seq += 1
        is_mortgage = random.random() < 0.4
        amount = rnd_amount(1_200_000_000, 2_500_000_000, 10_000_000) if is_mortgage \
            else rnd_amount(80_000_000, 400_000_000, 1_000_000)
        term = 240 if is_mortgage else random.choice([24, 36, 48])
        start = TODAY - timedelta(days=random.randint(200, 900))
        loan_rows.append((
            loan_seq, cid, "LOAN_MORTGAGE" if is_mortgage else "LOAN_CONSUMER",
            9 if is_mortgage else 8, main_id, "VND", str(amount),
            "8.5" if is_mortgage else "12.5", "1.2", str(term), "BRANCH",
            ymd(start), ymd(start + timedelta(days=term * 30)),
            str(main_id), str(main_id), "MSB",
        ))
    elif seg == "HNW" and random.random() < 0.6:
        loan_seq += 1
        amount = rnd_amount(3_000_000_000, 12_000_000_000, 100_000_000)
        start = TODAY - timedelta(days=random.randint(150, 700))
        loan_rows.append((
            loan_seq, cid, "LOAN_BUSINESS", 8, main_id, "VND", str(amount),
            "9.5", "2.0", "60", "BRANCH",
            ymd(start), ymd(start + timedelta(days=1800)),
            str(main_id), str(main_id), "MSB",
        ))

# ---------------------------------------------------------------------------
# 4. Người nhận
# ---------------------------------------------------------------------------
BANKS = ["MSB", "VCB", "TCB", "BIDV", "ACB", "VPB", "MBB", "TPB"]
PERSON_NAMES = [
    "NGUYEN VAN AN", "TRAN THI BINH", "LE VAN CUONG", "PHAM THI DUNG",
    "HOANG VAN EM", "VU THI GIANG", "DANG VAN HAI", "BUI THI HOA",
    "DO VAN KHANH", "NGO THI LAN", "DUONG VAN MANH", "LY THI NGA",
    "TRINH VAN PHONG", "CAO THI QUYEN", "MAI VAN SON", "TA THI THUY",
]
MERCHANTS = [
    "CONG TY TNHH VINMART", "CGV CINEMAS VIETNAM", "GRAB VIETNAM",
    "CTY DIEN LUC HA NOI", "VIETTEL TELECOM", "CTY CP THE GIOI DI DONG",
    "SHOPEE VIETNAM", "CTY CP NUOC SACH HA NOI", "BENH VIEN VINMEC",
    "CTY TNHH XANG DAU PETROLIMEX",
]

beneficiary_rows = []
benefs_of: dict[int, list[dict]] = defaultdict(list)
ben_seq = 300000

for cid, *_ in CUSTOMERS:
    seg = SEGMENT_OF[cid]
    prof = SEGMENT_PROFILE[seg]
    n = random.randint(*prof["benef_count"])
    # Mỗi khách có một nhóm merchant quen và một nhóm người thân/bạn bè
    n_merchant = max(2, n // 3)
    for k in range(n):
        ben_seq += 1
        is_merchant = k < n_merchant
        if is_merchant:
            bname = random.choice(MERCHANTS)
            btype, brel = "MERCHANT", "MERCHANT"
        else:
            bname = random.choice(PERSON_NAMES)
            btype = "PERSONAL"
            # Người nhận cá nhân đầu tiên luôn là người thân: mọi khách hàng đều
            # phải có ít nhất một người nhận thân thuộc để đối chiếu.
            brel = "FAMILY" if k == n_merchant else random.choices(
                ["FAMILY", "FRIEND", "EMPLOYER", "UNKNOWN"],
                weights=[0.3, 0.4, 0.13, 0.17],
            )[0]
        acct_no = str(random.randrange(10**9, 10**10))
        # Người nhận được "quen" từ trước khoảng lịch sử để baseline coi là quen thuộc
        first_seen = TODAY - timedelta(days=random.randint(HISTORY_DAYS, HISTORY_DAYS + 400))
        beneficiary_rows.append({
            "id": ben_seq, "customer_id": cid, "bank": random.choice(BANKS),
            "acct_no": acct_no, "name": bname, "type": btype, "rel": brel,
            "first_seen": first_seen, "is_mule": False,
        })
        benefs_of[cid].append(beneficiary_rows[-1])

# ---------------------------------------------------------------------------
# 5. Playbook 10 kịch bản lừa đảo
# ---------------------------------------------------------------------------
def questions(salary: str, hnw: str, senior: str) -> dict:
    return {"SALARY": [salary], "HNW": [hnw], "SENIOR": [senior]}


SCENARIOS = [
    {
        "id": "S01", "name": "Giả danh công an, yêu cầu chuyển vào 'tài khoản an toàn'",
        "group": "G1", "pattern": "DRAIN", "ask": "Y", "priority": 1,
        "signal": {
            "keywords": ["cong an", "vien kiem sat", "tai khoan an toan", "dieu tra", "phong toa", "lenh bat"],
            "recent_events": ["SAVINGS_CLOSED", "LIMIT_RAISED"],
            "new_beneficiary": True, "min_drain_ratio": 0.7, "min_amount": 50_000_000,
        },
        "questions": questions(
            "Có ai tự xưng là công an hoặc viện kiểm sát yêu cầu bạn chuyển khoản này không?",
            "Khoản chuyển này có liên quan tới một cuộc gọi từ cơ quan chức năng không?",
            "Bác ơi, có ai gọi điện xưng là công an rồi bảo bác chuyển tiền đi không ạ?",
        ),
        "options": ["Có, họ gọi điện cho tôi", "Không, tôi tự chuyển", "Tôi không chắc"],
        "advice_title": "Công an không bao giờ yêu cầu chuyển tiền",
        "advice_body": (
            "Cơ quan công an, viện kiểm sát và tòa án KHÔNG BAO GIỜ làm việc qua điện thoại "
            "và không bao giờ yêu cầu chuyển tiền vào 'tài khoản an toàn' để chứng minh vô tội. "
            "Mọi lệnh triệu tập đều bằng văn bản gửi tới nơi cư trú. Nếu ai đó gọi điện, gây áp "
            "lực về thời gian và yêu cầu giữ bí mật với người thân, đó chắc chắn là lừa đảo. "
            "Hãy dừng giao dịch và gọi tới số hotline ngân hàng in trên thẻ của bạn."
        ),
        "action": "cancel",
    },
    {
        "id": "S02", "name": "Giả danh nhân viên ngân hàng, moi OTP hoặc mật khẩu",
        "group": "G1", "pattern": "SINGLE", "ask": "Y", "priority": 2,
        "signal": {
            "keywords": ["otp", "ma xac thuc", "xac thuc", "mat khau", "bao mat",
                         "nhan vien ngan hang", "khoa tai khoan", "nang cap he thong",
                         "nang cap bao mat"],
            # Moi OTP gần như luôn đi kèm đổi mật khẩu hoặc đăng nhập thiết bị mới
            # ngay trước đó; thiếu liên hệ này thì kịch bản bị các kịch bản khác
            # có tổ hợp cờ chung chung hơn giành mất.
            "recent_events": ["PASSWORD_RESET", "NEW_DEVICE_LOGIN"],
            "session_flags": ["on_call"], "new_beneficiary": True,
        },
        "questions": questions(
            "Có ai vừa yêu cầu bạn đọc mã OTP hoặc mật khẩu qua điện thoại không?",
            "Bạn có vừa cung cấp mã xác thực cho người tự xưng là nhân viên ngân hàng?",
            "Bác có vừa đọc mã số nào trong tin nhắn cho người ta nghe không ạ?",
        ),
        "options": ["Có, tôi vừa đọc mã cho họ", "Không", "Tôi có bấm vào một đường link lạ"],
        "advice_title": "Ngân hàng không bao giờ hỏi OTP",
        "advice_body": (
            "Không một nhân viên ngân hàng thật nào hỏi mã OTP, mật khẩu hay mã PIN của bạn — "
            "họ không cần và cũng không được phép. Mã OTP là chữ ký điện tử của riêng bạn: đọc "
            "cho người khác nghe đồng nghĩa với việc ký vào lệnh chuyển tiền của họ. Nếu bạn vừa "
            "cung cấp mã, hãy đổi mật khẩu ngay và gọi hotline ngân hàng."
        ),
        "action": "cancel",
    },
    {
        "id": "S03", "name": "Deepfake video call giả người thân mượn tiền gấp",
        "group": "G2", "pattern": "SINGLE", "ask": "Y", "priority": 2,
        "signal": {
            "keywords": ["gap", "muon tien", "tai nan", "cap cuu", "chuyen gap", "con trai", "con gai"],
            "new_beneficiary": True, "min_amount": 20_000_000, "session_flags": ["on_call"],
        },
        "questions": questions(
            "Bạn có gọi lại cho người thân đó bằng số điện thoại bạn vẫn lưu không?",
            "Yêu cầu chuyển gấp này đến từ video call hay tin nhắn?",
            "Bác đã gọi lại cho con cháu bằng số quen để hỏi lại chưa ạ?",
        ),
        "options": ["Tôi gọi lại rồi, đúng là người nhà", "Chưa gọi lại", "Họ gọi video cho tôi"],
        "advice_title": "Hãy gọi lại bằng số bạn vẫn lưu",
        "advice_body": (
            "Công nghệ deepfake hiện có thể dựng khuôn mặt và giọng nói người thân của bạn trong "
            "một cuộc gọi video ngắn. Dấu hiệu nhận biết: hình ảnh mờ hoặc giật, người gọi viện lý "
            "do mạng kém để cúp nhanh, và luôn hối thúc chuyển tiền gấp vào một tài khoản lạ. "
            "Cách kiểm tra chắc chắn nhất: cúp máy và tự gọi lại bằng số điện thoại bạn vẫn lưu "
            "trong danh bạ, hoặc hỏi một câu chỉ hai người mới biết."
        ),
        "action": "hold",
    },
    {
        "id": "S04", "name": "Sàn đầu tư ảo cam kết lợi nhuận cao",
        "group": "G3", "pattern": "SERIES", "ask": "Y", "priority": 3,
        "signal": {
            "keywords": ["dau tu", "san", "tien ao", "loi nhuan", "chung khoan quoc te", "forex", "nap tien"],
            "new_beneficiary": True, "inbound_bait": True, "min_series_14d": 2,
        },
        "questions": questions(
            "Sàn đầu tư này có được Ủy ban Chứng khoán Nhà nước cấp phép không?",
            "Bạn đã rút được tiền gốc ra khỏi sàn này lần nào chưa?",
            "Bác có ai giới thiệu sàn đầu tư này qua mạng không ạ?",
        ),
        "options": ["Tôi đã rút được tiền vài lần", "Chưa rút được lần nào", "Có người quen giới thiệu"],
        "advice_title": "Rút được lần đầu là cách họ lấy niềm tin",
        "advice_body": (
            "Mô hình sàn đầu tư ảo luôn cho bạn rút được vài lần đầu với số nhỏ — đó chính là "
            "khoản 'tiền mồi' để bạn tin tưởng và nạp số lớn hơn. Đến khi bạn nạp khoản lớn, sàn "
            "sẽ báo lỗi hệ thống, yêu cầu nộp thêm 'thuế' hoặc 'phí rút', rồi biến mất. Không có "
            "kênh đầu tư hợp pháp nào cam kết lợi nhuận cố định trên 15%/năm. Hãy tra cứu giấy "
            "phép trên trang của Ủy ban Chứng khoán Nhà nước trước khi nạp thêm bất kỳ đồng nào."
        ),
        "action": "hold",
    },
    {
        "id": "S05", "name": "Việc nhẹ lương cao, nạp tiền làm nhiệm vụ",
        "group": "G3", "pattern": "SERIES", "ask": "Y", "priority": 3,
        "signal": {
            "keywords": ["nhiem vu", "hoa hong", "don hang", "viec lam online", "cong tac vien", "nap de rut"],
            "new_beneficiary": True, "inbound_bait": True, "min_series_14d": 3,
        },
        "questions": questions(
            "Bạn có phải nạp tiền trước để nhận hoa hồng cho nhiệm vụ này không?",
            "Công việc này có yêu cầu bạn ứng tiền cho từng đơn hàng?",
            "Bác có phải chuyển tiền trước rồi mới được nhận hoa hồng không ạ?",
        ),
        "options": ["Có, tôi phải nạp trước", "Không", "Nhiệm vụ này là nhiệm vụ cuối"],
        "advice_title": "Việc làm thật không bắt bạn nạp tiền",
        "advice_body": (
            "Không có công việc hợp pháp nào yêu cầu người lao động nạp tiền trước để nhận hoa "
            "hồng. Mô hình này luôn diễn ra theo cùng một kịch bản: vài nhiệm vụ đầu trả hoa hồng "
            "đầy đủ, rồi số tiền yêu cầu nạp tăng dần, và ở nhiệm vụ cuối họ báo bạn 'thao tác sai' "
            "nên phải nạp thêm để lấy lại toàn bộ. Số tiền bạn đã nạp càng lớn thì áp lực nạp tiếp "
            "càng mạnh — đó là lúc nên dừng lại hoàn toàn."
        ),
        "action": "hold",
    },
    {
        "id": "S06", "name": "Mua bán online, yêu cầu đặt cọc trước",
        "group": "G4", "pattern": "SINGLE", "ask": "Y", "priority": 4,
        "signal": {
            "keywords": ["dat coc", "giu cho", "chuyen khoan truoc", "ship cod", "ve may bay", "dat phong"],
            "new_beneficiary": True,
        },
        "questions": questions(
            "Bạn đã kiểm tra thông tin người bán ngoài nền tảng chat chưa?",
            "Giao dịch này có qua sàn thương mại điện tử chính thức không?",
            "Bác mua hàng này ở đâu, có phải qua tin nhắn Facebook không ạ?",
        ),
        "options": ["Người bán có cửa hàng rõ ràng", "Tôi chỉ liên hệ qua mạng xã hội", "Giá rẻ bất thường"],
        "advice_title": "Trả tiền khi nhận hàng, đừng cọc trước",
        "advice_body": (
            "Dấu hiệu của người bán giả: giá thấp bất thường so với thị trường, chỉ nhận chuyển "
            "khoản trước và từ chối giao dịch qua sàn có bảo vệ người mua, tài khoản nhận tiền "
            "mang tên khác với tên cửa hàng. Hãy ưu tiên thanh toán khi nhận hàng, hoặc giao dịch "
            "qua sàn thương mại điện tử chính thức nơi bạn có quyền khiếu nại."
        ),
        "action": "contact",
    },
    {
        "id": "S07", "name": "Thông báo trúng thưởng, yêu cầu đóng phí nhận quà",
        "group": "G4", "pattern": "SINGLE", "ask": "Y", "priority": 4,
        "signal": {
            "keywords": ["trung thuong", "qua tang", "phi nhan thuong", "may man", "trung xe", "le phi"],
            "new_beneficiary": True,
        },
        "questions": questions(
            "Bạn có tham gia chương trình bốc thăm nào trước đó không?",
            "Bên trao thưởng có yêu cầu bạn nộp phí trước khi nhận không?",
            "Bác có nhớ mình đăng ký chương trình trúng thưởng này lúc nào không ạ?",
        ),
        "options": ["Tôi có tham gia", "Tôi không hề tham gia", "Họ bảo phải đóng thuế trước"],
        "advice_title": "Giải thưởng thật không thu phí trước",
        "advice_body": (
            "Theo quy định pháp luật, thuế thu nhập từ trúng thưởng được khấu trừ trực tiếp vào "
            "giá trị giải thưởng, người trúng không phải nộp tiền trước để nhận quà. Bất kỳ yêu "
            "cầu nộp 'phí vận chuyển', 'thuế trước bạ' hay 'phí làm hồ sơ' nào cũng là lừa đảo. "
            "Đặc biệt cảnh giác nếu bạn chưa từng đăng ký tham gia chương trình đó."
        ),
        "action": "cancel",
    },
    {
        "id": "S08", "name": "Lừa đảo tình cảm, gửi quà từ nước ngoài",
        "group": "G4", "pattern": "SERIES", "ask": "Y", "priority": 4,
        "signal": {
            "keywords": ["hai quan", "gui qua", "nuoc ngoai", "phi van chuyen", "kien hang", "bao hiem kien hang"],
            "new_beneficiary": True, "min_series_14d": 2,
        },
        "questions": questions(
            "Bạn đã gặp mặt trực tiếp người này chưa?",
            "Khoản phí này có liên quan tới một kiện hàng gửi từ nước ngoài không?",
            "Bác đã gặp người gửi quà này ngoài đời lần nào chưa ạ?",
        ),
        "options": ["Chưa gặp mặt bao giờ", "Đã gặp rồi", "Họ nói kiện hàng đang bị giữ"],
        "advice_title": "Hải quan không thu phí qua tài khoản cá nhân",
        "advice_body": (
            "Kịch bản quen thuộc: một người quen qua mạng nói đã gửi cho bạn kiện hàng giá trị "
            "lớn, sau đó một 'nhân viên hải quan' liên hệ đòi phí thông quan, rồi phí bảo hiểm, "
            "rồi phí chống rửa tiền — mỗi lần một khoản, không bao giờ dứt. Cơ quan hải quan thật "
            "thu phí qua kho bạc nhà nước, không bao giờ qua tài khoản cá nhân. Nếu bạn chưa từng "
            "gặp mặt người đó ngoài đời, khả năng rất cao đây là lừa đảo."
        ),
        "action": "hold",
    },
    {
        "id": "S09", "name": "Chiếm quyền điều khiển thiết bị của khách",
        "group": "G5", "pattern": "TAKEOVER", "ask": "N", "priority": 0,
        "signal": {
            "keywords": ["ho tro tu xa", "cai dat ung dung", "dich vu cong", "quyet toan thue", "ultraviewer", "teamviewer"],
            "session_flags": ["screen_sharing", "remote_app", "accessibility_service", "new_device"],
            "recent_events": ["NEW_DEVICE_LOGIN", "PASSWORD_RESET"],
            "min_drain_ratio": 0.6,
        },
        "questions": questions(
            "Giao dịch đã được tạm khóa để xác minh lại danh tính.",
            "Giao dịch đã được tạm khóa để xác minh lại danh tính.",
            "Giao dịch đã được tạm khóa để xác minh lại danh tính.",
        ),
        "options": ["Xác thực sinh trắc học lại"],
        "advice_title": "Thiết bị của bạn có dấu hiệu bị điều khiển từ xa",
        "advice_body": (
            "Hệ thống phát hiện ứng dụng điều khiển từ xa hoặc dịch vụ trợ năng lạ đang hoạt động "
            "trên thiết bị của bạn. Trong tình huống này, người đang thao tác có thể không phải là "
            "bạn, nên chúng tôi tạm khóa giao dịch mà không hỏi thêm — nếu kẻ gian đang điều khiển "
            "máy, chính họ sẽ là người trả lời câu hỏi. Hãy gỡ các ứng dụng điều khiển từ xa vừa "
            "cài, khởi động lại thiết bị và xác thực sinh trắc học để mở khóa."
        ),
        "action": "hold",
    },
    {
        "id": "S10", "name": "Nhận tiền chuyển nhầm rồi bị đòi 'chuyển trả'",
        "group": "G1", "pattern": "RECEIVER", "ask": "Y", "priority": 3,
        "signal": {
            "keywords": ["chuyen nham", "chuyen tra", "hoan lai", "nham tai khoan", "thu ho"],
            "recent_events": ["INBOUND_UNKNOWN"], "new_beneficiary": True, "inbound_bait": True,
        },
        "questions": questions(
            "Bạn có vừa nhận một khoản tiền lạ rồi được yêu cầu chuyển trả không?",
            "Khoản chuyển trả này có đúng tài khoản đã chuyển tiền cho bạn không?",
            "Bác có vừa nhận tiền của người lạ rồi họ gọi đòi lại không ạ?",
        ),
        "options": ["Đúng, họ bảo chuyển nhầm", "Không liên quan", "Họ giục tôi chuyển gấp"],
        "advice_title": "Đừng tự chuyển trả tiền chuyển nhầm",
        "advice_body": (
            "Nếu có người chuyển nhầm tiền vào tài khoản của bạn, cách xử lý đúng là báo ngân hàng "
            "để ngân hàng đối soát và hoàn trả theo quy trình — tuyệt đối không tự chuyển trả theo "
            "hướng dẫn qua điện thoại. Kẻ gian thường dùng tiền từ tài khoản ăn cắp để 'chuyển "
            "nhầm', rồi yêu cầu bạn chuyển trả vào một tài khoản khác; khi chủ tài khoản thật khiếu "
            "nại, bạn vừa mất tiền vừa liên quan tới dòng tiền bất hợp pháp."
        ),
        "action": "cancel",
    },
]

# ---------------------------------------------------------------------------
# 6. Sinh 6 tháng lịch sử thanh toán
# ---------------------------------------------------------------------------
# Mỗi phân khúc có một "chữ ký chi tiêu" riêng: người đi làm nhiều giao dịch nhỏ
# rải đều, khách ưu tiên ít giao dịch hơn nhưng giá trị lớn, người cao tuổi rất
# ít giao dịch và gần như chỉ quanh mấy nhóm quen thuộc. Chính sự khác biệt này
# làm cho cùng một số tiền lại có mức rủi ro khác nhau ở ba phân khúc.
CATEGORY_MIX = {
    "SALARY": [
        ("FOOD", 0.30, (45_000, 420_000)),
        ("TRANSPORT", 0.14, (20_000, 180_000)),
        ("SHOPPING", 0.16, (100_000, 1_200_000)),
        ("BILLS", 0.11, (180_000, 1_200_000)),
        ("TRANSFER_P2P", 0.15, (150_000, 1_500_000)),
        ("HEALTH", 0.04, (150_000, 1_500_000)),
        ("INVESTMENT", 0.04, (500_000, 2_000_000)),
        ("OTHER", 0.06, (50_000, 600_000)),
    ],
    "HNW": [
        ("TRANSFER_P2P", 0.28, (3_000_000, 20_000_000)),
        ("SHOPPING", 0.18, (1_000_000, 15_000_000)),
        ("FOOD", 0.17, (250_000, 3_000_000)),
        ("BILLS", 0.10, (1_000_000, 6_000_000)),
        ("INVESTMENT", 0.12, (8_000_000, 40_000_000)),
        ("TRANSPORT", 0.07, (300_000, 3_000_000)),
        ("HEALTH", 0.03, (1_000_000, 15_000_000)),
        ("OTHER", 0.05, (500_000, 5_000_000)),
    ],
    "SENIOR": [
        ("FOOD", 0.32, (40_000, 350_000)),
        ("HEALTH", 0.20, (120_000, 1_500_000)),
        ("BILLS", 0.20, (150_000, 700_000)),
        ("FAMILY_SUPPORT", 0.16, (300_000, 1_500_000)),
        ("SHOPPING", 0.08, (100_000, 1_000_000)),
        ("OTHER", 0.04, (50_000, 600_000)),
    ],
}
# Biên độ trên được hiệu chỉnh cho mức thu nhập giữa dải của mỗi phân khúc; khách
# thu nhập cao hơn thì chi tiêu cũng giãn ra theo cùng tỷ lệ. Không có bước này,
# người lương 19 triệu và người lương 45 triệu sẽ tiêu y hệt nhau, và nửa số khách
# sẽ tiêu vượt thu nhập khiến số dư cạn dần một cách phi thực tế.
REFERENCE_INCOME = {"SALARY": 32_000_000, "HNW": 300_000_000, "SENIOR": 9_250_000}

MERCHANT_CATEGORIES = {"FOOD", "SHOPPING", "BILLS", "TRANSPORT", "HEALTH"}

tx_rows: list[dict] = []
tx_seq = 8_000_000
account_rows = [list(r) for r in account_rows]
acct_index = {r[0]: r for r in account_rows}
balances = {a["account_id"]: a["balance"] for lst in accounts_of.values() for a in lst}


def pick_category(seg: str) -> tuple[str, tuple[int, int]]:
    mix = CATEGORY_MIX[seg]
    cat = random.choices([m[0] for m in mix], weights=[m[1] for m in mix])[0]
    rng = next(m[2] for m in mix if m[0] == cat)
    return cat, rng


def add_tx(**kw) -> int:
    """Thêm một giao dịch và cập nhật số dư chạy của tài khoản."""
    global tx_seq
    tx_seq += 1
    aid = kw["account_id"]
    amount = kw["amount"]
    balances[aid] = balances.get(aid, 0) + (amount if kw["direction"] == "IN" else -amount)
    kw["transaction_id"] = tx_seq
    kw["balance_after"] = max(0, balances[aid])
    kw.setdefault("is_fraud", "N")
    kw.setdefault("fraud_case_id", None)
    kw.setdefault("status", "POSTED")
    kw.setdefault("risk_decision_id", None)
    kw.setdefault("currency", "VND")
    tx_rows.append(kw)
    return tx_seq


for cid, name, target, dob, _ in CUSTOMERS:
    seg = SEGMENT_OF[cid]
    prof = SEGMENT_PROFILE[seg]
    main = accounts_of[cid][0]["account_id"]
    monthly_income = rnd_amount(*prof["income"], step=100_000)
    spend_scale = monthly_income / REFERENCE_INCOME[seg]
    benefs = benefs_of[cid]
    merchants = [b for b in benefs if b["type"] == "MERCHANT"]
    persons = [b for b in benefs if b["type"] == "PERSONAL"]

    day = START
    while day <= TODAY:
        # --- Tiền vào ---
        if seg == "SALARY" and day.day == 5:
            add_tx(customer_id=cid, account_id=main, direction="IN",
                   amount=monthly_income, transaction_type="SALARY", category="OTHER",
                   description="LUONG THANG " + day.strftime("%m/%Y"),
                   channel="INTERNET", date=day, time=hms(9, 15, 0), beneficiary=None)
        elif seg == "SENIOR" and day.day == 10:
            add_tx(customer_id=cid, account_id=main, direction="IN",
                   amount=monthly_income, transaction_type="SALARY", category="OTHER",
                   description="LUONG HUU THANG " + day.strftime("%m/%Y"),
                   channel="BRANCH", date=day, time=hms(8, 30, 0), beneficiary=None)
        elif seg == "HNW" and day.day in (3, 12, 21):
            # Ba lần thu trong tháng, mỗi lần xấp xỉ một tháng doanh thu — chủ doanh
            # nghiệp nhận tiền theo đợt nghiệm thu chứ không đều như lương.
            add_tx(customer_id=cid, account_id=main, direction="IN",
                   amount=rnd_amount(int(monthly_income * 0.8), int(monthly_income * 1.25), 1_000_000),
                   transaction_type="FT", category="OTHER",
                   description="THANH TOAN HOP DONG", channel="INTERNET",
                   date=day, time=hms(random.choice([9, 10, 14]), random.randrange(60), 0),
                   beneficiary=None)

        # --- Chi cố định hàng tháng ---
        if seg == "SALARY" and day.day == 7:
            add_tx(customer_id=cid, account_id=main, direction="OUT",
                   amount=rnd_amount(int(4_000_000 * spend_scale), int(8_000_000 * spend_scale), 500_000),
                   transaction_type="FT", category="RENT",
                   description="TIEN THUE NHA THANG " + day.strftime("%m"),
                   channel="MOBILE", date=day, time=hms(19, random.randrange(60), 0),
                   beneficiary=random.choice(persons) if persons else None)
        if seg == "SENIOR" and day.day == 15:
            add_tx(customer_id=cid, account_id=main, direction="OUT",
                   amount=rnd_amount(int(800_000 * spend_scale), int(2_500_000 * spend_scale), 100_000),
                   transaction_type="FT", category="FAMILY_SUPPORT",
                   description="GUI CHAU AN HOC", channel="MOBILE",
                   date=day, time=hms(random.choice([9, 15]), random.randrange(60), 0),
                   beneficiary=random.choice(persons) if persons else None)

        # --- Chi tiêu hằng ngày ---
        per_month = random.randint(*prof["tx_per_month"])
        daily = per_month / 30.0
        n_today = int(daily) + (1 if random.random() < (daily % 1) else 0)
        if day.weekday() >= 5:
            n_today = max(0, n_today - (1 if random.random() < 0.3 else 0))

        for _ in range(n_today):
            cat, (lo, hi) = pick_category(seg)
            pool = merchants if cat in MERCHANT_CATEGORIES and merchants else persons
            ben = random.choice(pool) if pool else None
            amount = rnd_amount(int(lo * spend_scale), int(hi * spend_scale),
                                1000 if hi < 10_000_000 else 100_000)
            if balances.get(main, 0) < amount * 1.2:
                continue  # không để số dư âm một cách phi thực tế
            if random.random() < prof["night_rate"]:
                hour = random.choice([23, 0, 1])
            else:
                hour = random.choice(prof["hours"])
            add_tx(customer_id=cid, account_id=main, direction="OUT", amount=amount,
                   transaction_type=random.choices(["FT", "QR", "BILL", "ATM"],
                                                   weights=[0.5, 0.28, 0.17, 0.05])[0],
                   category=cat,
                   description={
                       "FOOD": "THANH TOAN AN UONG", "TRANSPORT": "DI CHUYEN",
                       "SHOPPING": "MUA SAM", "BILLS": "THANH TOAN HOA DON",
                       "TRANSFER_P2P": "CHUYEN TIEN", "HEALTH": "KHAM CHUA BENH",
                       "INVESTMENT": "DAU TU DINH KY", "FAMILY_SUPPORT": "GUI NGUOI THAN",
                       "RENT": "TIEN NHA", "OTHER": "GIAO DICH KHAC",
                   }[cat],
                   channel=random.choices(["MOBILE", "INTERNET", "ATM", "BRANCH"],
                                          weights=[0.72, 0.2, 0.06, 0.02])[0],
                   date=day, time=hms(hour, random.randrange(60), random.randrange(60)),
                   beneficiary=ben)
        day += timedelta(days=1)

# ---------------------------------------------------------------------------
# 7. Mười fraud case
# ---------------------------------------------------------------------------
# F01 và F02 là một cặp có chủ đích: cùng một khách hàng cao tuổi, cùng chuyển một
# khoản lớn bất thường so với thói quen. F01 là lừa đảo thật, F02 là giao dịch hợp
# lệ gửi cho con gái. Engine phải chặn F01 và cho F02 đi qua — đó là bằng chứng
# thuyết phục nhất rằng hệ thống không chặn bừa.
MULE_NAMES = [
    "NGUYEN VAN TRUNG", "LE THI HONG", "PHAM VAN DAT", "TRAN THI XUAN",
    "HOANG VAN LOC", "VU THI THANH", "DINH VAN HOA", "NGUYEN THI NHUNG",
    "BUI VAN TAI", "DO THI HUE",
]

FRAUD_SPECS = [
    {
        "id": "F01", "scenario": "S01", "customer": 100008, "scene": "Scene 3", "day_offset": 4,
        "desc": "Giả danh công an: tất toán sổ tiết kiệm rồi chuyển toàn bộ vào 'tài khoản an toàn'",
        "pre_event": ("SAVINGS_CLOSED", 520_000_000, 28),
        "series": [(560_000_000, 15, 20, "CHUYEN TIEN THEO YEU CAU CO QUAN DIEU TRA")],
        "flags": {"on_call": True}, "level": "intervene", "range": (78, 100), "fraud": "Y",
    },
    {
        "id": "F02", "scenario": "S01", "customer": 100008, "scene": "Scene 4", "day_offset": 13,
        "desc": "ĐỐI CHỨNG hợp lệ: cùng khách hàng chuyển khoản lớn cho con gái đã quen, "
                "không có sự kiện bất thường đi kèm — engine phải cho đi qua",
        "pre_event": None,
        "series": [(60_000_000, 10, 25, "GUI CON GAI MUA XE")],
        "flags": {}, "level": "pass", "range": (0, 39), "fraud": "N", "known_beneficiary": True,
    },
    {
        "id": "F03", "scenario": "S02", "customer": 100002, "scene": "Scene 5", "day_offset": 9,
        "desc": "Giả danh nhân viên ngân hàng moi OTP, đổi mật khẩu rồi rút tiền lúc nửa đêm",
        "pre_event": ("PASSWORD_RESET", None, 18),
        "series": [(28_000_000, 23, 41, "NANG CAP BAO MAT TAI KHOAN")],
        "flags": {"on_call": True}, "level": "intervene", "range": (75, 100), "fraud": "Y",
    },
    {
        "id": "F04", "scenario": "S03", "customer": 100005, "scene": None, "day_offset": 11,
        "desc": "Deepfake video call giả con trai bị tai nạn, xin chuyển gấp",
        "pre_event": ("NEW_DEVICE_LOGIN", None, 35),
        "series": [(180_000_000, 20, 15, "CHUYEN GAP CHO CON")],
        "flags": {"on_call": True, "new_device": True}, "level": "intervene", "range": (70, 95), "fraud": "Y",
    },
    {
        "id": "F05", "scenario": "S04", "customer": 100001, "scene": None, "day_offset": 7,
        "desc": "Sàn đầu tư ảo: nhận tiền mồi 8tr rồi nạp 3 lần tăng dần",
        "pre_event": None, "inbound_bait": 8_000_000,
        "series": [
            (6_000_000, 20, 30, "NAP DAU TU SAN QUOC TE"),
            (15_000_000, 21, 12, "NAP DAU TU SAN QUOC TE"),
            (32_000_000, 23, 47, "NAP THEM DE RUT LOI NHUAN"),
        ],
        "flags": {"on_call": True}, "level": "intervene", "range": (68, 95), "fraud": "Y",
    },
    {
        "id": "F06", "scenario": "S05", "customer": 100004, "scene": None, "day_offset": 6,
        "desc": "Việc nhẹ lương cao: làm nhiệm vụ nạp tiền 4 lần, hoa hồng mồi 2.5tr",
        "pre_event": None, "inbound_bait": 2_500_000,
        "series": [
            (2_000_000, 21, 10, "NAP NHIEM VU 1"),
            (6_000_000, 20, 35, "NAP NHIEM VU 2"),
            (14_000_000, 22, 18, "NAP NHIEM VU 3"),
            (30_000_000, 23, 52, "NAP NHIEM VU CUOI DE RUT"),
        ],
        "flags": {}, "level": "intervene", "range": (65, 95), "fraud": "Y",
    },
    {
        "id": "F07", "scenario": "S06", "customer": 100003, "scene": None, "day_offset": 10,
        "desc": "Đặt cọc mua hàng online giá rẻ bất thường qua mạng xã hội",
        "pre_event": None,
        "series": [(9_000_000, 20, 40, "DAT COC GIU CHO")],
        "flags": {}, "level": "soft_warn", "range": (38, 72), "fraud": "Y",
    },
    {
        "id": "F08", "scenario": "S08", "customer": 100010, "scene": None, "day_offset": 8,
        "desc": "Lừa đảo tình cảm: đóng phí hải quan nhận kiện hàng từ nước ngoài, 2 lần",
        "pre_event": None,
        "series": [
            (12_000_000, 15, 30, "PHI HAI QUAN KIEN HANG"),
            (22_000_000, 16, 20, "PHI BAO HIEM KIEN HANG"),
        ],
        "flags": {}, "level": "intervene", "range": (65, 95), "fraud": "Y",
    },
    {
        "id": "F09", "scenario": "S09", "customer": 100006, "scene": "Scene 6", "day_offset": 2,
        "desc": "Chiếm quyền điều khiển thiết bị qua ứng dụng 'dịch vụ công' giả, vét tài khoản",
        "pre_event": ("NEW_DEVICE_LOGIN", None, 12),
        "series": [(0, 14, 25, "QUYET TOAN THUE DICH VU CONG")],  # 0 = vét theo tỷ lệ số dư
        "drain_ratio": 0.93,
        "flags": {"screen_sharing": True, "remote_app": True, "accessibility_service": True, "new_device": True},
        "level": "intervene", "range": (85, 100), "fraud": "Y",
    },
    {
        "id": "F10", "scenario": "S10", "customer": 100009, "scene": None, "day_offset": 3,
        "desc": "Nhận 50tr 'chuyển nhầm' từ người lạ rồi bị hối chuyển trả sang tài khoản khác",
        "pre_event": ("INBOUND_UNKNOWN", 50_000_000, 40), "inbound_bait": 50_000_000,
        "series": [(50_000_000, 17, 15, "CHUYEN TRA TIEN CHUYEN NHAM")],
        "flags": {"on_call": True}, "level": "intervene", "range": (62, 92), "fraud": "Y",
    },
]

event_rows: list[tuple] = []
fraud_case_rows: list[tuple] = []

# Sự kiện lành tính: khách đổi điện thoại, nâng hạn mức trước kỳ mua sắm, tất toán
# sổ đến hạn rồi gửi lại. Những sự kiện này KHÔNG đi kèm giao dịch bất thường nào,
# nên engine không được phép chỉ dựa vào sự kiện để kết luận.
for cid, *_ in CUSTOMERS:
    main = accounts_of[cid][0]["account_id"]
    for _ in range(random.randint(1, 3)):
        d = START + timedelta(days=random.randint(10, HISTORY_DAYS - 25))
        etype = random.choices(
            ["NEW_DEVICE_LOGIN", "LIMIT_RAISED", "PASSWORD_RESET", "SAVINGS_CLOSED"],
            weights=[0.4, 0.25, 0.2, 0.15],
        )[0]
        event_rows.append((
            cid, main, etype,
            rnd_amount(50_000_000, 400_000_000, 1_000_000) if etype == "SAVINGS_CLOSED" else None,
            ts(d, random.choice([9, 11, 15, 19]), random.randrange(60)),
            f"benign-{etype.lower()}", {"benign": True},
        ))

fraud_tx_ids: dict[str, list[int]] = defaultdict(list)

for idx, spec in enumerate(FRAUD_SPECS):
    cid = spec["customer"]
    seg = SEGMENT_OF[cid]
    main = accounts_of[cid][0]["account_id"]
    # Mốc ngày đặt tường minh cho từng case: giao dịch đối chứng F02 phải xảy ra
    # TRƯỚC vụ vét tài khoản F01, nếu không thì khoản chuyển hợp lệ cho con gái sẽ
    # rơi vào lúc số dư đã về 0 và không còn ý nghĩa đối chứng.
    base_day = TODAY - timedelta(days=spec["day_offset"])

    if spec.get("known_beneficiary"):
        # Đối chứng F02 dùng người nhận quen thuộc nhất trong sổ, quan hệ FAMILY
        family = [b for b in benefs_of[cid] if b["rel"] == "FAMILY"]
        target_ben = family[0] if family else benefs_of[cid][0]
    else:
        ben_seq += 1
        target_ben = {
            "id": ben_seq, "customer_id": cid, "bank": random.choice(BANKS[1:]),
            "acct_no": str(random.randrange(10**9, 10**10)), "name": MULE_NAMES[idx],
            "type": "PERSONAL", "rel": "UNKNOWN",
            # Người nhận mới tinh: first_seen_at NULL chính là tín hiệu yếu tố số 2
            "first_seen": None, "is_mule": True,
        }
        beneficiary_rows.append(target_ben)
        benefs_of[cid].append(target_ben)

    # Tiền mồi: kẻ gian chuyển vào trước một khoản nhỏ để tạo lòng tin
    if spec.get("inbound_bait"):
        bait_day = base_day - timedelta(days=6)
        add_tx(customer_id=cid, account_id=main, direction="IN",
               amount=spec["inbound_bait"], transaction_type="FT", category="OTHER",
               description="HOAN TIEN" if spec["id"] != "F10" else "CHUYEN NHAM",
               channel="INTERNET", date=bait_day, time=hms(11, 20, 0),
               beneficiary=target_ben, is_fraud="N")

    if spec.get("pre_event"):
        etype, amt, minutes_before = spec["pre_event"]
        first_hour = spec["series"][0][1]
        first_min = spec["series"][0][2]
        ev_dt = datetime(base_day.year, base_day.month, base_day.day, first_hour, first_min) \
            - timedelta(minutes=minutes_before)
        event_rows.append((
            cid, main, etype, amt, ts(ev_dt.date(), ev_dt.hour, ev_dt.minute),
            spec["id"], {"fraud_case_id": spec["id"], "injected": True},
        ))
        # Tất toán sổ hay nhận tiền lạ đều làm số dư tăng vọt ngay trước lệnh chuyển
        if etype in ("SAVINGS_CLOSED", "INBOUND_UNKNOWN") and amt:
            add_tx(customer_id=cid, account_id=main, direction="IN", amount=amt,
                   transaction_type="FT", category="OTHER",
                   description="TAT TOAN SO TIET KIEM" if etype == "SAVINGS_CLOSED" else "NHAN TIEN",
                   channel="MOBILE" if etype == "SAVINGS_CLOSED" else "INTERNET",
                   date=ev_dt.date(), time=hms(ev_dt.hour, ev_dt.minute, 0),
                   beneficiary=None, is_fraud="N")

    for j, (amount, hour, minute, memo) in enumerate(spec["series"]):
        tx_day = base_day - timedelta(days=(len(spec["series"]) - 1 - j) * 3)
        if amount == 0:  # case vét sạch: số tiền tính theo tỷ lệ số dư hiện có
            amount = int(balances.get(main, 0) * spec.get("drain_ratio", 0.9) / 1_000_000) * 1_000_000
        if amount > balances.get(main, 0):
            print(f"-- CANH BAO: {spec['id']} chuyen {amount:,} vuot so du "
                  f"{balances.get(main, 0):,} cua TK {main}", file=sys.stderr)
        txid = add_tx(
            customer_id=cid, account_id=main, direction="OUT", amount=amount,
            transaction_type="FT", category="TRANSFER_P2P", description=memo,
            channel="MOBILE", date=tx_day, time=hms(hour, minute, 0),
            beneficiary=target_ben,
            is_fraud=spec["fraud"], fraud_case_id=spec["id"],
        )
        fraud_tx_ids[spec["id"]].append(txid)

    fraud_case_rows.append((
        spec["id"], cid, spec["scenario"],
        next(s["pattern"] for s in SCENARIOS if s["id"] == spec["scenario"]),
        spec["desc"],
        {
            "pre_events": [spec["pre_event"][0]] if spec.get("pre_event") else [],
            "series": [{"amount": a, "time": f"{h:02d}:{m:02d}", "memo": memo}
                       for a, h, m, memo in spec["series"]],
            "inbound_bait": spec.get("inbound_bait"),
            "session_flags": spec["flags"],
            "beneficiary_is_new": not spec.get("known_beneficiary", False),
            "drain_ratio": spec.get("drain_ratio"),
        },
        spec["level"], spec["range"][0], spec["range"][1], spec["scene"],
    ))

# ---------------------------------------------------------------------------
# 7b. Tính lại số dư sau giao dịch theo đúng trình tự thời gian
# ---------------------------------------------------------------------------
# Các giao dịch gian lận được sinh sau cùng nhưng mang ngày nằm giữa khoảng lịch
# sử, nên số dư cộng dồn theo thứ tự chèn sẽ sai lệch với thứ tự thời gian thật.
# Phát lại toàn bộ theo đúng ngày giờ để balance_after nhất quán — cột này là đầu
# vào của drain_ratio và max_drain_ratio_90d nên sai ở đây sẽ làm lệch cả engine.
opening_balance = {a["account_id"]: a["balance"]
                   for lst in accounts_of.values() for a in lst}
by_account: dict[int, list[dict]] = defaultdict(list)
for t in tx_rows:
    by_account[t["account_id"]].append(t)

overdrafts = []
for aid, items in by_account.items():
    items.sort(key=lambda t: (t["date"], t["time"], t["transaction_id"]))
    bal = opening_balance[aid]
    for t in items:
        bal += t["amount"] if t["direction"] == "IN" else -t["amount"]
        if bal < 0:
            overdrafts.append((aid, t["transaction_id"], t["date"], t["amount"], bal))
            bal = 0
        t["balance_after"] = bal
    balances[aid] = bal

for aid, txid, d, amt, bal in overdrafts:
    print(f"-- CANH BAO: TK {aid} am {bal:,} tai giao dich {txid} ngay {d} ({amt:,})",
          file=sys.stderr)

# ---------------------------------------------------------------------------
# 8. Tính behavior_profile từ chính lịch sử vừa sinh
# ---------------------------------------------------------------------------
# Dùng đúng công thức mà transaction-service áp dụng, để số liệu seed và số liệu
# service tính lại khớp nhau. Chỉ lấy giao dịch sạch: OUT, POSTED, is_fraud = 'N'.
def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


baseline_start = TODAY - timedelta(days=BASELINE_WINDOW)
profile_rows = []

for cid, *_ in CUSTOMERS:
    outs = [t for t in tx_rows
            if t["customer_id"] == cid and t["direction"] == "OUT"
            and t["is_fraud"] == "N" and t["date"] >= baseline_start]
    ins = [t for t in tx_rows
           if t["customer_id"] == cid and t["direction"] == "IN"
           and t["is_fraud"] == "N" and t["date"] >= baseline_start]
    if not outs:
        continue

    amounts = [float(t["amount"]) for t in outs]
    months = BASELINE_WINDOW / 30.0
    weeks = BASELINE_WINDOW / 7.0

    hours = [0] * 24
    night = weekend = 0
    per_day: dict[date, int] = defaultdict(int)
    for t in outs:
        h = int(t["time"][:2])
        hours[h] += 1
        if h >= 23 or h < 6:
            night += 1
        if t["date"].weekday() >= 5:
            weekend += 1
        per_day[t["date"]] += 1
    total_h = max(1, sum(hours))
    active_hours = [round(h / total_h, 4) for h in hours]

    benef_ids = {t["beneficiary"]["id"] for t in outs if t.get("beneficiary")}
    first_tx: dict[int, date] = {}
    for t in outs:
        b = t.get("beneficiary")
        if b and b["id"] not in first_tx:
            first_tx[b["id"]] = t["date"]
    new_cut = TODAY - timedelta(days=30)
    new_benefs = {b for b, d in first_tx.items() if d >= new_cut}
    total_out = sum(amounts)
    to_new = sum(float(t["amount"]) for t in outs
                 if t.get("beneficiary") and t["beneficiary"]["id"] in new_benefs)

    # Tổng dồn tối đa về một người nhận trong cửa sổ trượt 14 ngày
    max_cum = 0.0
    by_ben: dict[int, list[tuple[date, float]]] = defaultdict(list)
    for t in outs:
        if t.get("beneficiary"):
            by_ben[t["beneficiary"]["id"]].append((t["date"], float(t["amount"])))
    for items in by_ben.values():
        items.sort()
        left = 0
        window = 0.0
        for d, amt in items:
            window += amt
            while items[left][0] < d - timedelta(days=14):
                window -= items[left][1]
                left += 1
            max_cum = max(max_cum, window)

    bals = [float(t["balance_after"]) for t in outs if t.get("balance_after")]
    max_drain = 0.0
    for t in outs:
        after, amt = float(t["balance_after"]), float(t["amount"])
        if after + amt > 0:
            max_drain = max(max_drain, amt / (after + amt))

    profile_rows.append((
        cid, BASELINE_WINDOW,
        round(statistics.median(amounts)), round(percentile(amounts, 0.90)),
        round(percentile(amounts, 0.99)), round(max(amounts)),
        round(total_out / months),
        round(sum(float(t["amount"]) for t in ins) / months),
        len(benef_ids), float(len(new_benefs)),
        round(to_new / total_out, 4) if total_out else 0.0,
        active_hours, round(night / len(outs), 4), round(weekend / len(outs), 4),
        round(len(outs) / weeks, 2), max(per_day.values()) if per_day else 0,
        round(max_cum), round(statistics.median(bals)) if bals else 0,
        round(min(max_drain, 1.0), 4),
    ))

# ---------------------------------------------------------------------------
# 9. Quyết định rủi ro, case, phản hồi, thông báo, nhật ký LLM
# ---------------------------------------------------------------------------
import uuid  # noqa: E402 - đặt ở đây cho gần nơi dùng

NS = uuid.UUID("6f1c4a6e-0000-4000-8000-000000000001")


def decision_uuid(key: str) -> str:
    """UUID tất định để seed chạy lại vẫn trỏ đúng case/feedback/notification."""
    return str(uuid.uuid5(NS, key))


def shift(stamp: str, seconds: int) -> str:
    """Dịch một timestamp chuỗi đi vài giây để các mốc trong một quyết định lệch nhau."""
    dt = datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S") + timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + "+07"


SCENARIO_BY_ID = {s["id"]: s for s in SCENARIOS}
TOP_FACTORS = {
    "intervene": "recent_context,new_beneficiary,amount_deviation",
    "soft_warn": "new_beneficiary,amount_deviation,relationship_history",
    "pass": "none",
}

decision_rows, case_rows, feedback_rows, notif_rows, trace_rows = [], [], [], [], []
tx_decision_link: dict[int, str] = {}
decision_tx_link: dict[str, int] = {}
case_seq = 0

for spec in FRAUD_SPECS:
    cid = spec["customer"]
    main = accounts_of[cid][0]["account_id"]
    scen = SCENARIO_BY_ID[spec["scenario"]]
    last_tx_id = fraud_tx_ids[spec["id"]][-1]
    last_tx = next(t for t in tx_rows if t["transaction_id"] == last_tx_id)
    did = decision_uuid(spec["id"])

    level = spec["level"]
    score = (spec["range"][0] + spec["range"][1]) // 2
    # Kết cục được seed theo kịch bản demo: phần lớn case bị chặn thành công,
    # nhưng để lại vài case "vẫn tiếp tục" cho số liệu ops trung thực.
    if level == "pass":
        action, outcome, scen_id = None, None, None
    elif spec["id"] in ("F07", "F10"):
        action, outcome, scen_id = "continue", "proceeded", scen["id"]
    elif scen["action"] == "cancel":
        action, outcome, scen_id = "cancel", "prevented", scen["id"]
    else:
        action, outcome, scen_id = "hold", "held", scen["id"]

    snapshot = {
        "amount": float(last_tx["amount"]),
        "beneficiary_masked": mask_acct(last_tx["beneficiary"]["acct_no"]),
        "bank_code": last_tx["beneficiary"]["bank"],
        "tx_time": ts(last_tx["date"], int(last_tx["time"][:2]), int(last_tx["time"][2:4])),
        "memo_masked": last_tx["description"],
        "channel": last_tx["channel"],
        "session_flags": spec["flags"],
        "working_balance": float(last_tx["balance_after"]) + float(last_tx["amount"]),
    }
    factors = {
        "amount_deviation": {"score": 25 if level == "intervene" else (9 if level == "soft_warn" else 20)},
        "new_beneficiary": {"score": 0 if spec.get("known_beneficiary") else 20},
        "time_of_day": {"score": 10 if int(last_tx["time"][:2]) >= 23 else 0},
        "behavior_drift": {"score": 12 if spec["flags"] else 0},
        "relationship_history": {"score": 0 if spec.get("known_beneficiary") else 11},
        "recent_context": {"score": 20 if spec.get("pre_event") else 0},
    }
    created = ts(last_tx["date"], int(last_tx["time"][:2]), int(last_tx["time"][2:4]))

    decision_rows.append((
        did, cid, main, snapshot, score, level, factors, TOP_FACTORS[level],
        scen_id,
        f"Giao dịch {float(last_tx['amount']):,.0f} VND có điểm rủi ro {score}/100.",
        None if level == "pass" else [
            "Người nhận chưa từng xuất hiện trong lịch sử chuyển tiền của khách.",
            "Số tiền vượt xa mức khách thường chuyển trong 90 ngày qua.",
            "Có sự kiện bất thường xảy ra ngay trước lệnh chuyển.",
        ],
        None if level == "pass" else scen["questions"][SEGMENT_OF[cid]][0],
        None if level == "pass" else scen["options"],
        None if action == "continue" else "Khách đã chọn dừng giao dịch" if action else None,
        None if level == "pass" else scen["advice_title"],
        None if level == "pass" else scen["advice_body"],
        None if level == "pass" else scen["action"],
        action, outcome, created,
        # Khách mất vài chục giây tới vài phút để đọc cảnh báo rồi quyết định;
        # nếu ba mốc thời gian trùng nhau thì avg_response_sec của ops luôn bằng 0.
        None if level == "pass" else shift(created, random.randint(4, 25)),
        None if level == "pass" else shift(created, random.randint(35, 240)),
    ))
    tx_decision_link[last_tx_id] = did
    decision_tx_link[did] = last_tx_id

    # Case mở tự động cho các quyết định dẫn tới khóa tạm
    if action == "hold":
        case_seq += 1
        case_rows.append((
            f"CASE-2026-{case_seq:04d}", did, cid, scen["id"], "CLOSED_FRAUD",
            f"Khách hàng {mask_name(next(c[1] for c in CUSTOMERS if c[0] == cid))} "
            f"bị nghi ngờ theo kịch bản {scen['name']}. Giao dịch đã được khóa tạm.",
            "09** *** " + str(random.randrange(100, 999)), created, created,
        ))
        feedback_rows.append((did, cid, "fraud", "ops", f"Xác minh qua điện thoại: đúng là lừa đảo ({scen['id']})", "Y", created))
    elif action == "cancel":
        feedback_rows.append((did, cid, "fraud", "customer", "Khách tự xác nhận đây là lừa đảo", "Y", created))
    elif action == "continue":
        # Khách vẫn tiếp tục sau cảnh báo — đây là lúc thông báo hậu kiểm phát huy tác dụng
        notif_rows.append((
            cid, did, "push", "post_continue_warning",
            {"message": "Giao dịch của bạn có dấu hiệu rủi ro. Nếu phát hiện bất thường, "
                        "gọi ngay hotline để được hỗ trợ khóa giao dịch trong 24h.",
             "scenario": scen["id"]},
            "DELIVERED", created,
        ))
        feedback_rows.append((did, cid, "fraud", "ops", "Khách báo mất tiền sau khi vẫn tiếp tục", "N", created))

    if level != "pass":
        notif_rows.append((
            cid, did,
            "call_request" if action == "hold" else "push",
            "callback_scheduled" if action == "hold" else "hold_confirmed",
            {"scenario": scen["id"], "action": action or "none"},
            "ACTIONED", created,
        ))
        for agent, key, latency, status in [
            ("shield_explain", "shield_explain@v3", random.randint(680, 1400), "ok"),
            ("shield_interview", "shield_interview@v2", random.randint(520, 1100), "ok"),
            ("shield_advice", "shield_advice@v2", random.randint(600, 1500),
             random.choices(["ok", "timeout"], weights=[0.9, 0.1])[0]),
        ]:
            trace_rows.append((
                did, cid, agent, "claude-sonnet-5", key,
                f"[persona={SEGMENT_OF[cid]}] [amount={float(last_tx['amount']):,.0f}] "
                f"[scenario={scen['id']}] [beneficiary={snapshot['beneficiary_masked']}]",
                None if status == "timeout" else "Đã sinh nội dung theo advice_body của kịch bản.",
                latency, status, created,
            ))

# Vài quyết định "pass" trên giao dịch bình thường, để ops_summary không chỉ toàn case xấu
normal_pass = 0
for cid, *_ in CUSTOMERS:
    main = accounts_of[cid][0]["account_id"]
    candidates = [t for t in tx_rows
                  if t["customer_id"] == cid and t["direction"] == "OUT"
                  and t["is_fraud"] == "N" and t.get("beneficiary")
                  and t["date"] >= TODAY - timedelta(days=20)]
    for t in random.sample(candidates, min(2, len(candidates))):
        normal_pass += 1
        did = decision_uuid(f"PASS-{t['transaction_id']}")
        created = ts(t["date"], int(t["time"][:2]), int(t["time"][2:4]))
        decision_rows.append((
            did, cid, main,
            {"amount": float(t["amount"]),
             "beneficiary_masked": mask_acct(t["beneficiary"]["acct_no"]),
             "bank_code": t["beneficiary"]["bank"],
             "tx_time": created, "memo_masked": t["description"],
             "channel": t["channel"], "session_flags": {},
             "working_balance": float(t["balance_after"]) + float(t["amount"])},
            random.randint(3, 28), "pass", {
                "amount_deviation": {"score": 0}, "new_beneficiary": {"score": 0},
                "time_of_day": {"score": 0}, "behavior_drift": {"score": 0},
                "relationship_history": {"score": 0}, "recent_context": {"score": 0},
            }, "none", None,
            f"Giao dịch {float(t['amount']):,.0f} VND trong ngưỡng bình thường của bạn.",
            None, None, None, None, None, None, None, None, None, created, None, None,
        ))
        tx_decision_link[t["transaction_id"]] = did
        decision_tx_link[did] = t["transaction_id"]

# ---------------------------------------------------------------------------
# 10. Insight chi tiêu và gợi ý sản phẩm (Journey A)
# ---------------------------------------------------------------------------
insight_rows, reco_rows = [], []
insight_key_seq = 0
insight_ids: dict[tuple[int, str], int] = {}

periods = []
p = TODAY.replace(day=1)
for _ in range(3):
    p = (p - timedelta(days=1)).replace(day=1)
    periods.append(p.strftime("%Y%m"))
periods.reverse()

for cid, *_ in CUSTOMERS:
    prev_totals: dict[str, float] = {}
    for period in periods:
        by_cat: dict[str, float] = defaultdict(float)
        for t in tx_rows:
            if (t["customer_id"] == cid and t["direction"] == "OUT"
                    and t["is_fraud"] == "N" and t["date"].strftime("%Y%m") == period):
                by_cat[t["category"]] += float(t["amount"])
        if not by_cat:
            continue
        ranked = sorted(by_cat.items(), key=lambda kv: kv[1], reverse=True)
        total = sum(by_cat.values())
        prev_total = sum(prev_totals.values())

        insight_key_seq += 1
        insight_ids[(cid, period)] = insight_key_seq
        delta = round((total - prev_total) / prev_total * 100, 2) if prev_total else None
        insight_rows.append((insight_key_seq, cid, period, "TOTAL", round(total), delta, None,
                             f"Tổng chi tháng {period[4:]}/{period[:4]} là {total:,.0f} VND."))
        for rank, (cat, amt) in enumerate(ranked, start=1):
            insight_key_seq += 1
            base = prev_totals.get(cat, 0.0)
            d = round((amt - base) / base * 100, 2) if base else None
            insight_rows.append((
                insight_key_seq, cid, period, cat, round(amt), d, rank,
                f"Nhóm {cat} chiếm {amt / total:.0%} tổng chi, xếp thứ {rank}.",
            ))
        prev_totals = dict(by_cat)

    # Gợi ý sản phẩm dựa trên phần dư thực tế của tháng gần nhất
    last_period = periods[-1]
    income = sum(float(t["amount"]) for t in tx_rows
                 if t["customer_id"] == cid and t["direction"] == "IN"
                 and t["date"].strftime("%Y%m") == last_period)
    spend = sum(float(t["amount"]) for t in tx_rows
                if t["customer_id"] == cid and t["direction"] == "OUT"
                and t["is_fraud"] == "N" and t["date"].strftime("%Y%m") == last_period)
    surplus = income - spend
    if surplus > 1_000_000:
        seg = SEGMENT_OF[cid]
        picks = {"SALARY": [1, 4], "HNW": [5, 6], "SENIOR": [2, 4]}[seg]
        for pid in picks:
            rate = float(PRODUCTS[pid - 1][5] or 0)
            reco_rows.append((
                cid, pid, insight_ids.get((cid, last_period)),
                f"Dòng tiền dư khoảng {surplus:,.0f} VND trong tháng {last_period[4:]}, "
                f"đủ để tích lũy vào {PRODUCTS[pid - 1][1]}.",
                round(surplus * 12 * rate / 100),
                random.choices(["SHOWN", "ACCEPTED", "DISMISSED"], weights=[0.6, 0.25, 0.15])[0],
            ))

# ---------------------------------------------------------------------------
# 11. Chốt số dư cuối cùng của tài khoản
# ---------------------------------------------------------------------------
for aid, row in acct_index.items():
    final = max(0, int(balances.get(aid, 0)))
    row[7] = str(final)                       # working_balance
    row[6] = str(final + random.randint(0, 3_000_000))  # open_acct_bal (số dư đầu ngày)

# ---------------------------------------------------------------------------
# 12. Tổng hợp thống kê người nhận từ lịch sử thực tế
# ---------------------------------------------------------------------------
ben_stats: dict[int, dict] = {b["id"]: {
    "out": 0.0, "in": 0.0, "count": 0, "first": None, "last": None, "amounts": []
} for b in beneficiary_rows}

for t in tx_rows:
    b = t.get("beneficiary")
    if not b:
        continue
    st = ben_stats[b["id"]]
    d = t["date"]
    if t["direction"] == "OUT":
        st["out"] += float(t["amount"])
        st["count"] += 1
        st["amounts"].append(float(t["amount"]))
        st["first"] = d if st["first"] is None else min(st["first"], d)
        st["last"] = d if st["last"] is None else max(st["last"], d)
    else:
        st["in"] += float(t["amount"])

beneficiary_out = []
for b in beneficiary_rows:
    st = ben_stats[b["id"]]
    # Người nhận mới tinh giữ first_seen_at NULL — đó chính là tín hiệu "người nhận mới"
    first_seen = b["first_seen"]
    if first_seen is None and st["count"] > 0 and not b["is_mule"]:
        first_seen = st["first"]
    age_days = (TODAY - first_seen).days if first_seen else 0
    beneficiary_out.append((
        b["id"], b["customer_id"], b["bank"], b["acct_no"], mask_acct(b["acct_no"]),
        b["name"], mask_name(b["name"]), b["type"], b["rel"],
        ts(first_seen, 10, 0) if first_seen else None,
        ts(st["last"], 14, 0) if st["last"] else None,
        st["count"], round(st["out"]), round(st["in"]),
        round(statistics.mean(st["amounts"])) if st["amounts"] else None,
        age_days, b["is_mule"],
        "SUSPECTED" if b["is_mule"] else "ACTIVE",
    ))

# ---------------------------------------------------------------------------
# 13. Xuất SQL
# ---------------------------------------------------------------------------
out = sys.stdout
w = out.write

w(f"""-- =====================================================================
-- MSB AI Financial Guardian · SEED DATA
-- Sinh tự động bởi db/generate_seed.py (seed={SEED}) — KHÔNG sửa tay file này.
-- Chạy sau msb_guardian_schema.sql:
--     psql "$DATABASE_URL" -f db/seed.sql
--
-- Nội dung:
--   · {len(CUSTOMERS)} khách hàng phủ đủ 3 phân khúc (SALARY / HNW / SENIOR)
--   · {len(tx_rows):,} giao dịch trải {HISTORY_DAYS} ngày (tới {TODAY.isoformat()})
--   · {len(beneficiary_out)} người nhận, {len(profile_rows)} baseline hành vi
--   · {len(SCENARIOS)} kịch bản lừa đảo, {len(fraud_case_rows)} fraud case kiểm thử
--   · {len(decision_rows)} quyết định rủi ro, {len(case_rows)} case, {len(trace_rows)} nhật ký LLM
--
-- Script idempotent: xóa sạch dữ liệu cũ trước khi nạp lại.
-- =====================================================================

BEGIN;

TRUNCATE TABLE
  product_recommendation, spending_insight, notification, feedback, llm_trace,
  guardian_case, account_event, behavior_profile, transaction_history,
  risk_decision, fraud_case, scam_scenario, beneficiary, loan, deposit,
  account, customer, interest_rate, interest_rate_term, product
RESTART IDENTITY CASCADE;

""")

w("-- 1. Danh mục sản phẩm ------------------------------------------------\n")
w(insert("product",
         ["product_id", "product_name", "product_group", "product_currency",
          "product_status", "product_interest"],
         PRODUCTS))

w("-- 1b. Biểu lãi suất tiết kiệm (kỳ hạn + lãi theo đợt hiệu lực) --------\n")
w(insert("interest_rate_term",
         ["term_code", "term_months", "term_label"],
         RATE_TERMS))
w(insert("interest_rate",
         ["rate_id", "product_id", "term_code", "rate_pct", "effective_from"],
         interest_rate_rows))

w("-- 2. Khách hàng -------------------------------------------------------\n")
for cid, name, target, dob, note in CUSTOMERS:
    w(f"-- {cid} · {SEGMENT_OF[cid]:6s} · {name:18s} · {note}\n")
w(insert("customer",
         ["customer_id", "full_name", "street", "target", "nationality", "legal_id",
          "legal_type", "legal_description", "date_of_birth", "email", "phone_no", "co_code"],
         customer_rows))

w("-- 3. Tài khoản --------------------------------------------------------\n")
w(insert("account",
         ["account_id", "product_group", "product_id", "customer_id", "account_name",
          "currency", "open_acct_bal", "working_balance", "co_code"],
         [tuple(r) for r in account_rows]))

w("-- 4. Sổ tiết kiệm -----------------------------------------------------\n")
w(insert("deposit",
         ["deposit_id", "customer_id", "product_id", "currency", "amount", "interest",
          "interest_margin", "term", "rollover", "channel", "start_date", "maturity_date",
          "linked_account_id", "product_group", "payin_account", "payout_account", "co_code"],
         deposit_rows))

w("-- 5. Khoản vay --------------------------------------------------------\n")
w(insert("loan",
         ["loan_id", "customer_id", "product_group", "product_id", "account_reference",
          "currency", "amount", "rate", "rate_margin", "term", "channel", "start_date",
          "maturity_date", "payin_account", "payout_account", "co_code"],
         loan_rows))

w("-- 6. Người nhận -------------------------------------------------------\n")
w("-- beneficiary_first_seen_at = NULL nghĩa là người nhận hoàn toàn mới,\n")
w("-- đây là đầu vào trực tiếp của yếu tố rủi ro số 2.\n")
w(insert("beneficiary",
         ["beneficiary_id", "customer_id", "beneficiary_bank_code", "beneficiary_account_no",
          "beneficiary_account_masked", "beneficiary_name", "beneficiary_name_masked",
          "beneficiary_type", "beneficiary_relationship", "beneficiary_first_seen_at",
          "beneficiary_last_tx_at", "beneficiary_tx_count", "beneficiary_total_out",
          "beneficiary_total_in", "beneficiary_avg_amount", "beneficiary_age_days",
          "beneficiary_is_synthetic_mule", "beneficiary_status"],
         beneficiary_out))

w("-- 7. Playbook kịch bản lừa đảo ----------------------------------------\n")
w(insert("scam_scenario",
         ["scenario_id", "scenario_name", "group_code", "pattern", "agent_can_ask",
          "signal_pattern", "questions", "options", "advice_title", "advice_body",
          "recommended_action", "priority", "status"],
         [(s["id"], s["name"], s["group"], s["pattern"], s["ask"], s["signal"],
           s["questions"], s["options"], s["advice_title"], s["advice_body"],
           s["action"], s["priority"], "ACTIVE") for s in SCENARIOS]))

w("-- 8. Fraud case (bộ kiểm thử engine — không expose ra API khách hàng) --\n")
w(insert("fraud_case",
         ["fraud_case_id", "customer_id", "scenario_id", "pattern", "description",
          "injection", "expected_level", "expected_score_min", "expected_score_max",
          "demo_scene"],
         fraud_case_rows))

w("-- 9. Quyết định rủi ro (transaction_id gắn sau vì khóa ngoại vòng) -----\n")
w(insert("risk_decision",
         ["decision_id", "customer_id", "account_id", "tx_snapshot", "score", "level",
          "factors", "top_factors", "scenario_id", "template_text", "llm_reasons",
          "question", "options", "selected_option", "advice_title", "advice_body",
          "recommended_action", "action_taken", "outcome", "created_at",
          "intervened_at", "actioned_at"],
         decision_rows))

w("-- 10. Lịch sử giao dịch ------------------------------------------------\n")
tx_out = []
for t in sorted(tx_rows, key=lambda x: (x["date"], x["time"])):
    b = t.get("beneficiary")
    tx_out.append((
        t["transaction_id"], t["customer_id"], t["account_id"], t["direction"],
        b["id"] if b else None, b["bank"] if b else None,
        mask_acct(b["acct_no"]) if b else None, t["currency"],
        str(int(t["amount"])), str(int(t["balance_after"])),
        t["transaction_type"], t["category"], t["description"], t["channel"],
        ymd(t["date"]), t["time"], t["status"],
        tx_decision_link.get(t["transaction_id"]), t["is_fraud"], t["fraud_case_id"], "MSB",
    ))
w(insert("transaction_history",
         ["transaction_id", "customer_id", "account_id", "direction", "beneficiary_id",
          "beneficiary_bank_code", "beneficiary_account_masked", "currency", "amount",
          "balance_after", "transaction_type", "category", "transaction_description",
          "channel", "transaction_date", "transaction_time", "status", "risk_decision_id",
          "is_fraud", "fraud_case_id", "co_code"],
         tx_out))

w("-- 11. Nối ngược risk_decision.transaction_id --------------------------\n")
for did, txid in decision_tx_link.items():
    w(f"UPDATE risk_decision SET transaction_id = {txid} WHERE decision_id = {sql_value(did)};\n")
w("\n")

w("-- 12. Digital twin (tính từ giao dịch sạch trong 90 ngày) -------------\n")
w(insert("behavior_profile",
         ["customer_id", "window_days", "out_median", "out_p90", "out_p99", "out_max",
          "monthly_out_avg", "monthly_in_avg", "known_beneficiaries", "new_benef_per_30d",
          "share_to_new_benef", "active_hours", "night_tx_ratio", "weekend_tx_ratio",
          "tx_per_week", "max_tx_per_day", "max_cum_to_one_benef_14d", "balance_median",
          "max_drain_ratio_90d"],
         profile_rows))

w("-- 13. Sự kiện tài khoản (yếu tố rủi ro số 6) --------------------------\n")
w(insert("account_event",
         ["customer_id", "account_id", "event_type", "amount", "event_time", "source_ref", "meta"],
         event_rows))

w("-- 14. Case -------------------------------------------------------------\n")
w(insert("guardian_case",
         ["case_id", "decision_id", "customer_id", "scenario_id", "status", "narrative",
          "callback_phone_masked", "created_at", "closed_at"],
         case_rows))

w("-- 15. Phản hồi closed-loop --------------------------------------------\n")
w(insert("feedback",
         ["decision_id", "customer_id", "label", "source", "note",
          "applied_to_baseline", "created_at"],
         feedback_rows))

w("-- 16. Thông báo --------------------------------------------------------\n")
w(insert("notification",
         ["customer_id", "decision_id", "channel", "template_key", "payload", "status", "sent_at"],
         notif_rows))

w("-- 17. Nhật ký LLM ------------------------------------------------------\n")
w(insert("llm_trace",
         ["decision_id", "customer_id", "agent", "model", "prompt_key", "prompt_masked",
          "response", "latency_ms", "status", "created_at"],
         trace_rows))

w("-- 18. Insight chi tiêu -------------------------------------------------\n")
w(insert("spending_insight",
         ["insight_id", "customer_id", "period", "category", "amount", "delta_vs_prev",
          "rank_in_period", "insight_text"],
         insight_rows))
w("SELECT setval('spending_insight_insight_id_seq', "
  f"{max(insight_key_seq, 1)}, true);\n\n")

w("-- 19. Gợi ý sản phẩm ---------------------------------------------------\n")
w(insert("product_recommendation",
         ["customer_id", "product_id", "insight_id", "reason", "estimated_benefit", "status"],
         reco_rows))

w("COMMIT;\n\n")

w("-- Kiểm tra nhanh sau khi nạp -------------------------------------------\n")
w("""SELECT c.customer_id,
       CASE c.target WHEN '1' THEN 'SALARY' WHEN '2' THEN 'HNW' ELSE 'SENIOR' END AS phan_khuc,
       count(t.transaction_id)                                  AS so_giao_dich,
       min(t.transaction_date)                                  AS tu_ngay,
       max(t.transaction_date)                                  AS den_ngay,
       b.out_p99                                                AS nguong_p99
FROM customer c
LEFT JOIN transaction_history t ON t.customer_id = c.customer_id
LEFT JOIN behavior_profile   b ON b.customer_id = c.customer_id
GROUP BY c.customer_id, c.target, b.out_p99
ORDER BY c.customer_id;
""")

print(
    f"-- Tổng kết: {len(customer_rows)} KH, {len(tx_out):,} giao dịch, "
    f"{len(beneficiary_out)} người nhận, {len(decision_rows)} quyết định, "
    f"{len(fraud_case_rows)} fraud case.",
    file=sys.stderr,
)
