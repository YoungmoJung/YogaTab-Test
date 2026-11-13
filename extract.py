import json
import pandas as pd
from llm_client import chat
from graph_store import ALLOWED_LABELS, ALLOWED_RELS

SYSTEM=f"""너는 제조공정 지식을 그래프로 구조화하는 도우미다.
노드 라벨은 {sorted(ALLOWED_LABELS)} 중에서만 사용한다.
관계 타입은 {sorted(ALLOWED_RELS)} 중에서만 사용한다.
문서에 없는 내용은 추측해서 만들지 않는다.

각 노드와 관계에는 다음 두 항목을 함께 작성한다.
- evidence : 근거가 된 원문 문장(또는 구절)을 그대로 옮긴다.
- reason : 이 라벨/관계 타입을 선택한 이유를 한 문장으로 설명한다.

마지막으로 summary 에 전체 추출 결과를 2~3문장으로 요약하고,
판단이 애매했던 점이 있으면 notes 에 적는다. (없으면 빈 문자열)

설명이나 코드블록 없이 아래 형식의 JSON 만 출력한다.
한글 등 모든 문자는 \\u 이스케이프 없이 원래 글자 그대로 출력한다.
{{"nodes":[{{"label":"Process", "name":"사출", "props": {{}},
            "evidence": "...", "reason": "..."
}}],
"relationships":[{{"from":"건조", "from_label":"Process", "type":"NEXT", 
                "to":"사출","to_label":"Process",
                "evidence": "...", "reason": "..."}}],
"summary": "...", "notes": "..."
}}
"""

def _parse(raw):
    # 코드블록(```json)이나 앞뒤 설명문이 붙어도 JSON 부분만 잘라서 파싱
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없음: {raw[:200]}")
    return json.loads(raw[start:end + 1])


def extract(text, model=None, max_tokens=8192, timeout=180, retries=2):
    """retries: JSON 파싱 실패 시 다시 요청하는 횟수 (모델 출력이 간헐적으로 깨지는 경우 대비)"""
    # evidence/reason 때문에 출력이 길어지므로 max_tokens·timeout을 넉넉하게 설정
    for attempt in range(retries + 1):
        raw = chat(text, system=SYSTEM, model=model, max_tokens=max_tokens,
                   timeout=timeout, json_mode=True) or ""
        try:
            return _parse(raw)
        except ValueError as e:  # JSONDecodeError도 ValueError의 하위 클래스
            if attempt == retries:
                raise
            print(f"[재시도 {attempt + 1}/{retries}] JSON 파싱 실패: {e}")

def explain(data, title=None):
    """추출 결과를 요약 + 노드 표 + 관계 표로 보기 좋게 출력"""
    from IPython.display import display, Markdown
    head = f"### {title}\n\n" if title else ""
    md = head + f"**요약:** {data.get('summary','-')}"
    if data.get("notes"):
        md += f"\n\n**판단이 애매했던 점:** {data['notes']}"
    display(Markdown(md))

    nodes = pd.DataFrame([{
        "라벨": n.get("label"), "이름": n.get("name"),
        "근거 문장": n.get("evidence", ""), "선택 이유": n.get("reason", ""),
    } for n in data.get("nodes",[])])

    rels = pd.DataFrame([{
        "시작": n.get("from"), "관계": n.get("type"), "끝": n.get("to"),
        "근거 문장": n.get("evidence", ""), "선택 이유": n.get("reason", ""),        
    } for n in data.get("relationships",[])])

    style = {"text-align": "left", "white-space": "pre-wrap"}
    if not nodes.empty:
        display(nodes.style.set_properties(**style))
    if not rels.empty:
        display(rels.style.set_properties(**style))
