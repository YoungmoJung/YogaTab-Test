"""지식 그래프 조회 Agent
질문(LLM) -> Cypher 조회(Tool) -> 조회 결과 기반 답변(LLM)

사용 예)
    from graph_agent import ask_graph, show
    res = ask_graph("사출성형 공정은 어떤 순서로 진행돼?")
    show(res)
"""
import json
import re
from itertools import islice

from litellm import completion
from neo4j import READ_ACCESS, unit_of_work

import llm_client
from graph_store import GraphStore

MAX_ROWS = 50  # 한 번에 LLM에게 넘길 최대 행 수 (토큰 절약)
QUERY_TIMEOUT = 10  # 쿼리 1회 실행 제한 시간(초). 넘으면 오류로 LLM에게 전달

SYSTEM = """너는 제조공정 지식 그래프(Neo4j)를 조회해서 질문에 답하는 도우미다.
작업 순서:
1. 먼저 get_schema 도구로 라벨, 관계 타입, 속성, 노드 이름 예시를 확인한다.
2. run_cypher 도구로 읽기 전용 Cypher 쿼리를 실행한다.
    - 노드 이름은 name 속성에 있다. 이름이 정확히 일치하지 않을 수 있으니
      필요하면 CONTAINS 로 찾는다.
    - 근거 문장은 노드/관계의 evidence 속성(리스트)에 있다.
    - 결과가 비어 있으면 조건을 바꿔 한두 번 더 시도한다.
3. 조회 결과에 근거해서만 한국어로 답한다. 조회 결과에 없는 내용은 지어내지 않고
   "지식 DB에서 찾을 수 없습니다"라고 답한다.
4. 답변 끝에 근거가 된 evidence 문장이 있으면 함께 제시한다."""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_schema",
        "description": "지식 그래프의 노드 라벨, 관계 패턴, 속성 키, 라벨별 노드 이름 예시를 반환한다.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "run_cypher",
        "description": "읽기 전용 Cypher 쿼리를 실행하고 결과 행을 JSON으로 반환한다. "
                       "CREATE/MERGE/DELETE/SET 등 쓰기 쿼리는 거부된다.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "실행할 Cypher 쿼리"},
            "purpose": {"type": "string", "description": "이 쿼리로 확인하려는 내용 (한 문장)"},
        }, "required": ["query"]},
    }},
]

# 쓰기 키워드 1차 차단 (2차로 읽기 전용 트랜잭션에서 한 번 더 막힘)
_WRITE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|FOREACH)\b"
                    r"|CALL\s+(dbms|db\.create|apoc\.(create|merge|refactor|periodic))",
                    re.IGNORECASE)


@unit_of_work(timeout=QUERY_TIMEOUT)
def _read(tx, query, limit):
    result = tx.run(query)
    if limit is None:
        return result.data()
    # 필요한 행만 가져오고 나머지는 받지 않음 (큰 결과로 인한 메모리/시간 낭비 방지)
    return [rec.data() for rec in islice(result, limit)]


def _rows(store, query, limit=None):
    with store.driver.session(default_access_mode=READ_ACCESS) as s:
        return s.execute_read(_read, query, limit)


def get_schema(store):
    labels = _rows(store, "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS count "
                          "ORDER BY count DESC")
    patterns = _rows(store, "MATCH (a)-[r]->(b) RETURN labels(a)[0] AS from, type(r) AS rel, "
                            "labels(b)[0] AS to, count(*) AS count ORDER BY count DESC")
    props = _rows(store, "MATCH (n) UNWIND keys(n) AS k "
                         "RETURN labels(n)[0] AS label, collect(DISTINCT k) AS keys")
    names = _rows(store, "MATCH (n) WITH labels(n)[0] AS label, n.name AS name ORDER BY name "
                         "RETURN label, collect(name)[0..30] AS examples")
    return {"labels": labels, "relationship_patterns": patterns,
            "node_properties": props, "name_examples": names}


