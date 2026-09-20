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
    assert len(body["endpoints"]) == 38


def test_openapi_phuc_vu_dung_cac_endpoint_fe_goi():
    paths = client.get("/openapi.json").json()["paths"]
    assert {p for p in paths if p.startswith("/api/")} == {
        "/api/home",
        "/api/auth/login",
        "/api/session/customer",
        "/api/copilot/overview",
        "/api/copilot/intro",
        "/api/copilot/notifications",
        "/api/copilot/quarters",
        "/api/copilot/months",
        "/api/copilot/month",
        "/api/copilot/chat",
        "/api/invest/rates",
        "/api/invest/maturing-deposits",
        "/api/invest/open",
        "/api/chat-banking/parse",
        "/api/transfer/intervene",
        "/api/transfer/intervene/{decision_id}",
        "/api/products/rates",
        "/api/products/loan-options",
        "/api/products/savings-options",
        "/api/transfer/pending",
        "/api/transfer/action",
        "/api/transfer/beneficiaries",
        "/api/transfer/precheck",
        "/api/transfer/execute",
        "/api/transfer/history",
        "/api/scamshield/signals",
        "/api/scamshield/verdict",
        "/api/risk/assess",
        "/api/risk/explain",
        "/api/safety-center",
        "/api/safety-center/protections/{key}",
        "/api/ops/login",
        "/api/ops/session",
        "/api/ops/metrics",
        "/api/ops/dashboard",
        "/api/ops/alerts",
        "/api/ops/alerts/{alert_id}",
        "/api/ops/cases",
        "/api/ops/scenarios",
        "/api/ops/model",
        "/api/ops/audit",
        "/api/ops/alerts/{alert_id}/detail",
        "/api/ops/alerts/{alert_id}/timeline",
        "/api/ops/alerts/{alert_id}/decision",
    }


def test_invest_maturing_deposits_stub_tra_so_demo_den_han_hom_nay():
    """Domain tắt → sổ demo 7009 đến hạn ĐÚNG hôm nay: maturityDate phải trùng
    asOf và được đổ lúc trả lời (hằng số tĩnh sẽ sai ngay ngày hôm sau)."""
    body = client.get("/api/invest/maturing-deposits").json()
    assert body["count"] == 1 and len(body["deposits"]) == 1
    d = body["deposits"][0]
    assert d["dueToday"] is True and d["overdue"] is False
    assert d["maturityDate"] == body["asOf"] != ""
    assert d["amount"] == 770_000_000


def test_copilot_notifications_du_3_nhac_viec():
    """Màn Copilot có đúng 3 nhắc việc dưới nhóm chi tiêu, theo thứ tự
    sổ đến hạn → sao kê thẻ → kỳ trả nợ; mục sổ tiết kiệm phải có CTA
    (FE dẫn sang màn Biểu lãi suất để chọn sản phẩm tái gửi)."""
    r = client.get("/api/copilot/notifications")
    assert r.status_code == 200
    body = r.json()
    assert [n["kind"] for n in body] == ["saving", "card", "loan"]
    for n in body:
        assert {"id", "kind", "title", "body"} <= set(n)
    assert body[0]["ctaLabel"]


def test_invest_rates_tra_bieu_lai_suat_theo_ky_han():
    """Biểu lãi suất: nhóm theo kỳ hạn, xếp tăng dần, khớp danh mục MSB thật.

    conftest tắt DOMAIN_ENABLED nên đây là dữ liệu catalog — catalog được chụp
    từ chính bảng product × interest_rate × interest_rate_term, vì vậy test này
    cũng là chốt chống lệch giữa fallback và dữ liệu thật."""
    body = client.get("/api/invest/rates").json()
    assert body["asOf"] == "2026-09-17"
    # Mã sản phẩm lõi là CHUỖI ("RB.TK.LSCN"), không phải số tự tăng.
    assert {p["id"] for p in body["products"]} == {
        "RB.PW.TGTK", "RB.TK.DKSL", "RB.TK.LSCN",
        "RB.TK.MANGNON", "RB.TK.ONGVANG", "RB.TK.UPFRONT",
    }

    months = [r["term"]["months"] for r in body["rows"]]
    assert months == sorted(months)
    # Kỳ hạn ngắn hơn tháng phải giữ được phần thập phân, không bị ép về 0.
    assert 0.25 in months

    rows = {r["term"]["code"]: r for r in body["rows"]}
    t12 = {c["productId"]: c["ratePct"] for c in rows["12M"]["rates"]}
    assert t12["RB.TK.LSCN"] == 5.9 and t12["RB.PW.TGTK"] == 5.2


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


# ---- Bốn màn vận hành phụ ----------------------------------------------------

OPS_CASE_KEYS = {"id", "decisionId", "customer", "scenarioName", "status",
                 "statusLabel", "narrative", "openedAt", "closedAt"}
OPS_SCENARIO_KEYS = {"id", "name", "groupLabel", "patternLabel", "actionLabel",
                     "canAsk", "adviceTitle", "adviceBody", "priority", "alertsToday"}
OPS_MODEL_KEYS = {"softWarnMin", "interveneMin", "maxScore", "factors", "inputs"}
OPS_AUDIT_KEYS = {"totalCalls", "fallbackRatePct", "avgLatencyMs", "perAgent", "traces",
                  "customers", "latestTraceAt"}


def test_ops_cases_dung_hop_dong():
    body = client.get("/api/ops/cases").json()
    assert len(body) > 0
    for case in body:
        # closedAt là optional bên TS; case đang mở thì bỏ hẳn field.
        assert set(case) <= OPS_CASE_KEYS
        assert {"id", "decisionId", "customer", "status", "statusLabel"} <= set(case)
        assert case["status"] in {"open", "callbackDone", "closedFraud", "closedLegit"}


def test_ops_cases_loc_theo_trang_thai():
    dang_mo = client.get("/api/ops/cases?status=open").json()
    assert all(c["status"] == "open" for c in dang_mo)
    assert len(dang_mo) < len(client.get("/api/ops/cases").json())


def test_ops_scenarios_khong_ro_ma_noi_bo_ra_man_hinh():
    body = client.get("/api/ops/scenarios").json()
    assert len(body) > 0
    for scen in body:
        assert set(scen) == OPS_SCENARIO_KEYS
        # Chuyên viên vận hành đọc tiếng Việt, không đọc TAKEOVER hay G5.
        assert scen["patternLabel"] not in {"SINGLE", "SERIES", "DRAIN", "TAKEOVER", "RECEIVER"}
        assert scen["groupLabel"] not in {"G1", "G2", "G3", "G4", "G5"}
        assert scen["actionLabel"] not in {"cancel", "hold", "contact", "continue"}


def test_ops_scenarios_takeover_khong_duoc_hoi_khach():
    body = client.get("/api/ops/scenarios").json()
    takeover = [s for s in body if "Chiếm quyền" in s["patternLabel"]]
    assert takeover, "playbook dự phòng phải có kịch bản chiếm quyền thiết bị"
    # Kẻ gian đang nhìn thấy màn hình khách: hỏi khách là tự lộ.
    assert all(s["canAsk"] is False for s in takeover)


def test_ops_model_khop_nguong_cua_engine():
    body = client.get("/api/ops/model").json()
    assert set(body) == OPS_MODEL_KEYS
    # Hai ngưỡng này là hằng số của risk-scoring-service (LEVEL_SOFT_WARN,
    # LEVEL_INTERVENE); màn case hiển thị đúng cặp số đó.
    assert body["softWarnMin"] == 40
    assert body["interveneMin"] == 75
    assert body["softWarnMin"] < body["interveneMin"]
    for f in body["factors"]:
        assert set(f) == {"key", "label", "maxScore"}
        assert f["label"] != f["key"]


def test_ops_audit_dung_hop_dong():
    body = client.get("/api/ops/audit").json()
    assert set(body) == OPS_AUDIT_KEYS
    assert 0 <= body["fallbackRatePct"] <= 100
    for stat in body["perAgent"]:
        assert set(stat) == {"agentKey", "agentLabel", "calls", "fallbackCalls", "avgLatencyMs"}
        assert stat["fallbackCalls"] <= stat["calls"]
    for trace in body["traces"]:
        assert set(trace) <= {"id", "time", "agentLabel", "model", "status",
                              "statusLabel", "latencyMs", "decisionId",
                              "customerId", "customerLabel"}
        assert trace["status"] in {"ok", "cache", "timeout", "error"}
        if "customerId" in trace:
            assert isinstance(trace["customerId"], int) and trace["customerId"] > 0


