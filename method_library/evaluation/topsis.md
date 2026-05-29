# 优劣解距离法 (TOPSIS)

> Technique for Order Preference by Similarity to Ideal Solution

## 适用场景

- 给定 $m$ 个待评方案、$n$ 个评价指标，需要**排序**（不是只选最优）
- 权重已知或可获得（AHP、熵权、CRITIC、组合赋权均可）
- 指标可量化（连续或顺序），可被同向化

不适用：
- 指标含强非线性补偿关系（TOPSIS 是欧氏距离，假设可补偿）
- 方案数 < 3（没有排序意义）
- 指标含缺失值（TOPSIS 对缺失敏感，需先填补）

## 核心假设

1. 每个指标都能做"越大越好"或"越小越好"的单调判定
2. 加权欧氏距离对决策者偏好是合理表征
3. "正理想解"和"负理想解"在指标空间中可达（标准 TOPSIS 用实际最优 / 最差，不要求点真实存在）

## 数学形式

设原始矩阵 $X = (x_{ij})_{m \times n}$，$m$ 方案 × $n$ 指标，权重 $\mathbf{w} = (w_1, \ldots, w_n)$。

**步骤 1 — 同向化**：将极小型、中间型、区间型指标转为极大型。
- 极小型：$x'_{ij} = \max_k x_{kj} - x_{ij}$ 或 $x'_{ij} = 1/x_{ij}$（注意零值）
- 中间型 $x^*$：$x'_{ij} = 1 - |x_{ij}-x^*|/\max_k |x_{kj}-x^*|$
- 区间型 $[a,b]$：在区间内为 1，区间外按距离衰减

**步骤 2 — 向量归一化**：
$$z_{ij} = \frac{x'_{ij}}{\sqrt{\sum_{k=1}^m (x'_{kj})^2}}$$

**步骤 3 — 加权**：$v_{ij} = w_j \cdot z_{ij}$

**步骤 4 — 理想解**：
$$V^+_j = \max_i v_{ij}, \quad V^-_j = \min_i v_{ij}$$

**步骤 5 — 距离**：
$$D^+_i = \sqrt{\sum_j (v_{ij} - V^+_j)^2}, \quad D^-_i = \sqrt{\sum_j (v_{ij} - V^-_j)^2}$$

**步骤 6 — 相对接近度**：
$$C_i = \frac{D^-_i}{D^+_i + D^-_i} \in [0, 1]$$

$C_i$ 越大方案越优。

## 求解工具

`numpy` 即可。规模再大也是 $O(mn)$，无需求解器。

## 代码模板

```python
"""
topsis_template.py — TOPSIS 完整实现
用法: python topsis_template.py
"""
import numpy as np

def normalize_columns(X: np.ndarray,
                       directions: list[str],
                       optimum: list[float] | None = None) -> np.ndarray:
    """
    directions[j] in {"max", "min", "mid", "range"}
    optimum[j]: mid 型给目标值; range 型给 (lo, hi)
    """
    X = X.astype(float).copy()
    for j, d in enumerate(directions):
        col = X[:, j]
        if d == "max":
            continue
        elif d == "min":
            X[:, j] = col.max() - col
        elif d == "mid":
            x_star = optimum[j]
            denom = np.max(np.abs(col - x_star))
            X[:, j] = 1 - np.abs(col - x_star) / denom if denom > 0 else 1.0
        elif d == "range":
            lo, hi = optimum[j]
            denom = max(lo - col.min(), col.max() - hi, 1e-12)
            out = np.where((col >= lo) & (col <= hi), 1.0,
                  np.where(col < lo, 1 - (lo - col) / denom,
                                     1 - (col - hi) / denom))
            X[:, j] = out
        else:
            raise ValueError(f"unknown direction {d!r}")
    return X


def topsis(X: np.ndarray, weights: np.ndarray,
           directions: list[str], optimum: list | None = None) -> np.ndarray:
    """返回每个方案的相对接近度 C_i (越大越好)."""
    assert X.shape[1] == len(weights) == len(directions)
    weights = np.asarray(weights, dtype=float)
    weights = weights / weights.sum()  # 防御性归一化

    X_pos = normalize_columns(X, directions, optimum)
    # 向量归一化
    norm = np.sqrt((X_pos ** 2).sum(axis=0))
    norm = np.where(norm == 0, 1, norm)
    Z = X_pos / norm
    V = Z * weights

    V_plus = V.max(axis=0)
    V_minus = V.min(axis=0)
    D_plus = np.sqrt(((V - V_plus) ** 2).sum(axis=1))
    D_minus = np.sqrt(((V - V_minus) ** 2).sum(axis=1))
    C = D_minus / (D_plus + D_minus + 1e-12)
    return C


if __name__ == "__main__":
    # 4 方案 × 3 指标 (售价↓ 续航↑ 充电时间↓)
    X = np.array([
        [80, 400, 30],
        [70, 350, 25],
        [90, 450, 35],
        [60, 300, 20],
    ])
    weights = np.array([0.4, 0.4, 0.2])
    directions = ["min", "max", "min"]
    C = topsis(X, weights, directions)
    rank = np.argsort(-C) + 1
    for i, (c, r) in enumerate(zip(C, rank)):
        print(f"方案 {i+1}: C = {c:.4f}, rank = {r}")
```

## 常见陷阱

1. **同向化遗漏**：极小型/中间型直接进 TOPSIS 会得到反向排序。代码必须显式声明每个指标方向。
2. **向量归一化 vs min-max 归一化**：两种结果不同。论文需说明用哪种（TOPSIS 原始论文用向量法）。
3. **权重和指标方向耦合错误**：权重只表示相对重要性，正负方向另算。
4. **"理想解"是不是数据点**：标准 TOPSIS 的 V⁺ / V⁻ 是各指标列的极值，不必是某一具体方案。
5. **方案数 $m=2$ 时 $C_i$ 退化**：必然得到 0 和 1，没意义。
6. **零方差列**：会导致归一化分母为 0；代码须用 `np.where` 防御。
7. **与 AHP 串联时**：AHP 输出权重直接喂入 TOPSIS 是常见组合，但论文里要说明 AHP 的 CR 检验通过了。

## 在建模比赛中的典型应用

- 综合评价类题目的最常见排序工具
- 与熵权法组合（"熵权 TOPSIS"是国赛常见配方）做客观评价
- 多准则方案选优类子问题
- 注意：评委对 TOPSIS 已"祛魅"，**单纯 TOPSIS 难拿高分**，需在权重确定上做创新（组合赋权 / 灰色关联 / 改进距离公式）

## 参考文献

- Hwang, C.-L., & Yoon, K. (1981). *Multiple Attribute Decision Making: Methods and Applications*. Springer.
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 第 23 章.
- Behzadian, M. et al. (2012). "A state-of the-art survey of TOPSIS applications." *Expert Systems with Applications*, 39(17), 13051–13069.
