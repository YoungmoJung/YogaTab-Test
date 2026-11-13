import os
from neo4j import GraphDatabase

#제조공정 스키마 (작업하면서 확장)
ALLOWED_LABELS = {"Process", "Equipment", "Material", "Parameter", "Defect"}
ALLOWED_RELS = {"NEXT", "USES", "CONSUMES", "PRODUCES", "HAS_PARAMETER", "CAUSES"}

# evidence/reason을 같은 위치끼리 짝을 이루는 리스트로 누적하는 Cypher 구문 (노드/관계 공용)
#  - 새 근거 문장이면 evidence, reason에 함께 추가
#  - 이미 있는 근거 문장이면 추가하지 않고, 그 자리의 reason이 비어 있을 때만 채움
#  - reason이 없던 기존 데이터는 evidence 개수만큼 ''로 채워 짝을 맞춤
#  - 근거 문장이 없으면($ev = '') 아무것도 바꾸지 않음
def _evidence_clause(var):
    return (f"WITH {var}, coalesce({var}.evidence, []) AS evs, "
            f"     coalesce({var}.reason, [x IN coalesce({var}.evidence, []) | '']) AS rss "
            f"WITH {var}, evs, rss, "
            f"     [i IN range(0, size(evs) - 1) WHERE evs[i] = $ev][0] AS idx "
            f"SET {var}.evidence = CASE WHEN $ev = '' OR idx IS NOT NULL "
            f"                          THEN evs ELSE evs + $ev END, "
            f"    {var}.reason = CASE "
            f"      WHEN $ev = '' THEN rss "
            f"      WHEN idx IS NULL THEN rss + $rs "
            f"      WHEN rss[idx] = '' THEN "
            f"        [i IN range(0, size(rss) - 1) | CASE WHEN i = idx THEN $rs ELSE rss[i] END] "
            f"      ELSE rss END ")


def _text(v):
    return v.strip() if isinstance(v, str) else ""


class GraphStore:
    def __init__(self):
        self.driver = GraphDatabase.driver(
            os.environ["NEO4J_URI"],
            auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))
        self.driver.verify_connectivity()

    def close(self):
        self.driver.close()

    def setup(self):
        with self.driver.session() as s:
            for label in ALLOWED_LABELS:
                s.run(f"CREATE CONSTRAINT {label.lower()}_name IF NOT EXISTS "
                      f"FOR (n:{label}) REQUIRE n.name IS UNIQUE")
    
    def upsert(self, data):
        """노드/관계를 저장하고, 건너뛴 항목을 담은 결과 요약을 반환"""
        result = {"nodes": 0, "relationships": 0,
                  "skipped_nodes": [], "skipped_relationships": []}
        with self.driver.session() as s:
            for n in data.get("nodes", []):
                if n.get("label") not in ALLOWED_LABELS or not n.get("name"):
                    result["skipped_nodes"].append(n)
                    continue
                # name은 MERGE 키, evidence/reason은 리스트로 따로 관리하므로 props에서 제외
                props = {k: v for k, v in n.get("props", {}).items()
                         if k not in ("name", "evidence", "reason")
                         and isinstance(v, (str, int, float, bool))}
                s.run(f"MERGE (n:{n['label']} {{name: $name}}) SET n += $props "
                      + _evidence_clause("n"),
                      name=n["name"], props=props,
                      ev=_text(n.get("evidence")), rs=_text(n.get("reason"))).consume()
                result["nodes"] += 1

            for r in data.get("relationships", []):
                if (r.get("type") not in ALLOWED_RELS
                        or r.get("from_label") not in ALLOWED_LABELS
                        or r.get("to_label") not in ALLOWED_LABELS
                        or not r.get("from") or not r.get("to")):
                    result["skipped_relationships"].append(r)
                    continue
                # MATCH 실패(노드 없음) 시 행이 0개라 matched=0 → 누락으로 기록
                rec = s.run(f"MATCH (a:{r['from_label']} {{name: $a}}) "
                            f"MATCH (b:{r['to_label']} {{name: $b}}) "
                            f"MERGE (a)-[rel:{r['type']}]->(b) "
                            + _evidence_clause("rel")
                            + "RETURN count(*) AS matched",
                            a=r["from"], b=r["to"],
                            ev=_text(r.get("evidence")), rs=_text(r.get("reason"))).single()
                if rec and rec["matched"] > 0:
                    result["relationships"] += 1
                else:
                    result["skipped_relationships"].append(r)

        if result["skipped_nodes"] or result["skipped_relationships"]:
            print(f"[경고] 건너뜀 - 노드 {len(result['skipped_nodes'])}개, "
                  f"관계 {len(result['skipped_relationships'])}개")
        return result