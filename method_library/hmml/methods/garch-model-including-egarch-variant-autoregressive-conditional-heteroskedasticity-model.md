# GARCH Model (including EGARCH variant) (Autoregressive Conditional Heteroskedasticity model)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Prediction (Prediction) → Continuous Prediction (Continuous Prediction) → Time Series Models (Sequential Models)

## 建模方法

The GARCH model (Generalized Autoregressive Conditional Heteroskedasticity model) is a statistical model used to model and predict the conditional heteroskedasticity (i.e., time-varying volatility) in time series data. It is widely applied in the analysis of financial market volatility and risk management.

## 核心思想

The GARCH model describes the dynamic changes in current conditional variance by incorporating past error terms and variance information. Its basic form is: \[ \sigma_t^2 = \omega + \sum_{i=1}^{q} \alpha_i \epsilon_{t-i}^2 + \sum_{i=1}^{p} \beta_i \sigma_{t-i}^2 \] where \( \sigma_t^2 \) represents the conditional variance, \( \epsilon_{t-i}^2 \) is the squared past error term, \( \sigma_{t-i}^2 \) is the past conditional variance, \( \omega \) is a constant term, \( \alpha_i \) and \( \beta_i \) are model parameters, and \( p \) and \( q \) are the orders of the GARCH model. EGARCH Model: The EGARCH model (Exponential Generalized Autoregressive Conditional Heteroskedasticity model) is an extension of the GARCH model that can capture the asymmetric effects commonly observed in financial time series, where negative shocks typically have a larger impact on volatility than positive shocks. Its basic form is: \[ \log(\sigma_t^2) = \omega + \sum_{i=1}^{q} \beta_i g(\epsilon_{t-i}) + \sum_{i=1}^{p} \alpha_i \log(\sigma_{t-i}^2) \] where \( g(\epsilon_{t-i}) \) is a function, usually \( g(\epsilon) = \theta \epsilon + \lambda (|\epsilon| - E[|\epsilon|]) \), used to capture asymmetric effects.

## 典型应用

The GARCH and its variant models are widely used in the following fields: Financial market analysis: Modeling and predicting the volatility of asset returns and assessing market risk. Risk management: Estimating potential market risk in measures such as VaR (Value at Risk) and ES (Expected Shortfall). Economic research: Analyzing the volatility of macroeconomic indicators such as inflation rates and exchange rates.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
