import json


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_gemini_retries_max_tokens_without_text_parts(monkeypatch):
    from scripts import llm_judge_call

    responses = iter(
        [
            {
                "candidates": [
                    {"finishReason": "MAX_TOKENS", "content": {"role": "model"}}
                ],
                "usageMetadata": {"thoughtsTokenCount": 8192},
            },
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "VERDICT: PASS\n"}]},
                    }
                ]
            },
        ]
    )
    requested_budgets = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data)
        requested_budgets.append(body["generationConfig"]["maxOutputTokens"])
        return FakeResponse(next(responses))

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(llm_judge_call.urllib.request, "urlopen", fake_urlopen)

    text = llm_judge_call._gemini_call("audit", "gemini-2.5-pro", 30, 8000)

    assert text == "VERDICT: PASS\n"
    assert requested_budgets == [8192, 32768]


def test_gemini_missing_parts_reports_finish_reason(monkeypatch):
    from scripts import llm_judge_call

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_judge_call.urllib.request,
        "urlopen",
        lambda request, timeout: FakeResponse(
            {
                "candidates": [
                    {"finishReason": "SAFETY", "content": {"role": "model"}}
                ],
                "promptFeedback": {"blockReason": "SAFETY"},
            }
        ),
    )

    try:
        llm_judge_call._gemini_call("audit", "gemini-2.5-pro", 30, 8000)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "finishReason=SAFETY" in message
    assert "blockReason=SAFETY" in message
