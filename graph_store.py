import os
from neo4j import GraphDatabase

#제조공정 스키마 (작업하면서 확장)
ALLOWED_LABELS = {"Process", "Equipment", "Material", "Parameter"}
ALLOWED_RELS = {"NEXT", "USES", "CONSUMES", "PRODUCES", "HAS_PARAMETER", "CAUSES"}

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
                # name은 MERGE 키이므로 props로 덮어쓰지 않도록 제외
                props = {k: v for k, v in n.get("props", {}).items()
                         if k != "name" and isinstance(v, (str, int, float, bool))}
                s.run(f"MERGE (n:{n['label']} {{name: $name}}) SET n += $props",
                      name=n["name"], props=props).consume()
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
                            f"MERGE (a)-[:{r['type']}]->(b) "
                            f"RETURN count(*) AS matched",
                            a=r["from"], b=r["to"]).single()
                if rec and rec["matched"] > 0:
                    result["relationships"] += 1
                else:
                    result["skipped_relationships"].append(r)

        if result["skipped_nodes"] or result["skipped_relationships"]:
            print(f"[경고] 건너뜀 - 노드 {len(result['skipped_nodes'])}개, "
                  f"관계 {len(result['skipped_relationships'])}개")
        return result