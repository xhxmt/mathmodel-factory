# 指数平滑 / Holt-Winters (Exponential Smoothing / ETS)

## 适用场景

- 含**趋势 + 季节**的单变量时间序列短中期预测
- 作为 ARIMA 的**互补 / 对照**方法（评委喜欢看多模型对比）
- 数据较短、关系平滑、不想做 ARIMA 那套定阶的快速基线
- 需要清晰拆解水平 / 趋势 / 季节三分量

不适用：
- 强外生变量驱动 → SARIMAX / 回归 + 残差时序
- 非线性 / 机制切换 → 状态空间 / Markov-switching
- 长程依赖或高频精度 → LSTM / Prophet（含节假日效应）/ Transformer
- 序列无趋势无季节（纯随机游走）→ 简单方法即可，无需 HW

与 [ARIMA] 的分工：ARIMA 建模**自相关结构**，指数平滑建模**水平/趋势/季节的指数加权**。两者在 CUMCM 预测题里常并列对照。

## 核心假设

1. 近期观测比远期更有信息量（**指数衰减权重**）
2. 趋势 / 季节模式相对稳定（无结构断点）
3. 季节周期 $s$ 已知且恒定
4. 加法季节：季节振幅恒定；乘法季节：振幅随水平按比例放大

## 数学形式

**简单指数平滑**（无趋势无季节）：$\ell_t = \alpha y_t + (1-\alpha)\ell_{t-1}$

**Holt（含趋势）**：
$$\ell_t = \alpha y_t + (1-\alpha)(\ell_{t-1}+b_{t-1}), \quad b_t = \beta(\ell_t-\ell_{t-1}) + (1-\beta)b_{t-1}$$

**Holt-Winters（趋势 + 季节，加法）**：再加季节分量
$$s_t = \gamma(y_t - \ell_t) + (1-\gamma)s_{t-s}, \quad \hat{y}_{t+h} = \ell_t + h\,b_t + s_{t-s+1+(h-1)\bmod s}$$

平滑系数 $\alpha,\beta,\gamma \in (0,1)$ 由极大似然 / 最小 SSE 自动估计。

## 建模流程

1. **可视化分解**：看是否有趋势 / 季节，季节是加法还是乘法（振幅是否随水平增长）
2. **选模型变体**：无趋势→SES；有趋势→Holt；有季节→Holt-Winters（加法 / 乘法）
3. **拟合**：statsmodels 自动估计平滑参数
4. **加法 vs 乘法**：两个都跑，比 AIC / 样本外 RMSE 选优
5. **样本外评估**：hold-out 末段，报 RMSE / MAPE
6. **与 ARIMA 对照**：同一 hold-out 上比较，论文里并列

## 求解工具

- `statsmodels.tsa.holtwinters.ExponentialSmoothing` — Holt-Winters（trend / seasonal = "add"/"mul"）
- `statsmodels.tsa.holtwinters.SimpleExpSmoothing` — 简单指数平滑
- `statsmodels.tsa.statespace.ExponentialSmoothing` — 状态空间版（带置信区间）
- `statsmodels.tsa.seasonal.STL` — 先做 STL 分解再建模残差（强季节时）

## 代码模板

```python
"""
holt_winters_template.py — Holt-Winters 加法/乘法季节对照 + 样本外评估
用法: python holt_winters_template.py  (依赖 numpy / pandas / statsmodels)
"""
import numpy as np
import pandas as pd
from statsmodels.tsa.holtwinters import ExponentialSmoothing

np.random.seed(42)
n = 144                                  # 12 年月度数据
t = np.arange(n)
y = 100 + 0.5 * t + 20 * np.sin(2 * np.pi * t / 12) + np.random.normal(0, 3, n)
series = pd.Series(y, index=pd.date_range("2012-01", periods=n, freq="M"))
train, test = series[:-12], series[-12:]   # 末 12 期做 hold-out

def evaluate(pred, actual, label):
    rmse = np.sqrt(((pred - actual) ** 2).mean())
    mape = (np.abs(pred - actual) / actual).mean() * 100
    print(f"{label:18s} RMSE={rmse:.3f}  MAPE={mape:.2f}%")
    return rmse

# ---- 加法季节 ----
fit_add = ExponentialSmoothing(train, trend="add", seasonal="add",
                               seasonal_periods=12).fit()
pred_add = fit_add.forecast(len(test))
evaluate(pred_add, test, "HW 加法季节")
print(f"  平滑参数 alpha={fit_add.params['smoothing_level']:.3f} "
      f"beta={fit_add.params['smoothing_trend']:.3f} "
      f"gamma={fit_add.params['smoothing_seasonal']:.3f}")

# ---- 乘法季节(季节振幅随水平增长时更合适) ----
fit_mul = ExponentialSmoothing(train, trend="add", seasonal="mul",
                               seasonal_periods=12).fit()
pred_mul = fit_mul.forecast(len(test))
evaluate(pred_mul, test, "HW 乘法季节")

# ---- 用 AIC 选加法 vs 乘法 ----
print(f"AIC: 加法={fit_add.aic:.1f}  乘法={fit_mul.aic:.1f}  "
      f"-> 选 {'加法' if fit_add.aic < fit_mul.aic else '乘法'}")

# ---- (可选) 与 ARIMA 同 hold-out 对照 ----
try:
    from statsmodels.tsa.arima.model import ARIMA
    arima_pred = ARIMA(train, order=(1, 1, 1),
                       seasonal_order=(1, 1, 1, 12)).fit().forecast(len(test))
    evaluate(arima_pred, test, "SARIMA 对照")
except Exception as e:
    print(f"(ARIMA 对照跳过: {e})")
```

## 常见陷阱

1. **加法 / 乘法季节选错**：振幅随水平增长却用加法（或反之），预测系统性偏差。两个都试、比 AIC。
2. **乘法季节遇到非正 / 零值**：乘法模型要求序列为正，含 0 或负值会报错，需平移或改加法。
3. **季节周期 s 设错**：月度 12、季度 4、周度 7 / 52，设错则季节项无意义。
4. **不评样本外**：只看拟合优度会过拟合平滑参数，必须 hold-out。
5. **趋势外推过度**：Holt 线性趋势长程会发散，远期预测要加阻尼趋势（`damped_trend=True`）。
6. **结构断点**：政策 / 疫情突变后历史平滑权重失效，需分段或换模型。
7. **只用单一模型**：CUMCM 预测题应至少与 ARIMA 对照，单模型说服力弱。

## 在建模比赛中的典型应用

- 销量 / 客流 / 气象 / 能耗等带季节的预测
- 与 ARIMA、回归、（数据足时）LSTM 多模型对照，论证鲁棒性
- 趋势 / 季节分量拆解作为业务解读（旺季淡季、增长率）
- 真实案例：CUMCM 预测类题目中作为强基线，与 ARIMA 并列对比几乎是标准操作

## 参考文献

- Hyndman, R. J., & Athanasopoulos, G. (2021). *Forecasting: Principles and Practice* (3rd ed.), Ch. 8. OTexts. https://otexts.com/fpp3/expsmooth.html
- Holt, C. C. (2004). Forecasting seasonals and trends by exponentially weighted moving averages. *Int. J. Forecasting*, 20(1).
- statsmodels documentation: https://www.statsmodels.org/stable/tsa.html
