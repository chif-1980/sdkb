import json
import subprocess
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def required_checks():
    return ("api_health", "postgres_marker", "minio_health", "milvus_health")


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def compose_exec(service: str, *args: str):
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.phase1.yml",
            "--env-file",
            ".env",
            "exec",
            "-T",
            service,
            *args,
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def main():
    assert fetch_json("http://127.0.0.1:5050/api/system/health")
    marker = compose_exec(
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "yuxi",
        "-Atc",
        "select value from phase1_smoke where id=1",
    )
    assert marker == "survives-restart"
    with urllib.request.urlopen("http://127.0.0.1:9000/minio/health/live", timeout=10) as response:
        assert response.status == 200
    assert compose_exec(
        "milvus",
        "curl",
        "--fail",
        "--silent",
        "http://127.0.0.1:9091/healthz",
    )
    print("phase1 smoke: PASS")


if __name__ == "__main__":
    main()
