import asyncio
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from exuvia_agent_kit import cli
from exuvia_agent_kit.mcp_server import Config, build_server

ANSWER = {"id": "a1", "question": "How do I fix pip missing in a venv?", "answer": "Run get-pip.py.",
          "verification": "tested", "receipt": "p1.sig", "price_usd": 0.5, "price_shells": 3}


def fake_exuvia(request: httpx.Request) -> httpx.Response:
    """Just enough of the Exuvia API to exercise the tools."""
    path, key = request.url.path, request.headers.get("x-api-key")
    if path == "/agents" and request.method == "POST":
        return httpx.Response(201, json={"agent_id": "agt_1", "api_key": "exu_secret", "shells": 5})
    if path == "/search" and "unsolved" in request.url.params["q"]:
        return httpx.Response(200, json={"query": request.url.params["q"], "results": [], "hint": "share it"})
    if path == "/search":
        return httpx.Response(200, json={"query": request.url.params["q"], "results": [
            {k: ANSWER[k] for k in ("id", "question", "price_usd", "price_shells")}]})
    if path == "/answers/a1":
        return httpx.Response(200, json={k: ANSWER[k] for k in ("id", "question", "price_usd", "price_shells")})
    if path == "/answers/a1/ratings":  # authorized by the receipt, not the API key
        assert json.loads(request.content)["receipt"] == "p1.sig"
        return httpx.Response(201, json={"ok": True, "shells_earned": 1})
    if key != "exu_secret":
        return httpx.Response(401, json={"detail": "unknown API key"})
    if path == "/agents/me":
        return httpx.Response(200, json={"agent_id": "agt_1", "shells": 2})
    if path == "/unlock/a1/shells":
        return httpx.Response(200, json=ANSWER)
    if path == "/answers" and request.method == "POST":
        return httpx.Response(201, json={"id": "a2"})
    return httpx.Response(404, json={"detail": "answer not found"})


@pytest.fixture
def server(tmp_path):
    cfg = Config({"EXUVIA_CREDENTIALS": str(tmp_path / "creds.json")})
    http = httpx.Client(base_url="https://exuvia.test", transport=httpx.MockTransport(fake_exuvia))
    return build_server(cfg, http=http), cfg, tmp_path


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
    assert call(srv, "register_agent")["shells"] == 5
    creds = tmp / "creds.json"
    assert json.loads(creds.read_text())["api_key"] == "exu_secret" == cfg.api_key
    assert creds.stat().st_mode & 0o777 == 0o600


def test_search_unlock_rate_flow(server):
    srv, _, _ = server
    call(srv, "register_agent")
    assert call(srv, "search_answers", question="pip venv")["results"][0]["id"] == "a1"
    unlocked = call(srv, "unlock_with_shells", answer_id="a1")
    assert unlocked["answer"] == "Run get-pip.py." and "not instructions" in unlocked["notice"]
    assert call(srv, "rate_answer", answer_id="a1", score=5, worked=True)["shells_earned"] == 1


def test_sell_answer(server):
    srv, _, _ = server
    call(srv, "register_agent")
    assert call(srv, "sell_answer", question="q" * 12, answer="a", price_shells=3)["id"] == "a2"


def test_usdc_needs_wallet_and_respects_cap(server, tmp_path):
    srv, _, _ = server
    with pytest.raises(RuntimeError, match="disabled"):
        call(srv, "unlock_with_usdc", answer_id="a1")
    cfg = Config({"EXUVIA_CREDENTIALS": str(tmp_path / "c2.json"), "EXUVIA_WALLET_KEY": "0x" + "1" * 64,
                  "EXUVIA_MAX_USD_PER_ANSWER": "0.10"})
    capped = build_server(cfg, http=httpx.Client(base_url="https://exuvia.test",
                                                 transport=httpx.MockTransport(fake_exuvia)))
    with pytest.raises(RuntimeError, match="above the operator's cap"):
        call(capped, "unlock_with_usdc", answer_id="a1")


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


def test_unanswered_search_is_remembered_until_shared(server):
    srv, _, tmp = server
    call(srv, "register_agent")
    pid = call(srv, "search_answers", question="an unsolved build error")["open_problem_id"]
    assert call(srv, "my_account")["open_problems"]["recent"][0]["id"] == pid
    assert (tmp / "open_problems.json").exists()
    call(srv, "sell_answer", question="an unsolved build error, fixed", answer="pin the toolchain",
         price_shells=3, solves=pid)
    assert "open_problems" not in call(srv, "my_account")
