# 系统优化总览

本目录包含2026-06-13完成的Paper Factory系统优化文档。

## 📊 优化成果

| 优化项 | 关键功能 | 成本节约 | 质量提升 |
|--------|---------|---------|---------|
| **硬指标扩展** | 求解器收敛+数值链检测 | - | ⭐⭐⭐⭐⭐ 替代judge |
| **Step 2优化** | 资源配额+早停检测 | ⭐⭐⭐⭐⭐ 40-45% | - |
| **方法库智能化** | 引用学习+反例库 | ⭐⭐⭐ 10-15% | ⭐⭐⭐⭐ 准确率+15% |

**综合效益**: 节省**~17%总成本** + 建立客观质量评估体系

---

## 📁 文档导航

### 技术文档（深度阅读）
- [optimization_summary_2026-06-13.md](optimization_summary_2026-06-13.md) (367行)
  - 硬指标扩展原理与实现
  - Step 2资源配额和早停算法
  - 测试结果和成本分析

- [method_library_intelligence_summary.md](method_library_intelligence_summary.md) (436行)
  - 引用模式学习算法（适配度矩阵）
  - 反例库构建逻辑（失败模式识别）
  - 增量更新协议（git diff + 自动验证）

### 使用指南（快速上手）
- [OPTIMIZATION_USAGE.md](OPTIMIZATION_USAGE.md) (153行)
  - 硬指标工具使用
  - 资源配额和早停调优
  - 常见问题

- [METHOD_LIBRARY_INTELLIGENCE_USAGE.md](METHOD_LIBRARY_INTELLIGENCE_USAGE.md) (251行)
  - 方法推荐使用
  - 反例检查集成
  - 定期学习设置

### 变更日志
- [../CHANGELOG_optimization.md](../CHANGELOG_optimization.md)
- [../CHANGELOG_method_intelligence.md](../CHANGELOG_method_intelligence.md)

---

## 🚀 快速开始

### 1. 验证安装

```bash
# 测试所有优化功能
bash tests/test_step2_optimization.sh
bash tests/test_method_library_intelligence.sh
```

### 2. 使用硬指标

```bash
# 检查单个项目
python3 scripts/hard_metrics.py ongoing/my_project my_project

# 批量对比
python3 scripts/hard_metrics.py --batch complete/ > report.md
```

### 3. 初始化方法库智能化

```bash
# 从历史学习（首次运行）
python3 scripts/method_fit_score.py --learn complete/
python3 scripts/method_antipatterns.py --build complete/

# 为新项目推荐方法
python3 scripts/method_fit_score.py ongoing/new_project

# 检查反例
python3 scripts/method_antipatterns.py --check ongoing/new_project \
    method_library/optimization/milp.md
```

---

## 📈 新增工具清单

### 硬指标检测器
- `scripts/verify_solver.py` - 求解器收敛性
- `scripts/verify_number_chain.py` - 数值溯源链
- 已集成到 `scripts/hard_metrics.py`

### Step 2优化器
- `scripts/step2_resource_quota.py` - 动态流数量决策
- `scripts/step2_early_stop.py` - 快速失败检测
- 已集成到 `run_paper.sh`

### 方法库智能化
- `scripts/method_fit_score.py` - 引用模式学习
- `scripts/method_antipatterns.py` - 反例库
- `scripts/method_library_update.py` - 增量更新检测

---

## 💡 关键洞察

### 从消融实验得到的经验
1. **Judge评分锚定严重**: DeepSeek无法区分细粒度差异，Gemini虽能区分但信号弱
2. **硬指标是真正的锚**: `dangling_cites`（悬空引用）是唯一judge-free的强信号
3. **方法库是系统核心**: HMML-lite硬门禁证明了方法库的关键作用

### 优化设计原则
1. **程序可判优先**: 能用代码检测的不依赖LLM
2. **增量扩展**: 每个工具独立，容错设计
3. **历史学习**: 从6个项目中提取模式，随积累而改进
4. **可解释性**: 所有决策都有confidence/severity量化

---

## 🎯 预期收益详解

### 成本节约路径
```
Step 2优化 (节省40-45%)
├─ 资源配额: 简单问题5流→3流 (节省22%)
├─ 早停检测: 前5分钟终止失败流 (节省10%)
└─ 方法推荐: 提前选对方法 (节省10-15%)

总工厂成本节约 = 40% (Step 2占比) × 42.5% ≈ 17%
```

### 质量提升路径
```
硬指标体系
├─ 符号未定义检测 (覆盖率指标)
├─ 求解器收敛检测 (可信度指标)
└─ 数值链检测 (一致性指标)

方法选择准确率
├─ 当前: 60% (纯即时判断)
└─ 目标: 75-80% (历史学习 + 反例过滤)
```

---

## ⚠️ 注意事项

### 当前限制
- **样本不足**: 仅6个项目，置信度0.4，需50+项目达到稳定
- **特征工程**: 11个特征是手工设计，可能遗漏关键维度
- **方法映射**: 启发式关键词匹配，准确率70%

### 风险缓解
- 低置信度推荐仅供参考，不强制
- 早停阈值保守（confidence≥0.85）
- 所有工具容错设计，失败不阻塞主流程

---

## 🔄 持续改进

### 每周
- 重新学习方法库模型（有新项目时）
- Review新增的反例

### 每月
- 调优阈值（基于真实运行数据）
- 添加新的问题特征
- 扩展反例库规则

### 每季度
- 评估ROI（实际成本节约）
- 对比A/B测试（有/无智能推荐）
- 规划下一代优化

---

## 📞 支持与反馈

- **文档问题**: 检查对应的技术文档和使用指南
- **工具故障**: 运行测试脚本诊断
- **功能建议**: 提交issue，标签 `enhancement`

---

**文档版本**: v1.0  
**最后更新**: 2026-06-13  
**维护者**: Paper Factory Team
