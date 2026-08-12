import json
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_TIMEOUT_SECONDS = 30


class SmokeCheckError(RuntimeError):
    pass


def required_checks():
    return ("api_health", "postgres_marker", "minio_health", "milvus_health")


def fetch_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def compose_exec(service: str, *args: str):
    try:
        result = subprocess.run(
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
                service,
                *args,
            ],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
            timeout=COMPOSE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise SmokeCheckError(f"{service} check timed out after {COMPOSE_TIMEOUT_SECONDS} seconds") from None
    except subprocess.CalledProcessError as exc:
        raise SmokeCheckError(f"{service} check failed with exit code {exc.returncode}") from None
    return result.stdout.strip()


def main():
    api_health = fetch_json("http://127.0.0.1:5050/api/system/health")
    if not isinstance(api_health, dict) or api_health.get("status") != "ok":
        raise SmokeCheckError("API health check failed")
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
    if marker != "survives-restart":
        raise SmokeCheckError("Postgres marker check failed")
    with urllib.request.urlopen("http://127.0.0.1:9000/minio/health/live", timeout=10) as response:
        if response.status != 200:
            raise SmokeCheckError("MinIO health check failed")
    milvus_health = compose_exec(
        "milvus",
        "curl",
        "--fail",
        "--silent",
        "http://127.0.0.1:9091/healthz",
    )
    if not milvus_health:
        raise SmokeCheckError("Milvus health check failed")
    print("phase1 smoke: PASS")


def run_cli():
    try:
        main()
    except SmokeCheckError as exc:
        print(f"phase1 smoke: FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
