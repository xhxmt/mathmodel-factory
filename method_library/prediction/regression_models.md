# 回归模型 (Linear / Logistic / 正则化)

## 适用场景

- 连续目标预测、变量间定量关系建模（线性回归）
- 二分类 / 概率预测（逻辑回归）
- 高维 / 多重共线性 / 需特征选择（Ridge / Lasso / ElasticNet）
- 需要**可解释系数**（每个特征的边际效应），而非黑箱精度
- CUMCM/MCM 中"影响因素分析""预测 + 归因""分类判别"类问题的主力

不适用：
- 强非线性 / 高阶交互且不愿手工造特征 → 树模型（RF / XGBoost）/ 神经网络
- 纯时间序列（自相关结构）→ ARIMA / 指数平滑（见 [time series 方法]）
- 样本数 < 特征数且无正则 → OLS 不可解，必须 Ridge / Lasso

## 核心假设（OLS）

1. **线性**：目标对参数线性（特征可非线性变换后进入）
2. **独立同分布**误差，**同方差**（异方差 → 加权最小二乘 / 稳健标准误）
3. 误差**近似正态**（用于区间估计 / 显著性检验）
4. **无严重多重共线性**（VIF < 10；否则系数不稳，改 Ridge）
5. 逻辑回归：对数几率线性，观测独立

## 数学形式

**OLS**：$\hat{\beta} = \arg\min_\beta \|y - X\beta\|_2^2 = (X^\top X)^{-1}X^\top y$

**Ridge (L2)**：$\min_\beta \|y - X\beta\|_2^2 + \lambda\|\beta\|_2^2$ — 压缩系数、治共线性，不归零。

**Lasso (L1)**：$\min_\beta \|y - X\beta\|_2^2 + \lambda\|\beta\|_1$ — 部分系数**精确归零** → 自动特征选择。

**逻辑回归**：$P(y=1\mid x) = \sigma(\beta^\top x) = \dfrac{1}{1+e^{-\beta^\top x}}$，极大似然估计。

## 建模流程

1. **EDA + 标准化**：正则化前必须标准化（否则惩罚对不同量纲不公平）
2. **共线性诊断**：相关热图 / VIF；高共线 → Ridge 或删冗余特征
3. **选模型**：纯预测看精度；要可解释 + 特征选择用 Lasso；二者兼顾用 ElasticNet
4. **调正则强度 λ**：交叉验证（`RidgeCV` / `LassoCV`）
5. **评估**：回归看 R² / RMSE / 残差图；分类看 AUC / 混淆矩阵 / F1
6. **诊断**：残差正态性 + 同方差（回归）；校准曲线（分类）

## 求解工具

- `sklearn.linear_model`：`LinearRegression / Ridge / Lasso / ElasticNet / LogisticRegression`（+ `RidgeCV / LassoCV` 自动调 λ）
- `sklearn.preprocessing.StandardScaler`：标准化（正则化前必做）
- `statsmodels.api.OLS / Logit`：要 p 值、置信区间、完整统计报告时用
- `statsmodels.stats.outliers_influence.variance_inflation_factor`：VIF

## 代码模板

```python
"""
regression_template.py — OLS/Ridge/Lasso 对照 + Lasso 特征选择 + 逻辑回归分类
用法: python regression_template.py  (依赖 numpy / scikit-learn)
"""
import numpy as np
from sklearn.linear_model import LinearRegression, Ridge, Lasso, LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import r2_score, mean_squared_error, roc_auc_score

np.random.seed(42)
n, p = 200, 8
X = np.random.randn(n, p)
X[:, 1] = X[:, 0] * 0.9 + np.random.randn(n) * 0.1     # 故意制造多重共线性
beta_true = np.array([3, 0, 0, 1.5, 0, 0, -2, 0])      # 稀疏真系数
y = X @ beta_true + np.random.randn(n) * 0.5
y_class = (y > np.median(y)).astype(int)               # 派生二分类标签

idx = np.arange(n)
tr, te = train_test_split(idx, test_size=0.3, random_state=0)
sc = StandardScaler().fit(X[tr])                       # 标准化器只在训练集上 fit
Xtr, Xte = sc.transform(X[tr]), sc.transform(X[te])

# ---- 回归:OLS / Ridge / Lasso 对照 ----
for name, model in [("OLS", LinearRegression()),
                    ("Ridge(L2)", Ridge(alpha=1.0)),
                    ("Lasso(L1)", Lasso(alpha=0.1))]:
    model.fit(Xtr, y[tr])
    pred = model.predict(Xte)
    nz = int(np.sum(np.abs(model.coef_) > 1e-3))
    print(f"{name:10s} R2={r2_score(y[te], pred):.3f} "
          f"RMSE={np.sqrt(mean_squared_error(y[te], pred)):.3f} 非零系数={nz}/{p}")

# ---- Lasso 自动特征选择 ----
lasso = Lasso(alpha=0.1).fit(Xtr, y[tr])
print("Lasso 选中特征:", list(np.where(np.abs(lasso.coef_) > 1e-3)[0]),
      " (真实非零: [0, 3, 6])")

# ---- 分类:逻辑回归 ----
clf = LogisticRegression(max_iter=1000).fit(Xtr, y_class[tr])
proba = clf.predict_proba(Xte)[:, 1]
print(f"逻辑回归 AUC={roc_auc_score(y_class[te], proba):.3f} "
      f"准确率={clf.score(Xte, y_class[te]):.3f}")
```

## 常见陷阱

1. **正则化前不标准化**：L1/L2 惩罚对量纲敏感，不标准化等于偏袒大尺度特征。
2. **多重共线性下解读 OLS 系数**：共线时系数方差爆炸、符号可能反，必须先查 VIF。
3. **用 R² 衡量分类**：分类要用 AUC / F1 / 混淆矩阵，不是 R²。
4. **数据泄漏**：标准化器 / 特征选择在全数据上 fit 再 split，会高估性能。务必只在训练集 fit。
5. **过拟合不评样本外**：高训练 R² 无意义，必须 hold-out / 交叉验证。
6. **Lasso 在强共线组里随机选一个**：共线特征 Lasso 只留其一，解释要谨慎（用 ElasticNet 缓解）。
7. **逻辑回归类别不平衡**：用 `class_weight='balanced'` 或重采样，否则偏向多数类。
8. **外推**：回归在训练区间外预测不可靠，须说明适用域。

## 在建模比赛中的典型应用

- 影响因素分析（哪些变量显著、方向与大小）+ 可解释结论
- 预测基线模型，与树模型 / 神经网络对照展示
- Lasso 做高维特征筛选，再喂给下游模型
- 逻辑回归做风险 / 类别判别（违约、患病、合格判定）
- 真实案例：CUMCM 评价 / 预测 / 影响因素类题目几乎必含回归

## 参考文献

- Hastie, T., Tibshirani, R., & Friedman, J. (2009). *The Elements of Statistical Learning* (2nd ed.). Springer.
- James, G., et al. (2021). *An Introduction to Statistical Learning* (2nd ed.). Springer.
- scikit-learn documentation: https://scikit-learn.org/stable/modules/linear_model.html
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 回归分析章节.
