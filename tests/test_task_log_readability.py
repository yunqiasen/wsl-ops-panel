from app.tasks.executor import (
    _diagnose_text,
    _friendly_object,
    _humanize_log_line,
    _meaningful_lines,
    _suggestion_text,
)


def test_codex_remote_object_is_human_readable():
    assert _friendly_object('remote__mimi__node__openai-codex') == 'MiMI / Node / Codex CLI'
    assert _friendly_object('local__node__openai-codex') == '当前 WSL / Node / Codex CLI'


def test_npm_not_found_has_chinese_diagnosis_and_suggestion():
    stderr = 'zsh:1: command not found: npm\nworker failed: Command ... returned non-zero exit status 127.'
    assert _humanize_log_line('zsh:1: command not found: npm') == '没有找到 npm：远程 shell 没加载 Node/nvm 环境'
    assert '远程设备找不到 npm' in _diagnose_text(stderr)
    assert 'source ~/.zshrc' in _suggestion_text(stderr)


def test_npm_timing_noise_is_hidden_from_summary():
    text = 'npm timing npm Completed in 1969ms\nchanged 2 packages in 3s\n'
    assert _meaningful_lines(text) == ['已安装/更新 2 个包，用时 3s']


def test_ssh_test_node_error_is_explained():
    text = 'Warning: Identity file /tmp/id not accessible: No such file or directory.\nssh: Could not resolve hostname example.test: Name or service not known'
    assert 'SSH 主机名解析失败' in _diagnose_text(text)
    assert '无效 SSH 节点' in _suggestion_text(text)
