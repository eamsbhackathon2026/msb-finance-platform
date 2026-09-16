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
    """Ghi lại request cuối cùng để test soi header + body."""
    last: dict = {}
    payload: dict = {"status": "succeeded", "output": "trả lời thật từ agent"}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.last = {"url": url, "headers": headers or {}, "json": json or {}}
        return _FakeResponse(_FakeClient.payload)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(domain, "AGENT_SERVICE_URL", "http://agent-service:8080")
    monkeypatch.setattr(domain, "AGENT_API_KEY", "apk_test_key")
    monkeypatch.setattr(domain, "AGENT_ID", "agent-123")
    monkeypatch.setattr(domain, "DOMAIN_ENABLED", True)
    monkeypatch.setattr(domain.httpx, "AsyncClient", _FakeClient)
    domain.reset_request_state()
    _FakeClient.last = {}
    _FakeClient.payload = {"status": "succeeded", "output": "trả lời thật từ agent"}


def test_agent_dung_header_x_api_key_khong_phai_bearer(configured):
    out = asyncio.run(domain.agent_answer("Quý này tôi tiêu vào đâu?"))
    assert out == "trả lời thật từ agent"
    headers = _FakeClient.last["headers"]
    # api-key PHẢI đi trong X-API-Key; Authorization: Bearer sẽ bị 401.
    assert headers.get("X-API-Key") == "apk_test_key"
    assert "Authorization" not in headers


def test_agent_dung_hop_dong_than_request(configured):
    asyncio.run(domain.agent_answer("xin chào"))
    body = _FakeClient.last["json"]
    # input là OBJECT có message, mode sync — không phải {"input": str, "stream": ...}
    assert body == {"input": {"message": "xin chào"}, "mode": "sync"}
    assert _FakeClient.last["url"].endswith("/v1/agents/agent-123/runs")


def test_agent_output_rong_thi_tra_none(configured):
    _FakeClient.payload = {"status": "succeeded", "output": "   "}
    assert asyncio.run(domain.agent_answer("x")) is None


def test_agent_chua_cau_hinh_thi_tra_none(monkeypatch):
    # Thiếu bất kỳ mảnh nào (url/key/id) → coi như chưa cấu hình, không gọi mạng.
    monkeypatch.setattr(domain, "AGENT_SERVICE_URL", "")
    monkeypatch.setattr(domain, "AGENT_API_KEY", "apk_test_key")
    monkeypatch.setattr(domain, "AGENT_ID", "agent-123")
    assert asyncio.run(domain.agent_answer("x")) is None
