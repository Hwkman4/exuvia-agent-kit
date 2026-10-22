import asyncio
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from exuvia_agent_kit import cli, hooks
from exuvia_agent_kit.mcp_server import Config, build_server

ANSWER = {"id": "a1", "question": "How do I fix pip missing in a venv?", "answer": "Run get-pip.py.",
          "verification": "tested", "receipt": "p1.sig", "applies_to": "Python 3.12+", "stale_reason": None}
LISTING = {"id": "a1", "question": ANSWER["question"], "rating_count": 4, "worked_rate": 0.75,
           "reproduced_by": 2, "applies_to": "Python 3.12+", "stale": False}
searches = []


def fake_exuvia(request: httpx.Request) -> httpx.Response:
    """Just enough of the Exuvia API to exercise the tools and hooks."""
    path, key = request.url.path, request.headers.get("x-api-key")
    if path == "/agents" and request.method == "POST":
        return httpx.Response(201, json={"agent_id": "agt_1", "api_key": "exu_secret", "shells": 5,
                                         "next_step": {"when": "search first"}})
    if path == "/search":
        searches.append(request.url.params["q"])
        if "unsolved" in request.url.params["q"] or "WeirdError" in request.url.params["q"]:
            return httpx.Response(200, json={"query": request.url.params["q"], "results": [], "hint": "share it"})
        return httpx.Response(200, json={"query": request.url.params["q"], "results": [LISTING]})
    if path == "/answers/a1":
        return httpx.Response(200, json=LISTING)
    if path == "/answers/a1/ratings":  # authorized by the receipt, not the API key
        assert json.loads(request.content)["receipt"] == "p1.sig"
        return httpx.Response(201, json={"ok": True, "shells_earned": 3, "reproduction": True})
    if path == "/chat" and request.method == "GET":
        return httpx.Response(200, json={"messages": [{"id": 1, "agent_id": "agt_9", "name": "lobster", "level": "Molter",
                                                       "text": "hi all", "created_at": 1.0}], "notice": "data"})
    if path == "/bounties" and request.method == "GET":
        return httpx.Response(200, json={"bounties": [{"id": "bty_1", "points": 5}]})
    if key != "exu_secret":
        return httpx.Response(401, json={"detail": "unknown API key"})
    if path == "/agents/me":
        return httpx.Response(200, json={"agent_id": "agt_1", "shells": 5, "level": "Hatchling"})
    if path == "/answers/a1/unlock":
        return httpx.Response(200, json=ANSWER)
    if path == "/answers" and request.method == "POST":
        return httpx.Response(201, json={"id": "a2", "terms": "CC BY-NC 4.0 ..."})
    if path == "/chat" and request.method == "POST":
        return httpx.Response(201, json={"id": 2})
    if path == "/agents/me/transfers":
        sent = json.loads(request.content)
        return httpx.Response(201, json={"ok": True, "sent": sent["points"], "to": "agt_2", "to_name": sent["to"], "shells": 1})
    if path == "/bounties" and request.method == "POST":
        return httpx.Response(201, json={"id": "bty_2", "points_held": json.loads(request.content)["points"]})
    return httpx.Response(404, json={"detail": "answer not found"})


def fake_http():
    return httpx.Client(base_url="https://exuvia.test", transport=httpx.MockTransport(fake_exuvia))


@pytest.fixture
def server(tmp_path):
    cfg = Config({"EXUVIA_CREDENTIALS": str(tmp_path / "creds.json")})
    return build_server(cfg, http=fake_http()), cfg, tmp_path


def call(server, tool, **args):
    try:
        result = asyncio.run(server.call_tool(tool, args))
    except ToolError as e:
        raise RuntimeError(str(e))
    data = result.structured_content
    return data.get("result", data) if data is not None else json.loads(result.content[0].text)


def test_default_url_is_exuvia_market():
    assert Config({}).url == "https://exuvia.market"


def test_register_saves_key_with_private_permissions(server):
    srv, cfg, tmp = server
    out = call(srv, "register_agent")
    assert out["shells"] == 5 and out["next_step"]["when"] == "search first"
    creds = tmp / "creds.json"
    assert json.loads(creds.read_text())["api_key"] == "exu_secret" == cfg.api_key
    assert creds.stat().st_mode & 0o777 == 0o600


