"""지식 그래프 조회 Agent
질문(LLM) -> Cypher 조회(Tool) -> SQL 조회 (실측값) -> 조회 결과 기반 답변(LLM)

사용 예)
    from graph_agent import ask_graph, show
    res = ask_graph("사출성형 공정은 어떤 순서로 진행돼?")
    res = ask_graph("11월 10일 ~12일 3호기 금형온도가 규격을 벗어났어?")
    show(res)
"""
import json
import re
from itertools import islice

from litellm import completion
from neo4j import READ_ACCESS, unit_of_work

import llm_client
import rdb_client
from graph_store import GraphStore

MAX_ROWS = 50  # 한 번에 LLM에게 넘길 최대 행 수 (토큰 절약)
QUERY_TIMEOUT = 10  # 쿼리 1회 실행 제한 시간(초). 넘으면 오류로 LLM에게 전달

SYSTEM = """너는 제조공정 지식 그래프(Neo4j)와 실측 데이터(RDB)를 조회해서 질문에 답하는 도우미다.
작업 순서:

[지식 조회]
1. 먼저 get_schema 도구로 라벨, 관계 타입, 속성, 노드 이름 예시를 확인한다.
2. run_cypher 도구로 읽기 전용 Cypher 쿼리를 실행한다.
    - 노드 이름은 name 속성에 있다. 이름이 정확히 일치하지 않을 수 있으니
      필요하면 CONTAINS 로 찾는다.
    - 근거 문장은 노드/관계의 evidence 속성(리스트)에 있다.
    - 불량 발생 조건 등 특정 관계에서만 성립하는 조건은 관계 속성
      (condition, value, unit)에 있다. 노드의 value 는 관리 기준값이다.
    - 결과가 비어 있으면 조건을 바꿔 한두 번 더 시도한다.

[실측 데이터 조회] 질문에 측정값, 기간, 설비(호기), 추이, 규격이탈 등이 포함된 경우
3. run_cypher 도구로 데이터 위치(매핑)를 찾는다. 경로:
    (x:Parameter 또는 :Defect)-[m:MEASURED_AS]->(i:InspectionItem)-[:STORED_IN]->(:Column)-[:BELONGS_TO]->(:Table)
    - 검사항목 i: i.code(검사항목 코드), i.table, i.code_column, i.value_column(측정값),
      i.time_column(시각), i.line_column(설비 컬럼), i.denominator_column(비율의 분모),
      i.key_columns, i.unit, i.lsl/i.usl(규격 하한/상한), i.sql_template(설비 조건 없음)
    - 매핑 관계 m: m.line(설비), m.valid_from/m.valid_to(적용 기간), m.note,
      m.sql_template(설비 조건 AND <line_column> = :line 포함)
    - 반드시 m 의 속성도 함께 조회한다. 예:
      MATCH (x {name:'금형 온도'})-[m:MEASURED_AS]->(i:InspectionItem)
      RETURN m.line, m.valid_from, m.sql_template, i.code, i.lsl, i.usl, i.unit, i.line_column
    - m.line 이 질문의 설비와 같은 매핑을 우선 사용하고, 없으면 m.line 이 빈 문자열('')인
      공통 매핑을 사용한다. 설비별로 코드가 다를 수 있다 (예: 3호기만 다른 코드).
4. run_sql 로 조회한다. m.sql_template(설비 지정 시) 또는 i.sql_template 을 바탕으로 작성하고,
   코드·설비·기간 값은 반드시 params 로 전달한다 (자리표시자: :code, :line, :from_dt, :to_dt).
    - 기간은 'YYYY-MM-DD' 문자열, :to_dt 는 마지막 날의 다음 날 (예: 11/10~11/12 → '2026-11-13').
    - 공통 매핑(m.line='')으로 특정 설비를 볼 때는 i.line_column 조건을 직접 추가한다.
    - get_schema 의 data_tables 에 있는 테이블만 조회할 수 있다. 여러 테이블은 JOIN ... ON 으로 연결한다.
    - 원시 데이터를 많이 가져오지 말고 AVG/MIN/MAX/COUNT/SUM 과 GROUP BY 로 집계한다.
    - SQL 문법은 get_schema 의 sql_dialect 를 따른다.
5. 측정값은 lsl/usl 규격, 그리고 지식 그래프의 조건(관계 속성 condition/value)과 비교해서 설명한다.
   불량률은 value_column / denominator_column 으로 계산한다.

[답변]
6. 조회 결과에 근거해서만 한국어로 답한다. 조회 결과에 없는 내용은 지어내지 않고
   "지식 DB에서 찾을 수 없습니다"라고 답한다.
7. 답변 끝에 근거가 된 evidence 문장이 있으면 함께 제시한다: 지식은 evidence 문장, 실측값은 조회한 테이블, 검사항목 코드, 기간"""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_schema",
        "description": "지식 그래프의 노드 라벨, 관계 패턴, 속성 키, 라벨별 노드 이름 예시와 "
                       "조회 가능한 실측 데이터 테이블 목록을 반환한다.",
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
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "실측 데이터 RDB 에 읽기 전용 SELECT 쿼리를 실행한다. "
                       "지식 그래프에 Table 로 등록된 데이터만 조회할 수 있고, 값은 :이름 형식의 "
                       f"바인딩 파라미터로 전달한다. 결과는 최대 {rdb_client.MAX_ROWS}행.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "실행할 SELECT 문 (값 자리는 :code 처럼 표기)"},
            "params": {"type": "object",
                       "description": '바인딩 파라미터 (예: {"code": "C3", "line": "3호기", '
                                      '"from_dt": "2026-11-10", "to_dt": "2026-11-13"})'},
            "purpose": {"type": "string", "description": "이 쿼리로 확인하려는 내용 (한 문장)"},
        }, "required": ["sql"]},
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


