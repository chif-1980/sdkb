import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]


def test_phase1_compose_uses_isolated_project_and_required_services():
    config = yaml.safe_load((ROOT / "compose.phase1.yml").read_text())
    assert config["name"] == "quickdone-kb-yuxi"
    expected_container_names = {
        "api": "quickdone-kb-api",
        "worker": "quickdone-kb-worker",
        "web": "quickdone-kb-admin-web",
        "postgres": "quickdone-kb-postgres",
        "redis": "quickdone-kb-redis",
        "minio": "quickdone-kb-minio",
        "etcd": "quickdone-kb-etcd",
        "milvus": "quickdone-kb-milvus",
        "sandbox-provisioner": "quickdone-kb-sandbox-provisioner",
    }
    assert set(config["services"]) == set(expected_container_names)
    assert "graph" not in config["services"]
    for service, container_name in expected_container_names.items():
        assert config["services"][service]["extends"] == {
            "file": "docker-compose.yml",
            "service": service,
        }
        assert config["services"][service]["container_name"] == container_name
    assert config["services"]["api"]["environment"]["YUXI_ENV"] == "development"
    assert config["networks"] == {"app-network": {"name": "quickdone-kb-yuxi-network"}}
    assert set(config["volumes"]) == {"nltk_data"}


def test_secrets_and_runtime_volumes_are_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for entry in (".env", "docker/volumes/", "artifacts/acceptance/"):
        assert entry in ignored


def test_web_dockerfile_uses_the_declared_pnpm_version():
    package_json = json.loads((ROOT / "web/package.json").read_text())
    package_manager = package_json["packageManager"]
    assert package_manager.startswith("pnpm@")
    expected_version = package_manager.removeprefix("pnpm@")

    dockerfile = (ROOT / "docker/web.Dockerfile").read_text()
    installed_versions = re.findall(r"npm install -g pnpm@([^\s]+)", dockerfile)
    assert installed_versions
    assert set(installed_versions) == {expected_version}


def _parse_dotenv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", 1)
        if name in values:
            raise ValueError(f"duplicate dotenv name: {name}")
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
