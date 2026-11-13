import json
from llm_client import chat
from graph_store import ALLOWED_LABELS, ALLOWED_RELS

SYSTEM=f"""너는 제조공정 지식을 그래프로 구조화하는 도우미다.
노드 라벨은 {sorted(ALLOWED_LABELS)} 중에서만 사용한다.
관계 타입은 {sorted(ALLOWED_RELS)} 중에서만 사용한다.
문서에 없는 내용은 추측해서 만들지 않는다.
설명이나 코드블록 없이 아래 형식의 JSON 만 출력한다.
{{"nodes":[{{"label":"Process", "name":"사출", "props": {{}}}}],
"relationships":[{{"from":"건조", "from_label":"Process", "type":"NEXT", 
                "to":"사출","to_label":"Process"}}]
}}
"""

def extract(text, model=None):
    raw = chat(text, system=SYSTEM, model=model)
    # 코드블록(```json)이나 앞뒤 설명문이 붙어도 JSON 부분만 잘라서 파싱
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"JSON을 찾을 수 없음: {raw[:200]}")
    return json.loads(raw[start:end + 1])
