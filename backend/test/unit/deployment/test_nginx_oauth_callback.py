import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
CALLBACK_PATHS = ("/api/auth/feishu/callback", "/api/auth/feishu/callback/")


@pytest.mark.parametrize("callback_path", CALLBACK_PATHS)
def test_feishu_callback_uses_safe_logging_and_preserves_proxying(callback_path):
    config = (ROOT / "docker/nginx/default.conf").read_text()
    match = re.search(
        rf"location\s+=\s+{re.escape(callback_path)}\s*\{{(?P<body>[^{{}}]*)\}}",
        config,
    )

    assert match is not None, f"OAuth callback requires an exact Nginx location: {callback_path}"
    callback_location = match.group("body")
    assert "access_log /var/log/nginx/access.log feishu_callback;" in callback_location
    assert "error_log /dev/null;" in callback_location
    assert "proxy_pass http://api:5050;" in callback_location
    assert "proxy_set_header Host $host;" in callback_location
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in callback_location


def test_feishu_callback_log_format_excludes_query_and_headers():
    config = (ROOT / "docker/nginx/nginx.conf").read_text()
    match = re.search(r"log_format\s+feishu_callback\s+(?P<body>.*?);", config, re.DOTALL)

    assert match is not None, "OAuth callback requires a dedicated Nginx log format"
    log_format = match.group("body")
    for required_field in (
        "$request_method",
        "$uri",
        "$server_protocol",
        "$status",
        "$upstream_status",
        "$upstream_response_time",
    ):
        assert required_field in log_format
    variables = set(re.findall(r"\$[A-Za-z0-9_]+", log_format))
    forbidden_fields = {
        "$request",
        "$request_uri",
        "$args",
        "$http_referer",
        "$http_cookie",
    }
    assert forbidden_fields.isdisjoint(variables)
