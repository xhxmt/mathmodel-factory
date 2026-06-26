# 贝叶斯推断 (Bayesian Inference)

## 适用场景

- 小样本 / 数据稀缺，需要引入先验知识
- 需要量化参数的**不确定性**（给出后验分布 / 可信区间，而非单点估计）
- 序贯更新：新数据到来时增量修正信念
- 层次结构 / 多层数据（个体嵌套于组）
- CUMCM/MCM 中"参数估计 + 不确定性""风险量化""融合专家先验"类问题

不适用：
- 大样本 + 无先验偏好 → 频率派 MLE 更省事（结论也趋同）
- 实时高吞吐推断 → MCMC 太慢，考虑变分推断 / 共轭闭式解
- 先验难以合理设定且结论对先验高度敏感 → 谨慎，需做先验敏感性分析

## 核心假设

1. 参数本身被视为**随机变量**（贝叶斯哲学），有先验分布
2. 先验的选择**可辩护**（共轭、无信息、或基于真实领域知识）
3. 似然模型正确刻画数据生成过程
4. MCMC 链已**收敛**（否则后验样本不可信）

## 数学形式

**贝叶斯定理**：
$$p(\theta \mid D) = \frac{p(D \mid \theta)\,p(\theta)}{p(D)} \propto \underbrace{p(D\mid\theta)}_{\text{似然}}\,\underbrace{p(\theta)}_{\text{先验}}$$

**共轭先验**（后验与先验同族，有闭式解）：

| 似然 | 共轭先验 | 后验 |
|---|---|---|
| Binomial(n, θ) | Beta(α, β) | Beta(α+k, β+n−k) |
| Poisson(λ) | Gamma(α, β) | Gamma(α+Σx, β+n) |
| Normal(μ, σ² 已知) | Normal(μ₀, τ²) | Normal(加权均值, ·) |

