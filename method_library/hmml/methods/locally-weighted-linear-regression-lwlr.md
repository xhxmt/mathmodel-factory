# Locally Weighted Linear Regression (LWLR)

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Regression

## 建模方法

Locally Weighted Linear Regression (LWLR) is a non-parametric regression method that aims to capture the local characteristics of data by assigning different weights to the data around each prediction point, thereby better fitting nonlinear relationships.

## 核心思想

In LWLR, for each point to be predicted, different weights are assigned to the data points in the training set based on their distance. The closer the point, the greater the weight; the farther the point, the smaller the weight. Then, linear regression is performed on these weighted data points to obtain the predicted value for that point. Specifically, given a point to be predicted \( x \), its predicted value \( \hat{y} \) is calculated through the following steps: 1. Calculate the weight matrix: Calculate the weight for each point in the training set based on a distance metric (such as the Gaussian kernel function). 2. Weighted regression: Perform linear regression on the weighted dataset to calculate the regression coefficients. 3. Prediction: Use the obtained regression coefficients to predict the value for the point to be predicted.

## 典型应用

LWLR is widely used in the following fields: Nonlinear regression analysis: When data shows a nonlinear trend, LWLR can effectively capture local nonlinear relationships. Local modeling: LWLR provides a flexible solution when local characteristics of the data need to be modeled. Data smoothing: By local weighting, LWLR can effectively smooth data and reduce the impact of noise.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