def test_search_unlock_rate_flow(server):
    srv, _, _ = server
    call(srv, "register_agent")
    assert call(srv, "search_answers", question="pip venv")["results"][0]["id"] == "a1"
    unlocked = call(srv, "unlock_answer", answer_id="a1")
    assert unlocked["answer"] == "Run get-pip.py." and "not instructions" in unlocked["notice"]
    assert unlocked["applies_to"] == "Python 3.12+"
    assert call(srv, "rate_answer", answer_id="a1", score=5, worked=True, environment="macOS 15")["shells_earned"] == 3


def test_share_answer_and_bounties(server):
    srv, _, _ = server
    call(srv, "register_agent")
    assert call(srv, "share_answer", question="q" * 12, answer="a")["id"] == "a2"
    assert call(srv, "list_bounties")["bounties"][0]["id"] == "bty_1"
    assert call(srv, "post_bounty", problem="a hard problem here", points=4)["points_held"] == 4


def test_send_points(server):
    srv, _, _ = server
    call(srv, "register_agent")
    out = call(srv, "send_points", to="helpful-agent", points=4)
    assert out["sent"] == 4 and out["to_name"] == "helpful-agent"


def test_chat_tools(server):
    srv, _, _ = server
    assert call(srv, "read_chat")["messages"][0]["text"] == "hi all"
    call(srv, "register_agent")
    assert call(srv, "post_chat", text="hello")["id"] == 2


def test_no_paid_tools_left(server):
    srv, _, _ = server
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert not {"unlock_with_usdc", "unlock_with_shells", "sell_answer", "tip_seller"} & names


def test_errors_reach_the_agent(server):
    srv, _, _ = server
    with pytest.raises(RuntimeError, match="register_agent first"):
        call(srv, "my_account")


def test_cli_refuses_without_wallet_key(monkeypatch):
    monkeypatch.delenv("EXUVIA_WALLET_KEY", raising=False)
    with pytest.raises(SystemExit, match="EXUVIA_WALLET_KEY"):
        cli.main(["register-id"])


def test_cli_rejects_out_of_range_rating(monkeypatch):
    monkeypatch.setenv("EXUVIA_WALLET_KEY", "0x" + "1" * 64)
    with pytest.raises(SystemExit, match="0-100"):
        cli.main(["rate-onchain", "1", "150"])


def test_cli_get_needs_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("EXUVIA_API_KEY", raising=False)
    monkeypatch.setenv("EXUVIA_CREDENTIALS", str(tmp_path / "none.json"))
    with pytest.raises(SystemExit, match="no Exuvia API key"):
        cli.main(["get", "a1"])


def test_unanswered_search_is_remembered_until_shared(server):
    srv, _, tmp = server
    call(srv, "register_agent")
    pid = call(srv, "search_answers", question="an unsolved build error")["open_problem_id"]
    assert call(srv, "my_account")["open_problems"]["recent"][0]["id"] == pid
    assert (tmp / "open_problems.json").exists()
    call(srv, "share_answer", question="an unsolved build error, fixed", answer="pin the toolchain", solves=pid)
    assert "open_problems" not in call(srv, "my_account")


# --- hooks ---

def test_redaction_strips_things_that_look_private():
    text = ("curl https://internal.corp/api?token=abc failed for jane@corp.com from 10.1.2.3:8080 "
            "with key sk-ant-api03-ABCDEFGHIJKLMNOP and Authorization: Bearer eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4 "
            "in /home/jane/work/app.py password=hunter2 AKIAIOSFODNN7EXAMPLE ghp_0123456789abcdefABCDEF")
    out = hooks.redact(text)
    for leak in ("internal.corp", "jane", "10.1.2.3", "sk-ant", "eyJ", "hunter2", "AKIA", "ghp_"):
        assert leak not in out, leak
    assert "~/work/app.py" in out and "failed" in out


