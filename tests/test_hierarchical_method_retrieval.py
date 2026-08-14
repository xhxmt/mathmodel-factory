from scripts.method_retrieve import rank_methods


def test_hierarchical_retrieval_keeps_branch_and_direct_leaf_evidence():
    entries = [
        {
            "domain": "Operations Research",
            "subdomain": "Programming Theory",
            "hierarchy": ["Operations Research", "Programming Theory", "Linear Programming"],
            "method": "Mixed Integer Programming",
            "name_zh": "混合整数规划",
            "path": "method_library/hmml/methods/mip.md",
            "keywords": ["资源分配", "生产调度"],
            "applicable_problem_types": ["离散决策"],
            "required_data": [],
            "solver_stack": [],
        },
        {
            "domain": "Statistics",
            "subdomain": "Regression",
            "hierarchy": ["Statistics", "Regression"],
            "method": "Linear Regression",
            "name_zh": "线性回归",
            "path": "method_library/hmml/methods/linear-regression.md",
            "keywords": ["预测"],
            "applicable_problem_types": [],
            "required_data": [],
            "solver_stack": [],
        },
    ]

    ranked = rank_methods(entries, "生产调度中的离散资源分配优化", top_k=2)

    assert ranked[0]["method"] == "Mixed Integer Programming"
    assert ranked[0]["hierarchy_path"].endswith("Linear Programming")
    assert ranked[0]["retrieval_stage"] == "hierarchy+leaf"
    assert ranked[0]["leaf_score"] > 0
    assert len(rank_methods(entries, "生产调度", top_k=0)) == len(entries)
