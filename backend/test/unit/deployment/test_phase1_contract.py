import json
import re
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[4]
PYPI_INDEX = "https://pypi.org/simple"
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_ARTIFACT_HOST = "files.pythonhosted.org"
PYTORCH_ARTIFACT_HOSTS = {"download-r2.pytorch.org", PYPI_ARTIFACT_HOST}


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
    expected_command = f"RUN npm install -g pnpm@{expected_version}"

    dockerfile = (ROOT / "docker/web.Dockerfile").read_text()
    stage_pattern = re.compile(
        r"^FROM\s+\S+\s+AS\s+(?P<name>\S+)\s*$"
        r"(?P<body>.*?)(?=^FROM\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    stages = list(stage_pattern.finditer(dockerfile))

    for stage_name in ("development", "build-stage"):
        stage_bodies = [match["body"] for match in stages if match["name"] == stage_name]
        assert len(stage_bodies) == 1
        instructions = [
            line.strip()
            for line in stage_bodies[0].splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert instructions.count(expected_command) == 1


def test_python_dependencies_use_approved_package_indexes():
    with (ROOT / "backend/pyproject.toml").open("rb") as file:
        project = tomllib.load(file)

    indexes = project["tool"]["uv"]["index"]
    default_indexes = [index for index in indexes if index.get("default")]
    assert [index["url"] for index in default_indexes] == [PYPI_INDEX]

    pytorch_indexes = [index for index in indexes if index["url"] == PYTORCH_CPU_INDEX]
    assert len(pytorch_indexes) == 1
    assert pytorch_indexes[0].get("explicit") is True

    _assert_secure_dependency_lock(ROOT / "backend/uv.lock")


def test_nested_dependencies_and_sandbox_use_approved_sources():
    with (ROOT / "backend/package/pyproject.toml").open("rb") as file:
        package_project = tomllib.load(file)

    indexes = package_project["tool"]["uv"]["index"]
    default_indexes = [index for index in indexes if index.get("default")]
    assert [index["url"] for index in default_indexes] == [PYPI_INDEX]
    pytorch_indexes = [index for index in indexes if index["url"] == PYTORCH_CPU_INDEX]
    assert len(pytorch_indexes) == 1
    assert pytorch_indexes[0].get("explicit") is True

    _assert_secure_dependency_lock(ROOT / "backend/package/uv.lock")

    dockerfile = (ROOT / "docker/sandbox_provisioner/Dockerfile").read_text()
    assert "--index https://pypi.org/simple" in dockerfile
    assert "pypi.tuna.tsinghua.edu.cn" not in dockerfile


def _assert_secure_dependency_lock(path: Path) -> None:
    with path.open("rb") as file:
        lock = tomllib.load(file)

    allowed_registries = {PYPI_INDEX, PYTORCH_CPU_INDEX}
    for package in lock["package"]:
        source = package.get("source", {})
        registry = source.get("registry")
        if registry:
            assert registry in allowed_registries, package["name"]
            _assert_secure_url(registry, {urlparse(registry).hostname})

        for dependency in package.get("dependencies", []):
            dependency_registry = dependency.get("source", {}).get("registry")
            if dependency_registry:
                assert dependency_registry in allowed_registries, package["name"]
                _assert_secure_url(dependency_registry, {urlparse(dependency_registry).hostname})

        artifacts = [package.get("sdist"), *package.get("wheels", [])]
        for artifact in artifacts:
            if not artifact:
                continue
            assert artifact.get("hash", "").startswith("sha256:"), package["name"]
            if "url" not in artifact:
                continue
            parsed = _assert_secure_url(artifact["url"], PYTORCH_ARTIFACT_HOSTS)
            if source.get("registry") == PYTORCH_CPU_INDEX:
                assert parsed.hostname in PYTORCH_ARTIFACT_HOSTS, package["name"]
            else:
                assert parsed.hostname == PYPI_ARTIFACT_HOST, package["name"]

    _assert_secure_nested_urls(lock, allowed_registries)


def _assert_secure_nested_urls(value, allowed_registries: set[str]) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"registry", "index"}:
                assert isinstance(nested, str)
                assert nested in allowed_registries
                _assert_secure_url(nested, {urlparse(nested).hostname})
            elif key == "url":
                assert isinstance(nested, str)
                _assert_secure_url(nested, PYTORCH_ARTIFACT_HOSTS)
            else:
                _assert_secure_nested_urls(nested, allowed_registries)
    elif isinstance(value, list):
        for nested in value:
            _assert_secure_nested_urls(nested, allowed_registries)


def _assert_secure_url(value: str, allowed_hosts: set[str]):
    parsed = urlparse(value)
    assert parsed.scheme == "https"
    assert parsed.username is None and parsed.password is None
    assert parsed.port is None
    assert not parsed.query and not parsed.fragment
    assert parsed.hostname in allowed_hosts
    return parsed


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
