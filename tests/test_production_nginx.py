from pathlib import Path

CONFIG = Path(__file__).parents[1] / "infra" / "production" / "nginx.conf.example"


def test_public_proxy_allows_only_validated_v1_path_and_method_pairs() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    models_start = text.index("    location = /v1/models {")
    chat_start = text.index("    location = /v1/chat/completions {")
    fallback_start = text.index("    location / {", chat_start)
    models_block = text[models_start:chat_start]
    chat_block = text[chat_start:fallback_start]

    assert "if ($request_method != GET) {" in models_block
    assert 'add_header Allow "GET" always;' in models_block
    assert "if ($request_method != POST) {" not in models_block
    assert models_block.count("proxy_pass http://enterprise_ai_gateway;") == 1
    assert "if ($request_method != POST) {" in chat_block
    assert 'add_header Allow "POST" always;' in chat_block
    assert "if ($request_method != GET) {" not in chat_block
    assert chat_block.count("proxy_pass http://enterprise_ai_gateway;") == 1
    assert text.count("return 405;") == 2
    assert "location ^~ /v1/" not in text
    assert "location /v1/" not in text
    assert text.count("proxy_pass http://enterprise_ai_gateway;") == 2


def test_proxy_rejects_unknown_hosts_and_forwards_only_the_canonical_host() -> None:
    text = CONFIG.read_text(encoding="utf-8")

    assert text.count("if ($host != $server_name) {") == 2
    assert text.count("return 421;") == 2
    assert "return 308 https://$server_name$request_uri;" in text
    assert "return 308 https://$host$request_uri;" not in text
    assert text.count("proxy_set_header Host $server_name;") == 2
    assert text.count("proxy_set_header X-Forwarded-Host $server_name;") == 2
    assert "proxy_set_header Host $host;" not in text
    assert "proxy_set_header X-Forwarded-Host $host;" not in text


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
