"""Test hợp đồng gọi agent-service từ gateway.

Lỗi từng gặp: gửi api-key dưới dạng `Authorization: Bearer` → agent-service trả
401, gateway nuốt lỗi rồi rơi về kịch bản có sẵn, header báo "degraded" — nhìn
bên ngoài y như chưa cấu hình agent. Test này ghim đúng header và thân request
để lỗi ấy không tái diễn trong im lặng.

Dùng asyncio.run() thay vì pytest-asyncio để không thêm dependency chỉ-cho-test
vào image (requirements.txt cũng là thứ Docker build cài).
"""
import asyncio

import domain
import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Ghi lại mọi request để test soi header + body.

    Một lượt gọi agent nay sinh HAI request: lời gọi agent, rồi lời ghi nhật ký
    sang action-feedback. Giữ cả danh sách thay vì mỗi request cuối, để mỗi test
    soi đúng request mà nó quan tâm.
    """
    calls: list = []
    payload: dict = {"status": "succeeded", "output": "trả lời thật từ agent"}
    fail: Exception | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        if _FakeClient.fail is not None and "/v1/agents/" in url:
            raise _FakeClient.fail
        return _FakeResponse(_FakeClient.payload)


def _call_to(fragment: str) -> dict:
    """Request đầu tiên có URL chứa `fragment`."""
    for call in _FakeClient.calls:
        if fragment in call["url"]:
            return call
    raise AssertionError(f"không có request nào tới {fragment}: {[c['url'] for c in _FakeClient.calls]}")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(domain, "AGENT_SERVICE_URL", "http://agent-service:8080")
    monkeypatch.setattr(domain, "AGENT_API_KEY", "apk_test_key")
    monkeypatch.setattr(domain, "AGENT_ID", "agent-123")
    monkeypatch.setattr(domain, "DOMAIN_ENABLED", True)
    monkeypatch.setattr(domain.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(domain, "ACTION_FEEDBACK_URL", "http://action-feedback-service")
    domain.reset_request_state()
    _FakeClient.calls = []
    _FakeClient.fail = None
    _FakeClient.payload = {"status": "succeeded", "output": "trả lời thật từ agent"}


def test_agent_dung_header_x_api_key_khong_phai_bearer(configured):
    out = asyncio.run(domain.agent_answer("Quý này tôi tiêu vào đâu?"))
    assert out == "trả lời thật từ agent"
    headers = _call_to("/v1/agents/")["headers"]
    # api-key PHẢI đi trong X-API-Key; Authorization: Bearer sẽ bị 401.
    assert headers.get("X-API-Key") == "apk_test_key"
    assert "Authorization" not in headers


def test_agent_dung_hop_dong_than_request(configured):
    asyncio.run(domain.agent_answer("xin chào"))
    agent_call = _call_to("/v1/agents/")
    body = agent_call["json"]
    # input là OBJECT có message, mode sync — không phải {"input": str, "stream": ...}
    assert body == {"input": {"message": "xin chào"}, "mode": "sync"}
    assert agent_call["url"].endswith("/v1/agents/agent-123/runs")


def test_agent_output_rong_thi_tra_none(configured):
    _FakeClient.payload = {"status": "succeeded", "output": "   "}
    assert asyncio.run(domain.agent_answer("x")) is None


def test_agent_chua_cau_hinh_thi_tra_none(monkeypatch):
    # Thiếu bất kỳ mảnh nào (url/key/id) → coi như chưa cấu hình, không gọi mạng.
    monkeypatch.setattr(domain, "AGENT_SERVICE_URL", "")
    monkeypatch.setattr(domain, "AGENT_API_KEY", "apk_test_key")
    monkeypatch.setattr(domain, "AGENT_ID", "agent-123")
    assert asyncio.run(domain.agent_answer("x")) is None


# ---- Nhật ký lượt gọi agent ---------------------------------------------------
#
# Không ai ghi bảng llm_trace lúc chạy: tool log_llm_call không gán cho trợ lý
# nào, còn gateway thì gọi xong là thôi. Màn "Nhật ký quyết định AI" vì vậy chỉ
# hiện dữ liệu mẫu. Các test dưới đây ghim việc ghi lại đó.

def test_moi_luot_goi_agent_deu_de_lai_mot_dong_nhat_ky(configured):
    asyncio.run(domain.agent_answer("Quý này tôi tiêu vào đâu?", kind="copilot"))
    trace = _call_to("/llm-traces")["json"]
    assert trace["agent"] == "copilot"
    assert trace["status"] == "ok"
    assert trace["prompt_masked"] == "Quý này tôi tiêu vào đâu?"
    assert trace["response"] == "trả lời thật từ agent"
    # Run của Agent Platform không trả tên model, nên gateway không được đoán.
    assert trace["model"] == "agent-platform"


def test_luot_goi_hong_van_phai_de_lai_dau_vet(configured):
    _FakeClient.fail = domain.httpx.TimeoutException("hết giờ")
    assert asyncio.run(domain.agent_answer("x", kind="shield_advice")) is None
    trace = _call_to("/llm-traces")["json"]
    # Bản ghi timeout/error phải tồn tại, nếu không tỷ lệ dùng bản dự phòng trên
    # màn Ops là con số tự khai chứ không phải số đo.
    assert trace["status"] == "timeout"
    assert trace["agent"] == "shield_advice"


def test_nhat_ky_gan_case_khi_biet_quyet_dinh_nao(configured):
    asyncio.run(domain.agent_answer("x", kind="shield_advice",
                                    customer_id=100009, decision_id="dec-1"))
    trace = _call_to("/llm-traces")["json"]
    # Có decision_id thì bấm một dòng nhật ký mở được đúng case.
    assert (trace["decision_id"], trace["customer_id"]) == ("dec-1", 100009)


def test_ghi_nhat_ky_hong_khong_lam_hong_cau_tra_loi(configured, monkeypatch):
    async def no_trace(*a, **k):
        raise RuntimeError("action-feedback chết")
    monkeypatch.setattr(domain, "_write_llm_trace", no_trace)
    # Nhật ký là việc phụ; khách vẫn phải nhận được câu trả lời.
    assert asyncio.run(domain.agent_answer("x")) == "trả lời thật từ agent"


# ---- Phát trực tiếp từ agent --------------------------------------------------
#
# Đường sync phải chờ trọn lần xử lý mới có chữ. Các test dưới đây ghim việc đọc
# SSE của nền tảng: chỉ message.delta mới là chữ, heartbeat và các sự kiện khác
# phải bỏ qua, và mọi lượt đều để lại một dòng nhật ký.

class _FakeStream:
    """Phản hồi SSE dựng sẵn, đủ hình dạng mà httpx.stream() trả về."""
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aread(self):
        return b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise domain.httpx.HTTPStatusError("lỗi", request=None, response=None)

    async def aiter_lines(self):
        for line in self._lines:
            yield line


def _client_phat(lines, status_code=200):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            _FakeClient.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            return _FakeResponse({"ok": True})

        def stream(self, method, url, headers=None, json=None):
            _FakeClient.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
            return _FakeStream(lines, status_code)
    return _Client


async def _gom(gen):
    return [piece async for piece in gen]


def test_stream_chi_lay_message_delta(configured, monkeypatch):
    monkeypatch.setattr(domain.httpx, "AsyncClient", _client_phat([
        ": ping",                                    # heartbeat: bỏ qua
        "event: run.started",
        'data: {"type": "run.started"}',
        'data: {"type": "message.delta", "text": "Tháng này "}',
        'data: {"type": "message.delta", "text": "bạn chi 12 triệu."}',
        'data: {"type": "run.completed"}',
    ]))
    out = asyncio.run(_gom(domain.agent_stream("x", session_key="copilot-1")))
    assert out == ["Tháng này ", "bạn chi 12 triệu."]
    trace = _call_to("/llm-traces")["json"]
    assert trace["status"] == "ok"


def test_stream_gui_kem_khoa_hoi_thoai(configured, monkeypatch):
    monkeypatch.setattr(domain.httpx, "AsyncClient", _client_phat([
        'data: {"type": "message.delta", "text": "xong"}',
        'data: {"type": "run.completed"}',
    ]))
    asyncio.run(_gom(domain.agent_stream("x", session_key="copilot-100008")))
    goi = _call_to("/runs/stream")
    assert goi["json"]["session_key"] == "copilot-100008"
    assert goi["json"]["input"] == {"message": "x"}


def test_stream_hong_giua_chung_van_giu_phan_da_phat(configured, monkeypatch):
    monkeypatch.setattr(domain.httpx, "AsyncClient", _client_phat([
        'data: {"type": "message.delta", "text": "Phần đầu"}',
        'data: {"type": "run.failed", "error": {"code": "provider_error"}}',
    ]))
    out = asyncio.run(_gom(domain.agent_stream("x")))
    assert out == ["Phần đầu"]
    trace = _call_to("/llm-traces")["json"]
    # Lượt hỏng phải để lại dấu vết kèm lý do, không im lặng.
    assert trace["status"] == "error"
    assert "provider_error" in trace["response"]


def test_stream_409_la_ban_chu_khong_phai_hong(configured, monkeypatch):
    monkeypatch.setattr(domain.httpx, "AsyncClient", _client_phat([], status_code=409))
    with pytest.raises(domain.AgentBusy):
        asyncio.run(_gom(domain.agent_stream("x")))


def test_stream_chua_cau_hinh_thi_khong_goi_mang(monkeypatch):
    monkeypatch.setattr(domain, "AGENT_SERVICE_URL", "")
    assert asyncio.run(_gom(domain.agent_stream("x"))) == []
