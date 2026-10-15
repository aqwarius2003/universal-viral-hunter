from pathlib import Path
from src.sanitize_env import sanitize_env_file


def test_sanitize_env_broken_file(tmp_path):
    env_file = tmp_path / ".env"
    sample = (
        'AI_API_KEY="my-key"\n'
        'EDITOR_SYSTEM_PROMPT="You are an expert\\nNo emojis in title"\n'
        'EDITOR_PROMPT_TEMPLATE="Analyze this tweet: {text}\n'
        'Author: {author}\n'
        'Date: {date}"\n'
        'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"\n'
        'Random rogue line with spaces\n'
    )
    env_file.write_text(sample, encoding="utf-8")

    result = sanitize_env_file(env_file)
    assert result is True

    cleaned = env_file.read_text(encoding="utf-8")
    # Prompt keys must be removed
    assert "EDITOR_PROMPT_TEMPLATE" not in cleaned
    assert "EDITOR_SYSTEM_PROMPT" not in cleaned
    assert "Author: {author}" not in cleaned or "# [sanitized]" in cleaned
    # Regular valid keys must remain
    assert 'AI_API_KEY="my-key"' in cleaned
    assert 'TELEGRAM_BOT_TOKEN="123456:ABC-DEF"' in cleaned
    # Rogue line must be commented
    assert "# [sanitized] Random rogue line with spaces" in cleaned

    # Every non-empty, non-comment line must match KEY=...
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "=" in line
        key = line.split("=", 1)[0].strip()
        assert " " not in key
