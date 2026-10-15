#!/usr/bin/env python3
"""
Sanitizes .env files to ensure full compatibility with Docker Compose v2.
- Removes multiline prompt configurations (which are already safely stored in SQLite).
- Escapes newlines in any remaining multiline values.
- Comments out orphan lines (lines without valid KEY= syntax).
"""
import os
import re
import sys
from pathlib import Path

# Keys that belong in SQLite system_settings, not .env
SKIP_KEYS = {
    "EDITOR_SYSTEM_PROMPT",
    "EDITOR_PROMPT_TEMPLATE",
    "X_RELEVANCE_PROMPT",
}

VALID_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_env_file(filepath: Path) -> bool:
    if not filepath.exists():
        return False

    raw_content = filepath.read_text(encoding="utf-8", errors="ignore")
    lines = raw_content.splitlines()

    cleaned_lines = []
    inside_multiline_key = None
    accumulated_val = []
    quote_char = None
    modified = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Handle inside multiline quote
        if inside_multiline_key:
            modified = True
            # Check if this line closes the quote
            if quote_char and stripped.endswith(quote_char) and not stripped.endswith("\\" + quote_char):
                val_chunk = line[: line.rfind(quote_char)]
                accumulated_val.append(val_chunk)
                if inside_multiline_key not in SKIP_KEYS:
                    full_val = "\\n".join(accumulated_val).replace('"', '\\"')
                    cleaned_lines.append(f'{inside_multiline_key}="{full_val}"')
                inside_multiline_key = None
                accumulated_val = []
                quote_char = None
            else:
                accumulated_val.append(line)
            i += 1
            continue

        # Blank or comment line
        if not stripped or stripped.startswith("#"):
            cleaned_lines.append(line)
            i += 1
            continue

        # Key-value assignment line
        if "=" in stripped:
            k, rest = stripped.split("=", 1)
            k = k.strip()
            rest = rest.strip()

            if VALID_KEY_RE.match(k):
                # Check for start of multiline quote
                is_double_quote = rest.startswith('"') and (len(rest) == 1 or not (rest.endswith('"') and not rest.endswith('\\"')))
                is_single_quote = rest.startswith("'") and (len(rest) == 1 or not (rest.endswith("'") and not rest.endswith("\\'")))

                if is_double_quote or is_single_quote:
                    inside_multiline_key = k
                    quote_char = rest[0]
                    accumulated_val = [rest[1:]]
                    i += 1
                    continue

                if k in SKIP_KEYS:
                    # Drop prompt keys from .env (they are stored in SQLite)
                    modified = True
                    i += 1
                    continue

                cleaned_lines.append(line)
                i += 1
                continue

        # Line is not comment, blank, or valid KEY= assignment
        # Comment it out so Docker Compose parser does not fail with "key cannot contain a space"
        cleaned_lines.append(f"# [sanitized] {line}")
        modified = True
        i += 1

    if inside_multiline_key and inside_multiline_key not in SKIP_KEYS:
        full_val = "\\n".join(accumulated_val).replace('"', '\\"')
        cleaned_lines.append(f'{inside_multiline_key}="{full_val}"')
        modified = True

    if modified:
        # Backup original
        try:
            bak_path = filepath.with_suffix(filepath.suffix + ".bak")
            bak_path.write_text(raw_content, encoding="utf-8")
        except Exception:
            pass

        filepath.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
        print(f"✅ Файл {filepath} успешно очищен для совместимости с Docker Compose.")
        return True

    return False


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
    sanitize_env_file(target)
