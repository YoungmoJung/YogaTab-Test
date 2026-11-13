import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D

# 한글 폰트 자동 선택
for _f in ["NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"]:
    if any(_f in f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = _f
        break
plt.rcParams["axes.unicode_minus"] = False
FONT = plt.rcParams["font.family"][0] if isinstance(plt.rcParams["font.family"], list) else plt.rcParams["font.family"]

COLORS = {"Process": "#4C78A8", "Equipment": "#F58518", "Material": "#54A24B",
          "Parameter": "#B279A2", "Product": "#E45756", "Defect": "#9D755D"}


def _nodes(d):
    return {(n.get("label"), n.get("name")) for n in d.get("nodes", []) if n.get("name")}


def _rels(d):
    return {(r.get("from"), r.get("type"), r.get("to")) for r in d.get("relationships", [])
            if r.get("from") and r.get("to")}


def compare_tables(a, b, names=("Gemini", "Claude")):
    """노드/관계를 표로 비교 (한쪽에만 있는 항목은 노란색 강조)"""
    def build(sa, sb, cols):
        rows = [list(x) + ["O" if x in sa else "", "O" if x in sb else ""]
                for x in sorted(sa | sb, key=lambda t: tuple(str(v) for v in t))]
        return pd.DataFrame(rows, columns=cols + list(names))

    def hl(row):
        diff = row[names[0]] != row[names[1]]
        return ["background-color: #fff3b0" if diff else ""] * len(row)

    na, nb, ra, rb = _nodes(a), _nodes(b), _rels(a), _rels(b)
    summary = pd.DataFrame({
        "항목": ["노드 수", "관계 수", "공통 노드", "공통 관계"],
        names[0]: [len(na), len(ra), len(na & nb), len(ra & rb)],
        names[1]: [len(nb), len(rb), len(na & nb), len(ra & rb)],
    })
    nodes = build(na, nb, ["라벨", "이름"]).style.apply(hl, axis=1)
    rels = build(ra, rb, ["시작", "관계", "끝"]).style.apply(hl, axis=1)
    return summary, nodes, rels


def _graph(d):
    # 같은 노드 쌍에 관계가 여러 개일 수 있으므로 간선마다 관계 타입 목록(types)을 보관
    G = nx.DiGraph()
    for n in d.get("nodes", []):
        if not n.get("name"):
            continue
        G.add_node(n["name"], label=n.get("label"))
    for r in d.get("relationships", []):
        if not r.get("from") or not r.get("to"):
            continue
        if G.has_edge(r["from"], r["to"]):
            types = G.edges[r["from"], r["to"]]["types"]
            if r.get("type") not in types:
                types.append(r.get("type"))
        else:
            G.add_edge(r["from"], r["to"], types=[r.get("type")])
    return G


def _rel_count(g):
    return sum(len(t) for _, _, t in g.edges(data="types"))


def draw_compare(a, b, names=("Gemini", "Claude"), figsize=(16, 7)):
    """두 결과를 같은 배치로 나란히 그림 (빨간 테두리 = 상대 모델에 없거나 라벨이 다른 노드)"""
    ga, gb = _graph(a), _graph(b)
    pos = nx.spring_layout(nx.compose(ga, gb), seed=42, k=1.5)
    xs = [p[0] for p in pos.values()] or [0]
    ys = [p[1] for p in pos.values()] or [0]
    xlim = (min(xs) - 0.3, max(xs) + 0.3)
    ylim = (min(ys) - 0.3, max(ys) + 0.3)
    fig, axes = plt.subplots(1, 2, figsize=figsize)
    for ax, g, other, title in zip(axes, (ga, gb), (gb, ga), names):
        nodes = list(g.nodes)
        colors = [COLORS.get(g.nodes[n].get("label"), "#BBBBBB") for n in nodes]
        borders = ["red" if n not in other
                   or other.nodes[n].get("label") != g.nodes[n].get("label")
                   else "white" for n in nodes]
        nx.draw_networkx_nodes(g, pos, ax=ax, nodelist=nodes, node_color=colors,
                               edgecolors=borders, linewidths=3, node_size=1800)
        nx.draw_networkx_labels(g, pos, ax=ax, font_family=FONT, font_size=9,
                                font_color="white")
        nx.draw_networkx_edges(g, pos, ax=ax, arrows=True, arrowsize=15,
                               node_size=1800, edge_color="#888888")
        edge_labels = {(u, v): " / ".join(str(x) for x in t)
                       for u, v, t in g.edges(data="types")}
        nx.draw_networkx_edge_labels(g, pos, ax=ax, font_family=FONT, font_size=7,
                                     edge_labels=edge_labels)
        ax.set_title(f"{title} (노드 {g.number_of_nodes()} / 관계 {_rel_count(g)})")
        ax.set_xlim(xlim); ax.set_ylim(ylim)  # 두 그림의 배치를 동일하게
        ax.axis("off")
    # 범례는 실제로 사용된 라벨만 표시 (색상 미지정 라벨은 회색)
    used = sorted({str(l) for g in (ga, gb) for _, l in g.nodes(data="label")})
    handles = [Line2D([0], [0], marker="o", color="w",
                      markerfacecolor=COLORS.get(l, "#BBBBBB"),
                      markersize=12, label=l) for l in used]
    if handles:
        fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False)
    plt.tight_layout(rect=(0, 0.06, 1, 1))
    plt.show()
