# 层次分析法 (Analytic Hierarchy Process, AHP)

## 适用场景

- 多准则决策（multi-criteria decision making）：方案在多个不可比指标上各有优劣，需要给指标分配主观权重后综合
- 准则数 ≤ 9（超过 9 一致性检验几乎必然超标，需要分层）
- **没有可靠客观数据**给指标赋权的情形（有数据时优先用熵权法或主成分；AHP 是兜底）
- 通常作为 TOPSIS / 综合评价 / 多目标加权的**前置赋权步骤**

不适用：
- 指标本身可以从数据计算客观重要性（销售额、相关系数等）—— 用熵权 / PCA
- 准则之间有强非线性交互（AHP 假设加性可分）
- 需要稳健性的高风险决策（AHP 对判断矩阵微小扰动可能不稳）

## 核心假设

1. 准则可分层（goal → criteria → sub-criteria → alternatives）
2. 同层准则间可两两比较，且比较是基数（不只是排序）
3. 判断者对相对重要性的认知一致（用 CR 检验）
4. 加性可分性：综合得分 = Σ wᵢ · sᵢ

## 数学形式

判断矩阵 $A = (a_{ij})_{n\times n}$，$a_{ij}\in\{1/9, 1/8, \ldots, 1, \ldots, 8, 9\}$，$a_{ji} = 1/a_{ij}$。

权重向量 $\mathbf{w}$ 满足 $A\mathbf{w} = \lambda_{\max}\mathbf{w}$，归一化使 $\sum w_i = 1$。

一致性指标：
$$\mathrm{CI} = \frac{\lambda_{\max} - n}{n-1}, \quad \mathrm{CR} = \frac{\mathrm{CI}}{\mathrm{RI}_n}$$

其中 $\mathrm{RI}_n$ 为 Saaty 给定的随机一致性指标查表值：

| n | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|
| RI | 0 | 0 | 0.58 | 0.90 | 1.12 | 1.24 | 1.32 | 1.41 | 1.45 |

判定准则：**$\mathrm{CR} < 0.1$ 通过**，否则重构判断矩阵。

## 求解工具

- `numpy` 即可：`numpy.linalg.eig` 求最大特征值与特征向量
- 不需要专门求解器；典型规模 ≤ 9 维，毫秒级

## 代码模板

```python
"""
ahp_template.py — 标准 AHP 求权重 + 一致性检验
用法: python ahp_template.py
"""
import numpy as np

RI = {1: 0, 2: 0, 3: 0.58, 4: 0.90, 5: 1.12, 6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45}

def ahp_weights(A: np.ndarray) -> tuple[np.ndarray, float, float]:
    """返回 (权重, CI, CR)。"""
    n = A.shape[0]
    assert A.shape == (n, n), "判断矩阵必须方阵"
    # 互反性检查（精度 1e-6）
    assert np.allclose(A * A.T, 1, atol=1e-6), "判断矩阵不满足互反性 a_ij*a_ji=1"

    eigvals, eigvecs = np.linalg.eig(A)
    idx = np.argmax(eigvals.real)
    lam_max = eigvals[idx].real
    w = eigvecs[:, idx].real
    w = w / w.sum()  # 归一化使和为 1

    ci = (lam_max - n) / (n - 1) if n > 1 else 0.0
    cr = ci / RI[n] if RI.get(n, 0) > 0 else 0.0
    return w, ci, cr


if __name__ == "__main__":
    # 示例: 4 准则的判断矩阵
    A = np.array([
        [1,   2, 4, 3],
        [1/2, 1, 3, 2],
        [1/4, 1/3, 1, 1/2],
        [1/3, 1/2, 2, 1],
    ], dtype=float)
    w, ci, cr = ahp_weights(A)
    print(f"权重 = {w.round(4)}")
    print(f"CI = {ci:.4f}, CR = {cr:.4f}, {'通过' if cr < 0.1 else '不通过, 需重构'}")
```

## 常见陷阱

1. **CR ≥ 0.1 不可强行使用**：必须调整判断矩阵。强行使用是国赛扣分常见点。
2. **判断矩阵填写方向**：$a_{ij}$ 表示"$i$ 比 $j$ 重要多少倍"。写反一次全盘错。
3. **几何平均法 vs 特征向量法**：两者结果相近但不完全相同，论文需说明用哪个。
4. **对客观数据用 AHP**：错误。客观数据用熵权 / 标准差 / 变异系数法。
5. **层数过多**：建议 ≤ 3 层（目标 → 准则 → 方案）。子准则一般另开判断矩阵，不要嵌套到 9×9 以上。
6. **正向化遗漏**：与下游 TOPSIS / 加权评分组合时，必须先把所有指标变成同向（越大越好或越小越好），AHP 只给权重不做标准化。

## 在建模比赛中的典型应用

- 综合评价类题目的子模块（赋权）
- 配合 TOPSIS / 模糊综合评价 / 灰色关联分析
- 国赛 C 题（数据评价类）常以 AHP 作为对照方法被审稿期待"是否做过一致性检验"
- 注意：评委已经见过太多 AHP，**仅用 AHP 难拿高分**，需结合客观赋权做对比

## 参考文献

- Saaty, T. L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.
- Saaty, T. L. (2008). "Decision making with the analytic hierarchy process." *International Journal of Services Sciences*, 1(1), 83–98.
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 国防工业出版社. 第 22 章.
