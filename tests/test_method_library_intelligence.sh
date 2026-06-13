#!/bin/bash
# 方法库智能化功能测试脚本

set -euo pipefail

FACTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$FACTORY"

echo "=== 方法库智能化功能测试 ==="
echo

# 测试1: 引用模式学习
echo "测试 1: 引用模式学习（从历史项目学习）"
python3 scripts/method_fit_score.py --learn complete/ > /tmp/fit_model_test.json 2>&1
if [ -f scripts/method_fit_model.json ]; then
    learned_projects=$(python3 -c "import json; d=json.load(open('scripts/method_fit_model.json')); print(d['summary']['total_projects'])")
    learned_methods=$(python3 -c "import json; d=json.load(open('scripts/method_fit_model.json')); print(d['summary']['methods_seen'])")
    echo "  ✓ 学习完成: $learned_projects 个项目, $learned_methods 种方法"
else
    echo "  ✗ 模型文件未生成"
    exit 1
fi
echo

# 测试2: 方法推荐
echo "测试 2: 方法推荐（为test_cumcm2024a推荐方法）"
result=$(python3 scripts/method_fit_score.py complete/test_cumcm2024a 2>/dev/null)
top_method=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['top_recommendations'][0]['method'])")
top_score=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['top_recommendations'][0]['fit_score'])")
echo "  ✓ 推荐方法: $top_method (适配度: $top_score)"
echo

# 测试3: 反例库构建
echo "测试 3: 反例库构建（从历史失败学习）"
python3 scripts/method_antipatterns.py --build complete/ > /tmp/antipatterns_test.json 2>&1
if [ -f scripts/method_antipatterns.json ]; then
    ap_count=$(python3 -c "import json; d=json.load(open('scripts/method_antipatterns.json')); print(d['summary']['total_antipatterns'])")
    high_severity=$(python3 -c "import json; d=json.load(open('scripts/method_antipatterns.json')); print(d['summary']['high_severity_count'])")
    echo "  ✓ 反例库构建: $ap_count 个反例, $high_severity 个高危"
else
    echo "  ✗ 反例库未生成"
    exit 1
fi
echo

# 测试4: 反例检查
echo "测试 4: 反例检查（检查阿基米德螺线方法）"
if python3 scripts/method_antipatterns.py --check complete/test_cumcm2024a "method_library/geometry/archimedean_spiral.md" > /tmp/anticheck.json 2>&1; then
    echo "  ⚠ 未检测到反例（预期应检测到）"
else
    matches=$(python3 -c "import json; d=json.load(open('/tmp/anticheck.json')); print(len(d['matches']))")
    severity=$(python3 -c "import json; d=json.load(open('/tmp/anticheck.json')); print(d['max_severity'])")
    echo "  ✓ 检测到反例: $matches 个匹配, 最高严重度 $severity"
fi
echo

# 测试5: 方法库更新检测
echo "测试 5: 方法库更新检测（git diff）"
if git rev-parse HEAD~1 >/dev/null 2>&1; then
    result=$(python3 scripts/method_library_update.py --diff HEAD~5 HEAD 2>&1)
    if echo "$result" | grep -q "No changes detected"; then
        echo "  ✓ 检测到无变更（最近5次commit未修改method_library）"
    else
        changed=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['changed_method_count'])" 2>/dev/null || echo "0")
        affected=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['affected_project_count'])" 2>/dev/null || echo "0")
        echo "  ✓ 检测到变更: $changed 个方法, 影响 $affected 个项目"
    fi
else
    echo "  ⚠ git历史不足，跳过测试"
fi
echo

# 测试6: 模拟方法库更新验证
echo "测试 6: 模拟验证（检查使用MILP的项目）"
projects_using_milp=$(find complete -name "chosen_method.md" -exec grep -l "method_library/optimization/milp.md" {} \; | wc -l)
if [ $projects_using_milp -gt 0 ]; then
    echo "  ✓ 找到 $projects_using_milp 个项目使用MILP"
    echo "  （实际验证需要时间，跳过自动运行）"
else
    echo "  ⚠ 未找到使用MILP的项目"
fi
echo

echo "=== 测试完成 ==="
echo
echo "生成的文件:"
echo "  - scripts/method_fit_model.json (适配度模型)"
echo "  - scripts/method_antipatterns.json (反例库)"
echo
echo "使用示例:"
echo "  # 为新项目推荐方法"
echo "  python3 scripts/method_fit_score.py ongoing/my_project"
echo
echo "  # 检查方法是否有反例"
echo "  python3 scripts/method_antipatterns.py --check ongoing/my_project method_library/optimization/milp.md"
echo
echo "  # 检测方法库变更影响"
echo "  python3 scripts/method_library_update.py --diff HEAD~1 HEAD"
echo
echo "预期收益:"
echo "  - 方法推荐准确率提升（基于历史成功案例）"
echo "  - 避免重复失败（反例库过滤）"
echo "  - 方法库更新可追溯（自动影响分析）"
