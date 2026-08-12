import importlib.util
from pathlib import Path
from unittest.mock import Mock


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
    monkeypatch.setattr(module.subprocess, "run", run)

    result = module.compose_exec("postgres", "psql", "-Atc", "select 1")

    assert result == "survives-restart"
    run.assert_called_once_with(
        [
            "docker",
            "compose",
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
    )


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
