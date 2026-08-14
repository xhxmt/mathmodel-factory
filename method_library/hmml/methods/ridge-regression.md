# Ridge Regression

> 来源：LLM-MM-Agent HMML；本文件由 `scripts/import_hmml.py` 确定性生成。

## 分层位置

Machine Learning (Machine Learning) → Regression

## 建模方法

Ridge Regression is a statistical method that introduces L2 regularization into the standard linear regression model to address multicollinearity issues and prevent model overfitting.

## 核心思想

Ridge Regression adds an L2 norm penalty term, which is the sum of the squares of all regression coefficients multiplied by a regularization parameter \( \lambda \), to the loss function of the least squares method. This regularization term forces the regression coefficients to remain small, reducing overfitting to the training data and improving the model's generalization ability.

## 典型应用

Ridge Regression is widely used in the following fields: Multicollinearity issues: When there is high correlation among independent variables, Ridge Regression can stabilize the estimation of regression coefficients, avoiding the instability of ordinary least squares (OLS) estimates. High-dimensional data analysis: In cases where the number of features far exceeds the number of samples, Ridge Regression can effectively handle ill-conditioned matrices and provide stable parameter estimates. Improving model generalization: By regularization, Ridge Regression reduces overfitting to the training data and improves prediction performance on new data.

## 使用边界

该条目来自广覆盖方法目录，仅用于候选召回；实际采用前必须结合题目数据、假设、求解器与失败模式做二次审查。
