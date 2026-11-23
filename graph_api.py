"""graph_agent를 HTTP API로 공개 (다른 기기·외부 프로그램에서 호출)

설치:  pip install fastapi uvicorn
실행:  uvicorn graph_api:app --host 0.0.0.0 --port 8765
호출:  POST /ask  (헤더 Authorization: Bearer <GRAPH_API_TOKEN>)
        {"question": "사출성형 공정은 어떤 순서로 진행돼?", "model": "claude"}
"""
import os
import secrets
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from graph_agent import ask_graph

TOKEN = os.environ.get("GRAPH_API_TOKEN", "")
app = FastAPI(title="mfg-graph Agent API")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    model: Optional[str] = None         # claude / claude-pro / gemini / qwen (대소문자 무관, 생략 시 LLM_MODEL)
    include_steps: bool = True          # 실행된 Cypher·SQL과 행 수를 함께 반환


def _check_token(authorization: Optional[str]):
    if not TOKEN:
        raise HTTPException(500, "서버에 GRAPH_API_TOKEN이 설정되지 않았습니다.")
    if not authorization or not secrets.compare_digest(authorization,
                                                       f"Bearer {TOKEN}"):
        raise HTTPException(401, "인증 실패")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ask")
def ask(req: AskRequest, authorization: Optional[str] = Header(default=None)):
    _check_token(authorization)
    try:
        res = ask_graph(req.question, model=req.model, verbose=False)
    except Exception as e:  # LLM/Neo4j 오류는 내용 일부만 전달
        raise HTTPException(502, f"{type(e).__name__}: {str(e)[:200]}")

    out = {"answer": res["answer"]}
    if req.include_steps:
        out["queries"] = [{
            "tool": s["tool"],                 # run_cypher(지식 그래프) 또는 run_sql(실측 데이터)
            "query": s["query"],
            "params": s.get("params"),         # run_sql 바인딩 값
            "purpose": s.get("purpose"),
            "row_count": s["result"].get("row_count"),
            "truncated": s["result"].get("truncated"),
            "error": s["result"].get("error"),
        } for s in res["steps"] if s["tool"] in ("run_cypher", "run_sql")]
    return out
