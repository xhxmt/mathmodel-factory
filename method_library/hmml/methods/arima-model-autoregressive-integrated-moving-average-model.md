# ARIMA Model (Autoregressive Integrated Moving Average model)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Prediction (Prediction) → Continuous Prediction (Continuous Prediction) → Time Series Models (Sequential Models)

## 建模方法

The ARIMA model (Autoregressive Integrated Moving Average model) is a widely used method for time series forecasting. It aims to predict future values by analyzing the autocorrelation and trends in historical data.

## 核心思想

The ARIMA model consists of three main components: 1. Autoregressive (AR): There is a linear relationship between the current value and its previous p values. 2. Integrated (I): By differencing the original data, it becomes stationary, eliminating trends. 3. Moving Average (MA): There is a linear relationship between the current value and the prediction errors from the previous q time periods. The ARIMA model is usually denoted as ARIMA(p, d, q), where: p: Number of autoregressive terms. d: Number of differences. q: Number of moving average terms.

## 典型应用

The ARIMA model is widely used in the following fields: Economic forecasting: Predicting economic indicators such as GDP growth rate and inflation rate. Financial markets: Forecasting stock prices, exchange rates, and other financial data. Energy demand: Predicting consumption of electricity, natural gas, and other energy sources. Traffic flow: Forecasting road traffic volume and public transportation ridership.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
