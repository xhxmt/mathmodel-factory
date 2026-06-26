# 假设检验 (Hypothesis Testing)

## 适用场景

- 比较两组 / 多组样本是否存在显著差异（均值、分布、比例）
- 检验变量间的相关性 / 独立性
- 验证某分布假设、某政策 / 处理是否有效
- CUMCM/MCM 中"数据分析""差异性论证""相关性挖掘"类小问的标配工具

不适用：
- 样本量极小（n < 5）且无分布信息 → 谨慎，改用 Bayesian 或精确检验
- 纯预测任务 → 回归 / 时间序列，而非检验
- 因果推断 → 需实验设计 / 工具变量 / DID，单纯检验只给相关不给因果

## 核心假设

1. **样本随机、独立**（最关键，违反则所有 p 值失效）
2. 参数检验（t / ANOVA）额外要求**近似正态**与**方差齐性**（不满足 → 非参数替代）
3. 卡方检验要求列联表**期望频数 ≥ 5**（否则用 Fisher 精确检验）
4. 多重比较时**未校正的 p 值会放大假阳性**

## 检验选择决策树

```
比较组数？
├─ 2 组，连续变量
│   ├─ 正态 + 方差齐 → 独立样本 t 检验 (ttest_ind)
│   ├─ 正态 + 方差不齐 → Welch t 检验 (equal_var=False)
│   ├─ 配对设计 → 配对 t 检验 (ttest_rel)
│   └─ 非正态 → Mann-Whitney U / Wilcoxon 符号秩
├─ ≥3 组，连续变量
│   ├─ 正态 → 单因素 ANOVA (f_oneway) + Tukey 事后
│   └─ 非正态 → Kruskal-Wallis
├─ 分类 × 分类 → 卡方独立性 (chi2_contingency) / Fisher 精确
└─ 相关性 → Pearson(线性,正态) / Spearman(单调,稳健)
```

## 数学形式

**独立样本 Welch t 统计量**：
$$t = \frac{\bar{X}_1 - \bar{X}_2}{\sqrt{s_1^2/n_1 + s_2^2/n_2}}$$

**效应量 Cohen's d**（必须报告，p 值只说"有没有差异"，d 说"差异多大"）：
$$d = \frac{\bar{X}_1 - \bar{X}_2}{s_{\text{pooled}}}$$
经验阈值：0.2 小、0.5 中、0.8 大。

**卡方独立性**：$\chi^2 = \sum_{i,j} \frac{(O_{ij}-E_{ij})^2}{E_{ij}}$，$E_{ij}=\frac{R_i C_j}{N}$。

## 求解工具

- `scipy.stats`：`ttest_ind / ttest_rel / f_oneway / mannwhitneyu / kruskal / chi2_contingency / shapiro / levene / pearsonr / spearmanr`
- `statsmodels.stats.multitest.multipletests`：多重比较校正（Bonferroni / FDR）
- `statsmodels.stats.anova` + `pingouin`：事后检验（Tukey HSD）、效应量一站式

## 代码模板

```python
"""
hypothesis_testing_template.py — 假设检验完整流程:
前置检验(正态/方差齐) → 选检验 → 效应量 → 多重比较校正
用法: python hypothesis_testing_template.py
"""
import numpy as np
from scipy import stats

np.random.seed(42)
a = np.random.normal(50, 10, 30)
b = np.random.normal(56, 10, 30)
c = np.random.normal(52, 11, 30)

# ---- 1. 前置:正态性 (Shapiro-Wilk) ----
for name, x in [("a", a), ("b", b)]:
    w, p = stats.shapiro(x)
    print(f"Shapiro {name}: W={w:.3f} p={p:.4f} -> {'正态' if p > 0.05 else '非正态'}")

# ---- 2. 前置:方差齐性 (Levene) ----
_, p_lev = stats.levene(a, b)
equal_var = p_lev > 0.05
print(f"Levene p={p_lev:.4f} -> {'方差齐' if equal_var else '方差不齐(用 Welch)'}")

# ---- 3. 两组比较:t 检验 (按方差齐性自动切 Welch) + 效应量 ----
t, p = stats.ttest_ind(a, b, equal_var=equal_var)
sp = np.sqrt((a.std(ddof=1) ** 2 + b.std(ddof=1) ** 2) / 2)
cohen_d = (a.mean() - b.mean()) / sp
print(f"t={t:.3f} p={p:.4f} Cohen_d={cohen_d:.3f} "
      f"({'显著' if p < 0.05 else '不显著'}, 效应{'大' if abs(cohen_d) > 0.8 else '中' if abs(cohen_d) > 0.5 else '小'})")

# ---- 4. 非参数替代 (非正态时优先看这个) ----
u, p_u = stats.mannwhitneyu(a, b)
print(f"Mann-Whitney U={u:.1f} p={p_u:.4f}")

# ---- 5. 多组:ANOVA (正态) / Kruskal (非正态) ----
f, p_anova = stats.f_oneway(a, b, c)
h, p_kw = stats.kruskal(a, b, c)
print(f"ANOVA F={f:.3f} p={p_anova:.4f}; Kruskal H={h:.3f} p={p_kw:.4f}")

# ---- 6. 分类独立性:卡方 ----
table = np.array([[30, 10], [18, 42]])
chi2, p_chi, dof, expected = stats.chi2_contingency(table)
print(f"卡方={chi2:.3f} p={p_chi:.4f} dof={dof} (期望最小={expected.min():.1f}"
      f"{', <5 应改 Fisher' if expected.min() < 5 else ''})")

# ---- 7. 多重比较校正 (做了 k 个检验必须校正) ----
try:
    from statsmodels.stats.multitest import multipletests
    pvals = [p, p_u, p_anova, p_chi]
    rej, p_corr, _, _ = multipletests(pvals, alpha=0.05, method="fdr_bh")
    print("FDR-BH 校正后显著性:", list(zip(np.round(pvals, 4), rej)))
except ImportError:
    print("(statsmodels 未安装,跳过多重比较校正)")
```

## 常见陷阱

1. **不做前置检验直接 t 检验**：正态/方差齐性未验证就用参数检验，是评委明确扣分点。
2. **只报 p 值不报效应量**：大样本下微小差异也"显著"，必须用 Cohen's d / η² 说明实际意义。
3. **多重比较不校正**：做 20 个检验，期望 1 个假阳性（α=0.05）。必须 Bonferroni / FDR。
4. **把"不显著"当作"无差异"**：p > 0.05 只是"证据不足"，不等于接受原假设（功效不足时尤甚）。
5. **配对设计用独立样本检验**：前后测、同一对象重复测量必须用配对检验，否则功效损失。
6. **卡方期望频数 < 5 仍用卡方**：应改 Fisher 精确检验。
7. **p 值操纵 (p-hacking)**：反复换检验 / 删异常值直到 p < 0.05。论文要预先声明分析方案。

## 在建模比赛中的典型应用

- 数据分析类子问题的"差异性论证"（不同组别 / 时段 / 处理是否有显著差异）
- 相关性分析作为后续回归 / 建模的前置筛选
- 模型残差检验（正态性、自相关）与本方法共用工具
- 真实案例：CUMCM 数据挖掘类题目（医学、社会、农业数据）几乎都需要差异性 / 相关性检验作为论证支柱

## 参考文献

- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 概率统计相关章节.
- Wasserman, L. (2004). *All of Statistics*. Springer.
- scipy.stats documentation: https://docs.scipy.org/doc/scipy/reference/stats.html
- Cohen, J. (1988). *Statistical Power Analysis for the Behavioral Sciences* (2nd ed.). Routledge.
