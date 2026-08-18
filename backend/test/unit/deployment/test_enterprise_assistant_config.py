from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = ROOT / ".env.example"


def _read_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_EXAMPLE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name] = value
    return values


def test_enterprise_assistant_env_example_declares_required_configuration():
    values = _read_env_example()

    assert {
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_PRODUCT_REDIRECT_URI",
        "PRODUCT_FEISHU_SOURCE_ID",
        "YUXI_CORS_ORIGINS",
    } <= values.keys()


def test_enterprise_assistant_env_example_contains_no_real_product_credentials():
    values = _read_env_example()

    for name in (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_PRODUCT_REDIRECT_URI",
        "PRODUCT_FEISHU_SOURCE_ID",
    ):
        assert values.get(name) == ""
