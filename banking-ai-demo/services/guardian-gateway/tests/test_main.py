"""Test cho guardian-gateway — trọng tâm là HỢP ĐỒNG với FE.

Vì sao contract test quan trọng hơn bình thường ở service này: `guardedCall`
bên FE bắt mọi lỗi rồi im lặng rơi về dữ liệu demo. Đặt sai tên một field,
response vẫn 200, FE vẫn hiện đủ màn hình bằng mock — không ai phát hiện ra cho
tới lúc trình bày. Nên các test dưới đây so khớp CHÍNH XÁC tập key trả về với
interface trong `src/data/types.ts`, chứ không chỉ kiểm tra status code.
"""
import json

import catalog
import main
import pytest
from fastapi.testclient import TestClient

client = TestClient(main.app)

# Chép tay từ src/data/types.ts. Cố tình không sinh tự động từ models.py —
# test phải hỏng khi models.py đổi mà FE thì không.
SPENDING_CATEGORY_KEYS = {"key", "labelVi", "amount", "pct", "trendPct"}
BUDGET_KEYS = {"monthLabel", "spentVnd", "budgetVnd"}
RISK_SIGNAL_KEYS = {"id", "label", "detail", "weight", "severity", "confidencePct"}
RISK_ASSESSMENT_KEYS = {"score", "level", "scenarioName", "signals", "recommendations"}
BENEFICIARY_KEYS = {"accountNo", "bankName", "holderName", "accountAgeDays", "reportCount"}
SCAM_ALERT_KEYS = {"id", "timestamp", "customer", "amount", "beneficiary", "assessment", "status"}
OPS_METRICS_KEYS = {"scannedToday", "alertsFired", "cancelRatePct", "protectedValueVnd"}


# ---- GET /api/copilot/overview ----------------------------------------------

def test_overview_dung_hop_dong():
    r = client.get("/api/copilot/overview")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"budget", "categories", "insights"}
    assert set(body["budget"]) == BUDGET_KEYS
    for cat in body["categories"]:
        assert set(cat) == SPENDING_CATEGORY_KEYS


def test_overview_ngan_sach_cong_khop_cac_nhom():
    body = client.get("/api/copilot/overview").json()
    # Con số trên màn hình phải tự nhất quán: tổng các nhóm đúng bằng số đã chi.
    assert sum(c["amount"] for c in body["categories"]) == body["budget"]["spentVnd"]
    assert body["budget"]["spentVnd"] == 12_460_000
    assert body["budget"]["budgetVnd"] == 18_000_000


def test_insight_khong_co_cta_thi_bo_han_field():
    # ctaLabel khai báo optional bên TS. Trả null sẽ khiến FE render nút rỗng.
    insights = client.get("/api/copilot/overview").json()["insights"]
    khong_cta = [i for i in insights if i["id"] == "ins-1"][0]
    co_cta = [i for i in insights if i["id"] == "ins-3"][0]
    assert "ctaLabel" not in khong_cta
    assert co_cta["ctaLabel"] == "Chuyển 3.000.000 ₫ vào Tiết kiệm"


# ---- POST /api/risk/assess ---------------------------------------------------

def test_so_tien_chuan_cua_demo_ra_dung_87_diem():
    # 85.000.000 ₫ → 87/100 là con số xuất hiện trên màn Scam Shield và trong
    # kịch bản trình bày. Đổi công thức mà quên chỗ này là hỏng đúng màn ⭐.
    r = client.post("/api/risk/assess", json={"amount": 85_000_000})
    assert r.status_code == 200
    body = r.json()
    assert body["score"] == 87
    assert body["level"] == "high"
    assert body["scenarioName"] == "Mạo danh cơ quan công an"


def test_assess_dung_hop_dong():
    body = client.post("/api/risk/assess", json={"amount": 85_000_000}).json()
    assert set(body) == RISK_ASSESSMENT_KEYS
    for sig in body["signals"]:
        assert set(sig) == RISK_SIGNAL_KEYS
        assert sig["severity"] in {"low", "med", "high"}


@pytest.mark.parametrize("amount", [0, 1_000_000, 42_500_000, 85_000_000, 156_000_000, 10_000_000_000])
def test_trong_so_luon_cong_dung_bang_tong_diem(amount):
    # Màn "Vì sao chúng tôi cảnh báo?" cộng các weight lại và hiển thị cạnh
    # tổng điểm; lệch một điểm là người xem nhìn ra ngay.
    body = client.post("/api/risk/assess", json={"amount": amount}).json()
    assert sum(s["weight"] for s in body["signals"]) == body["score"]


def test_diem_tang_theo_so_tien_va_bi_chan_hai_dau():
    assert main.score_for_amount(0) == 30
    assert main.score_for_amount(10_000_000_000) == 97      # chặn trên
    assert main.score_for_amount(-5_000_000_000) == 12      # chặn dưới
    moc = [1_000_000, 20_000_000, 50_000_000, 85_000_000, 120_000_000]
    diem = [main.score_for_amount(a) for a in moc]
    assert diem == sorted(diem)


def test_muc_do_khop_nguong_cua_fe():
    assert catalog.level_of(39) == "low"
    assert catalog.level_of(40) == "medium"
    assert catalog.level_of(69) == "medium"
    assert catalog.level_of(70) == "high"


# ---- GET /api/ops/* ----------------------------------------------------------

def test_ops_metrics_dung_hop_dong():
    r = client.get("/api/ops/metrics")
    assert r.status_code == 200
    assert set(r.json()) == OPS_METRICS_KEYS


def test_danh_sach_canh_bao_dung_hop_dong():
    r = client.get("/api/ops/alerts")
    assert r.status_code == 200
    alerts = r.json()
    assert len(alerts) == 6
    assert alerts[0]["id"] == "ALT-4092"   # case chính của kịch bản demo
    for a in alerts:
        assert set(a) == SCAM_ALERT_KEYS
        assert set(a["beneficiary"]) == BENEFICIARY_KEYS
        assert set(a["assessment"]) == RISK_ASSESSMENT_KEYS
        assert a["status"] in {"pending", "confirmed", "dismissed", "investigating"}


def test_chi_tiet_mot_canh_bao():
    r = client.get("/api/ops/alerts/ALT-4092")
    assert r.status_code == 200
    assert r.json()["amount"] == 85_000_000


def test_canh_bao_khong_ton_tai_tra_404():
    # FE gọi getOpsAlert(id) từ URL; id gõ sai phải ra 404 chứ không phải 200
    # kèm body rỗng — 200 rỗng sẽ làm FE render màn trắng thay vì báo không thấy.
    assert client.get("/api/ops/alerts/ALT-0000").status_code == 404