def _data_tables(store):
    """조회 허용 테이블 = 지식 그래프에 :Table 로 등록된 테이블 (load_mapping.py가 생성)"""
    return [r["name"] for r in _rows(store, "MATCH (t:Table) RETURN t.name AS name")]


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
            "node_properties": props, "name_examples": names,
            "data_tables": _data_tables(store), "sql_dialect":rdb_client.DIALECT}


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


def run_sql(store, sql, params=None):
    params = params if isinstance(params, dict) else {}
    try:
        allowed = _data_tables(store)
    except Exception as e:
        return {"error": f"허용 테이블 조회 실패: {type(e).__name__}: {str(e)[:200]}"}
    return rdb_client.run_sql(sql, params, allowed_tables=allowed)


def run_tool(store, name, args):
    """도구 실행 (graph_agent, ma_bridge 공용)"""
    args = args if isinstance(args, dict) else {}
    if name == "get_schema":
        return get_schema(store)
    if name == "run_cypher":
        return run_cypher(store, args.get("query", ""))
    if name == "run_sql":
        return run_sql(store, args.get("sql", ""), args.get("params"))
    return {"error": f"알 수 없는 도구: {name}"}


def ask_graph(question, model=None, max_steps=6, verbose=True, temperature=None):
    """temperature: None이면 모델 기본값 사용 (claude-sonnet-5-5 등 1만 허용하는 모델이 있음)
    반환값: {"answer": 답변, "steps": [{"tool", "query", "purpose", "result"}...]}"""
    name = llm_client.canonical(model)  # 대소문자 구분 없이 MODELS 이름으로 맞춤
    opts = llm_client.options(name)
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
                result = run_tool(store, tc.function.name, args)
                # query: Cypher 또는 SQL 문 (graph_api·show 공용), params: run_sql 의 바인딩 값
                steps.append({"tool": tc.function.name,
                              "query": args.get("query") or args.get("sql"),
                              "params": args.get("params"),
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
    kind = "SQL" if st["tool"] == "run_sql" else "Cypher"
    params = f"\n  params: {st['params']}" if st.get("params") else ""
    print(f"[{n}] ({kind}) {st.get('purpose') or kind + ' 실행'} -> {status}\n"
          f"{st['query']}{params}")


def show(res):
    """노트북에서 답변 + 실행된 쿼리(Cypher·SQL) + 조회 결과를 보기 좋게 출력"""
    import pandas as pd
    from IPython.display import display, Markdown
    display(Markdown(f"### 답변\n\n{res['answer']}"))
    queries = [st for st in res["steps"] if st["tool"] in ("run_cypher", "run_sql")]
    for i, st in enumerate(queries, 1):
        lang = "sql" if st["tool"] == "run_sql" else "cypher"
        params = f"\n\nparams: `{st['params']}`" if st.get("params") else ""
        display(Markdown(f"**쿼리 {i}** ({lang.upper()}) - {st.get('purpose') or ''}\n"
                         f"```{lang}\n{st['query']}\n```{params}"))
        r = st["result"]
        if r.get("error"):
            display(Markdown(f"> 오류: {r['error']}"))
        elif not r.get("rows"):
            display(Markdown("> 조회 결과 없음 (0행)"))
        else:
            if r.get("truncated"):
                display(Markdown(f"> 결과가 많아 상위 {MAX_ROWS}행만 표시합니다."))
            display(pd.DataFrame(r["rows"]))
