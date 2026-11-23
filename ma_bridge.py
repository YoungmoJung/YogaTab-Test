"""Claude Managed Agents <-> 태블릿 Neo4j 브리지

Agent 루프(판단·답변)는 Anthropic 클라우드에서 실행되고,
지식 그래프 조회(custom tool)만 이 스크립트가 태블릿에서 실행해 결과를 돌려준다.
태블릿의 포트를 외부에 열 필요가 없다.

사용:
  python ma_bridge.py setup                  # 최초: Agent·Environment 생성 (ID를 ~/.ma_ids.json에 저장)
                                             # 이후: 기존 Agent를 현재 프롬프트·도구·모델로 수정 (버전 증가)
  python ma_bridge.py ask "질문"             # 세션 생성 → 질문 → 도구 처리 → 답변 출력 → 세션 삭제
  python ma_bridge.py ask "질문" --keep      # 세션을 지우지 않고 남김 (Console에서 기록 확인용)
  python ma_bridge.py attach <session_id>    # (실험) 다른 곳에서 시작한 세션의 도구 호출을 처리
"""
import json
import os
import sys
from pathlib import Path

import anthropic

import graph_agent
from graph_agent import SYSTEM, run_tool
from graph_store import GraphStore

IDS_FILE = Path.home() / ".ma_ids.json"
MODEL = os.getenv("MA_MODEL", "claude-haiku-5-5")

# 도구 정의는 graph_agent.TOOLS(OpenAI/LiteLLM 형식)를 Managed Agents custom tool 형식으로 변환해 공유
# → graph_agent에 도구를 추가·수정하면 여기도 자동 반영 (단, 클라우드 Agent에는 setup을 다시 실행해야 반영)
TOOLS = [{"type": "custom", "name": t["function"]["name"],
          "description": t["function"]["description"],
          "input_schema": t["function"]["parameters"]}
         for t in graph_agent.TOOLS]

client = anthropic.Anthropic()  # ANTHROPIC_API_KEY 사용 (Console 크레딧)


def setup():
    """처음에는 Agent·Environment를 새로 만들고, 이후에는 기존 Agent를 수정(버전 증가)한다."""
    if IDS_FILE.exists():
        ids = json.loads(IDS_FILE.read_text())
        agent = client.beta.agents.update(
            ids["agent"], model=MODEL, system=SYSTEM, tools=TOOLS)
        print(f"Agent 수정: {agent.id} (version {agent.version})")
        return

    agent = client.beta.agents.create(
        name="mfg-graph-agent",
        description="제조공정 지식 그래프(Neo4j) 조회 Agent. 조회는 태블릿에서 실행된다.",
        model=MODEL, system=SYSTEM, tools=TOOLS)
    env = client.beta.environments.create(
        name="mfg-graph-env",
        # API는 networking을 생략하면 unrestricted가 기본값이므로 반드시 limited로 지정
        config={"type": "cloud", "networking": {"type": "limited", "allowed_hosts": []}})
    IDS_FILE.write_text(json.dumps({"agent": agent.id, "environment": env.id}))
    print(f"Agent: {agent.id} (version {agent.version})\nEnvironment: {env.id}\n저장 위치: {IDS_FILE}")


def _ids():
    if not IDS_FILE.exists():
        sys.exit("먼저 'python ma_bridge.py setup'을 실행하세요.")
    return json.loads(IDS_FILE.read_text())


def serve_session(session_id, question=None):
    """세션 이벤트를 받아 custom tool 호출을 처리. question이 있으면 보내고 답변이 끝나면 종료."""
    store = GraphStore()
    answer = []
    try:
        with client.beta.sessions.events.stream(session_id) as stream:
            if question:  # 스트림을 연 뒤에 질문을 보내야 이후 이벤트를 모두 받음
                client.beta.sessions.events.send(session_id, events=[
                    {"type": "user.message", "content": [{"type": "text", "text": question}]}])
            for event in stream:
                if event.type == "agent.custom_tool_use":
                    args = event.input or {}
                    result = run_tool(store, event.name, args)  # graph_agent와 같은 도구 실행 함수
                    status = (result.get("error") or
                              (f"{result['row_count']}행" if "row_count" in result else "완료"))
                    stmt = args.get("query") or args.get("sql")  # Cypher 또는 SQL
                    print(f"[도구] {event.name} -> {status}"
                          + (f"\n      {stmt}" if stmt else "")
                          + (f"\n      params: {args['params']}" if args.get("params") else ""))
                    client.beta.sessions.events.send(session_id, events=[{
                        "type": "user.custom_tool_result",
                        "custom_tool_use_id": event.id,
                        "content": [{"type": "text",
                                     "text": json.dumps(result, ensure_ascii=False, default=str)}],
                    }])
                elif event.type == "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            answer.append(block.text)
                            print(block.text, end="", flush=True)
                elif event.type == "session.error":
                    print(f"\n[세션 오류] {event.error.message}")
                elif event.type == "session.status_idle":
                    reason = event.stop_reason.type if event.stop_reason else None
                    if question and reason in ("end_turn", "budget_reached"):
                        break
    finally:
        store.close()
    print()
    return "".join(answer)


def ask(question, keep=False):
    ids = _ids()
    session = client.beta.sessions.create(
        agent=ids["agent"], environment_id=ids["environment"],
        title=question[:60])
    print(f"세션: {session.id}")
    try:
        return serve_session(session.id, question)
    finally:
        if not keep:  # 세션 기록은 삭제할 때까지 서버에 남으므로 기본은 삭제
            try:
                client.beta.sessions.delete(session.id)
            except Exception as e:
                print(f"[안내] 세션 자동 삭제 실패({type(e).__name__}). Console에서 직접 삭제하세요.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        setup()
    elif cmd == "ask" and len(sys.argv) > 2:
        ask(sys.argv[2], keep="--keep" in sys.argv)
    elif cmd == "attach" and len(sys.argv) > 2:
        print("도구 호출 대기 중... (종료: Ctrl + C)")
        serve_session(sys.argv[2])
    else:
        print(__doc__)
