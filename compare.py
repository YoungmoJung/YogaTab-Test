import pandas as pd
import networkx as nx
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D

# Font Autoselect
for _f in ["NanumGothic", "Noto Sans CJK KR", "Noto Sans KR"]:
    if any(_f in f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"]=_f
        break