**无共轭时**用 MCMC（Metropolis-Hastings）从后验采样：接受率 $\alpha = \min\!\big(1, \frac{p(\theta'\mid D)}{p(\theta\mid D)}\big)$。

## 求解流程

1. **设定先验**：共轭（图省事）/ 无信息（Jeffreys）/ 弱信息（推荐,正则化但不主导）
2. **写似然**：明确数据生成模型
3. **求后验**：共轭 → 闭式；否则 MCMC / 变分
4. **收敛诊断**：轨迹图、$\hat{R}$（Gelman-Rubin，<1.1）、有效样本量 ESS
5. **后验总结**：均值 / 中位数 + 可信区间（HDI 最高密度区间）
6. **先验敏感性分析**：换几个先验看结论是否稳健（必做，否则被质疑"先验凑结果"）

## 求解工具

- 共轭闭式：`scipy.stats`（beta / gamma / norm 的 ppf 给可信区间）
- 通用 MCMC：`PyMC`（推荐，NUTS 采样器）、`emcee`、`numpyro`（GPU）
- 手写 Metropolis-Hastings：仅 numpy，适合教学 / 简单模型 / 无依赖环境

## 代码模板

```python
"""
bayesian_template.py — 两种路径:
(A) 共轭 Beta-Binomial 闭式后验  (B) Metropolis-Hastings 估计正态均值
用法: python bayesian_template.py  (仅依赖 numpy / scipy)
"""
import numpy as np
from scipy.stats import beta

np.random.seed(42)

# ===== (A) 共轭:Beta-Binomial(估计某事件发生率 θ)=====
a0, b0 = 1, 1          # 先验 Beta(1,1) = 均匀 (无信息)
k, n = 7, 10           # 观测:10 次试验 7 次成功
a_post, b_post = a0 + k, b0 + (n - k)
mean_post = a_post / (a_post + b_post)
hdi = beta.ppf([0.025, 0.975], a_post, b_post)
print(f"[共轭] 后验 Beta({a_post},{b_post}) 均值={mean_post:.3f} "
      f"95%可信区间=[{hdi[0]:.3f}, {hdi[1]:.3f}]")
print(f"       频率派点估计(MLE)={k/n:.3f}  -- 贝叶斯被先验拉向 0.5")

# ===== (B) Metropolis-Hastings:未知均值 μ,已知 σ =====
true_mu, sigma = 5.0, 2.0
data = np.random.normal(true_mu, sigma, 50)

def log_posterior(mu):
    log_prior = -0.5 * (mu / 10.0) ** 2                  # 先验 N(0, 10^2)
    log_lik = -0.5 * np.sum(((data - mu) / sigma) ** 2)  # 似然
    return log_prior + log_lik

n_iter, step = 20000, 0.5
mu, chain, n_accept = 0.0, [], 0
for _ in range(n_iter):
    cand = mu + np.random.normal(0, step)
    if np.log(np.random.rand()) < log_posterior(cand) - log_posterior(mu):
        mu, n_accept = cand, n_accept + 1
    chain.append(mu)

chain = np.array(chain[2000:])            # 丢弃 burn-in
acc_rate = n_accept / n_iter
print(f"[MCMC] 接受率={acc_rate:.2f} (理想 0.2~0.5)")
print(f"       后验均值={chain.mean():.3f} 95%CI="
      f"[{np.percentile(chain, 2.5):.3f}, {np.percentile(chain, 97.5):.3f}]")
print(f"       真值={true_mu}, 样本均值={data.mean():.3f}")

# ===== 先验敏感性分析 (必做) =====
print("\n[先验敏感性] 不同 Beta 先验下的后验均值:")
for pa, pb, label in [(1, 1, "均匀"), (5, 5, "偏向0.5"), (0.5, 0.5, "Jeffreys")]:
    m = (pa + k) / (pa + k + pb + n - k)
    print(f"  先验 Beta({pa},{pb}) [{label}] -> 后验均值={m:.3f}")
# 若不同先验下结论差异大,必须在论文中披露并论证先验选择
```

> 进阶：模型稍复杂（层次模型、多参数、非共轭）即应改用 PyMC：
> `with pm.Model(): mu = pm.Normal('mu', 0, 10); pm.Normal('y', mu, sigma, observed=data); idata = pm.sample()`，
> 并用 `az.summary(idata)` 看 `r_hat`（应 < 1.01）与 ESS。

## 常见陷阱

1. **先验拍脑袋且不做敏感性分析**：最大软肋。必须用 2-3 个先验对比，证明结论稳健。
2. **MCMC 未收敛就用后验**：必须看轨迹图 + $\hat{R}$ + ESS；单链、迭代太少会给假后验。
3. **burn-in 不丢弃**：链初期未到平稳分布，前若干样本必须丢。
4. **接受率失衡**：太高（>0.7）步长太小、太低（<0.1）步长太大，调 proposal 方差。
5. **可信区间 ≠ 置信区间**：贝叶斯可信区间是"参数有 95% 概率落在此区间"，解释比频率派直接，别混用术语。
6. **用强先验掩盖数据**：先验过强会主导后验，小样本时尤甚，需明示先验权重。
7. **算力误判**：复杂层次模型 NUTS 采样可能很慢，要在时间预算里留够。

## 在建模比赛中的典型应用

- 参数估计 + 不确定性量化（点估计配可信区间，比频率派单点更有说服力）
- 融合专家先验 / 历史数据与当前观测
- 风险 / 可靠性建模中的参数不确定性传播
- 与频率派结果对照，作为稳健性论证（两套方法结论一致 = 强证据）
- 真实案例：MCM/ICM 中数据稀缺、需量化不确定性的题目（流行病参数、可靠性、小样本估计）

## 参考文献

- Gelman, A., et al. (2013). *Bayesian Data Analysis* (3rd ed.). CRC Press.
- McElreath, R. (2020). *Statistical Rethinking* (2nd ed.). CRC Press.
- PyMC documentation: https://www.pymc.io/
- Kruschke, J. (2014). *Doing Bayesian Data Analysis* (2nd ed.). Academic Press.
