from codd.ask_user_question_adapter import (
    format_ask_for_ntfy,
    parse_user_answer,
    send_ask_items,
)
from codd.coherence_engine import EventBus, use_coherence_bus
from codd.hitl_session import HitlSession
from codd.lexicon import AskItem, AskOption, load_lexicon


def _ask_item() -> AskItem:
    return AskItem(
        id="q_auth",
        question="Which auth method?",
        options=[
            AskOption(id="password", label="Password"),
            AskOption(id="oauth", label="OAuth", recommended=True),
            AskOption(id="sso", label="SSO"),
        ],
        recommended_id="oauth",
    )


def test_format_ask_for_ntfy_marks_recommended_option():
    message = format_ask_for_ntfy(_ask_item())

    assert "Q: Which auth method?" in message
    assert "[B] OAuth (recommended)" in message


def test_parse_user_answer_maps_letter_to_option_id():
    assert parse_user_answer("B", _ask_item()) == "oauth"


def test_parse_user_answer_accepts_option_id():
    assert parse_user_answer("sso", _ask_item()) == "sso"


def test_parse_user_answer_unknown_is_free_text():
    assert parse_user_answer("Use corporate IdP", _ask_item()) == "Use corporate IdP"


def test_send_ask_items_non_claude_uses_ntfy_and_lexicon(monkeypatch, tmp_path):
    posted: list[tuple[str, str]] = []
    asked: list[str] = []
    monkeypatch.setattr("codd.ask_user_question_adapter.is_claude_code_env", lambda: False)
    monkeypatch.setattr("codd.ask_user_question_adapter._post_ntfy", lambda topic, msg: posted.append((topic, msg)))
    monkeypatch.setattr("codd.ask_user_question_adapter._send_ask_user_question", lambda item: asked.append(item.id))
    lexicon_path = tmp_path / "project_lexicon.yaml"

    send_ask_items([_ask_item()], ntfy_topic="topic", lexicon_path=lexicon_path)

    assert posted == [("topic", format_ask_for_ntfy(_ask_item()))]
    assert asked == []
    assert load_lexicon(tmp_path).coverage_decisions[0].id == "q_auth"


def test_send_ask_items_claude_env_uses_askuserquestion(monkeypatch):
    asked: list[str] = []
    monkeypatch.setattr("codd.ask_user_question_adapter.is_claude_code_env", lambda: True)
    monkeypatch.setattr("codd.ask_user_question_adapter._send_ask_user_question", lambda item: asked.append(item.id))

    send_ask_items([_ask_item()], channels=["askuserquestion"])

    assert asked == ["q_auth"]


def test_hitl_session_confirmed_answer_needs_no_patch():
    session = HitlSession([_ask_item()])
    session.proceed_with_recommended()

    assert session.apply_answer("q_auth", "oauth") is False
    assert session.ask_items[0].status == "CONFIRMED"


def test_hitl_session_override_publishes_drift_event():
    bus = EventBus()
    session = HitlSession([_ask_item()])
    session.proceed_with_recommended()

    with use_coherence_bus(bus):
        assert session.apply_answer("q_auth", "sso") is True

    events = bus.published_events()
    assert events[0].kind == "requirement_override_drift"
    assert events[0].payload["source"] == "requirement_decision"
    assert events[0].payload["answer"] == "sso"
