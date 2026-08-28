from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[4]


def test_nginx_applies_security_headers_and_removes_server_header():
    config = (ROOT / "docker/nginx/nginx.conf").read_text()

    assert "server_tokens off;" in config
    assert 'more_clear_headers "Server";' in config
    assert 'add_header Content-Security-Policy "' in config
    assert 'add_header X-Content-Type-Options "nosniff" always;' in config
    assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in config
    assert 'add_header X-Frame-Options "DENY" always;' in config


def test_nginx_does_not_forward_or_log_untrusted_forwarded_for():
    http_config = (ROOT / "docker/nginx/nginx.conf").read_text()
    site_config = (ROOT / "docker/nginx/default.conf").read_text()

    assert "$http_x_forwarded_for" not in http_config
    assert "$request_uri" not in http_config
    assert "$args" not in http_config
    assert "$proxy_add_x_forwarded_for" not in site_config
    assert site_config.count("proxy_set_header X-Forwarded-For $remote_addr;") == 3
    assert "proxy_hide_header Server;" in site_config


def test_web_image_installs_header_filter_module_from_maintained_alpine():
    dockerfile = (ROOT / "docker/web.Dockerfile").read_text()

    assert "FROM alpine:3.22 AS production" in dockerfile
    assert "nginx-mod-http-headers-more" in dockerfile


def test_api_server_header_is_disabled_in_compose_commands():
    development = (ROOT / "docker-compose.yml").read_text()
    production = (ROOT / "docker-compose.prod.yml").read_text()

    assert "uvicorn server.main:app" in development
    assert "uvicorn server.main:app" in production
    assert "--no-server-header" in development
    assert "--no-server-header" in production


def test_tls_overlay_requires_certificates_and_redirects_http():
    compose = (ROOT / "docker-compose.tls.yml").read_text()
    tls_config = (ROOT / "docker/nginx/default.tls.conf").read_text()

    assert "YUXI_TLS_CERT_PATH:?" in compose
    assert "YUXI_TLS_KEY_PATH:?" in compose
    assert '"443:443"' in compose
    assert "listen 443 ssl;" in tls_config
    assert "return 308 https://$host$request_uri;" in tls_config
