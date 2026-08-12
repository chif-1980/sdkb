import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts" / "phase1_smoke.py"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("phase1_smoke", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_script_requires_health_and_persistence_marker():
    module = _load_smoke_module()

    assert module.required_checks() == (
        "api_health",
        "postgres_marker",
        "minio_health",
        "milvus_health",
    )


def test_compose_exec_uses_phase1_environment(monkeypatch):
    module = _load_smoke_module()
    completed = Mock(stdout=" survives-restart\n")
    run = Mock(return_value=completed)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "unrelated")
    monkeypatch.setattr(module.subprocess, "run", run)

    result = module.compose_exec("postgres", "psql", "-Atc", "select 1")

    assert result == "survives-restart"
    run.assert_called_once_with(
        [
            "docker",
            "compose",
            "--project-name",
            "quickdone-kb-yuxi",
            "-f",
            "compose.phase1.yml",
            "--env-file",
            ".env",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-Atc",
            "select 1",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_compose_exec_reports_timeout_without_command_or_output(monkeypatch):
    module = _load_smoke_module()
    run = Mock(
        side_effect=subprocess.TimeoutExpired(
            ["private-command", "--env-file", ".env"],
            timeout=30,
            output="stdout-secret",
            stderr="stderr-secret",
        )
    )
    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(module.SmokeCheckError) as exc_info:
        module.compose_exec("postgres", "psql")

    message = str(exc_info.value)
    assert "postgres" in message
    assert "30" in message
    assert "private-command" not in message
    assert ".env" not in message
    assert "stdout-secret" not in message
    assert "stderr-secret" not in message


def test_compose_exec_reports_only_service_and_exit_code(monkeypatch):
    module = _load_smoke_module()
    secret_uri = "https://user:password@example.test/path?token=private"
    run = Mock(
        side_effect=subprocess.CalledProcessError(
            17,
            ["private-command", "--env-file", ".env"],
            output="stdout-secret",
            stderr=f"database unavailable: {secret_uri}",
        )
    )
    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(module.SmokeCheckError) as exc_info:
        module.compose_exec("postgres", "psql")

    message = str(exc_info.value)
    assert message == "postgres check failed with exit code 17"
    assert "database unavailable" not in message
    assert secret_uri not in message
    assert "private-command" not in message
    assert ".env" not in message
    assert "stdout-secret" not in message


def test_main_checks_live_services_and_existing_marker(monkeypatch, capsys):
    module = _load_smoke_module()
    fetch_json = Mock(return_value={"status": "ok"})
    compose_exec = Mock(side_effect=["survives-restart", "ok"])
    minio_response = Mock(status=200)
    minio_context = Mock()
    minio_context.__enter__ = Mock(return_value=minio_response)
    minio_context.__exit__ = Mock(return_value=False)
    urlopen = Mock(return_value=minio_context)
    monkeypatch.setattr(module, "fetch_json", fetch_json)
    monkeypatch.setattr(module, "compose_exec", compose_exec)
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)

    module.main()

    fetch_json.assert_called_once_with("http://127.0.0.1:5050/api/system/health")
    assert compose_exec.call_args_list == [
        (
            (
                "postgres",
                "psql",
                "-U",
                "postgres",
                "-d",
                "yuxi",
                "-Atc",
                "select value from phase1_smoke where id=1",
            ),
        ),
        (
            (
                "milvus",
                "curl",
                "--fail",
                "--silent",
                "http://127.0.0.1:9091/healthz",
            ),
        ),
    ]
    urlopen.assert_called_once_with("http://127.0.0.1:9000/minio/health/live", timeout=10)
    assert capsys.readouterr().out == "phase1 smoke: PASS\n"


@pytest.mark.parametrize("api_health", [{"status": "error"}, ["ok"]])
def test_main_rejects_invalid_api_health(monkeypatch, api_health):
    module = _load_smoke_module()
    monkeypatch.setattr(module, "fetch_json", Mock(return_value=api_health))

    with pytest.raises(module.SmokeCheckError, match="API health"):
        module.main()


def test_main_rejects_wrong_postgres_marker(monkeypatch):
    module = _load_smoke_module()
    monkeypatch.setattr(module, "fetch_json", Mock(return_value={"status": "ok"}))
    monkeypatch.setattr(module, "compose_exec", Mock(return_value="wrong-marker"))

    with pytest.raises(module.SmokeCheckError, match="Postgres marker"):
        module.main()


def test_main_rejects_non_200_minio_health(monkeypatch):
    module = _load_smoke_module()
    minio_context = Mock()
    minio_context.__enter__ = Mock(return_value=Mock(status=503))
    minio_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, "fetch_json", Mock(return_value={"status": "ok"}))
    monkeypatch.setattr(module, "compose_exec", Mock(return_value="survives-restart"))
    monkeypatch.setattr(module.urllib.request, "urlopen", Mock(return_value=minio_context))

    with pytest.raises(module.SmokeCheckError, match="MinIO health"):
        module.main()


def test_main_rejects_empty_milvus_health(monkeypatch):
    module = _load_smoke_module()
    minio_context = Mock()
    minio_context.__enter__ = Mock(return_value=Mock(status=200))
    minio_context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module, "fetch_json", Mock(return_value={"status": "ok"}))
    monkeypatch.setattr(
        module,
        "compose_exec",
        Mock(side_effect=["survives-restart", ""]),
    )
    monkeypatch.setattr(module.urllib.request, "urlopen", Mock(return_value=minio_context))

    with pytest.raises(module.SmokeCheckError, match="Milvus health"):
        module.main()


def test_main_still_rejects_bad_api_health_with_python_optimization():
    code = f"""
import importlib.util
spec = importlib.util.spec_from_file_location("phase1_smoke", {str(SCRIPT_PATH)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.fetch_json = lambda url: {{"status": "error"}}
try:
    module.main()
except module.SmokeCheckError:
    raise SystemExit(23)
"""

    result = subprocess.run([sys.executable, "-O", "-c", code], check=False)

    assert result.returncode == 23


def test_run_cli_returns_nonzero_for_smoke_failure(monkeypatch, capsys):
    module = _load_smoke_module()
    monkeypatch.setattr(
        module,
        "main",
        Mock(side_effect=module.SmokeCheckError("API health failed")),
    )

    assert module.run_cli() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "phase1 smoke: FAIL: API health failed\n"
