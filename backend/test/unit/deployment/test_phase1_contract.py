import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[4]
PYPI_INDEX = "https://pypi.org/simple"
PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_ARTIFACT_HOST = "files.pythonhosted.org"
PYTORCH_ARTIFACT_HOSTS = {"download-r2.pytorch.org", PYPI_ARTIFACT_HOST}


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor(
    "!override", lambda loader, node: loader.construct_sequence(node, deep=True)
)


def _compose_version_at_least(value: str, minimum: tuple[int, int, int]) -> bool:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        return False
    return tuple(map(int, match.groups())) >= minimum


def _render_phase1_config() -> dict:
    version = subprocess.run(
        ["docker", "compose", "version", "--short"],
        check=False,
        capture_output=True,
        text=True,
    )
    if version.returncode or not _compose_version_at_least(version.stdout, (2, 24, 4)):
        raise AssertionError("phase one requires Docker Compose 2.24.4 or newer")

    command = [
        "docker",
        "compose",
        "-f",
        str(ROOT / "compose.phase1.yml"),
        "--env-file",
        str(ROOT / ".env.example"),
        "config",
        "--no-env-resolution",
        "--format",
        "json",
    ]
    environment = {
        name: os.environ[name]
        for name in ("HOME", "PATH", "TMPDIR")
        if name in os.environ
    }
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise AssertionError(f"phase one Compose config failed with exit code {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError("phase one Compose config did not return valid JSON") from exc


def test_phase1_compose_uses_isolated_project_and_required_services():
    config = yaml.load((ROOT / "compose.phase1.yml").read_text(), Loader=ComposeLoader)
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
    assert set(config["volumes"]) == {
        "nltk_data",
        "yuxi_data",
        "models_data",
        "postgres_data",
        "redis_data",
        "minio_data",
        "minio_config",
        "milvus_etcd_data",
        "milvus_data",
        "milvus_logs",
    }


def test_phase1_runtime_uses_images_and_named_volumes_without_host_file_shares():
    config = _render_phase1_config()

    allowed_bind = "/var/run/docker.sock"
    for service in config["services"].values():
        for volume in service.get("volumes", []):
            if volume["type"] == "bind":
                assert volume["source"] == allowed_bind

    expected_named_targets = {
        "api": {"/app/saves", "/app/models", "/root/nltk_data"},
        "worker": {"/app/saves", "/app/models"},
        "sandbox-provisioner": {"/app/saves"},
        "postgres": {"/var/lib/postgresql/data"},
        "redis": {"/data"},
        "minio": {"/minio_data", "/root/.minio"},
        "etcd": {"/etcd"},
        "milvus": {"/var/lib/milvus", "/var/lib/milvus/logs"},
    }
    for service, targets in expected_named_targets.items():
        named_targets = {
            volume["target"]
            for volume in config["services"][service].get("volumes", [])
            if volume["type"] == "volume"
        }
        assert named_targets == targets

    minio_environment = config["services"]["minio"]["environment"]
    milvus_environment = config["services"]["milvus"]["environment"]
    assert milvus_environment["MINIO_ACCESS_KEY_ID"] == minio_environment["MINIO_ACCESS_KEY"]
    assert milvus_environment["MINIO_SECRET_ACCESS_KEY"] == minio_environment["MINIO_SECRET_KEY"]


def test_phase1_config_rendering_does_not_resolve_private_env_file():
    config = _render_phase1_config()
    api_environment = config["services"]["api"]["environment"]
    private_names = {
        "OPENAI_API_KEY",
        "JWT_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
    }
    assert private_names.isdisjoint(api_environment)


def test_phase1_documents_compose_version_required_by_override_tag():
    deployment = (ROOT / "docs/advanced/deployment.md").read_text()
    assert "阶段一隔离部署要求 Docker Compose (v2.24.4+)" in deployment


def test_phase1_compose_version_gate_matches_override_requirement():
    assert not _compose_version_at_least("2.24.3", (2, 24, 4))
    assert _compose_version_at_least("2.24.4", (2, 24, 4))
    assert _compose_version_at_least("v5.3.1", (2, 24, 4))


def test_sandbox_env_does_not_expose_management_credentials():
    sandbox_environment = _parse_dotenv(
        (ROOT / "docker/sandbox_provisioner/sandbox.env").read_text()
    )
    forbidden_names = {
        "OPENAI_API_KEY",
        "JWT_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "MINIO_ACCESS_KEY",
        "MINIO_SECRET_KEY",
        "SANDBOX_PROVISIONER_TOKEN",
    }
    assert forbidden_names.isdisjoint(sandbox_environment)


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
    assert "--index-url https://pypi.org/simple" in dockerfile
    assert "pypi.tuna.tsinghua.edu.cn" not in dockerfile


def test_sandbox_pip_uses_supported_index_url_option():
    dockerfile = (ROOT / "docker/sandbox_provisioner/Dockerfile").read_text()
    assert "--index-url https://pypi.org/simple" in dockerfile
    assert "--index " not in dockerfile


def test_api_dockerfile_uses_official_debian_package_sources():
    dockerfile = (ROOT / "docker/api.Dockerfile").read_text()
    assert re.search(r"^FROM\s+python:3\.13-slim\s*$", dockerfile, re.MULTILINE)
    assert "mirrors.tuna.tsinghua.edu.cn" not in dockerfile
    assert "/etc/apt/sources" not in dockerfile
    assert "apt-get update" in dockerfile


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