# ---- POST /api/ops/alerts/{id}/decision --------------------------------------

def test_quyet_dinh_duoc_ghi_nhan_va_doc_lai_duoc():
    main._decisions.clear()
    r = client.post("/api/ops/alerts/ALT-4092/decision",
                    json={"decision": "confirmed", "note": "Đã gọi xác minh khách hàng"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # Đọc lại phải thấy trạng thái mới, ở cả chi tiết lẫn danh sách.
    assert client.get("/api/ops/alerts/ALT-4092").json()["status"] == "confirmed"
    trong_ds = [a for a in client.get("/api/ops/alerts").json() if a["id"] == "ALT-4092"][0]
    assert trong_ds["status"] == "confirmed"
    main._decisions.clear()


def test_quyet_dinh_tren_id_khong_ton_tai_tra_404():
    assert client.post("/api/ops/alerts/ALT-0000/decision",
                       json={"decision": "confirmed", "note": ""}).status_code == 404


def test_quyet_dinh_khong_hop_le_bi_tu_choi():
    r = client.post("/api/ops/alerts/ALT-4092/decision", json={"decision": "xoa-luon", "note": ""})
    assert r.status_code == 422


def test_note_la_tuy_chon():
    main._decisions.clear()
    assert client.post("/api/ops/alerts/ALT-4091/decision", json={"decision": "dismissed"}).status_code == 200
    main._decisions.clear()


# ---- POST /api/copilot/chat (SSE) --------------------------------------------

def _doc_token(body: str) -> tuple[list[str], bool, dict | None]:
    """Giải mã stream đúng như bộ parse trong src/lib/api.ts.

    Stream có hai loại sự kiện: {"token": "..."} lặp lại, và tối đa một
    {"chart": {...}} phát sau khi hết token.
    """
    tokens, thay_done, chart = [], False, None
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            thay_done = True
            continue
        parsed = json.loads(payload)
        if "token" in parsed:
            tokens.append(parsed["token"])
        elif "chart" in parsed:
            chart = parsed["chart"]
    return tokens, thay_done, chart


def test_chat_tra_dung_dinh_dang_sse():
    r = client.post("/api/copilot/chat", json={"message": "Tháng này tôi tiêu nhiều nhất vào đâu?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    tokens, thay_done, _ = _doc_token(r.text)
    assert thay_done, "thiếu dòng kết thúc data: [DONE]"
    assert tokens, "không có token nào"


def test_ghep_token_lai_ra_dung_cau_tra_loi():
    # Tách theo (\s+) và giữ nguyên khoảng trắng: ghép lại phải khớp từng ký tự,
    # nếu không câu trả lời hiện ra sẽ dính chữ.
    r = client.post("/api/copilot/chat", json={"message": "dự báo số dư cuối tháng"})
    tokens, _, _ = _doc_token(r.text)
    assert "".join(tokens) == catalog.SCRIPTED_REPLIES[2][1]


def test_cau_hoi_la_roi_ve_cau_tra_loi_mac_dinh():
    r = client.post("/api/copilot/chat", json={"message": "thời tiết hôm nay thế nào"})
    tokens, _, _ = _doc_token(r.text)
    assert "".join(tokens) == catalog.FALLBACK_REPLY


def test_tieng_viet_khong_bi_escape_thanh_unicode():
    r = client.post("/api/copilot/chat", json={"message": "chi tiêu"})
    assert "\\u1ec1" not in r.text  # ensure_ascii=False
    tokens, _, _ = _doc_token(r.text)
    assert "₫" in "".join(tokens)


# ---- Vận hành ----------------------------------------------------------------

def test_health_khong_cham_gi_ben_ngoai():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "guardian-gateway"}


def test_moi_response_deu_co_header_nguon_du_lieu():
    # Đây là cách duy nhất phân biệt "FE gọi được gateway" với "FE đã im lặng
    # rơi về mock" mà không phải mở Console.
    for path in ["/health", "/info", "/api/ops/metrics", "/api/copilot/overview"]:
        r = client.get(path)
        assert r.headers["X-Guardian-Data-Source"] == "stub"
        assert r.headers["X-Guardian-Service"] == "guardian-gateway"


def test_info_phan_anh_dung_cau_hinh_domain():
    body = client.get("/info").json()
    # conftest tắt DOMAIN_ENABLED nên /info phải báo đúng như vậy.
    assert body["integrated_with_domain_services"] is False
    assert len(body["endpoints"]) == 29


def test_openapi_phuc_vu_dung_cac_endpoint_fe_goi():
    paths = client.get("/openapi.json").json()["paths"]
    assert {p for p in paths if p.startswith("/api/")} == {
        "/api/home",
        "/api/auth/login",
        "/api/session/customer",
        "/api/copilot/overview",
        "/api/copilot/intro",
        "/api/copilot/quarters",
        "/api/copilot/months",
        "/api/copilot/month",
        "/api/copilot/chat",
        "/api/invest/rates",
        "/api/transfer/pending",
        "/api/transfer/action",
        "/api/transfer/beneficiaries",
        "/api/transfer/precheck",
        "/api/transfer/execute",
        "/api/transfer/history",
        "/api/scamshield/signals",
        "/api/risk/assess",
        "/api/risk/explain",
        "/api/safety-center",
        "/api/safety-center/protections/{key}",
        "/api/ops/session",
        "/api/ops/metrics",
        "/api/ops/dashboard",
        "/api/ops/alerts",
        "/api/ops/alerts/{alert_id}",
        "/api/ops/alerts/{alert_id}/detail",
        "/api/ops/alerts/{alert_id}/timeline",
        "/api/ops/alerts/{alert_id}/decision",
    }


def test_invest_rates_tra_bieu_lai_suat_theo_ky_han():
    """Biểu lãi suất: nhóm theo kỳ hạn, xếp tăng dần, con số khớp đợt 2026-09-01.

    conftest tắt DOMAIN_ENABLED nên đây là dữ liệu catalog — catalog được chép
    đúng từ db/seed.sql, vì vậy test này cũng là chốt chống lệch giữa fallback
    và dữ liệu thật."""
    body = client.get("/api/invest/rates").json()
    assert body["asOf"] == "2026-09-01"
    assert {p["id"] for p in body["products"]} == {1, 2, 3, 4, 7}

    months = [r["term"]["months"] for r in body["rows"]]
    assert months == sorted(months)

    rows = {r["term"]["code"]: r for r in body["rows"]}
    t12 = {c["productId"]: c["ratePct"] for c in rows["T12"]["rates"]}
    assert t12 == {1: 5.5, 2: 5.8, 4: 6.1}
    kkh = {c["productId"]: c["ratePct"] for c in rows["KKH"]["rates"]}
    assert kkh == {3: 0.5, 7: 0.1}


# ---- Các endpoint gộp theo màn hình ------------------------------------------

CUSTOMER_KEYS = {"id", "name", "maskedAccount", "balance"}
TIMELINE_EVENT_KEYS = {"id", "label", "time", "tone"}
SAFETY_CENTER_KEYS = {"safetyScore", "scoreLabel", "updatedLabel", "shieldEnabled",
                      "blockedCount", "warnedCount", "reportedCount", "history", "protections"}
OPS_DASHBOARD_KEYS = {"deltas", "hourlyAlerts", "scenarioCounts", "modelInputs"}
CASE_STEP_KEYS = {"id", "time", "label", "done"}


def test_khach_hang_cua_phien():
    body = client.get("/api/session/customer").json()
    assert set(body) == CUSTOMER_KEYS
    assert body["maskedAccount"] == "**** 4821"
    assert body["balance"] == 47_820_000


def test_lenh_chuyen_tien_dang_cho():
    body = client.get("/api/transfer/pending").json()
    assert set(body) == {"amount", "beneficiary"}
    assert body["amount"] == 85_000_000
    assert set(body["beneficiary"]) == BENEFICIARY_KEYS


def test_man_giai_thich_rui_ro():
    body = client.get("/api/risk/explain").json()
    assert set(body) == {"assessment", "beneficiaryTimeline", "similarScenario"}
    assert set(body["assessment"]) == RISK_ASSESSMENT_KEYS
    assert body["assessment"]["score"] == 87
    for ev in body["beneficiaryTimeline"]:
        # detail là optional bên TS nên chỉ được phép thiếu, không được là null.
        assert TIMELINE_EVENT_KEYS <= set(ev) <= TIMELINE_EVENT_KEYS | {"detail"}
        assert ev["tone"] in {"neutral", "warning", "danger"}
    assert body["similarScenario"]["reportedCases"] == 1_284


def test_trung_tam_an_toan():
    body = client.get("/api/safety-center").json()
    assert set(body) == SAFETY_CENTER_KEYS
    assert len(body["protections"]) == 4
    # store.ts dựng map protections theo key nên key phải là duy nhất.
    keys = [p["key"] for p in body["protections"]]
    assert len(keys) == len(set(keys))
    for h in body["history"]:
        assert h["status"] in {"blocked", "ignored", "processing"}


def test_ops_dashboard():
    body = client.get("/api/ops/dashboard").json()
    assert set(body) == OPS_DASHBOARD_KEYS
    # deltas phải khớp đúng 4 khoá của OpsMetrics, vì FE tra delta theo tên KPI.
    assert set(body["deltas"]) == OPS_METRICS_KEYS
    for d in body["deltas"].values():
        assert set(d) == {"valueLabel", "up"}
    assert len(body["hourlyAlerts"]) == 24
    assert len(body["modelInputs"]) == 4


def test_dong_thoi_gian_cua_case():
    body = client.get("/api/ops/alerts/ALT-4092/timeline").json()
    assert all(set(step) == CASE_STEP_KEYS for step in body)
    assert body[-1]["done"] is False   # bước cuối là "chờ quyết định"


def test_dong_thoi_gian_cua_case_khong_ton_tai_tra_404():
    assert client.get("/api/ops/alerts/ALT-0000/timeline").status_code == 404


def test_goi_y_cau_hoi():
    intro = client.get("/api/copilot/intro").json()
    assert set(intro) == {"greeting", "suggestions", "monthLabel"}
    body = intro["suggestions"]
    assert isinstance(body, list) and len(body) == 3
    # Mỗi gợi ý phải khớp một kịch bản trả lời, nếu không bấm vào sẽ ra câu
    # trả lời mặc định — người xem tưởng chat hỏng.
    for q in body:
        r = client.post("/api/copilot/chat", json={"message": q})
        tokens, _, _ = _doc_token(r.text)
        assert "".join(tokens) != catalog.FALLBACK_REPLY, q


def test_chat_ve_chi_tieu_kem_bieu_do():
    r = client.post("/api/copilot/chat", json={"message": "Tháng này tôi tiêu nhiều nhất vào đâu?"})
    tokens, done, chart = _doc_token(r.text)
    assert done and tokens
    assert chart is not None, "câu hỏi về chi tiêu phải kèm biểu đồ"
    assert chart["type"] == "bar"
    assert len(chart["data"]) == 5
    assert set(chart["data"][0]) == {"label", "value"}


def test_chat_khac_khong_kem_bieu_do():
    r = client.post("/api/copilot/chat", json={"message": "thời tiết hôm nay thế nào"})
    _, _, chart = _doc_token(r.text)
    assert chart is None


# ---- Thao tác ghi ------------------------------------------------------------

def test_khach_huy_giao_dich_cap_nhat_case_va_timeline():
    main._decisions.clear(); main._customer_steps.clear()
    truoc = len(client.get("/api/ops/alerts/ALT-4092/timeline").json())

    r = client.post("/api/transfer/action", json={"action": "cancelled"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["caseStatus"] == "confirmed"

    # Khép vòng: chuyên viên vận hành phải thấy hành động của khách.
    assert client.get("/api/ops/alerts/ALT-4092").json()["status"] == "confirmed"
    sau = client.get("/api/ops/alerts/ALT-4092/timeline").json()
    assert len(sau) == truoc + 1
    # Bước cuối luôn là mốc chưa hoàn thành, bước của khách chèn ngay trước nó.
    assert sau[-1]["done"] is False
    assert sau[-2]["label"] == "Khách hàng huỷ giao dịch sau cảnh báo"
    main._decisions.clear(); main._customer_steps.clear()


def test_van_chuyen_tien_thi_case_chuyen_dieu_tra():
    main._decisions.clear(); main._customer_steps.clear()
    body = client.post("/api/transfer/action", json={"action": "proceeded"}).json()
    assert body["caseStatus"] == "investigating"
    main._decisions.clear(); main._customer_steps.clear()


def test_hanh_dong_khong_hop_le_bi_tu_choi():
    assert client.post("/api/transfer/action", json={"action": "chuyen-luon"}).status_code == 422


def test_bat_tat_lop_bao_ve_duoc_luu():
    main._protections.clear()
    assert client.patch("/api/safety-center/protections/biometric", json={"enabled": True}).status_code == 200
    layers = {p["key"]: p["enabled"] for p in client.get("/api/safety-center").json()["protections"]}
    assert layers["biometric"] is True          # mặc định là False
    assert layers["realtime"] is True           # các lớp khác giữ nguyên
    main._protections.clear()


def test_lop_bao_ve_khong_ton_tai_tra_404():
    assert client.patch("/api/safety-center/protections/khong-co", json={"enabled": True}).status_code == 404


# ---- Dữ liệu còn lại của các màn ---------------------------------------------

def test_noi_dung_man_home():
    body = client.get("/api/home").json()
    assert set(body) == {"greeting", "customerName", "productTier", "assistantHint"}
    assert body["customerName"] == "Nguyễn Minh Anh"


def test_phien_lam_viec_ops():
    body = client.get("/api/ops/session").json()
    assert set(body) == {"operator", "systemStatus", "nowLabel"}
    assert set(body["operator"]) == {"name", "role", "shift", "initials"}
    for row in body["systemStatus"]:
        # tone là ngữ nghĩa để FE tự chọn màu, không phải biến CSS.
        assert row["tone"] in {"ok", "warn", "danger"}
        assert not row["tone"].startswith("var(")


def test_chi_tiet_case():
    body = client.get("/api/ops/alerts/ALT-4092/detail").json()
    assert set(body) == {"transaction", "customerProfile", "model", "noteChips"}
    # So khớp CẢ key bên trong, không chỉ key cấp ngoài. Bản trước chỉ kiểm tra
    # cấp ngoài nên để lọt alerts90DCount: alias_generator sinh chữ D hoa ở ranh
    # giới chữ số, response vẫn 200 và FE chỉ nhận undefined.
    assert set(body["transaction"]) == {"channel", "content", "holdStatus", "slaMinutes"}
    assert set(body["customerProfile"]) == {
        "customerSince", "segment", "avgTransferVnd",
        "recentAlertsWindowDays", "recentAlertsCount", "recentAlertsTopScore",
    }
    assert set(body["model"]) == {
        "version", "method", "scoringMs", "confidencePct",
        "interveneThreshold", "softWarnMin", "softWarnMax",
    }
    assert set(body["noteChips"][0]) == {"label", "primary"}
    assert body["model"]["confidencePct"] == 92
    assert body["model"]["interveneThreshold"] == 75
    assert len(body["noteChips"]) == 3


def test_khong_field_nao_bi_alias_sinh_chu_hoa_giua_ten():
    """Chặn cả lớp lỗi vừa gặp, không chỉ một trường.

    alias_generator=to_camel coi ranh giới chữ số là ranh giới từ, nên một tên
    như alerts90d_count thành alerts90DCount. Không có gì báo lỗi: response vẫn
    200 và FE nhận undefined. Quét mọi response để chặn từ gốc.
    """
    import re

    def quet(node, duong_dan=""):
        if isinstance(node, dict):
            for k, v in node.items():
                # Hợp lệ: camelCase thường. Sai: có chữ hoa ngay sau chữ số.
                assert not re.search(r"\d[A-Z]", k), f"{duong_dan}.{k} bị alias sinh chữ hoa sau chữ số"
                quet(v, f"{duong_dan}.{k}")
        elif isinstance(node, list):
            for item in node:
                quet(item, duong_dan)

    for path in ["/api/home", "/api/session/customer", "/api/copilot/overview",
                 "/api/copilot/intro", "/api/transfer/pending", "/api/risk/explain",
                 "/api/safety-center", "/api/ops/session", "/api/ops/metrics",
                 "/api/ops/dashboard", "/api/ops/alerts", "/api/ops/alerts/ALT-4092",
                 "/api/ops/alerts/ALT-4092/detail", "/api/ops/alerts/ALT-4092/timeline"]:
        quet(client.get(path).json(), path)


def test_chi_tiet_case_khong_ton_tai_tra_404():
    assert client.get("/api/ops/alerts/ALT-0000/detail").status_code == 404


def test_trung_tam_an_toan_co_nhan_cap_nhat():
    body = client.get("/api/safety-center").json()
    assert body["updatedLabel"] and body["shieldEnabled"] is True


# ---- POST /api/auth/login ----------------------------------------------------

LOGIN_USER_KEYS = {"userId", "username", "role", "customerId",
                   "emailMasked", "phoneMasked", "userStatus"}


def test_login_che_do_stub_cho_qua_bang_ho_so_demo():
    # conftest tắt DOMAIN_ENABLED nên auth_verify trả None mà không gọi service
    # nào: đăng nhập rơi về hồ sơ demo. Body đánh dấu source="degraded" (đăng
    # nhập này không được xác thực thật), còn header là "stub" vì gateway không
    # chạm domain lần nào — hai trục khác nhau, cả hai đều đúng.
    r = client.post("/api/auth/login", json={"username": "kh100008", "password": "bất kỳ"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["source"] == "degraded"
    assert body["user"]["username"] == "kh100008"
    assert body["user"]["customerId"] == 100008
    assert r.headers["X-Guardian-Data-Source"] == "stub"


def test_login_identity_chet_khi_domain_bat_thi_header_degraded(monkeypatch):
    # Cảnh thật khi chạy production: DOMAIN_ENABLED=true nhưng identity-service
    # không gọi được. _post đánh dấu touched+degraded, header phải là "degraded"
    # để nhìn từ tab Network biết đăng nhập đã rơi về bản dự phòng.
    monkeypatch.setattr(main.domain, "DOMAIN_ENABLED", True)
    monkeypatch.setattr(main.domain, "IDENTITY_URL", "http://127.0.0.1:1")  # cổng cụt → từ chối ngay
    r = client.post("/api/auth/login", json={"username": "kh100008", "password": "123456"})
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    assert r.json()["source"] == "degraded"
    assert r.headers["X-Guardian-Data-Source"] == "degraded"


def test_login_dung_mat_khau_tra_ho_so_da_che_pii(monkeypatch):
    async def fake_verify(username, password):
        return {"authenticated": True, "user": {
            "user_id": 13, "username": "kh100008", "role": "CUSTOMER",
            "customer_id": 100008, "full_name_masked": None,
            "email_masked": "ng***@example.com", "phone_masked": "09** *** 303",
            "user_status": "ACTIVE",
        }}
    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    r = client.post("/api/auth/login", json={"username": "kh100008", "password": "123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["source"] == "domain"
    assert set(body["user"]) == LOGIN_USER_KEYS  # fullNameMasked là None nên bị loại
    # Không được lộ password_hash dù identity-service lỡ trả về.
    assert "passwordHash" not in body["user"]


def test_login_sai_mat_khau_tra_ly_do_khong_cho_vao(monkeypatch):
    async def fake_verify(username, password):
        return {"authenticated": False, "reason": "invalid_credentials"}
    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    r = client.post("/api/auth/login", json={"username": "kh100008", "password": "sai"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["reason"] == "invalid_credentials"
    assert "user" not in body  # response_model_exclude_none bỏ user=None


def test_login_password_hash_khong_bao_gio_ra_response(monkeypatch):
    # Kể cả identity-service trả thừa password_hash, response_model của gateway
    # chỉ lấy đúng các field khai báo trong LoginUser — hash không lọt ra.
    async def fake_verify(username, password):
        return {"authenticated": True, "user": {
            "user_id": 13, "username": "kh100008", "role": "CUSTOMER",
            "customer_id": 100008, "user_status": "ACTIVE",
            "password_hash": "$2a$10$hacke",
        }}
    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    body = client.post("/api/auth/login", json={"username": "x", "password": "y"}).json()
    assert "passwordHash" not in body["user"]
    assert "password_hash" not in body["user"]


# ---- Bảng + biểu đồ số liệu thật đính vào câu trả lời chi tiêu ----------------

def _doc_events(body: str):
    """Bóc tách token / table / chart từ stream SSE."""
    tokens, table, chart = [], None, None
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p == "[DONE]":
            continue
        o = json.loads(p)
        if "token" in o:
            tokens.append(o["token"])
        elif "table" in o:
            table = o["table"]
        elif "chart" in o:
            chart = o["chart"]
    return tokens, table, chart


def test_chat_chi_tieu_thang_dinh_kem_bang_va_bieu_do():
    r = client.post("/api/copilot/chat", json={"message": "Tổng hợp chi tiêu tháng này của tôi"})
    assert r.status_code == 200
    _, table, chart = _doc_events(r.text)
    assert table is not None, "câu hỏi chi tiêu tháng phải kèm bảng"
    assert chart is not None and chart["type"] == "bar"
    # Hợp đồng bảng khớp FE
    assert set(table) == {"title", "rows", "totalLabel", "totalAmount", "rowHeader",
                          "amountHeader", "pctHeader", "trendHeader", "footnote"}
    # Bảng chi tiêu giữ nguyên nhãn mặc định và VẪN có cột Δ.
    assert table["trendHeader"] == "Δ kỳ trước"
    assert table["rows"] and set(table["rows"][0]) >= {"label", "amount", "pct"}
    # Số trong bảng cộng đúng bằng tổng, và bằng dữ liệu biểu đồ (một nguồn số).
    assert sum(row["amount"] for row in table["rows"]) == table["totalAmount"]
    assert [r_["amount"] for r_ in table["rows"]] == [p["value"] for p in chart["data"]]


def test_chat_chi_tieu_quy_dinh_kem_bang():
    r = client.post("/api/copilot/chat", json={"message": "Cho tôi xem chi tiêu quý vừa qua"})
    _, table, chart = _doc_events(r.text)
    assert table is not None and chart is not None
    assert "quý" in table["title"].lower() or "q" in table["title"].lower()
    # Bảng quý sắp theo số tiền giảm dần (nhóm lớn nhất đứng đầu).
    amounts = [row["amount"] for row in table["rows"]]
    assert amounts == sorted(amounts, reverse=True)


def test_chat_cau_hoi_khong_phai_chi_tieu_thi_khong_co_bang():
    r = client.post("/api/copilot/chat", json={"message": "thời tiết hôm nay thế nào"})
    _, table, _ = _doc_events(r.text)
    assert table is None, "câu ngoài tài chính không nên kèm bảng"


def test_chat_trend_none_giu_nguyen_khong_thanh_khong():
    # trendPct có thể là null (chưa có kỳ trước). Không được ép thành 0 —
    # "chưa có mốc so" khác hẳn "không đổi".
    r = client.post("/api/copilot/chat", json={"message": "chi tiêu quý này theo nhóm"})
    _, table, _ = _doc_events(r.text)
    assert table is not None
    # ít nhất phải serialize được, và trendPct nếu có mặt thì là int hoặc None
    for row in table["rows"]:
        assert row.get("trendPct") is None or isinstance(row["trendPct"], int)


def test_quarter_visual_lam_tron_delta_so_thuc():
    # delta_vs_prev_pct của domain là SỐ THỰC (3.5, -49.8). ChatTableRow.trend_pct
    # là int — nếu không làm tròn, pydantic ném ValidationError và chat trả 500.
    # Bug này đã xảy ra trên production nhưng test cũ không bắt vì catalog fallback
    # không có delta số thực. Test này dựng thẳng dữ liệu có phần thập phân.
    from models import QuarterCategory, QuarterlyReport, QuarterSummary
    report = QuarterlyReport(
        quarters=[QuarterSummary(
            period="2026-Q3", label="Quý 3/2026", income=0, expense=1_000_000, net=0, count=3,
            by_category=[
                QuarterCategory(category="FAMILY_SUPPORT", label_vi="Hỗ trợ gia đình",
                                amount=600_000, pct=60, rank=1, delta_vs_prev_pct=3.5),
                QuarterCategory(category="HEALTHCARE", label_vi="Y tế",
                                amount=400_000, pct=40, rank=2, delta_vs_prev_pct=-49.8),
                QuarterCategory(category="BILLS", label_vi="Hoá đơn",
                                amount=0, pct=0, rank=3, delta_vs_prev_pct=None),
            ],
        )],
        category_totals=[],
    )
    table, chart = main._quarter_visual(report)
    assert table is not None and chart is not None
    trends = {r.label: r.trend_pct for r in table.rows}
    assert trends["Hỗ trợ gia đình"] == 4      # 3.5 → làm tròn
    assert trends["Y tế"] == -50               # -49.8 → làm tròn
    assert trends["Hoá đơn"] is None           # None giữ nguyên (chưa có kỳ trước)
    assert all(isinstance(r.trend_pct, int) or r.trend_pct is None for r in table.rows)


# ---- GET /api/copilot/months + bảng so sánh tháng ----------------------------

def test_copilot_months_dung_hop_dong():
    body = client.get("/api/copilot/months").json()
    assert set(body) == {"months", "categoryTotals"}
    assert len(body["months"]) >= 2
    m = body["months"][-1]
    assert {"period", "label", "year", "month", "income", "expense", "net",
            "count", "byCategory"} <= set(m)
    # deltaVsPrevPct có mặt hoặc null, không được là chuỗi
    assert m.get("deltaVsPrevPct") is None or isinstance(m["deltaVsPrevPct"], (int, float))
    assert m["label"].startswith("Tháng")


def test_chat_so_sanh_thang_dinh_kem_bang_nhieu_thang():
    # "so với tháng 8" là câu SO SÁNH — bảng phải có mỗi dòng là một tháng,
    # không phải bảng nhóm của một tháng.
    r = client.post("/api/copilot/chat", json={"message": "chi tiêu tháng này so với tháng 8 thì sao?"})
    _, table, chart = _doc_events(r.text)
    assert table is not None and chart is not None
    assert "so sánh" in table["title"].lower()
    assert all(row["label"].startswith("Tháng") for row in table["rows"])
    assert len(table["rows"]) >= 2
    # Tổng = cộng các tháng
    assert sum(row["amount"] for row in table["rows"]) == table["totalAmount"]


def test_months_visual_can_it_nhat_hai_thang():
    from models import MonthlyReport, MonthSummary
    one = MonthlyReport(months=[MonthSummary(
        period="202609", label="Tháng 9/2026", year=2026, month=9,
        income=0, expense=1_000_000, net=0, count=1, delta_vs_prev_pct=None, by_category=[])],
        category_totals=[])
    assert main._months_compare_visual(one) == (None, None)


def test_months_visual_delta_lam_tron():
    from models import MonthCategory, MonthlyReport, MonthSummary
    rep = MonthlyReport(months=[
        MonthSummary(period="202608", label="Tháng 8/2026", year=2026, month=8,
                     income=0, expense=2_000_000, net=0, count=2, delta_vs_prev_pct=None, by_category=[]),
        MonthSummary(period="202609", label="Tháng 9/2026", year=2026, month=9,
                     income=0, expense=3_000_000, net=0, count=3, delta_vs_prev_pct=49.8, by_category=[]),
    ], category_totals=[])
    table, chart = main._months_compare_visual(rep)
    assert [r.label for r in table.rows] == ["Tháng 8/2026", "Tháng 9/2026"]
    assert table.rows[0].trend_pct is None            # tháng đầu không mốc
    assert table.rows[1].trend_pct == 50              # 49.8 → làm tròn
    assert table.total_amount == 5_000_000
    assert [p.value for p in chart.data] == [2_000_000, 3_000_000]


def test_strip_markdown_bo_bang_va_dam_giu_chu():
    # Agent trả bảng markdown + **đậm**; FE văn bản thuần phải nhận chữ sạch,
    # không dấu | và không **, vì bảng số đã có bản riêng do gateway đính.
    md = (
        "Đây là chi tiêu Quý 3/2026:\n\n"
        "| Nhóm | Số tiền |\n|---|---:|\n| Hỗ trợ gia đình | 9.300.000 ₫ |\n\n"
        "**Nhận xét:**\n- **Y tế** giảm gần 50%.\n"
    )
    out = main._strip_markdown_for_plain(md)
    assert "|" not in out
    assert "**" not in out and "---" not in out
    assert "Đây là chi tiêu Quý 3/2026:" in out
    assert "Nhận xét:" in out
    assert "- Y tế giảm gần 50%." in out
    assert ">" not in main._strip_markdown_for_plain("> 📊 Thu nhập: 1 ₫\nchữ")


def test_strip_markdown_van_ban_thuan_giu_nguyen():
    plain = "Tháng 9 bạn chi 2.465.000 ₫, giảm 62% so tháng 8.\n- Ăn uống giảm mạnh."
    assert main._strip_markdown_for_plain(plain) == plain


# ---- Một tháng cụ thể (tháng 6) — không được nhầm sang tháng hiện tại --------

def test_copilot_month_endpoint():
    r = client.get("/api/copilot/month", params={"period": "202606"})
    assert r.status_code == 200
    b = r.json()
    assert b["period"] == "202606" and "Tháng 6" in b["label"]
    assert b["byCategory"]
    assert client.get("/api/copilot/month", params={"period": "6"}).status_code == 422
    assert client.get("/api/copilot/month", params={"period": "202001"}).status_code == 404


def test_chat_hoi_thang_6_dinh_bang_thang_6_khong_phai_thang_nay():
    # Lỗi đã gặp: hỏi tháng 6, bảng lại là tháng 9 (tháng hiện tại). Bảng phải
    # đúng tháng được hỏi.
    r = client.post("/api/copilot/chat", json={"message": "thống kê cho tôi chi tiêu tháng 6"})
    _, table, chart = _doc_events(r.text)
    assert table is not None and chart is not None
    assert "Tháng 6" in table["title"], f"bảng sai tháng: {table['title']}"
    assert "Tháng 9" not in table["title"]


def test_resolve_period():
    assert main._resolve_period(6, 2026) == "202606"
    # không cho năm: lấy lần gần nhất tháng đó đã qua (đuôi phải là số tháng)
    assert main._resolve_period(6, None).endswith("06")
    assert len(main._resolve_period(6, None)) == 6


def test_so_sanh_6_thang_van_ra_bang_nhieu_thang():
    # "6 tháng gần đây" là SO SÁNH, không phải "tháng 6"
    r = client.post("/api/copilot/chat", json={"message": "xem chi tiêu 6 tháng gần đây"})
    _, table, _ = _doc_events(r.text)
    assert table is not None and "So sánh" in table["title"]


def test_agent_loi_cau_chi_tieu_khong_doc_kich_ban_thang_9():
    # Lỗi đã gặp: agent lỗi/timeout → chữ kịch bản viết cứng tháng 9
    # ("12.460.000 ₫", "GrabFood") hiện KÈM bảng tháng 7 → mâu thuẫn. Khi câu
    # hỏi đã có bảng số thật, chữ phải bám tiêu đề bảng, không đọc kịch bản.
    # (Trong test agent chưa cấu hình nên from_agent=None — đúng như agent lỗi.)
    r = client.post("/api/copilot/chat", json={"message": "thống kê chi tiêu tháng 7"})
    tokens, table, _ = _doc_events(r.text)
    txt = "".join(tokens)
    assert table is not None and "Tháng 7" in table["title"]
    assert "12.460.000" not in txt and "GrabFood" not in txt  # không đọc kịch bản
    assert "Tháng 7" in txt                                    # chữ khớp bảng


def test_agent_loi_cau_khong_chi_tieu_van_dung_kich_ban():
    # Câu KHÔNG có bảng (dự báo số dư) vẫn dùng kịch bản như cũ — không đổi.
    r = client.post("/api/copilot/chat", json={"message": "dự báo số dư cuối tháng"})
    tokens, table, _ = _doc_events(r.text)
    assert table is None
    assert "số dư" in "".join(tokens).lower()


# ---- Luồng chuyển tiền: favorite (bỏ Scam Shield) vs stk mới (agent) ----------

def test_transfer_beneficiaries_dung_hop_dong():
    r = client.get("/api/transfer/beneficiaries")
    assert r.status_code == 200
    bl = r.json()
    assert bl and set(bl[0]) == {"id", "name", "bank", "account", "relationship", "trusted"}


def test_is_trusted_logic():
    assert main._is_trusted({"known": True, "is_new": False, "status": "ACTIVE"}) is True
    assert main._is_trusted({"known": True, "is_new": True, "status": "ACTIVE"}) is False    # stk mới
    assert main._is_trusted({"known": True, "is_new": False, "status": "SUSPECTED"}) is False  # bị nghi
    assert main._is_trusted({"known": False, "is_new": False, "status": "ACTIVE"}) is False    # không quen
    assert main._is_trusted({}) is False


def test_precheck_stk_moi_can_review_co_verdict():
    # stub: resolve None → không quen → phải review; agent chưa cấu hình → verdict fallback
    r = client.post("/api/transfer/precheck",
                    json={"bankCode": "VPB", "accountNo": "1902664130", "amount": 85_000_000, "note": "gap"})
    assert r.status_code == 200
    b = r.json()
    assert b["requiresReview"] is True and b["trusted"] is False
    assert b["verdict"]["level"] in {"safe", "suspect", "danger"}
    assert b["verdict"]["source"] == "fallback"
    assert set(b["verdict"]) >= {"level", "title", "summary", "reasons", "recommendation", "source"}


def test_parse_verdict_boc_dung_muc_do():
    v = main._parse_verdict("NGUY_HIEM\n- Tài khoản mới\n- Đã bị báo cáo\nKhuyến nghị: không nên chuyển.")
    assert v is not None and v.level == "danger" and v.source == "agent"
    assert any("mới" in r for r in v.reasons)
    assert "không nên" in v.recommendation.lower()
    assert main._parse_verdict("câu không có tag mức độ") is None


def test_fallback_verdict_stk_bi_nghi_la_danger():
    from models import ScamShieldSignals
    s = ScamShieldSignals(bank_code="ACB", account_no="x", account_masked="x", known=True, is_new=True,
                          relationship="UNKNOWN", status="SUSPECTED", age_days=0, tx_count=0,
                          amount=85_000_000, note="", scenario_match="Giả danh công an")
    v = main._fallback_verdict(s)
    assert v.level == "danger"
    assert any("báo cáo" in r for r in v.reasons)


# ---- Tư vấn: lộ trình tiết kiệm cho mục tiêu lớn ------------------------------

def _thang(period: str, income: int, expense: int):
    from models import MonthSummary
    return MonthSummary(period=period, label=f"Tháng {int(period[4:])}/{period[:4]}",
                        year=int(period[:4]), month=int(period[4:]), income=income,
                        expense=expense, net=income - expense, count=40, by_category=[])


@pytest.mark.parametrize("cau, mong_doi", [
    ("kế hoạch mua ô tô khoảng 500 triệu", 500_000_000),
    ("tôi muốn mua nhà 1,5 tỷ", 1_500_000_000),
    ("để dành 800tr mua đất", 800_000_000),
    ("mục tiêu 2 tỉ", 2_000_000_000),
    # Số nhỏ là khoản chi lẻ, không phải mục tiêu tích lũy.
    ("tháng này ăn uống hết 5 triệu", None),
    ("kế hoạch tiết kiệm của tôi thế nào", None),
])
def test_doc_so_tien_muc_tieu(cau, mong_doi):
    assert main._goal_amount(cau) == mong_doi


def test_kha_nang_de_danh_khong_bi_thang_bat_thuong_keo_lech():
    """Tháng 9 có khoản tiền về hơn 500 triệu. Trung bình sẽ ra ~88 triệu/tháng —
    sai hoàn toàn; trung vị phải bám nhịp thật (~1 triệu)."""
    months = [_thang("202604", 7_600_000, 7_372_000), _thang("202605", 7_600_000, 6_582_000),
              _thang("202606", 7_600_000, 5_354_000), _thang("202607", 7_600_000, 7_715_000),
              _thang("202608", 7_600_000, 6_545_000), _thang("202609", 527_600_000, 2_465_000)]
    kha_nang, thu_nhap, so_thang = main._saving_capacity(months)
    trung_binh = sum(m.net for m in months) // len(months)
    assert kha_nang == 1_018_000
    assert kha_nang < trung_binh / 50, "trung vị phải miễn nhiễm với tháng bất thường"
    assert thu_nhap == 7_600_000
    assert so_thang == 5, "tháng đang chạy chưa đủ ngày nên bị loại"


def test_lo_trinh_tiet_kiem_vach_ro_muc_bat_kha_thi():
    """Điểm mấu chốt: lộ trình 5 năm đòi 7,9 triệu/tháng trong khi thu nhập chỉ
    7,6 triệu — bảng phải nói ra điều đó bằng cột "% thu nhập" > 100%."""
    months = [_thang("202604", 7_600_000, 7_372_000), _thang("202605", 7_600_000, 6_582_000),
              _thang("202606", 7_600_000, 5_354_000), _thang("202607", 7_600_000, 7_715_000),
              _thang("202608", 7_600_000, 6_545_000)]
    table, chart = main._savings_plan_visual(500_000_000, 28_679_000, months)
    assert table is not None and chart is not None
    assert [r.label for r in table.rows] == ["1 năm", "2 năm", "3 năm", "5 năm"]
    con_thieu = 500_000_000 - 28_679_000
    assert table.rows[3].amount == round(con_thieu / 60)
    assert table.rows[3].pct > 100, "5 năm vẫn vượt thu nhập → phải lộ ra"
    # Dòng tổng là mức để dành THẬT, nhỏ hơn hẳn mọi lộ trình.
    assert table.total_amount == 1_018_000
    assert all(r.amount > table.total_amount for r in table.rows)
    assert table.trend_header is None, "kịch bản tương lai không có kỳ trước để so"
    assert table.row_header == "Lộ trình" and table.pct_header == "% thu nhập"
    assert "còn thiếu" in table.footnote and "năm" in table.footnote
    # Cột cuối biểu đồ là mức thật, đặt cạnh các lộ trình để thấy khoảng cách.
    assert chart.data[-1].label == "Thực tế" and chart.data[-1].value == 1_018_000


def test_da_du_tien_thi_khong_ve_lo_trinh():
    months = [_thang("202604", 7_600_000, 7_000_000), _thang("202605", 7_600_000, 7_000_000)]
    assert main._savings_plan_visual(20_000_000, 50_000_000, months) == (None, None)


def test_chat_hoi_ke_hoach_mua_xe_dinh_kem_bang_lo_trinh():
    r = client.post("/api/copilot/chat", json={
        "message": "tôi đang có kế hoạch mua xe ô tô khoảng 500 triệu, "
                   "bạn đề xuất kế hoạch tiết kiệm dựa trên chi tiêu của tôi không"})
    assert r.status_code == 200
    tokens, table, chart = _doc_events(r.text)
    assert table is not None, "câu tư vấn mục tiêu phải kèm bảng lộ trình"
    assert table["rowHeader"] == "Lộ trình" and table["amountHeader"] == "Cần/tháng"
    assert table["trendHeader"] is None and table["footnote"]
    assert len(table["rows"]) == 4
    assert chart is not None and chart["data"][-1]["label"] == "Thực tế"
    # Không đọc kịch bản "dư 6,5 triệu" nữa — nó mâu thuẫn với số thật trong bảng.
    assert "6.500.000" not in "".join(tokens)


def test_chat_tu_van_khong_neu_so_tien_thi_ve_bang_tien_du():
    r = client.post("/api/copilot/chat", json={"message": "tôi nên tiết kiệm thế nào"})
    _, table, chart = _doc_events(r.text)
    assert table is not None and table["rowHeader"] == "Tháng"
    assert table["amountHeader"] == "Thu − chi"
    assert chart is not None


def test_ke_hoach_chi_tieu_thang_6_van_ra_bang_thang_6():
    """Bảo vệ thứ tự xét: câu có chữ "kế hoạch" nhưng hỏi một tháng cụ thể thì
    vẫn phải ra bảng tháng đó, không bị nhánh tư vấn cướp mất."""
    r = client.post("/api/copilot/chat", json={"message": "kế hoạch chi tiêu tháng 6 của tôi"})
    _, table, _ = _doc_events(r.text)
    assert table is not None
    assert "Tháng 6" in table["title"] and table["rowHeader"] == "Nhóm"


def test_bo_thang_mep_cua_so_khong_co_thu_nhap():
    """Tháng đầu cửa sổ dữ liệu bắt đầu từ giữa tháng nên chưa có kỳ lương:
    income = 0, net âm sâu. Tính vào thì mức để dành tụt gần một nửa (1,0 triệu
    → 623 nghìn) và lộ trình dài thêm 24 năm — phải loại."""
    months = [_thang("202603", 0, 4_588_000), _thang("202604", 7_600_000, 7_372_000),
              _thang("202605", 7_600_000, 6_582_000), _thang("202606", 7_600_000, 5_354_000),
              _thang("202607", 7_600_000, 7_715_000), _thang("202608", 7_600_000, 6_545_000)]
    kha_nang, thu_nhap, so_thang = main._saving_capacity(months)
    assert so_thang == 5, "tháng không có thu nhập không phải một chu kỳ sống"
    assert kha_nang == 1_018_000
    assert thu_nhap == 7_600_000


# ---- Bảng markdown của agent → bảng thật (áp dụng cho MỌI câu hỏi) ------------

_MD_MAU = """Phân bổ chi tiêu tháng 9/2026:

| Nhóm chi tiêu | Số tiền | Tỷ trọng |
|---------------|--------:|---------:|
| Hỗ trợ gia đình | 1.600.000 ₫ | 65% |
| **Y tế** | **811.000 ₫** | 33% |

Nhận xét: nhóm Y tế tăng đột biến.
"""


def test_doc_bang_markdown_cua_agent():
    gs = main._parse_markdown_grids(_MD_MAU)
    assert len(gs) == 1
    g = gs[0]
    assert [c.label for c in g.columns] == ["Nhóm chi tiêu", "Số tiền", "Tỷ trọng"]
    # Cột chữ canh trái, cột số canh phải cho thẳng hàng.
    assert [c.align for c in g.columns] == ["left", "right", "right"]
    # **đậm** phải được gỡ, nếu không ô sẽ hiện thô "**Y tế**".
    assert g.rows[1] == ["Y tế", "811.000 ₫", "33%"]


def test_tu_suy_canh_le_khi_agent_khong_ghi():
    md = "| Kịch bản | Số tiền |\n| --- | --- |\n| 3 năm | 13.092.250 ₫ |\n| 5 năm | 7.855.350 ₫ |"
    g = main._parse_markdown_grids(md)[0]
    assert [c.align for c in g.columns] == ["left", "right"]


def test_khoi_khong_phai_bang_thi_bo_qua():
    # Thiếu dòng kẻ ngang → không phải bảng, không được dựng bảng rỗng.
    assert main._parse_markdown_grids("| a | b |\n| 1 | 2 |") == []
    # Có tiêu đề + kẻ ngang nhưng không có dòng dữ liệu nào.
    assert main._parse_markdown_grids("| a | b |\n| --- | --- |") == []
    assert main._parse_markdown_grids("không có bảng nào ở đây") == []


def test_dong_thieu_o_van_du_cot():
    md = "| a | b | c |\n| --- | --- | --- |\n| 1 | 2 |"
    g = main._parse_markdown_grids(md)[0]
    assert g.rows == [["1", "2", ""]], "dòng thiếu ô phải đệm cho đủ, không lệch cột"


def test_gioi_han_so_bang_va_so_dong():
    mot_bang = "| a |\n| --- |\n" + "".join(f"| {i} |\n" for i in range(40))
    gs = main._parse_markdown_grids(mot_bang * 6)
    assert len(gs) <= 3
    assert all(len(g.rows) <= 15 for g in gs)


def test_so_sanh_cac_nhom_trong_thang_khong_ra_bang_so_thang():
    """"so sánh chi tiêu các NHÓM tháng này" là so các nhóm trong một tháng.
    Trước đây rơi vào bảng so tháng-với-tháng nên chữ nói về nhóm mà bảng lại
    liệt kê từng tháng."""
    r = client.post("/api/copilot/chat",
                    json={"message": "so sánh chi tiêu các nhóm tháng này và cho tôi lời khuyên cắt giảm"})
    _, table, _ = _doc_events(r.text)
    assert table is not None
    assert table["rowHeader"] == "Nhóm", "phải là bảng theo nhóm, không phải theo tháng"
    assert "so sánh" not in table["title"].lower()
