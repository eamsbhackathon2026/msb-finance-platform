"""scam-knowledge-service

Sở hữu: scam_scenario (playbook 10 kịch bản lừa đảo) và fraud_case (bộ kiểm thử engine).

Đây là hàng rào an toàn của cả hệ: mọi câu hỏi và khuyến cáo hiển thị cho khách
đều phải có gốc trong bảng scam_scenario. LLM chỉ được diễn đạt lại `advice_body`
cho mượt, không được tự nghĩ ra kịch bản mới hay tự đổi `recommended_action` —
nếu không, một lời khuyên sai của LLM sẽ đi thẳng tới khách hàng thật.

`POST /scams/match` nhận tổ hợp dấu hiệu từ risk-scoring-service và trả về kịch bản
khớp nhất kèm bộ câu hỏi đã chọn đúng theo persona (người làm công ăn lương, khách
ưu tiên và người cao tuổi cần được hỏi bằng ba giọng khác nhau).

fraud_case KHÔNG được expose ra API phục vụ khách hàng — nó chứa đáp án của bộ
kiểm thử; các endpoint dưới /fraud-cases là công cụ nội bộ cho đội phát triển.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common import (
    COMMON_TAGS,
    PERSONAS,
    agent_tools_payload,
    db_health,
    execute_returning,
    install_db_error_handlers,
    now_vn,
    openapi_description,
    query,
    query_one,
    service_info,
    setup_docs,
    tool,
)

SERVICE_NAME = "scam-knowledge-service"
OWNS_TABLES = ["scam_scenario", "fraud_case"]

OPENAPI_TAGS = COMMON_TAGS + [
    {
        "name": 'playbook',
        "description": (
            'Mười kịch bản lừa đảo và bộ câu hỏi tương ứng. Đây là hàng rào an toàn: mọi câu hỏi và khuyến cáo hiển thị cho khách đều phải có gốc ở đây, LLM chỉ được diễn đạt lại chứ không được tự nghĩ ra.'
        ),
    },
    {
        "name": 'internal',
        "description": (
            'Bộ kiểm thử engine. KHÔNG dùng cho ứng dụng khách hàng — bảng này chứa đáp án kỳ vọng của từng case.'
        ),
    },
]

app = FastAPI(
    title=SERVICE_NAME,
    version="1.0.0",
    summary='Playbook kịch bản lừa đảo phổ biến tại Việt Nam: tra cứu, đối chiếu dấu hiệu và bộ câu hỏi theo phân khúc khách hàng.',
    description=openapi_description(SERVICE_NAME, 'Playbook kịch bản lừa đảo phổ biến tại Việt Nam: tra cứu, đối chiếu dấu hiệu và bộ câu hỏi theo phân khúc khách hàng.', OWNS_TABLES),
    openapi_tags=OPENAPI_TAGS,
    contact={"name": "MSB AI Financial Guardian — xem manifest tại GET /agent/tools"},
    docs_url="/docs",
    redoc_url="/redoc",
)
setup_docs(app, SERVICE_NAME)
# Lỗi Postgres do id sai của người gọi phải ra 404/409/422 chứ không phải
# 500, vì trợ lý đọc thẳng thân phản hồi này để quyết bước tiếp theo.
install_db_error_handlers(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class MatchRequest(BaseModel):
    persona: Literal["SALARY", "HNW", "SENIOR"] = "SALARY"
    memo: str = Field("", description="Nội dung chuyển khoản, đã mask")
    amount: float = 0
    is_new_beneficiary: bool = False
    drain_ratio: float = Field(0, description="amount / số dư khả dụng, 0-1")
    is_night: bool = False
    recent_events: list[str] = Field(default_factory=list)
    session_flags: dict = Field(default_factory=dict)
    beneficiary_total_in: float = Field(
        0, description="Tổng tiền đã nhận về từ người nhận này — dấu hiệu tiền mồi"
    )
    series_count_14d: int = Field(0, description="Số lần đã chuyển cho người này trong 14 ngày")


class TestResultRequest(BaseModel):
    score: int = Field(..., ge=0, le=100)
    passed: Literal["Y", "N"] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_accents(text: str) -> str:
    """Khách gõ 'cong an' hay 'công an' đều phải khớp, nên so sánh trên bản bỏ dấu."""
    nfkd = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Khớp theo ranh giới từ, không theo chuỗi con.

    Khớp chuỗi con làm từ khóa 'gap' trúng ngay trong 'NANG CAP BAO MAT', khiến một
    giao dịch bị moi OTP lại bị gán nhầm sang kịch bản deepfake — sai kịch bản thì
    câu hỏi và khuyến cáo đưa cho khách cũng sai chủ đề.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", haystack) is not None


def _scenario_payload(row: dict, persona: str | None = None) -> dict:
    """Bản ra ngoài của một kịch bản. `questions` là dict theo persona trong DB;
    khi biết persona thì phẳng hóa thành đúng bộ câu hỏi của persona đó."""
    questions = row.get("questions") or {}
    if persona:
        selected = questions.get(persona) or questions.get("SALARY") or []
    else:
        selected = questions
    return {
        "scenario_id": row["scenario_id"],
        "scenario_name": row["scenario_name"],
        "group_code": row.get("group_code"),
        "pattern": row.get("pattern"),
        "agent_can_ask": row.get("agent_can_ask", "Y"),
        "question": selected[0] if persona and selected else None,
        "questions": selected,
        "options": row.get("options") or [],
        "advice_title": row.get("advice_title"),
        "advice_body": row.get("advice_body"),
        "recommended_action": row.get("recommended_action"),
        "priority": row.get("priority"),
    }


def _match_score(pattern: dict, req: MatchRequest) -> tuple[float, list[str]]:
    """Chấm độ khớp giữa một kịch bản và tổ hợp dấu hiệu quan sát được.

    Trả về (điểm khớp, các dấu hiệu đã khớp). Điểm dùng để xếp hạng chứ không phải
    xác suất; khi hòa thì `priority` trong DB quyết định — TAKEOVER luôn priority 0
    nên luôn thắng, vì khi khách đang bị chiếm quyền thiết bị thì hỏi han là vô ích.
    """
    hits: list[str] = []
    score = 0.0

    keywords = pattern.get("keywords") or []
    if keywords:
        memo = _strip_accents(req.memo)
        matched = [k for k in keywords if _contains_phrase(memo, _strip_accents(k))]
        if matched:
            score += 3.0 * len(matched)
            hits.append("từ khóa: " + ", ".join(matched))

    flags = pattern.get("session_flags") or []
    matched_flags = [f for f in flags if req.session_flags.get(f)]
    if matched_flags:
        score += 6.0 * len(matched_flags)
        hits.append("cờ phiên: " + ", ".join(matched_flags))

    events = pattern.get("recent_events") or []
    matched_events = [e for e in events if e in req.recent_events]
    if matched_events:
        score += 4.0 * len(matched_events)
        hits.append("sự kiện gần đây: " + ", ".join(matched_events))

    if pattern.get("new_beneficiary") and req.is_new_beneficiary:
        score += 2.5
        hits.append("người nhận mới")
    if pattern.get("night") and req.is_night:
        score += 1.5
        hits.append("giao dịch ban đêm")
    if (mn := pattern.get("min_amount")) and req.amount >= mn:
        score += 2.0
        hits.append(f"số tiền ≥ {mn:,.0f}")
    if (dr := pattern.get("min_drain_ratio")) and req.drain_ratio >= dr:
        score += 3.5
        hits.append(f"vét ≥ {dr:.0%} số dư")
    if pattern.get("inbound_bait") and req.beneficiary_total_in > 0:
        score += 3.0
        hits.append("từng nhận tiền mồi từ người nhận")
    if (sc := pattern.get("min_series_14d")) and req.series_count_14d >= sc:
        score += 3.0
        hits.append(f"đã chuyển {req.series_count_14d} lần trong 14 ngày")

    return score, hits


# ---------------------------------------------------------------------------
# Common endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/health/db", tags=["meta"])
def health_db():
    return {"service": SERVICE_NAME, **db_health()}


@app.get("/info", tags=["meta"])
def info():
    payload = service_info(
        SERVICE_NAME,
        "Playbook kịch bản lừa đảo và bộ kiểm thử fraud case.",
        OWNS_TABLES,
        AGENT_TOOLS,
    )
    payload["personas"] = list(PERSONAS)
    payload["guardrail"] = (
        "Mọi câu hỏi và khuyến cáo hiển thị cho khách phải lấy từ bảng scam_scenario. "
        "LLM được diễn đạt lại advice_body nhưng không được đổi recommended_action."
    )
    return payload


@app.get("/agent/tools", tags=["meta"])
def agent_tools():
    return agent_tools_payload(SERVICE_NAME, "SCAM_KNOWLEDGE_SERVICE_URL", AGENT_TOOLS)


# ---------------------------------------------------------------------------
# Playbook
# ---------------------------------------------------------------------------
@app.get("/scams", tags=["playbook"])
def list_scams(
    group_code: str | None = Query(None, description="G1..G5"),
    pattern: str | None = Query(None, description="SINGLE|SERIES|DRAIN|TAKEOVER|RECEIVER"),
    status: str = Query("ACTIVE"),
):
    sql = "SELECT * FROM scam_scenario WHERE status = %s"
    params: list = [status.upper()]
    if group_code:
        sql += " AND group_code = %s"
        params.append(group_code.upper())
    if pattern:
        sql += " AND pattern = %s"
        params.append(pattern.upper())
    sql += " ORDER BY priority, scenario_id"
    rows = query(sql, params)
    return {"count": len(rows), "scenarios": [_scenario_payload(r) for r in rows]}


@app.get("/scams/{scenario_id}", tags=["playbook"])
def get_scam(scenario_id: str, persona: str | None = Query(None)):
    row = query_one(
        "SELECT * FROM scam_scenario WHERE scenario_id = %s", (scenario_id.upper(),)
    )
    if row is None:
        raise HTTPException(404, f"kịch bản {scenario_id} không tồn tại")
    return _scenario_payload(row, persona.upper() if persona else None)


@app.get("/scams/{scenario_id}/questions", tags=["playbook"])
def get_questions(
    scenario_id: str, persona: Literal["SALARY", "HNW", "SENIOR"] = "SALARY"
):
    """Cùng một kịch bản nhưng hỏi người cao tuổi khác với hỏi khách ưu tiên."""
    row = query_one(
        "SELECT * FROM scam_scenario WHERE scenario_id = %s", (scenario_id.upper(),)
    )
    if row is None:
        raise HTTPException(404, f"kịch bản {scenario_id} không tồn tại")
    payload = _scenario_payload(row, persona)
    return {
        "scenario_id": payload["scenario_id"],
        "persona": persona,
        "agent_can_ask": payload["agent_can_ask"],
        "questions": payload["questions"],
        "options": payload["options"],
        "note": (
            "agent_can_ask = N: kịch bản chiếm quyền thiết bị, bỏ qua lượt hỏi và "
            "khóa giao dịch ngay — người đang thao tác có thể không phải chủ tài khoản."
            if payload["agent_can_ask"] == "N"
            else "Hỏi câu đầu tiên trước, chỉ hỏi tiếp nếu câu trả lời chưa rõ."
        ),
    }


@app.post("/scams/match", tags=["playbook"])
def match_scam(req: MatchRequest):
    """Đối chiếu tổ hợp dấu hiệu với playbook, trả kịch bản khớp nhất.

    Không khớp kịch bản nào là kết quả hợp lệ (trả scenario = null): giao dịch bất
    thường chưa chắc là lừa đảo, và ép gán bừa một kịch bản sẽ khiến khách nhận
    cảnh báo sai chủ đề.
    """
    rows = query("SELECT * FROM scam_scenario WHERE status = 'ACTIVE' ORDER BY priority")
    ranked = []
    for row in rows:
        score, hits = _match_score(row.get("signal_pattern") or {}, req)
        if score > 0:
            ranked.append((score, -int(row.get("priority") or 99), row, hits))
    if not ranked:
        return {
            "matched": False,
            "scenario": None,
            "candidates": [],
            "note": "Không dấu hiệu nào khớp playbook; dùng cảnh báo chung theo điểm rủi ro.",
        }

    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    best_score, _, best_row, best_hits = ranked[0]
    total = sum(r[0] for r in ranked)
    payload = _scenario_payload(best_row, req.persona)
    payload["confidence"] = round(best_score / total, 3) if total else 0.0
    payload["matched_signals"] = best_hits
    return {
        "matched": True,
        "scenario": payload,
        "candidates": [
            {
                "scenario_id": r[2]["scenario_id"],
                "scenario_name": r[2]["scenario_name"],
                "match_score": round(r[0], 2),
                "signals": r[3],
            }
            for r in ranked[:3]
        ],
    }


# ---------------------------------------------------------------------------
# Fraud case — công cụ nội bộ, không dùng cho khách hàng
# ---------------------------------------------------------------------------
@app.get("/fraud-cases", tags=["internal"])
def list_fraud_cases(scenario_id: str | None = None):
    sql = """
        SELECT f.*, s.scenario_name
        FROM fraud_case f JOIN scam_scenario s ON s.scenario_id = f.scenario_id
        WHERE 1 = 1
    """
    params: list = []
    if scenario_id:
        sql += " AND f.scenario_id = %s"
        params.append(scenario_id.upper())
    sql += " ORDER BY f.fraud_case_id"
    rows = query(sql, params)
    return {"count": len(rows), "fraud_cases": rows}


@app.get("/fraud-cases/{fraud_case_id}", tags=["internal"])
def get_fraud_case(fraud_case_id: str):
    row = query_one(
        """
        SELECT f.*, s.scenario_name
        FROM fraud_case f JOIN scam_scenario s ON s.scenario_id = f.scenario_id
        WHERE f.fraud_case_id = %s
        """,
        (fraud_case_id.upper(),),
    )
    if row is None:
        raise HTTPException(404, f"fraud case {fraud_case_id} không tồn tại")
    return row


@app.post("/fraud-cases/{fraud_case_id}/test-result", tags=["internal"])
def record_test_result(fraud_case_id: str, req: TestResultRequest):
    """Ghi kết quả chấm engine cho một case. Nếu không truyền `passed`, service tự
    kết luận bằng cách so điểm với khoảng kỳ vọng của case."""
    case = query_one(
        "SELECT * FROM fraud_case WHERE fraud_case_id = %s", (fraud_case_id.upper(),)
    )
    if case is None:
        raise HTTPException(404, f"fraud case {fraud_case_id} không tồn tại")
    passed = req.passed or (
        "Y"
        if case["expected_score_min"] <= req.score <= case["expected_score_max"]
        else "N"
    )
    row = execute_returning(
        """
        UPDATE fraud_case
        SET last_test_score = %s, last_test_passed = %s, last_test_at = now()
        WHERE fraud_case_id = %s RETURNING *
        """,
        (req.score, passed, fraud_case_id.upper()),
    )
    return {
        "fraud_case_id": row["fraud_case_id"],
        "score": req.score,
        "expected_range": [case["expected_score_min"], case["expected_score_max"]],
        "expected_level": case["expected_level"],
        "passed": passed,
        "tested_at": row["last_test_at"],
    }


@app.get("/fraud-cases-report", tags=["internal"])
def fraud_case_report():
    """Bảng điểm của engine trên toàn bộ bộ kiểm thử — chạy trước khi demo."""
    rows = query(
        """
        SELECT fraud_case_id, scenario_id, expected_level, expected_score_min,
               expected_score_max, last_test_score, last_test_passed, last_test_at,
               demo_scene
        FROM fraud_case ORDER BY fraud_case_id
        """
    )
    tested = [r for r in rows if r["last_test_passed"] is not None]
    return {
        "total": len(rows),
        "tested": len(tested),
        "passed": sum(1 for r in tested if r["last_test_passed"] == "Y"),
        "failed": sum(1 for r in tested if r["last_test_passed"] == "N"),
        "cases": rows,
    }


# ---------------------------------------------------------------------------
# Agent manifest
# ---------------------------------------------------------------------------
AGENT_TOOLS = [
    tool(
        "list_scams",
        "Liệt kê các kịch bản lừa đảo trong playbook, lọc theo nhóm (G1 mạo danh, "
        "G2 deepfake, G3 đầu tư/việc làm, G4 mua bán, G5 chiếm thiết bị) hoặc theo "
        "dạng hành vi.",
        "GET", "/scams",
        params={"group_code": "G1..G5", "pattern": "SINGLE|SERIES|DRAIN|TAKEOVER|RECEIVER", "status": "ACTIVE"},
        returns="Danh sách kịch bản kèm khuyến cáo.",
    ),
    tool(
        "get_scam",
        "Chi tiết một kịch bản. Truyền persona để nhận đúng bộ câu hỏi của phân khúc đó.",
        "GET", "/scams/{scenario_id}",
        params={"scenario_id": "S01..S10", "persona": "SALARY|HNW|SENIOR (tùy chọn)"},
        returns="Kịch bản đầy đủ.",
    ),
    tool(
        "match_scam",
        "Đối chiếu tổ hợp dấu hiệu quan sát được với playbook và trả về kịch bản khớp "
        "nhất kèm độ tin cậy. Không khớp gì cũng là kết quả hợp lệ — đừng ép gán kịch bản.",
        "POST", "/scams/match",
        body={
            "persona": "SALARY|HNW|SENIOR", "memo": "nội dung CK đã mask",
            "amount": "number", "is_new_beneficiary": "bool", "drain_ratio": "0-1",
            "is_night": "bool", "recent_events": "list[str]", "session_flags": "object",
            "beneficiary_total_in": "number", "series_count_14d": "int",
        },
        returns="scenario khớp nhất + 3 ứng viên hàng đầu.",
    ),
    tool(
        "get_questions",
        "Bộ câu hỏi nên hỏi khách cho một kịch bản, chọn đúng theo persona. "
        "Nếu agent_can_ask = N thì bỏ qua lượt hỏi và khóa giao dịch ngay.",
        "GET", "/scams/{scenario_id}/questions",
        params={"scenario_id": "S01..S10", "persona": "SALARY|HNW|SENIOR"},
        returns="questions, options và hướng dẫn cách hỏi.",
    ),
]