def run_cypher(store, query):
    if _WRITE.search(query):
        return {"error": "쓰기 쿼리는 허용되지 않습니다. 읽기 전용 쿼리만 사용하세요."}
    try:
        # MAX_ROWS보다 1행 더 가져와서 잘렸는지 판단
        rows = _rows(store, query, limit=MAX_ROWS + 1)
    except Exception as e:  # 문법 오류, 시간 초과 등은 LLM이 보고 고칠 수 있도록 그대로 전달
        return {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    truncated = len(rows) > MAX_ROWS
    # truncated=True면 row_count는 전체 개수가 아니라 MAX_ROWS (실제로는 그 이상)
    return {"row_count": min(len(rows), MAX_ROWS), "rows": rows[:MAX_ROWS],
            "truncated": truncated}


def ask_graph(question, model=None, max_steps=6, verbose=True, temperature=None):
    """temperature: None이면 모델 기본값 사용 (claude-sonnet-5-5 등 1만 허용하는 모델이 있음)
    반환값: {"answer": 답변, "steps": [{"tool", "query", "purpose", "result"}...]}"""
    name = model or llm_client.DEFAULT
    opts = dict(getattr(llm_client, "MODEL_OPTIONS", {}).get(name, {}))
    if temperature is not None:
        opts["temperature"] = temperature
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": question}]
    steps = []
    store = GraphStore()
    try:
        for i in range(max_steps + 1):
            last = i == max_steps  # 마지막 차례에는 도구 없이 답변만 하도록 강제
            resp = completion(model=llm_client.resolve(name), messages=messages,
                              tools=TOOLS, tool_choice="none" if last else "auto",
                              timeout=120, num_retries=3, **opts)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return {"answer": msg.content or "(모델이 빈 응답을 반환했습니다)",
                        "steps": steps}

            messages.append(msg)
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if tc.function.name == "get_schema":
                    result = get_schema(store)
                elif tc.function.name == "run_cypher":
                    result = run_cypher(store, args.get("query", ""))
                else:
                    result = {"error": f"알 수 없는 도구: {tc.function.name}"}

                steps.append({"tool": tc.function.name, "query": args.get("query"),
                              "purpose": args.get("purpose"), "result": result})
                if verbose:
                    _print_step(len(steps), steps[-1])
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "name": tc.function.name,
                                 "content": json.dumps(result, ensure_ascii=False, default=str)})
        return {"answer": "(답변 생성 실패: 최대 단계 초과)", "steps": steps}
    finally:
        store.close()


def _print_step(n, st):
    if st["tool"] == "get_schema":
        print(f"[{n}] 스키마 확인")
        return
    r = st["result"]
    status = r.get("error") or (f"{r.get('row_count', 0)}행 이상 (잘림)" if r.get("truncated")
                                else f"{r.get('row_count', 0)}행")
    print(f"[{n}] {st.get('purpose') or 'Cypher 실행'} -> {status}\n"
          f"{st['query']}")


def show(res):
    """노트북에서 답변 + 실행된 쿼리 + 조회 결과를 보기 좋게 출력"""
    import pandas as pd
    from IPython.display import display, Markdown
    display(Markdown(f"### 답변\n\n{res['answer']}"))
    queries = [st for st in res["steps"] if st["tool"] == "run_cypher"]
    for i, st in enumerate(queries, 1):
        display(Markdown(f"**쿼리 {i}** - {st.get('purpose') or ''}\n```cypher\n{st['query']}\n```"))
        r = st["result"]
        if r.get("error"):
            display(Markdown(f"> 오류: {r['error']}"))
        elif not r.get("rows"):
            display(Markdown("> 조회 결과 없음 (0행)"))
        else:
            if r.get("truncated"):
                display(Markdown(f"> 결과가 많아 상위 {MAX_ROWS}행만 표시합니다."))
            display(pd.DataFrame(r["rows"]))