def test_nhat_ky_ai_loc_duoc_ca_khi_dung_du_lieu_du_phong():
    """Bộ lọc phải có tác dụng ở cả hai nguồn dữ liệu.

    Test chạy với DOMAIN_ENABLED=false nên đây chính là nhánh dự phòng: nếu bộ
    lọc chỉ hoạt động khi có database, người trình bày sẽ tưởng màn hình hỏng.
    """
    body = client.get("/api/ops/audit?status=timeout").json()
    assert len(body["traces"]) > 0
    assert all(t["status"] == "timeout" for t in body["traces"])
    # Ba con số là của toàn bộ nhật ký, không đổi theo bộ lọc.
    assert body["totalCalls"] == client.get("/api/ops/audit").json()["totalCalls"]


def test_o_chon_khach_va_moc_thoi_gian_khong_doi_theo_bo_loc():
    """Danh sách khách và mốc mới nhất là của toàn bộ nhật ký.

    Nếu chúng co lại theo bộ lọc thì lọc xong khách A sẽ không còn cách nào chọn
    khách B, và khoảng "7 ngày" sẽ trôi mỗi lần bấm.
    """
    day_du = client.get("/api/ops/audit").json()
    da_loc = client.get("/api/ops/audit?customer_id=100002").json()
    assert day_du["customers"] == da_loc["customers"]
    assert day_du["latestTraceAt"] == da_loc["latestTraceAt"]
    for c in day_du["customers"]:
        assert set(c) <= {"id", "label", "calls"}
        assert c["calls"] > 0


def test_loc_theo_tro_ly_dung_khoa_chu_khong_dung_nhan():
    """Endpoint nhận khoá agent, nên facet phải phát ra chính khoá đó.

    Nếu màn hình phải đoán khoá từ nhãn tiếng Việt thì đổi nhãn một chữ là bộ lọc
    chết, và không ai biết cho tới lúc trình bày.
    """
    body = client.get("/api/ops/audit").json()
    keys = {a["agentKey"] for a in body["perAgent"]}
    assert keys and all(k and " " not in k for k in keys)

    khoa = sorted(keys)[0]
    nhan = next(a["agentLabel"] for a in body["perAgent"] if a["agentKey"] == khoa)
    loc = client.get(f"/api/ops/audit?agent={khoa}").json()["traces"]
    assert loc and all(t["agentLabel"] == nhan for t in loc)


def test_loc_nhat_ky_theo_khach_va_khoang_nua_mo():
    """Khoảng [since, until): bản ghi đúng mốc `until` bị loại, nếu không một
    lượt gọi sẽ được đếm ở cả hai khoảng liền nhau."""
    theo_khach = client.get("/api/ops/audit?customer_id=100002").json()["traces"]
    assert theo_khach and all(t["customerId"] == 100002 for t in theo_khach)

    tu_0930 = client.get("/api/ops/audit?since=2026-09-15T09:30:00%2B07:00").json()["traces"]
    assert {t["id"] for t in tu_0930} == {"1042", "1041"}

    truoc_093850 = client.get("/api/ops/audit?until=2026-09-15T09:38:50%2B07:00").json()["traces"]
    assert {t["id"] for t in truoc_093850} == {"1040"}


def test_nhat_ky_ai_noi_duoc_luot_goi_thuoc_ve_khach_nao():
    """Nhật ký không có cột khách hàng thì chuyên viên không lần được lượt gọi
    này phục vụ ai — kể cả ở nhánh dự phòng, nơi buổi trình bày hay rơi vào."""
    traces = client.get("/api/ops/audit").json()["traces"]
    assert any(t.get("customerId") for t in traces)
    for t in traces:
        # Có mã khách thì phải có tên đã che đi kèm, nếu không cột hiện mỗi con số.
        if t.get("customerId"):
            assert t.get("customerLabel")


def test_mapper_nhat_ky_giu_ma_khach_khi_thieu_bang_ten():
    """Thiếu bảng tên che chỉ được mất phần tên, không được mất luôn mã khách:
    mã khách chính là thứ nối bản ghi này với hồ sơ khách hàng."""
    import mappers

    rows = [{"trace_id": 7, "agent": "copilot", "model": "m", "status": "ok",
             "customer_id": 100008, "created_at": "2026-09-15T09:41:04+07:00"}]
    stats = {"total_calls": 1, "breakdown": [{"agent": "copilot", "status": "ok",
                                              "n": 1, "avg_latency_ms": 100}]}
    khong_ten = mappers.map_ops_audit(rows, stats)
    assert khong_ten.traces[0].customer_id == 100008
    assert khong_ten.traces[0].customer_label is None

    co_ten = mappers.map_ops_audit(rows, stats, {100008: "NGUYEN THI B***"})
    assert co_ten.traces[0].customer_label == "NGUYEN THI B***"


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


def test_home_khong_do_vi_insight_chua_co_chu():
    """Dòng insight sinh trước khi transaction-service biết tự viết câu có
    insight_text = NULL. Lấy nguyên nó thì HomeContent (assistant_hint: str)
    ném ValidationError và /api/home trả 500 — FE nuốt lỗi rồi hiện dữ liệu
    demo nên màn Home vẫn trông đẹp, không ai biết tên khách là đồ giả."""
    from datetime import datetime
    import mappers
    cust = {"name_masked": "Nguyễn Minh A***", "persona": "SALARY"}
    ins = {"insights": [
        {"period": "202609", "rank_in_period": 1, "insight_text": None},
        {"period": "202609", "rank_in_period": 2, "insight_text": None},
        {"period": "202608", "rank_in_period": 1, "insight_text": "Nhóm Ăn uống chiếm 34% tổng chi."},
    ]}
    out = mappers.map_home(cust, ins, datetime(2026, 9, 19, 9, 0))
    # Bỏ qua kỳ mới nhất vì nó chưa có chữ, lấy câu thật gần nhất.
    assert out.assistant_hint == "Nhóm Ăn uống chiếm 34% tổng chi."


def test_home_khong_co_insight_nao_co_chu_thi_dung_cau_mac_dinh():
    from datetime import datetime
    import mappers
    out = mappers.map_home(
        {"name_masked": "A***", "persona": "SALARY"},
        {"insights": [{"period": "202609", "rank_in_period": 1, "insight_text": None}]},
        datetime(2026, 9, 19, 9, 0),
    )
    assert out.assistant_hint == "Xem phân tích chi tiêu tháng này của bạn."


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


# ---- POST /api/ops/login ------------------------------------------------------
#
# Khác /api/auth/login: không có nhánh "degraded" (identity-service chết thì
# không ai vào Ops được), và còn phải gate bằng quyền hiệu lực chứ không chỉ
# xác thực đúng mật khẩu — xem domain.user_permissions.

_ADMIN_USER = {
    "user_id": 1, "username": "0901000001", "role": "ADMIN",
    "customer_id": None, "full_name_masked": None,
    "email_masked": None, "phone_masked": None, "user_status": "ACTIVE",
}
_CUSTOMER_USER = {
    "user_id": 13, "username": "kh100008", "role": "CUSTOMER",
    "customer_id": 100008, "full_name_masked": "NGUYEN VAN M***",
    "email_masked": None, "phone_masked": None, "user_status": "ACTIVE",
}


