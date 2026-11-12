"""爆破停损分类器：写脚本不能算“尝试登录”。"""

from app.chat.engine import _looks_like_auth_attempt


def test_heredoc_script_write_is_not_an_auth_attempt() -> None:
    command = "cat > /tmp/dev.py <<'PY'\nimport requests\npassword = 'x'\nPY\npython3 /tmp/dev.py"
    assert _looks_like_auth_attempt("run_shell", {"command": command}) is False


def test_redirected_script_write_is_not_an_auth_attempt() -> None:
    assert _looks_like_auth_attempt(
        "run_shell", {"command": "echo 'password=x' > /tmp/a.txt"}
    ) is False


def test_real_brute_tools_still_count() -> None:
    assert _looks_like_auth_attempt(
        "run_shell", {"command": "hydra -l admin -P pass.txt ssh://10.0.0.1"}
    )
    assert _looks_like_auth_attempt(
        "verify_http",
        {"url": "https://x/login", "method": "POST", "body": "username=a&password=b"},
    )
