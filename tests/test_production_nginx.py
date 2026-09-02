from pathlib import Path

CONFIG = Path(__file__).parents[1] / "infra" / "production" / "nginx.conf.example"


def test_public_proxy_allows_only_validated_v1_endpoints() -> None:
    text = CONFIG.read_text(encoding="utf-8")

    assert "location ~ ^/v1/(models|chat/completions)$ {" in text
    assert "location ^~ /v1/" not in text
    assert "location /v1/" not in text
    assert text.count("proxy_pass http://enterprise_ai_gateway;") == 1


def test_all_other_paths_are_denied() -> None:
    text = CONFIG.read_text(encoding="utf-8")

    fallback = text.index("    location / {")
    closing = text.index("    }", fallback)
    assert "return 404;" in text[fallback:closing]


def test_access_logs_omit_request_secrets_and_query_strings() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    start = text.index("log_format enterprise_ai_safe")
    end = text.index(";", start)
    log_format = text[start:end]

    assert "$uri" in log_format
    assert "$request_method" in log_format
    assert "$request " not in log_format
    assert "$request_uri" not in log_format
    assert "$args" not in log_format
    assert "$http_referer" not in log_format
    assert "$http_cookie" not in log_format
    assert "$http_authorization" not in log_format
    assert text.count("access_log /var/log/nginx/enterprise-ai-access.log enterprise_ai_safe;") == 2
