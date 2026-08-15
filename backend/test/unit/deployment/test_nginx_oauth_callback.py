import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_feishu_callback_disables_query_logging_and_preserves_proxying():
    config = (ROOT / "docker/nginx/default.conf").read_text()
    match = re.search(
        r"location\s+=\s+/api/auth/feishu/callback\s*\{(?P<body>[^{}]*)\}",
        config,
    )

    assert match is not None, "OAuth callback requires an exact Nginx location"
    callback_location = match.group("body")
    assert "access_log off;" in callback_location
    assert "proxy_pass http://api:5050;" in callback_location
    assert "proxy_set_header Host $host;" in callback_location
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in callback_location
