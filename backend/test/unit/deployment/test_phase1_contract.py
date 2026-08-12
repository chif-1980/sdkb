from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]


def test_phase1_compose_uses_isolated_project_and_required_services():
    config = yaml.safe_load((ROOT / "compose.phase1.yml").read_text())
    assert config["name"] == "quickdone-kb-yuxi"
    expected_services = {
        "api",
        "worker",
        "web",
        "postgres",
        "redis",
        "minio",
        "etcd",
        "milvus",
        "sandbox-provisioner",
    }
    assert set(config["services"]) == expected_services
    assert "graph" not in config["services"]
    assert config["services"]["api"]["environment"]["YUXI_ENV"] == "development"


def test_secrets_and_runtime_volumes_are_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for entry in (".env", "docker/volumes/", "artifacts/acceptance/"):
        assert entry in ignored


def _parse_dotenv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        values[name] = value
    return values


def test_env_example_contains_names_but_no_secret_values():
    text = (ROOT / ".env.example").read_text()
    secret_names = (
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "JWT_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    )
    for name in secret_names:
        assert f"{name}=" in text

    values = _parse_dotenv(text)
    for name in secret_names:
        assert values[name] == ""
    assert "SANDBOX_PROVISIONER_TOKEN=" in text
    assert values["SANDBOX_PROVISIONER_TOKEN"] == "replace-me"
