from app.preflight import run_checks


def test_missing_key_is_reported(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    failures = run_checks()
    assert failures >= 1
    assert "[FAIL] GEMINI_API_KEY is set" in capsys.readouterr().out