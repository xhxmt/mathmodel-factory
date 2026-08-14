from web.backend.content_blocks import ContentAction, ContentBlock, NodeOutput


def test_node_output_separates_full_ui_payload_from_bounded_llm_context():
    output = NodeOutput(
        node_id="directions",
        title="分层召回",
        blocks=[
            ContentBlock(
                id="directions.methods",
                type="collection",
                label="方法",
                render_type="method_cards",
                content=[{"id": "milp", "title": "混合整数规划"}],
                actions=[
                    ContentAction(
                        id="select_method",
                        label="选择",
                        payload={"direction_id": "milp"},
                        style="primary",
                    )
                ],
                meta={"cache_key": "safe"},
            )
        ],
        metadata={"internal_revision": 7},
    )

    ui = output.to_ui_dict()
    llm = output.to_llm_context()

    assert ui["blocks"][0]["actions"][0]["payload"]["direction_id"] == "milp"
    assert ui["metadata"]["internal_revision"] == 7
    assert "select_method" not in llm
    assert "internal_revision" not in llm
    assert "混合整数规划" in llm


def test_node_output_marks_truncated_model_context():
    output = NodeOutput(
        node_id="long",
        title="长内容",
        blocks=[
            ContentBlock(
                id="long.text",
                type="document",
                label="正文",
                render_type="markdown",
                content="甲" * 1_000,
            )
        ],
    )

    context = output.to_llm_context(max_chars=120)

    assert len(context) <= 120
    assert context.endswith("…[内容已按模型上下文上限截断]")
