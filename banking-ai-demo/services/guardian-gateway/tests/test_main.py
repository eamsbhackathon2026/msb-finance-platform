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

def _doc_token(body: str) -> tuple[list[str], bool]:
    """Giải mã stream đúng như bộ parse trong src/lib/api.ts."""
    tokens, thay_done = [], False
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            thay_done = True
            continue
        tokens.append(json.loads(payload)["token"])
    return tokens, thay_done


def test_chat_tra_dung_dinh_dang_sse():
    r = client.post("/api/copilot/chat", json={"message": "Tháng này tôi tiêu nhiều nhất vào đâu?"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    tokens, thay_done = _doc_token(r.text)
    assert thay_done, "thiếu dòng kết thúc data: [DONE]"
    assert tokens, "không có token nào"


def test_ghep_token_lai_ra_dung_cau_tra_loi():
    # Tách theo (\s+) và giữ nguyên khoảng trắng: ghép lại phải khớp từng ký tự,
    # nếu không câu trả lời hiện ra sẽ dính chữ.
    r = client.post("/api/copilot/chat", json={"message": "Tôi có thể tiết kiệm bao nhiêu?"})
    tokens, _ = _doc_token(r.text)
    assert "".join(tokens) == catalog.SCRIPTED_REPLIES[1][1]


def test_cau_hoi_la_roi_ve_cau_tra_loi_mac_dinh():
    r = client.post("/api/copilot/chat", json={"message": "thời tiết hôm nay thế nào"})
    tokens, _ = _doc_token(r.text)
    assert "".join(tokens) == catalog.FALLBACK_REPLY


def test_tieng_viet_khong_bi_escape_thanh_unicode():
    r = client.post("/api/copilot/chat", json={"message": "chi tiêu"})
    assert "\\u1ec1" not in r.text  # ensure_ascii=False
    tokens, _ = _doc_token(r.text)
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


def test_info_noi_ro_chua_noi_domain():
    body = client.get("/info").json()
    assert body["integrated_with_domain_services"] is False
    assert len(body["endpoints"]) == 7


def test_openapi_phuc_vu_dung_7_endpoint_cua_fe():
    paths = client.get("/openapi.json").json()["paths"]
    assert {p for p in paths if p.startswith("/api/")} == {
        "/api/copilot/overview",
        "/api/copilot/chat",
        "/api/risk/assess",
        "/api/ops/metrics",
        "/api/ops/alerts",
        "/api/ops/alerts/{alert_id}",
        "/api/ops/alerts/{alert_id}/decision",
    }