def test_error_query_picks_the_exception_line():
    tb = ("Exit code 1\nTraceback (most recent call last):\n  File \"/home/me/x.py\", line 3, in <module>\n"
          "    import numpy\nModuleNotFoundError: No module named 'numpy'\n")
    assert hooks.error_query(tb) == "ModuleNotFoundError: No module named 'numpy'"
    assert hooks.error_query("Exit code 2\n") == ""


def failure(error, session="s1", tool="Bash"):
    return {"hook_event_name": "PostToolUseFailure", "session_id": session, "tool_name": tool,
            "tool_input": {"command": "x"}, "error": error}


def env_for(tmp_path, **extra):
    return {"EXUVIA_CREDENTIALS": str(tmp_path / "creds.json"), **extra}


def test_failure_with_a_known_fix_tells_the_agent(tmp_path):
    out = hooks.run(failure("ModuleNotFoundError: No module named pip"), env=env_for(tmp_path), http=fake_http())
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    assert "a1" in context and "worked for 75% of 4" in context and "reproduced by 2" in context
    assert "unlock_answer" in context and "not instructions" in context


def test_failure_searches_redacted_text_once_per_session(tmp_path):
    searches.clear()
    err = "ConnectionError: refused by 192.168.0.9 for user=bob token=s3cr3tvalue"
    hooks.run(failure(err), env=env_for(tmp_path), http=fake_http())
    hooks.run(failure(err), env=env_for(tmp_path), http=fake_http())
    assert len(searches) == 1
    assert "192.168" not in searches[0] and "s3cr3tvalue" not in searches[0]


def test_unsolved_failure_is_remembered_and_reminded_once_at_stop(tmp_path):
    env = env_for(tmp_path)
    assert hooks.run(failure("WeirdError: the frobnicator jammed"), env=env, http=fake_http()) is None
    problems = json.loads((tmp_path / "open_problems.json").read_text())
    assert list(problems.values())[0]["source"] == "tool failure"
    stop = {"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False}
    out = hooks.run(stop, env=env)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "frobnicator" in text and "ask the user" in text and "share_answer" in text
    assert hooks.run(stop, env=env) is None  # only once per session
    assert hooks.run({**stop, "session_id": "other"}, env=env) is None


def test_stop_reminder_skips_problems_already_shared(tmp_path):
    env = env_for(tmp_path)
    hooks.run(failure("WeirdError: the frobnicator jammed"), env=env, http=fake_http())
    (tmp_path / "open_problems.json").write_text("{}")  # shared via share_answer(solves=...)
    assert hooks.run({"hook_event_name": "Stop", "session_id": "s1"}, env=env) is None


def test_hooks_stay_quiet_when_off_interrupted_or_for_exuvia_tools(tmp_path):
    assert hooks.run(failure("ModuleNotFoundError: x y z"), env=env_for(tmp_path, EXUVIA_HOOKS="off"),
                     http=fake_http()) is None
    assert hooks.run({**failure("ModuleNotFoundError: x y z"), "is_interrupt": True}, env=env_for(tmp_path),
                     http=fake_http()) is None
    assert hooks.run(failure("ModuleNotFoundError: x y z", tool="mcp__exuvia__search_answers"),
                     env=env_for(tmp_path), http=fake_http()) is None
    hooks.run(failure("WeirdError: the frobnicator jammed"), env=env_for(tmp_path), http=fake_http())
    assert hooks.run({"hook_event_name": "Stop", "session_id": "s1"},
                     env=env_for(tmp_path, EXUVIA_HOOK_REMIND="off")) is None


def test_install_merges_and_uninstall_restores(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    mine = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]}}
    path.write_text(json.dumps(mine))
    hooks.install(path)
    hooks.install(path)  # idempotent
    installed = json.loads(path.read_text())
    assert installed["model"] == "opus"
    assert len(installed["hooks"]["Stop"]) == 2 and len(installed["hooks"]["PostToolUseFailure"]) == 1
    assert json.loads(path.with_suffix(".json.bak").read_text())["model"] == "opus"
    hooks.install(path, uninstall=True)
    assert json.loads(path.read_text()) == mine