def test_ops_login_tai_khoan_khach_bi_tu_choi_role_chua_khai(monkeypatch):
    # app_role chưa khai vai trò CUSTOMER: role_defined=False.
    async def fake_verify(username, password):
        return {"authenticated": True, "user": _CUSTOMER_USER}

    async def fake_permissions(user_id):
        return {"user_id": user_id, "role": "CUSTOMER", "role_defined": False,
                "effective_permissions": [], "note": "chưa khai"}

    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    monkeypatch.setattr(main.domain, "user_permissions", fake_permissions)
    r = client.post("/api/ops/login", json={"username": "kh100008", "password": "123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["reason"] == "not_backoffice"
    assert "operator" not in body


def test_ops_login_tai_khoan_dung_mat_khau_nhung_thieu_quyen_ops_bi_tu_choi(monkeypatch):
    # Vai trò đã khai (role_defined=True) nhưng không có quyền ops.dashboard.read.
    async def fake_verify(username, password):
        return {"authenticated": True, "user": _CUSTOMER_USER}

    async def fake_permissions(user_id):
        return {"user_id": user_id, "role": "CUSTOMER", "role_defined": True,
                "role_status": "ACTIVE", "user_status": "ACTIVE",
                "effective_permissions": ["app.transfer.create"],
                "declared_permissions": ["app.transfer.create"]}

    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    monkeypatch.setattr(main.domain, "user_permissions", fake_permissions)
    r = client.post("/api/ops/login", json={"username": "kh100008", "password": "123456"})
    body = r.json()
    assert body["authenticated"] is False
    assert body["reason"] == "not_backoffice"


def test_ops_login_tai_khoan_noi_bo_du_quyen_duoc_nhan(monkeypatch):
    async def fake_verify(username, password):
        return {"authenticated": True, "user": _ADMIN_USER}

    async def fake_permissions(user_id):
        assert user_id == 1
        return {"user_id": user_id, "role": "ADMIN", "role_defined": True,
                "role_status": "ACTIVE", "user_status": "ACTIVE",
                "effective_permissions": ["ops.dashboard.read", "ops.alerts.read",
                                          "ops.alerts.decide"],
                "declared_permissions": ["ops.dashboard.read", "ops.alerts.read",
                                         "ops.alerts.decide"]}

    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    monkeypatch.setattr(main.domain, "user_permissions", fake_permissions)
    r = client.post("/api/ops/login", json={"username": "0901000001", "password": "đúng"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["operator"]["name"] == "0901000001"  # không có full_name → rơi về username
    assert body["operator"]["role"] == "Fraud Ops"
    assert body["operator"]["initials"] == "09"
    assert body["operator"]["shift"] in {"Ca sáng", "Ca chiều", "Ca tối"}


def test_ops_login_sai_mat_khau_hoac_khoa_chuyen_tiep_ly_do_khong_goi_them_permissions(monkeypatch):
    async def fake_verify(username, password):
        return {"authenticated": False, "reason": "locked"}

    async def fail_permissions(user_id):
        raise AssertionError("không được gọi permissions khi chưa xác thực được")

    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    monkeypatch.setattr(main.domain, "user_permissions", fail_permissions)
    r = client.post("/api/ops/login", json={"username": "0901000005", "password": "sai"})
    body = r.json()
    assert body["authenticated"] is False
    assert body["reason"] == "locked"


def test_ops_login_identity_chet_luc_xac_thuc_tra_503_khong_co_nhanh_du_phong(monkeypatch):
    async def fake_verify(username, password):
        return None
    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    r = client.post("/api/ops/login", json={"username": "0901000001", "password": "x"})
    assert r.status_code == 503


def test_ops_login_identity_chet_luc_tra_quyen_tra_503(monkeypatch):
    async def fake_verify(username, password):
        return {"authenticated": True, "user": _ADMIN_USER}

    async def fake_permissions(user_id):
        return None

    monkeypatch.setattr(main.domain, "auth_verify", fake_verify)
    monkeypatch.setattr(main.domain, "user_permissions", fake_permissions)
    r = client.post("/api/ops/login", json={"username": "0901000001", "password": "x"})
    assert r.status_code == 503


# ---- GET /api/ops/session?username= -------------------------------------------

def test_ops_session_khong_kem_username_giu_hang_so_catalog(monkeypatch):
    monkeypatch.setattr(main.domain, "DOMAIN_ENABLED", True)

    async def health(base):
        return True
    monkeypatch.setattr(main.domain, "health", health)
    body = client.get("/api/ops/session").json()
    assert body["operator"]["name"] == catalog.OPS_SESSION.operator.name


def test_ops_session_kem_username_tra_chuyen_vien_that(monkeypatch):
    monkeypatch.setattr(main.domain, "DOMAIN_ENABLED", True)

    async def health(base):
        return True

    async def user_by_username(username):
        assert username == "0901000002"
        return {"user_id": 2, "username": "0901000002", "role": "ADMIN",
                "customer_id": None, "full_name_masked": "TRAN THI H***",
                "email_masked": None, "phone_masked": None, "user_status": "ACTIVE"}

    monkeypatch.setattr(main.domain, "health", health)
    monkeypatch.setattr(main.domain, "user_by_username", user_by_username)
    body = client.get("/api/ops/session?username=0901000002").json()
    assert body["operator"]["name"] == "TRAN THI H***"
    assert body["operator"]["initials"] == "TH"
    assert body["operator"]["role"] == "Fraud Ops"


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
    # Màn Guardian in chữ thô nên gạch đầu dòng phải thành dấu tròn đọc được ngay.
    assert "• Y tế giảm gần 50%." in out
    assert ">" not in main._strip_markdown_for_plain("> 📊 Thu nhập: 1 ₫\nchữ")


def test_strip_markdown_van_ban_thuan_giu_nguyen():
    plain = "Tháng 9 bạn chi 2.465.000 ₫, giảm 62% so tháng 8.\nĂn uống giảm mạnh."
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


def test_precheck_tra_ve_muc_guardian_va_khong_goi_llm():
    """Wireframe: bước này phải dưới 300 ms nên KHÔNG được gọi agent.

    stub: engine tắt → không có điểm; stk lạ thì vẫn phải quyết được là
    soft_warn chứ không chặn giao dịch vì Guardian im lặng.
    """
    r = client.post("/api/transfer/precheck",
                    json={"bankCode": "VPB", "accountNo": "1902664130", "amount": 85_000_000, "note": "gap"})
    assert r.status_code == 200
    b = r.json()
    assert b["trusted"] is False
    assert b["level"] in {"pass", "soft_warn", "intervene"}
    # Chỉ mức intervene mới chèn màn Guardian; soft_warn ở lại màn nhập lệnh.
    assert b["requiresReview"] is (b["level"] == "intervene")
    assert set(b) >= {"score", "level", "topFactors", "templateText", "decisionId", "txCount"}
    # Không còn verdict của agent trong bước này (đã chuyển sang lượt 2).
    assert "verdict" not in b


def test_precheck_engine_chet_thi_khong_chan_chuyen_tien():
    """"Precheck lỗi backend → fallback luồng cũ, không được chặn chuyển tiền
    vì Guardian lỗi" — đúng dòng cuối bảng tình huống trong wireframe."""
    r = client.post("/api/transfer/precheck",
                    json={"bankCode": "VPB", "accountNo": "1902664130", "amount": 85_000_000, "note": "gap"})
    b = r.json()
    assert b["level"] != "intervene", "engine im lặng thì không được tự dựng màn chặn"


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


def test_agent_configured_khong_muon_agent_khac_khi_id_rong():
    """Hỏi đích danh một agent chưa cấu hình phải ra False.

    Trả True ở đây là cách lỗi tốn một buổi debug: câu hỏi chống lừa đảo lặng
    lẽ chạy vào agent Copilot (11 công cụ, 15-20s), quá trần chờ nên lượt nào
    cũng rơi về playbook — nhìn như agent trả lời kém chứ không như gọi nhầm.
    """
    import domain
    cu = (domain.AGENT_SERVICE_URL, domain.AGENT_API_KEY, domain.AGENT_ID)
    domain.AGENT_SERVICE_URL, domain.AGENT_API_KEY, domain.AGENT_ID = "http://x", "k", "agent-copilot"
    try:
        assert domain.agent_configured() is True          # không truyền → xét AGENT_ID
        assert domain.agent_configured("") is False       # hỏi đích danh, id rỗng
        assert domain.agent_configured(None) is False
        assert domain.agent_configured("agent-shield") is True
    finally:
        domain.AGENT_SERVICE_URL, domain.AGENT_API_KEY, domain.AGENT_ID = cu


def test_scamshield_verdict_khong_cau_hinh_agent_van_tra_ket_luan():
    """Endpoint không bao giờ chặn khách: agent chưa cấu hình → verdict tín hiệu."""
    r = client.post("/api/scamshield/verdict", json={
        "bankCode": "ACB", "accountNo": "5270384262",
        "amount": 85_000_000, "note": "chuyen gap theo huong dan cong an"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"level", "title", "summary", "reasons", "recommendation", "source", "steps"}
    assert body["steps"] == []  # nhánh dự phòng không chạy bước nào nên không kể bước nào
    assert body["level"] in {"safe", "suspect", "danger"}
    assert body["source"] == "fallback"


def test_parse_verdict_doc_duoc_dung_cau_tra_loi_that_cua_agent():
    """Chuỗi dưới đây chép nguyên từ một lượt chạy thật của agent MSB Scam Shield."""
    that = ("NGUY_HIEM\n"
            "- Người nhận là tài khoản mới, quan hệ không xác định và đã bị hệ thống cảnh báo SUSPECTED.\n"
            "- Nội dung chuyển khoản chứa từ khóa \"công an\", khớp mô típ giả danh cơ quan chức năng.\n"
            "Khuyến nghị: Ngừng giao dịch ngay, gọi cho người thân hoặc công an địa phương để xác minh.")
    v = main._parse_verdict(that)
    assert v is not None
    assert v.level == "danger" and v.source == "agent"
    assert len(v.reasons) == 2
    assert v.recommendation.startswith("Khuyến nghị:")
    # Dòng tag mức độ không được lọt vào phần khách đọc.
    assert "NGUY_HIEM" not in v.summary and all("NGUY_HIEM" not in r for r in v.reasons)


def test_parse_verdict_tu_choi_doan_van_cua_luot_2():
    """Lượt 2 màn Guardian trả đoạn văn thuần; ép nó thành verdict là sai."""
    doan_van = ("Bạn ơi, vì đang có người hướng dẫn nên hãy bình tĩnh ngắt cuộc gọi ngay. "
                "Công an không bao giờ yêu cầu chuyển tiền để chứng minh trong sạch.")
    assert main._parse_verdict(doan_van) is None


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


# ---- Tư vấn gói sản phẩm: vay / gửi tiết kiệm --------------------------------

@pytest.mark.parametrize("cau, mong_doi", [
    ("vay 500 triệu trong 5 năm", 60),
    ("gửi tiết kiệm 100 triệu 12 tháng", 12),
    ("vay mua nhà 2 tỷ kỳ hạn 20 năm", 240),
    ("chi tiêu tháng 6 thế nào", None),      # "tháng 6" là mốc, không phải kỳ hạn
    ("vay 300 triệu", None),                  # chưa nói kỳ hạn
])
def test_doc_ky_han(cau, mong_doi):
    assert main._goal_months(cau) == mong_doi


def test_ky_han_qua_dai_bi_bo_qua():
    assert main._goal_months("gửi 50 năm") is None  # > 360 tháng


_LOAN_RAW = {
    "amount": 500_000_000, "months": 60, "as_of": "2026-09-17",
    "options": [
        {"product_id": "RLNNNGHIEP", "product_name": "RB-Cho vay nong nghiep",
         "term_code": "5Y", "term_label": "5 năm", "rate_pct": 9.4,
         "monthly_payment": 10_476_513, "total_payment": 628_590_772, "total_interest": 128_590_772},
        {"product_id": "RLNOTO", "product_name": "RB-Cho vay mua o to",
         "term_code": "5Y", "term_label": "5 năm", "rate_pct": 9.7,
         "monthly_payment": 10_549_867, "total_payment": 632_992_010, "total_interest": 132_992_010},
    ],
}


def test_bang_goi_vay_giu_nguyen_so_cua_domain():
    g = main._loan_grid(_LOAN_RAW)
    assert g.title == "Vay 500 triệu trong 60 tháng"
    assert [c.label for c in g.columns] == ["Gói vay", "Lãi suất", "Trả/tháng", "Tổng lãi"]
    assert [c.align for c in g.columns] == ["left", "right", "right", "right"]
    # Lãi suất phải giữ phần thập phân: 9,4% khác hẳn 9%.
    assert g.rows[0] == ["RB-Cho vay nong nghiep", "9,4%", "10.476.513 ₫", "128.590.772 ₫"]


def test_bang_goi_tiet_kiem():
    raw = {"amount": 100_000_000, "months": 12, "as_of": "2026-09-17", "options": [
        {"product_id": "RB.TK.LSCN", "product_name": "TIET KIEM LAI SUAT CAO NHAT",
         "term_code": "12M", "term_label": "12 tháng", "rate_pct": 5.9,
         "interest_amount": 5_900_000, "maturity_amount": 105_900_000}]}
    g = main._savings_grid(raw)
    assert g.title == "Gửi 100 triệu trong 12 tháng"
    assert g.rows[0] == ["TIET KIEM LAI SUAT CAO NHAT", "5,9%", "5.900.000 ₫", "105.900.000 ₫"]


def test_bieu_lai_suat_thanh_bang_ky_han_x_san_pham():
    raw = {
        "products": [{"product_id": "A", "product_name": "Gói A"},
                     {"product_id": "B", "product_name": "Gói B"}],
        "terms": [{"term_code": "6M", "term_label": "6 tháng", "term_months": 6.0, "seq": 1},
                  {"term_code": "12M", "term_label": "12 tháng", "term_months": 12.0, "seq": 2}],
        "rates": [
            {"product_id": "A", "term_code": "6M", "rate_pct": 4.5},
            {"product_id": "B", "term_code": "6M", "rate_pct": 5.0},
            {"product_id": "A", "term_code": "12M", "rate_pct": 5.2},
        ],
    }
    g = main._rate_grid(raw, "Lãi suất tiết kiệm theo kỳ hạn")
    assert [c.label for c in g.columns] == ["Kỳ hạn", "Gói A", "Gói B"]
    assert g.rows[0] == ["6 tháng", "4,5%", "5,0%"]
    # Ô thiếu phải là "—", không được để trống hay tụt cột.
    assert g.rows[1] == ["12 tháng", "5,2%", "—"]


def test_bieu_lai_rong_thi_khong_dung_bang():
    assert main._rate_grid({"products": [], "terms": [], "rates": []}, "x") is None


def test_cau_hoi_khong_lien_quan_san_pham_thi_khong_co_bang():
    import asyncio
    assert asyncio.run(main._product_visual("chi tiêu tháng này thế nào")) is None


def test_cau_di_vay_khong_gan_bang_lo_trinh_tiet_kiem():
    """"vay 500 triệu mua ô tô" khớp cả "mua ô tô" lẫn số tiền nên trước đây rơi
    vào bảng lộ trình TIẾT KIỆM — khuyên ngược hẳn điều khách hỏi."""
    import asyncio
    t, _, lo_trinh = asyncio.run(main._spending_visual("tôi muốn vay 500 triệu mua ô tô trong 5 năm"))
    assert t is None, "câu đi vay không được gắn bảng tích lũy"
    assert lo_trinh is False


def test_cau_gui_goi_tiet_kiem_khong_gan_bang_lo_trinh():
    import asyncio
    t, _, lo_trinh = asyncio.run(main._spending_visual("gửi tiết kiệm 100 triệu trong 12 tháng thì gói nào lợi"))
    assert t is None and lo_trinh is False


def test_ke_hoach_tich_luy_van_ra_bang_lo_trinh():
    """Chốt chặn hai chiều: câu tích lũy thật vẫn phải ra bảng lộ trình."""
    import asyncio
    t, _, lo_trinh = asyncio.run(main._spending_visual("kế hoạch tiết kiệm mua ô tô 500 triệu"))
    assert t is not None and "Lộ trình" in t.title
    assert lo_trinh is True, "phải báo đây là nhánh lộ trình để stream biết mà nhường trợ lý"


# ---- Guardian 3 mức (wireframe màn Transfer) ---------------------------------

def test_ly_do_lay_nguyen_detail_cua_engine():
    """Ba lý do hiện cho khách phải là chữ của engine, không phải chữ gateway
    bịa — nếu không, lý do sẽ không khớp điểm đã chấm."""
    raw = {
        "top_factors": ["amount_deviation", "recent_context", "new_beneficiary", "time_of_day"],
        "factors": {
            "amount_deviation": {"score": 25, "detail": "gấp 17 lần mức thường chuyển"},
            "recent_context": {"score": 20, "detail": "ngay sau khi tất toán tiết kiệm"},
            "new_beneficiary": {"score": 20, "detail": "người nhận hoàn toàn mới"},
            "time_of_day": {"score": 10, "detail": "ngoài giờ"},
        },
    }
    ly_do = main._factor_reasons(raw)
    assert len(ly_do) == 3, "wireframe chỉ có chỗ cho ba lý do"
    assert ly_do[0] == "Số tiền: gấp 17 lần mức thường chuyển"
    assert ly_do[2] == "Người nhận: người nhận hoàn toàn mới"


def test_yeu_to_thieu_detail_thi_bo_qua():
    raw = {"top_factors": ["amount_deviation", "behavior_drift"],
           "factors": {"amount_deviation": {"detail": "x"}, "behavior_drift": {}}}
    assert main._factor_reasons(raw) == ["Số tiền: x"]


def test_bon_hanh_dong_guardian_dung_tu_vung_engine():
    keys = [k for k, _ in main._GUARDIAN_ACTIONS]
    assert keys == ["hold", "cancel", "contact", "continue"]
    # Mọi hành động FE gửi lên phải ánh xạ được xuống động từ của engine.
    for act in ("cancelled", "proceeded", "reported", "held", "contacted"):
        assert act in main.DOMAIN_ACTIONS and act in catalog.CUSTOMER_ACTIONS
    assert main.DOMAIN_ACTIONS["held"][0] == "hold"
    assert main.DOMAIN_ACTIONS["contacted"][0] == "contact"


def test_hanh_dong_khoa_tam_tra_thong_diep_cho_khach():
    r = client.post("/api/transfer/action", json={"action": "held"})
    assert r.status_code == 200
    assert "24 giờ" in r.json()["message"]


def test_intervene_khong_co_quyet_dinh_thi_404():
    r = client.get("/api/transfer/intervene/khong-ton-tai")
    assert r.status_code == 404
    r2 = client.post("/api/transfer/intervene",
                     json={"decisionId": "khong-ton-tai", "selectedOption": "Không, tôi tự chuyển"})
    assert r2.status_code == 404


def test_top_factors_dang_chuoi_van_doc_duoc():
    """Engine trả list, cột risk_decision lưu chuỗi ngăn phẩy. Duyệt thẳng chuỗi
    sẽ ra từng ký tự và màn Guardian mở bằng deep-link sẽ trắng phần lý do."""
    raw = {
        "top_factors": "amount_deviation,new_beneficiary",
        "factors": {"amount_deviation": {"detail": "gấp 17 lần"},
                    "new_beneficiary": {"detail": "chưa từng chuyển"}},
    }
    assert main._factor_reasons(raw) == ["Số tiền: gấp 17 lần", "Người nhận: chưa từng chuyển"]


def test_top_factors_none_thi_khong_ra_ly_do_rac():
    assert main._factor_reasons({"top_factors": "none", "factors": {}}) == []


# ---- Đường đi dữ liệu thật của màn Ops ----------------------------------------
#
# Bộ test chạy với DOMAIN_ENABLED=false nên mặc định chỉ đi nhánh dự phòng. Các
# test dưới đây giả lập 5 service để đi đúng nhánh đọc database — nơi ba endpoint
# này từng trả hằng số cho mọi case.

import mappers

DECISION_ID = "cab99d05-c621-4552-b57c-f644ba1f67de"

_DECISION = {
    "decision_id": DECISION_ID, "customer_id": 100008,
    "tx_snapshot": {"amount": 95_000_000, "memo_masked": "Nop tien xac minh",
                    "session_flags": {"new_device": True}},
    "score": 87, "level": "intervene", "scenario_id": "S05",
    "created_at": "2026-09-15T09:41:02+07:00",
    "intervened_at": "2026-09-15T09:41:03+07:00", "actioned_at": None,
}
_CASE = {"case_id": "CASE-2026-0007", "decision_id": DECISION_ID, "customer_id": 100008,
         "status": "OPEN", "narrative": "x", "created_at": "2026-09-15T09:41:20+07:00",
         "closed_at": None}


@pytest.fixture
def domain_that(monkeypatch):
    """Năm service trả dữ liệu thật; ghi lại các lời gọi ghi để test soi."""
    written: dict = {}

    async def risk_decision(decision_id):
        return _DECISION if decision_id == DECISION_ID else None

    async def cases(limit=50):
        return {"cases": [_CASE]}

    async def customer(customer_id):
        return {"customer_id": customer_id, "name_masked": "NGUYEN THI B***", "persona": "SENIOR"}

    async def risk_info():
        return {"levels": {"pass": "< 40", "soft_warn": "40 - 74", "intervene": ">= 75"},
                "factor_caps": {"amount_deviation": 25}}

    async def customer_decisions(customer_id, limit=50):
        return {"decisions": [_DECISION]}

    async def llm_traces(agent=None, status=None, decision_id=None, limit=50):
        return {"traces": [{"trace_id": 1, "agent": "shield_explain", "status": "ok",
                            "latency_ms": 1080, "created_at": "2026-09-15T09:41:04+07:00"}]}

    async def transfer_action(payload):
        written["action"] = payload
        return {"ok": True}

    async def update_case(case_id, status, note=None):
        written["case"] = {"case_id": case_id, "status": status, "note": note}
        return {"case": {}}

    monkeypatch.setattr(main.domain, "DOMAIN_ENABLED", True)
    for name, fn in [("risk_decision", risk_decision), ("cases", cases), ("customer", customer),
                     ("risk_info", risk_info), ("customer_decisions", customer_decisions),
                     ("llm_traces", llm_traces), ("transfer_action", transfer_action),
                     ("update_case", update_case)]:
        monkeypatch.setattr(main.domain, name, fn)
    return written


def test_chi_tiet_case_doc_duoc_bang_id_that(domain_that):
    # Trước đây id dạng UUID luôn 404 vì chỉ tra trong dữ liệu tạm.
    r = client.get(f"/api/ops/alerts/{DECISION_ID}/detail")
    assert r.status_code == 200
    body = r.json()
    assert "thiết bị mới" in body["transaction"]["channel"]
    assert body["transaction"]["content"] == "Nop tien xac minh"
    assert body["customerProfile"]["segment"] == "Cao tuổi"
    assert body["model"]["interveneThreshold"] == 75


def test_dong_thoi_gian_doc_duoc_bang_id_that(domain_that):
    body = client.get(f"/api/ops/alerts/{DECISION_ID}/timeline").json()
    labels = [s["label"] for s in body]
    assert any("Mở case CASE-2026-0007" in l for l in labels)
    assert any("phản hồi" in l for l in labels)
    assert body[-1]["done"] is False


def test_quyet_dinh_duoc_ghi_xuong_ca_quyet_dinh_lan_case(domain_that):
    r = client.post(f"/api/ops/alerts/{DECISION_ID}/decision",
                    json={"decision": "confirmed", "note": "đã gọi khách xác minh"})
    assert r.status_code == 200
    # Ghi vào risk_decision để vòng đời quyết định khép lại...
    assert domain_that["action"]["action_taken"] == "cancel"
    # ...và đóng case, việc này khiến action-feedback sinh feedback nguồn ops.
    assert domain_that["case"] == {"case_id": "CASE-2026-0007", "status": "CLOSED_FRAUD",
                                   "note": "đã gọi khách xác minh"}


def test_phien_lam_viec_bao_dung_service_nao_chet(domain_that, monkeypatch):
    async def health(base):
        return "risk-scoring" in base
    monkeypatch.setattr(main.domain, "health", health)
    body = client.get("/api/ops/session").json()
    tones = {row["label"]: row["tone"] for row in body["systemStatus"]}
    assert tones["Risk Engine"] == "ok"
    assert tones["Tri thức lừa đảo"] == "danger"
    # Nhãn giờ không còn là chuỗi cứng "Thứ Ba, 15/09/2026 · 09:41".
    assert body["nowLabel"] != catalog.OPS_SESSION.now_label


# ---- Chat phát thẳng từ agent -------------------------------------------------
#
# Trước đây gateway chờ trọn lần xử lý `mode=sync` rồi mới cắt câu trả lời thành
# token phát lại, nên khách nhìn màn hình trắng suốt thời gian mô hình chạy.

def _gia_lap_agent_stream(monkeypatch, pieces, busy=False):
    """Thay domain.agent_events bằng một dòng chảy dựng sẵn, ghi lại tham số.

    Phần tử là chuỗi thì phát ra chữ; là AgentStep thì phát ra bước dùng công cụ.
    """
    ghi_nhan: dict = {}

    async def fake(question, agent_id=None, kind="copilot", customer_id=None,
                   session_key=None, decision_id=None):
        ghi_nhan.update(question=question, kind=kind, session_key=session_key,
                        customer_id=customer_id)
        if busy:
            raise main.domain.AgentBusy()
        for piece in pieces:
            yield ("step", piece) if isinstance(piece, main.domain.AgentStep) else ("text", piece)

    monkeypatch.setattr(main.domain, "agent_events", fake)
    return ghi_nhan


def test_chat_phat_thang_tung_mau_cua_agent(monkeypatch):
    ghi_nhan = _gia_lap_agent_stream(monkeypatch, ["Tháng này ", "bạn chi ", "12.460.000 ₫."])
    r = client.post("/api/copilot/chat", json={"message": "Tháng này tôi tiêu vào đâu?"})
    tokens, thay_done, _ = _doc_token(r.text)
    assert thay_done
    # Mỗi mẩu của agent ra một sự kiện riêng: chữ tới trình duyệt dần dần chứ
    # không dồn một cục sau khi mô hình chạy xong.
    assert "".join(tokens) == "Tháng này bạn chi 12.460.000 ₫."
    assert len(tokens) == 3


def test_chat_giu_markdown_nhung_bo_dong_bang(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, ["Bạn chi **12.4", "60.000 ₫** tháng này.\n", "| Nhóm | Tiền |\n", "Hết."])
    r = client.post("/api/copilot/chat", json={"message": "chi tiêu"})
    tokens, _, _ = _doc_token(r.text)
    noi_dung = "".join(tokens)
    # FE render markdown thật, nên **đậm** phải tới nơi nguyên vẹn để render.
    # Riêng dòng bảng vẫn bị bỏ: gateway đã đính bảng số liệu riêng của mình.
    assert "**12.460.000 ₫**" in noi_dung
    assert "|" not in noi_dung
    assert noi_dung.rstrip().endswith("Hết.")


def test_chat_gui_kem_khoa_hoi_thoai_rieng_tung_khach(monkeypatch):
    """Khoá hội thoại phải mang mã khách đang đăng nhập.

    Dùng chung một khoá thì mọi khách ghi vào MỘT hội thoại bên nền tảng: khách
    sau đọc được số dư và bảng chi tiêu của khách trước ngay trong ngữ cảnh mô
    hình. Thiếu khoá hẳn thì ngược lại — mỗi câu hỏi mở một hội thoại mới và trợ
    lý không nhớ gì.
    """
    monkeypatch.setattr(main.domain, "current_customer_id", lambda: 100001)
    ghi_nhan = _gia_lap_agent_stream(monkeypatch, ["xong"])
    client.post("/api/copilot/chat", json={"message": "còn tháng trước thì sao?"})
    assert ghi_nhan["session_key"] == "copilot-100001"
    assert ghi_nhan["customer_id"] == 100001
    assert ghi_nhan["kind"] == "copilot"


def test_hai_khach_khong_dung_chung_khoa_hoi_thoai():
    assert main.domain.copilot_session_key(100001) != main.domain.copilot_session_key(100008)


def test_chat_hoi_don_thi_noi_thanh_loi(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, [], busy=True)
    r = client.post("/api/copilot/chat", json={"message": "câu thứ hai"})
    tokens, thay_done, _ = _doc_token(r.text)
    noi_dung = "".join(tokens)
    # 409 run_in_progress không được nuốt thành im lặng rơi về kịch bản.
    assert "đợi một chút" in noi_dung
    assert thay_done


def test_chat_agent_khong_phat_gi_thi_roi_ve_kich_ban(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, [])
    r = client.post("/api/copilot/chat", json={"message": "thời tiết hôm nay thế nào"})
    tokens, thay_done, _ = _doc_token(r.text)
    assert "".join(tokens) == catalog.FALLBACK_REPLY
    assert thay_done


def test_chat_van_dinh_bang_sau_phan_chu(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, ["Đây là chi tiêu của bạn."])
    r = client.post("/api/copilot/chat", json={"message": "Tổng hợp chi tiêu tháng này của tôi"})
    dong = [l for l in r.text.split("\n") if l.startswith("data:")]
    co_bang = [i for i, l in enumerate(dong) if '"table"' in l]
    co_token = [i for i, l in enumerate(dong) if '"token"' in l]
    if co_bang:
        # Thứ tự cũ phải giữ: hết chữ mới tới bảng, rồi mới tới [DONE].
        assert max(co_token) < min(co_bang)


# ---- Chat Banking: agent hiểu câu, gateway khớp danh bạ ----------------------

def test_khop_ten_theo_tu_khong_phai_chuoi_con():
    """"Khánh" phải khớp "NGUYEN VAN KHANH" nhưng KHÔNG khớp "KHANHLY" —
    khớp chuỗi con sẽ soạn lệnh cho nhầm người."""
    from models import TransferBeneficiary
    db = [
        TransferBeneficiary(id="1", name="NGUYEN VAN KHANH", bank="MSB", account="111", relationship="FRIEND", trusted=True),
        TransferBeneficiary(id="2", name="TRAN KHANHLY", bank="VCB", account="222", relationship="UNKNOWN", trusted=False),
    ]
    ra = main._khop_nguoi_nhan("anh Khánh", db)
    assert [b.id for b in ra] == ["1"]


def test_khop_theo_so_tai_khoan():
    from models import TransferBeneficiary
    db = [TransferBeneficiary(id="1", name="DO VAN DUC", bank="MSB", account="0362554873", relationship="FRIEND", trusted=True)]
    assert [b.id for b in main._khop_nguoi_nhan("0362554873", db)] == ["1"]
    assert main._khop_nguoi_nhan("0999999999", db) == []


def test_bo_xung_ho_va_dau():
    from models import TransferBeneficiary
    db = [TransferBeneficiary(id="1", name="LE THI HOA", bank="MSB", account="1", relationship="FAMILY", trusted=True)]
    for cach_goi in ("chị Hoa", "chi Hoa", "HOA", "le thi hoa"):
        assert main._khop_nguoi_nhan(cach_goi, db), f"trượt với: {cach_goi}"


def test_doc_json_agent_chiu_duoc_rac_quanh():
    """Mô hình hay bọc trong ```json hoặc thêm câu dẫn dù đã dặn không."""
    assert main._doc_json_agent('{"amount": 500000, "recipient": "Khanh", "intent": "transfer"}')["amount"] == 500000
    assert main._doc_json_agent('Đây là kết quả:\n```json\n{"intent": "other"}\n```')["intent"] == "other"
    assert main._doc_json_agent("không có json") is None
    assert main._doc_json_agent("") is None


def test_agent_chua_cau_hinh_thi_tra_fallback():
    """Không có agent → FE phải được báo để tự dùng regex, không được im lặng."""
    r = client.post("/api/chat-banking/parse", json={"message": "chuyen 500k cho anh Khanh"})
    assert r.status_code == 200
    b = r.json()
    assert b["source"] == "fallback" and b["intent"] == "other"


def test_so_tien_am_hoac_khong_phai_so_bi_bo():
    """Mô hình trả rác thì không được dựng thẻ soạn lệnh với số tiền vô nghĩa."""
    assert main._doc_json_agent('{"amount": -5, "recipient": null, "intent": "transfer"}')["amount"] == -5


@pytest.mark.parametrize("cau, mong_doi", [
    ("2 triệu rưỡi", 2_500_000),
    ("3 trieu ruoi", 3_500_000),
    ("20 triệu rưỡi", 20_500_000),
    ("2tr5", 2_500_000),
    ("1tr2", 1_200_000),
    ("500k", 500_000),
    ("1,5 triệu", 1_500_000),
    ("2 tỷ", 2_000_000_000),
    ("300 nghìn", 300_000),
    ("2 củ", 2_000_000),
    ("gửi 2 triệu rưỡi nhé", 2_500_000),
    ("không có số nào", None),
])
def test_doc_so_tien_noi_mieng(cau, mong_doi):
    """Số tiền phải do CODE tính. qwen3.6-flash từng trả "3 triệu rưỡi" =
    4.500.000 và "20 triệu rưỡi" = 30.000.000 — sai một chữ số ở ô số tiền là
    khách chuyển nhầm tiền thật."""
    assert main._tien_tu_chu(cau) == mong_doi


def test_don_vi_dai_phai_khop_truoc_don_vi_ngan():
    """"tr" đứng trước "triệu" trong regex sẽ khớp "tr" rồi bỏ lại "iệu rưỡi",
    làm mất phần "rưỡi" — lỗi này đã xảy ra thật."""
    assert main._tien_tu_chu("2 triệu rưỡi") == 2_500_000
    assert main._tien_tu_chu("2 trieu") == 2_000_000


def test_agent_hong_van_tra_so_tien_vi_code_tu_tinh():
    """Số tiền do code tính nên không phụ thuộc agent. Agent chết mà vẫn bỏ
    trống số tiền là vứt đi thông tin đã có sẵn trong tay."""
    r = client.post("/api/chat-banking/parse", json={"message": "chuyen 2 trieu ruoi cho anh Son"})
    b = r.json()
    assert b["source"] == "fallback"       # conftest tắt agent
    assert b["amount"] == 2_500_000


# ---- Bước dùng công cụ trên màn chat ------------------------------------------
#
# Khách hỏi xong nhìn ba chấm nhấp nháy cho tới khi câu trả lời hiện ra. Nền tảng
# thì biết trợ lý đang đọc dữ liệu gì; các test dưới đây ghim việc đưa điều đó ra
# tới trình duyệt, đúng lúc nó xảy ra chứ không phải sau khi xong.

def _doc_su_kien(body: str) -> list[tuple[str, dict | str]]:
    """Mọi sự kiện theo đúng thứ tự phát, để soi được thứ tự xen kẽ."""
    ra: list[tuple[str, dict | str]] = []
    for line in body.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            ra.append(("done", ""))
            continue
        parsed = json.loads(payload)
        for khoa in ("token", "step", "reasoning", "table", "grid", "chart"):
            if khoa in parsed:
                ra.append((khoa, parsed[khoa]))
                break
    return ra


def test_chat_phat_buoc_xen_giua_token(monkeypatch):
    buoc = main.domain.AgentStep("c1", "Đang tổng hợp thu chi theo tháng", "running")
    xong = main.domain.AgentStep("c1", "Đang tổng hợp thu chi theo tháng", "done", 312)
    _gia_lap_agent_stream(monkeypatch, [buoc, "Tháng này ", xong, "bạn chi 12 triệu."])
    # Câu trung tính: hỏi về chi tiêu sẽ kèm bảng + biểu đồ và làm loãng điều
    # test này muốn nói, là thứ tự xen kẽ giữa bước và chữ.
    r = client.post("/api/copilot/chat", json={"message": "giúp tôi với"})
    loai = [k for k, _ in _doc_su_kien(r.text)]
    # Bước ĐANG CHẠY phải ra trước chữ: giá trị của nó nằm ở lúc khách đang chờ.
    assert loai == ["step", "token", "step", "token", "done"]
    su_kien = dict(enumerate(_doc_su_kien(r.text)))
    dau = su_kien[0][1]
    assert dau["label"] == "Đang tổng hợp thu chi theo tháng"
    assert (dau["callId"], dau["status"]) == ("c1", "running")
    cuoi = su_kien[2][1]
    assert (cuoi["status"], cuoi["durationMs"]) == ("done", 312)


def test_chat_khong_co_buoc_thi_khung_stream_giu_nguyen(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, ["Xin chào."])
    r = client.post("/api/copilot/chat", json={"message": "chào"})
    assert [k for k, _ in _doc_su_kien(r.text)] == ["token", "done"]


def test_chat_ban_thi_khong_phat_buoc_nao(monkeypatch):
    _gia_lap_agent_stream(monkeypatch, [], busy=True)
    r = client.post("/api/copilot/chat", json={"message": "chào"})
    loai = {k for k, _ in _doc_su_kien(r.text)}
    # Câu trả lời "đợi một chút" là của gateway, không phải của agent; gắn bước
    # vào đó là kể chuyện không có thật.
    assert "step" not in loai


# ---- Bước dùng công cụ ở ba luồng đồng bộ -------------------------------------
#
# Scam Shield, Chat Banking và khuyến cáo Guardian không phát SSE: chúng gọi agent
# rồi trả về một response. Bước đi kèm trong response để màn hình kể lại được trợ
# lý đã tra những gì trước khi kết luận.

def _gia_lap_agent_dong_bo(monkeypatch, answer, steps):
    """Thay domain.agent_answer_with_steps, trả sẵn (câu trả lời, các bước)."""
    async def fake(question, agent_id=None, kind="copilot", customer_id=None,
                   decision_id=None, session_key=None):
        return answer, steps

    monkeypatch.setattr(main.domain, "agent_answer_with_steps", fake)
    monkeypatch.setattr(main.domain, "agent_configured", lambda *a, **k: True)


def test_chat_banking_tra_kem_buoc_cua_agent(monkeypatch):
    buoc = main.domain.AgentStep("c1", "Đang tra sổ người nhận", "done", 42)
    _gia_lap_agent_dong_bo(monkeypatch, '{"intent": "transfer", "recipient": "Khanh"}', [buoc])
    r = client.post("/api/chat-banking/parse", json={"message": "chuyen 500k cho anh Khanh"})
    b = r.json()
    assert b["source"] == "agent"
    assert b["steps"] == [{"callId": "c1", "label": "Đang tra sổ người nhận",
                           "status": "done", "durationMs": 42}]


def test_chat_banking_hong_thi_khong_kem_buoc(monkeypatch):
    _gia_lap_agent_dong_bo(monkeypatch, None, [])
    r = client.post("/api/chat-banking/parse", json={"message": "chuyen 500k cho anh Khanh"})
    b = r.json()
    # Nhánh dự phòng do gateway dựng, không có bước nào của agent để kể.
    assert b["source"] == "fallback" and b.get("steps", []) == []


def test_scamshield_ket_luan_kem_buoc_da_tra(monkeypatch):
    """Kết luận của agent đi kèm những gì nó đã tra.

    Màn Scam Shield nói tài khoản này đáng ngờ; khách có quyền biết kết luận đó
    dựa trên việc gì.
    """
    import asyncio

    from models import ScamShieldSignals
    buoc = main.domain.AgentStep("c1", "Đang đối chiếu với các thủ đoạn lừa đảo đã biết", "done", 120)
    _gia_lap_agent_dong_bo(
        monkeypatch,
        "NGUY_HIEM\n- Tài khoản mới mở\nKhuyến nghị: không nên chuyển.",
        [buoc])
    s = ScamShieldSignals(bank_code="ACB", account_no="x", account_masked="x", known=True,
                          is_new=True, relationship="UNKNOWN", status="SUSPECTED", age_days=0,
                          tx_count=0, amount=85_000_000, note="", scenario_match="Giả danh công an")
    v = asyncio.run(main._scamshield_verdict("ACB", "x", 85_000_000, "", s))
    assert v.source == "agent"
    assert [b.label for b in v.steps] == ["Đang đối chiếu với các thủ đoạn lừa đảo đã biết"]


def test_scamshield_roi_ve_tin_hieu_thi_khong_kem_buoc(monkeypatch):
    import asyncio

    from models import ScamShieldSignals
    buoc = main.domain.AgentStep("c1", "Đang đối chiếu", "done", 10)
    # Agent chạy nhưng trả câu không đọc được → verdict suy từ tín hiệu, nên các
    # bước kia không còn giải thích cho chữ đang hiện trên màn hình.
    _gia_lap_agent_dong_bo(monkeypatch, "câu không có tag mức độ", [buoc])
    s = ScamShieldSignals(bank_code="ACB", account_no="x", account_masked="x", known=True,
                          is_new=True, relationship="UNKNOWN", status="ACTIVE", age_days=0,
                          tx_count=0, amount=1_000_000, note="", scenario_match=None)
    v = asyncio.run(main._scamshield_verdict("ACB", "x", 1_000_000, "", s))
    assert v.source == "fallback" and v.steps == []


def test_guardian_khuyen_cao_kem_buoc(monkeypatch):
    import asyncio

    buoc = main.domain.AgentStep("c1", "Đang chuẩn bị câu hỏi xác minh", "done", 88)
    _gia_lap_agent_dong_bo(monkeypatch, "Anh/chị dừng lại giúp em nhé.", [buoc])
    tieu_de, noi_dung, nguon, buoc_ra = asyncio.run(
        main._guardian_advice({}, {"advice_title": "Cẩn thận", "advice_body": "Dừng lại"},
                              "Tôi đang được hướng dẫn qua điện thoại", None))
    assert nguon == "agent" and noi_dung == "Anh/chị dừng lại giúp em nhé."
    assert [b.label for b in buoc_ra] == ["Đang chuẩn bị câu hỏi xác minh"]


def test_chat_phat_tom_tat_suy_nghi_ra_sse(monkeypatch):
    """Tóm tắt suy nghĩ đi tới trình duyệt trong lúc trợ lý còn đang nghĩ."""
    ghi_nhan: dict = {}

    async def fake(question, agent_id=None, kind="copilot", customer_id=None,
                   session_key=None, decision_id=None):
        ghi_nhan["hoi"] = question
        yield ("reasoning", "Mình xem giao dịch trước.")
        yield ("text", "Tháng này bạn tiêu 12 triệu.")

    monkeypatch.setattr(main.domain, "agent_events", fake)
    r = client.post("/api/copilot/chat", json={"message": "giúp tôi với"})
    su_kien = _doc_su_kien(r.text)
    assert ("reasoning", "Mình xem giao dịch trước.") in [
        (k, v) for k, v in su_kien if k == "reasoning"]
    # Suy nghĩ ra TRƯỚC chữ: đó là lúc khách đang chờ và cần biết máy đang làm gì.
    assert [k for k, _ in su_kien].index("reasoning") < [k for k, _ in su_kien].index("token")


def test_bang_lo_trinh_cua_gateway_nhuong_cho_tro_ly(monkeypatch):
    """Gateway chia đều mục tiêu cho số tháng, KHÔNG tính lãi kép; công cụ
    plan_savings_goal tính trên biểu lãi thật. Trong một lần thử trên cụm, chữ
    của trợ lý nói "cần 12.672.987đ/tháng, 20,5 năm" còn bảng gateway ngay bên
    dưới ghi "13.092.250đ" và "39 năm". Hai con số chọi nhau là thứ khách nhìn
    thấy trước tiên, nên nhánh này gateway phải rút lui khi trợ lý đã trả lời."""
    async def gia_lap(_q):
        from models import ChatTable, ChatTableRow, ChatChart, ChatChartPoint
        bang = ChatTable(title="Lộ trình tiết kiệm",
                         rows=[ChatTableRow(label="3 năm", amount=13_092_250, pct=172)],
                         total_label="Thực tế để dành được", total_amount=1_018_000)
        do_thi = ChatChart(type="bar", title="Cần để dành mỗi tháng",
                           data=[ChatChartPoint(label="3 năm", value=13_092_250)])
        return bang, do_thi, True

    async def phat(*a, **k):
        yield "text", "Cần 12.672.987 đ/tháng.\n\n| Gói | Lãi |\n|---|---:|\n| Tiết kiệm | 6,2% |\n"

    monkeypatch.setattr(main, "_spending_visual", gia_lap)
    monkeypatch.setattr(main.domain, "agent_events", phat)
    monkeypatch.setattr(main.domain, "agent_configured", lambda *a, **k: True)
    r = client.post("/api/copilot/chat", json={"message": "kế hoạch tiết kiệm mua ô tô 500 triệu"})
    body = r.text
    # SSE trả số thô, không định dạng — kiểm "13.092.250" sẽ đúng một cách vô nghĩa.
    assert "13092250" not in body, "bảng gateway phải rút lui, không được chọi số với trợ lý"
    assert '"table"' not in body
    assert '"chart"' not in body, "biểu đồ đi theo bảng, cùng rút lui"
    assert '"grid"' in body, "bảng markdown của trợ lý phải được dựng thay vào"


def test_tro_ly_hong_thi_bang_lo_trinh_cua_gateway_van_hien(monkeypatch):
    """Nhường là nhường cho một câu trả lời CÓ THẬT. Trợ lý chết mà gateway cũng
    rút bảng thì khách nhận được một màn trống."""
    async def gia_lap(_q):
        from models import ChatTable, ChatTableRow
        return ChatTable(title="Lộ trình tiết kiệm",
                         rows=[ChatTableRow(label="3 năm", amount=13_092_250, pct=172)],
                         total_label="Thực tế để dành được", total_amount=1_018_000), None, True

    async def hong(*a, **k):
        # agent_events tự nuốt mọi lỗi và không phát chữ nào — đó mới là hình
        # dạng thật của "agent hỏng", không phải một exception ném ra ngoài.
        return
        yield  # pragma: no cover

    monkeypatch.setattr(main, "_spending_visual", gia_lap)
    monkeypatch.setattr(main.domain, "agent_events", hong)
    monkeypatch.setattr(main.domain, "agent_configured", lambda *a, **k: True)
    r = client.post("/api/copilot/chat", json={"message": "kế hoạch tiết kiệm mua ô tô 500 triệu"})
    assert "13092250" in r.text, "agent hỏng thì bảng gateway phải quay lại"


def test_bon_bang_markdown_deu_duoc_dung():
    """Câu mục tiêu tiết kiệm sinh đúng bốn bảng: con số, rổ chi, phương án, gói.
    Cắt ở ba thì mục "Gói tiết kiệm phù hợp" chỉ còn tiêu đề trống."""
    md = "\n\n".join(
        f"## Mục {i}\n\n| Cột A | Cột B |\n|---|---:|\n| x{i} | {i}00 |"
        for i in range(1, 5)
    )
    grids = main._parse_markdown_grids(md)
    assert len(grids) == 4
    assert [g.rows[0][0] for g in grids] == ["x1", "x2", "x3", "x4"]


def test_verdict_giu_muc_cua_he_thong_du_agent_phan_khac(monkeypatch):
    """Hỏi bốn lần CÙNG một lệnh chuyển (5 triệu tiền ăn trưa cho người thân đã
    chuyển 28 lần) nhận về bốn mức khác nhau: suspect, danger, safe, suspect —
    lượt "danger" còn viết là khớp kịch bản lừa đảo sàn đầu tư. match_scam luôn
    trả candidates[] kèm điểm kể cả khi matched=false, và mô hình đọc ứng viên
    thành kịch bản đã khớp. Mức phải do luật giữ, agent chỉ được viết chữ."""
    import asyncio
    from models import ScamShieldSignals
    s = ScamShieldSignals(bank_code="VCB", account_no="3028127210", account_masked="3028 ****",
                          known=True, is_new=False, relationship="FAMILY", status="ACTIVE",
                          age_days=430, tx_count=28, amount=5_000_000, note="tra tien an trua")

    async def agent_phan_bua(*a, **k):
        return ("NGUY_HIEM\n- Khớp kịch bản lừa đảo sàn đầu tư.\nKhuyến nghị: Dừng ngay.", [])

    monkeypatch.setattr(main.domain, "agent_answer_with_steps", agent_phan_bua)
    monkeypatch.setattr(main.domain, "agent_configured", lambda *a, **k: True)
    v = asyncio.run(main._scamshield_verdict("VCB", "3028127210", 5_000_000, "tra tien an trua", s))

    assert v.level == "safe", "người quen, không dấu hiệu nào — agent phán NGUY_HIEM cũng không được lọt"
    assert v.title == "Chưa thấy dấu hiệu bất thường"
    # Chữ của agent vẫn được dùng, chỉ mức là không.
    assert v.source == "agent"
    assert "sàn đầu tư" in v.reasons[0]


def test_verdict_agent_hong_thi_van_co_ket_luan_theo_luat(monkeypatch):
    import asyncio
    from models import ScamShieldSignals
    s = ScamShieldSignals(bank_code="ACB", account_no="x", account_masked="x", known=True, is_new=True,
                          relationship="UNKNOWN", status="SUSPECTED", age_days=0, tx_count=0,
                          amount=85_000_000, note="", scenario_match="Giả danh công an")

    async def hong(*a, **k):
        raise RuntimeError("agent chết")

    monkeypatch.setattr(main.domain, "agent_answer_with_steps", hong)
    monkeypatch.setattr(main.domain, "agent_configured", lambda *a, **k: True)
    v = asyncio.run(main._scamshield_verdict("ACB", "x", 85_000_000, "", s))
    assert v.level == "danger" and v.source == "fallback" and v.steps == []


def test_chat_banking_mang_cau_noi_thanh_noi_dung(monkeypatch):
    """Nội dung chuyển khoản phải là NGUYÊN câu khách gõ, để precheck/Scam Shield
    đọc được ngữ cảnh và nhận ra kịch bản lừa đảo. Không có nó thì memo trung
    tính và mọi kịch bản (công an, đầu tư, shipper...) lọt lưới qua chatpay."""
    _gia_lap_agent_dong_bo(monkeypatch, '{"intent": "transfer", "recipient": "Trung"}', [])
    cau = "chuyển cho Trung 85 triệu, công an bảo chuyển gấp để chứng minh trong sạch"
    b = client.post("/api/chat-banking/parse", json={"message": cau}).json()
    assert b["source"] == "agent"
    assert b["note"] == cau            # nguyên câu, để Guardian bắt "công an"
    assert "công an" in b["note"]


def test_chat_banking_khong_phai_transfer_thi_khong_co_noi_dung(monkeypatch):
    """Xem danh bạ / câu linh tinh không phải lệnh chuyển thì không đặt nội dung."""
    _gia_lap_agent_dong_bo(monkeypatch, '{"intent": "list_beneficiaries"}', [])
    b = client.post("/api/chat-banking/parse", json={"message": "danh bạ của tôi"}).json()
    assert b.get("note") is None
