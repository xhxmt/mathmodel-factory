# 自回归滑动平均模型 (ARIMA / SARIMA)

## 适用场景

- 单变量时间序列（univariate）短中期预测
- 序列经差分后平稳
- 没有强外生变量（有则用 ARIMAX / VAR / SARIMAX）
- 历史长度 ≥ 50 期更稳

不适用：
- 多变量耦合预测 → VAR / VECM
- 非线性结构（机制切换、阈值效应）→ TAR、Markov-Switching
- 长程依赖（分形、自相似）→ ARFIMA
- 数据量大、关注高频精度 → LSTM / Transformer / Prophet 等
- 季节性强但 SARIMA 表达不出来 → STL 分解 + 残差 ARIMA

## 核心假设

1. 经过若干次差分（$d$ 阶）后序列平稳
2. 残差独立同分布，白噪声
3. 参数稳定（不存在结构断点）
4. 季节模式恒定（SARIMA 加入周期 $s$）

## 数学形式

**ARIMA(p, d, q)**：
$$\phi(B)(1-B)^d X_t = \theta(B)\varepsilon_t$$

其中
- $B$ 滞后算子，$BX_t = X_{t-1}$
- $\phi(B) = 1 - \phi_1 B - \cdots - \phi_p B^p$ AR 多项式
- $\theta(B) = 1 + \theta_1 B + \cdots + \theta_q B^q$ MA 多项式
- $\varepsilon_t \sim \mathrm{WN}(0, \sigma^2)$

**SARIMA(p,d,q)(P,D,Q)$_s$**：再叠加季节项 $\Phi(B^s)(1-B^s)^D X_t = \Theta(B^s)\varepsilon_t$

## 定阶流程

1. **平稳性检验**：ADF / KPSS。p 值 < 0.05 拒绝单位根 → 平稳
2. **差分**：不平稳就一阶差分 $\nabla X_t = X_t - X_{t-1}$；季节性强加 $\nabla_s X_t$
3. **看 ACF / PACF**：
   - AR(p)：PACF 在 $p$ 后截尾，ACF 拖尾
   - MA(q)：ACF 在 $q$ 后截尾，PACF 拖尾
   - ARMA：两者都拖尾
4. **网格搜索 AIC / BIC**：`pmdarima.auto_arima` 自动
5. **残差白噪声检验**：Ljung-Box；自相关 = 0 才合格

## 求解工具

- `statsmodels.tsa.arima.model.ARIMA` — 标准实现
- `statsmodels.tsa.statespace.SARIMAX` — SARIMA 和外生变量
- `pmdarima.auto_arima` — 自动定阶（需要 pip install pmdarima）
- 检验：`statsmodels.tsa.stattools.adfuller`, `acorr_ljungbox`

## 代码模板

```python
"""
arima_template.py — ARIMA 完整流程：平稳性 → 定阶 → 拟合 → 残差检验 → 预测
用法: python arima_template.py
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.stattools import adfuller
from statsmodels.stats.diagnostic import acorr_ljungbox

np.random.seed(42)

# ---- 1. 数据 (替换为题目实际数据) ----
n = 200
t = np.arange(n)
y = 0.5 * t + 5 * np.sin(2 * np.pi * t / 12) + np.random.normal(0, 2, n)
series = pd.Series(y, index=pd.date_range("2010-01", periods=n, freq="M"))
train, test = series[:-12], series[-12:]

# ---- 2. 平稳性检验 ----
def adf_report(x, name="series"):
    stat, p, *_ = adfuller(x.dropna())
    print(f"ADF({name}): stat={stat:.3f}  p={p:.4f}  "
          f"{'平稳' if p < 0.05 else '不平稳, 需差分'}")

adf_report(train, "原序列")
adf_report(train.diff().dropna(), "一阶差分")

# ---- 3. 定阶 (这里手动指定; 实际可用 auto_arima) ----
order = (1, 1, 1)             # (p, d, q)
seasonal_order = (1, 1, 1, 12)  # (P, D, Q, s) 月度数据周期 12

# ---- 4. 拟合 ----
model = ARIMA(train, order=order, seasonal_order=seasonal_order)
fit = model.fit()
print(fit.summary().tables[1])

# ---- 5. 残差白噪声检验 ----
lb = acorr_ljungbox(fit.resid, lags=[10], return_df=True)
print(f"Ljung-Box p={lb['lb_pvalue'].iloc[0]:.4f}  "
      f"{'残差白噪声 (合格)' if lb['lb_pvalue'].iloc[0] > 0.05 else '残差有自相关 (重选阶)'}")

# ---- 6. 预测 ----
forecast = fit.get_forecast(steps=len(test))
mean = forecast.predicted_mean
ci = forecast.conf_int(alpha=0.05)

# ---- 7. 评估 ----
rmse = np.sqrt(((mean - test) ** 2).mean())
mape = (np.abs(mean - test) / test).mean() * 100
print(f"测试集 RMSE = {rmse:.3f}  MAPE = {mape:.2f}%")

# ---- 8. 出图 (按 modeling_guide.md 规范保存到 figures/) ----
# 此处省略 matplotlib 代码; 实际项目中需用规定色板与字体
```

## 常见陷阱

1. **不做平稳性检验直接拟合**：国赛扣分点。必须明确报告 ADF / KPSS 结果。
2. **过差分**：差分阶数 $d$ 过大会引入噪声 + 信息损失。一般 $d \le 2$。
3. **季节性被忽略**：月度 / 季度 / 日度数据多有周期，必须 SARIMA 或先 STL 分解。
4. **预测区间被低估**：ARIMA 的预测区间假设残差正态。真实数据厚尾时区间不可靠 → 用 bootstrapping。
5. **样本外性能不报**：只看训练集 AIC 是不够的，必须 hold-out 评估 RMSE / MAPE。
6. **拟合优度 ≠ 预测能力**：高 R² 也可能过拟合。
7. **结构断点**：政策、疫情等导致序列结构突变时 ARIMA 不可用，需先 Chow 检验。
8. **`auto_arima` 的网格范围**：默认 `max_p=5, max_q=5`，对长序列可能太窄，建议手动放大。
9. **预测多步衰减到均值**：ARIMA 长程预测会回归均值。题目要求超过 20 步预测时考虑 LSTM / 状态空间模型。

## 在建模比赛中的典型应用

- 单变量经济 / 销量 / 气象数据预测
- 与多种模型对比（ARIMA vs LSTM vs Prophet）展示鲁棒性
- 作为残差建模的二阶段方法（例：先回归出趋势，再 ARIMA 残差）
- 真实 CUMCM 案例：CUMCM 历年涉及预测的题目（如人口、销量、价格预测）几乎都会出现 ARIMA 作为基线方法

## 参考文献

- Box, G. E. P., Jenkins, G. M., Reinsel, G. C., & Ljung, G. M. (2015). *Time Series Analysis: Forecasting and Control* (5th ed.). Wiley.
- statsmodels documentation: https://www.statsmodels.org/stable/tsa.html
- Hyndman, R. J., & Athanasopoulos, G. (2021). *Forecasting: Principles and Practice* (3rd ed.). OTexts. https://otexts.com/fpp3/
- 司守奎 & 孙兆亮 (2015). 《数学建模算法与应用》(第二版). 第 26 章.
