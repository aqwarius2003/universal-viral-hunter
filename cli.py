#!/usr/bin/env python3
"""
Universal Viral Hunter - Remote Server Deployment & Management CLI.
Allows zero-hassle server bootstrapping, deploying, monitoring and backup via SSH/Docker.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# Enable UTF-8 encoding on Windows console to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

CONFIG_FILE = Path(".deploy.json")


# ---------------------------------------------------------------------------
# Colors & Console Output
# ---------------------------------------------------------------------------
class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    END = "\033[0m"


def print_info(msg: str) -> None:
    print(f"{Colors.CYAN}ℹ️  {msg}{Colors.END}")


def print_success(msg: str) -> None:
    print(f"{Colors.GREEN}✅ {msg}{Colors.END}")


def print_warning(msg: str) -> None:
    print(f"{Colors.YELLOW}⚠️  {msg}{Colors.END}")


def print_error(msg: str) -> None:
    print(f"{Colors.RED}❌ {msg}{Colors.END}")


def print_header(title: str) -> None:
    bar = "=" * 60
    print(f"\n{Colors.BOLD}{Colors.CYAN}{bar}\n  🚀 {title}\n{bar}{Colors.END}\n")


# ---------------------------------------------------------------------------
# Config Management
# ---------------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(config: Dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# SSH Key Resolver
# ---------------------------------------------------------------------------
def resolve_ssh_key(explicit_key: Optional[str], config: Dict[str, Any]) -> Optional[str]:
    # 1. Explicit CLI argument
    if explicit_key:
        path = Path(explicit_key).expanduser().resolve()
        if path.exists():
            return str(path)
        print_warning(f"Указанный ключ не найден: {explicit_key}")

    # 2. Key from .deploy.json
    cfg_key = config.get("key_path")
    if cfg_key:
        path = Path(cfg_key).expanduser().resolve()
        if path.exists():
            return str(path)

    # 3. Local project directory keys (convenience for single-project keys)
    project_keys = [
        Path("deploy_key"),
        Path("deploy_key.pem"),
        Path("id_rsa"),
        Path("id_rsa.pem"),
        Path("id_ed25519"),
        Path("id_ed25519.pem"),
        Path("key.pem"),
    ]
    for k in project_keys:
        if k.exists() and k.is_file():
            return str(k.resolve())

    # 4. Standard system user SSH keys
    home_ssh = Path.home() / ".ssh"
    default_keys = [
        home_ssh / "id_ed25519",
        home_ssh / "id_rsa",
        home_ssh / "id_ecdsa",
    ]
    for k in default_keys:
        if k.exists() and k.is_file():
            return str(k.resolve())

    return None


def read_env_var(key: str) -> Optional[str]:
    """Reads a variable from environment or local .env file."""
    val = os.environ.get(key)
    if val:
        return val.strip()
    env_file = Path(".env")
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip("'\"")
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Connection Resolution
# ---------------------------------------------------------------------------
def resolve_connection(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_config()

    env_ip = read_env_var("DEPLOY_SERVER_IP") or read_env_var("SERVER_IP")
    ip = getattr(args, "ip", None) or env_ip or config.get("server_ip")
    user = getattr(args, "user", None) or read_env_var("DEPLOY_USER") or config.get("user") or "root"
    port = getattr(args, "port", None) or (int(read_env_var("DEPLOY_PORT")) if read_env_var("DEPLOY_PORT") else None) or config.get("port") or 22
    remote_dir = getattr(args, "dir", None) or config.get("remote_dir") or "~/viral-hunter"
    key_path = resolve_ssh_key(getattr(args, "key", None) or read_env_var("DEPLOY_SSH_KEY"), config)

    # If IP is still unknown, ask interactively
    if not ip:
        print_header("Настройка подключения к серверу")
        try:
            ip = input("👉 Введите IP-адрес сервера (VPS): ").strip()
            if not ip:
                print_error("IP-адрес не может быть пустым.")
                sys.exit(1)

            input_user = input(f"👉 Пользователь SSH [{user}]: ").strip()
            if input_user:
                user = input_user

            input_port = input(f"👉 Порт SSH [{port}]: ").strip()
            if input_port:
                port = int(input_port)

            detected_key_prompt = f" [{key_path}]" if key_path else ""
            input_key = input(f"👉 Путь к SSH-ключу{detected_key_prompt}: ").strip()
            if input_key:
                resolved = Path(input_key).expanduser().resolve()
                if resolved.exists():
                    key_path = str(resolved)
                else:
                    print_warning(f"Файл ключа {input_key} не найден. Попробуем без флага ключа (ssh-agent).")

        except (KeyboardInterrupt, EOFError):
            print("\nОтменено пользователем.")
            sys.exit(1)

    # Save to config for fast future runs
    config["server_ip"] = ip
    config["user"] = user
    config["port"] = port
    config["remote_dir"] = remote_dir
    if key_path:
        config["key_path"] = key_path
    save_config(config)

    return {
        "ip": ip,
        "user": user,
        "port": port,
        "remote_dir": remote_dir,
        "key_path": key_path,
        "target": f"{user}@{ip}",
    }


# ---------------------------------------------------------------------------
# SSH Execution
# ---------------------------------------------------------------------------
def run_ssh(
    conn: Dict[str, Any],
    command: str,
    stream: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Executes a command on the remote server via system SSH."""
    cmd = ["ssh", "-p", str(conn["port"])]

    # SSH key
    if conn.get("key_path"):
        cmd.extend(["-i", conn["key_path"]])

    # Disable strict host key checking prompt on new servers for clean UX
    cmd.extend([
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        conn["target"],
        command,
    ])

    if stream:
        res = subprocess.run(cmd)
    else:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if check and res.returncode != 0:
        err_msg = res.stderr if not stream and res.stderr else f"Exit code {res.returncode}"
        print_error(f"Команда завершилась с ошибкой на {conn['target']}:\n{err_msg}")
        sys.exit(res.returncode)

    return res


def run_scp_upload(conn: Dict[str, Any], local_file: Path, remote_file: str) -> None:
    """Uploads a file to remote server via SCP."""
    cmd = ["scp", "-P", str(conn["port"])]
    if conn.get("key_path"):
        cmd.extend(["-i", conn["key_path"]])

    cmd.extend([
        "-o", "StrictHostKeyChecking=accept-new",
        str(local_file),
        f"{conn['target']}:{remote_file}",
    ])

    res = subprocess.run(cmd)
    if res.returncode != 0:
        print_error(f"Не удалось загрузить файл {local_file.name} на {conn['target']}")
        sys.exit(res.returncode)


def run_scp_download(conn: Dict[str, Any], remote_file: str, local_file: Path) -> None:
    """Downloads a file from remote server via SCP."""
    cmd = ["scp", "-P", str(conn["port"])]
    if conn.get("key_path"):
        cmd.extend(["-i", conn["key_path"]])

    cmd.extend([
        "-o", "StrictHostKeyChecking=accept-new",
        f"{conn['target']}:{remote_file}",
        str(local_file),
    ])

    res = subprocess.run(cmd)
    if res.returncode != 0:
        print_error(f"Не удалось скачать файл {remote_file} с {conn['target']}")
        sys.exit(res.returncode)


# ---------------------------------------------------------------------------
# Project Packaging
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".agent",
    ".kilo",
    ".openclaw_tmp_probe",
    "data",
    ".env",
    ".deploy.json",
    "tweets.sqlite",
    "tweets.sqlite3",
    "cookies.json",
}


def create_project_archive(output_path: Path) -> None:
    """Packages the project files into a tar.gz archive, ignoring local data and virtualenvs."""
    root_dir = Path(".").resolve()

    def tar_filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        name_parts = Path(tarinfo.name).parts
        for part in name_parts:
            if part in EXCLUDE_PATTERNS:
                return None
            if part.endswith(".pyc") or part.endswith(".sqlite") or part.endswith(".pem"):
                return None
            if part.startswith("id_rsa") or part.startswith("id_ed25519") or part.startswith("deploy_key"):
                return None
        return tarinfo

    with tarfile.open(output_path, "w:gz") as tar:
        for item in root_dir.iterdir():
            if item.name in EXCLUDE_PATTERNS:
                continue
            if item.suffix in (".sqlite", ".sqlite3", ".pem"):
                continue
            if item.name.startswith("id_rsa") or item.name.startswith("deploy_key"):
                continue
            tar.add(item, arcname=item.name, filter=tar_filter)


# ---------------------------------------------------------------------------
# CLI Command Implementations
# ---------------------------------------------------------------------------
def cmd_bootstrap(args: argparse.Namespace) -> None:
    """Installs Docker, Docker Compose, 2GB swap and configures firewall on fresh server."""
    conn = resolve_connection(args)
    print_header(f"Подготовка сервера (Bootstrap): {conn['target']}")

    bootstrap_script = r"""set -e
echo "==> [1/4] Проверка операционной системы и обновление пакетов..."
export DEBIAN_FRONTEND=noninteractive
if [ -f /etc/debian_version ]; then
    apt-get update -y
    apt-get install -y curl tar ufw ca-certificates gnupg lsb-release
fi

echo "==> [2/4] Настройка SWAP (2 GB для предотвращения OOM при сборке)..."
if ! swapon --show | grep -q "/swapfile"; then
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile || true
    if ! grep -q "/swapfile" /etc/fstab; then
        echo "/swapfile none swap sw 0 0" >> /etc/fstab
    fi
    echo "SWAP 2GB успешно включен."
else
    echo "SWAP уже настроен."
fi

echo "==> [3/4] Проверка и установка Docker & Docker Compose v2..."
if ! command -v docker >/dev/null 2>&1; then
    echo "Устанавливаю Docker через официальный скрипт..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    systemctl enable docker
    systemctl start docker
else
    echo "Docker уже установлен: $(docker --version)"
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "Обновляю Docker Compose до современной версии v2..."
    apt-get update -y && apt-get install -y docker-compose-plugin 2>/dev/null || true
    if ! docker compose version >/dev/null 2>&1; then
        ARCH=$(uname -m)
        mkdir -p /root/.docker/cli-plugins /usr/local/bin
        curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$ARCH" -o /usr/local/bin/docker-compose || true
        chmod +x /usr/local/bin/docker-compose 2>/dev/null || true
        cp /usr/local/bin/docker-compose /root/.docker/cli-plugins/docker-compose 2>/dev/null || true
        cp /usr/local/bin/docker-compose /usr/bin/docker-compose 2>/dev/null || true
    fi
fi

echo "==> [4/4] Настройка базового фаервола (порт 22, 80, 443, 8000)..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp || true
    ufw allow 80/tcp || true
    ufw allow 443/tcp || true
    ufw allow 8000/tcp || true
    ufw --force enable || true
fi

echo "==> Сервер готов к развертыванию!"
"""
    run_ssh(conn, bootstrap_script)
    print_success(f"Сервер {conn['ip']} успешно подготовлен (Docker, Swap 2GB, UFW настроены).")
    print_info(f"Теперь можно запустить деплой: python cli.py deploy")


def cmd_version(args: argparse.Namespace) -> None:
    """Shows current version info and optionally bumps version."""
    from src.version import get_version, get_git_commit
    ver = get_version()
    commit = get_git_commit()

    bump = getattr(args, "bump", None)
    if bump:
        parts = [int(p) for p in ver.split(".")]
        while len(parts) < 3:
            parts.append(0)
        if bump == "major":
            parts[0] += 1
            parts[1] = 0
            parts[2] = 0
        elif bump == "minor":
            parts[1] += 1
            parts[2] = 0
        elif bump == "patch":
            parts[2] += 1
        new_ver = f"{parts[0]}.{parts[1]}.{parts[2]}"
        Path("VERSION").write_text(new_ver + "\n", encoding="utf-8")
        print_success(f"Версия успешно повышена: v{ver} ➔ v{new_ver}")
        try:
            subprocess.run(["git", "add", "VERSION"], check=True)
            subprocess.run(["git", "commit", "-m", f"chore(release): bump version to v{new_ver}"], check=True)
            subprocess.run(["git", "tag", "-a", f"v{new_ver}", "-m", f"Release v{new_ver}"], check=True)
            print_success(f"Создан Git-тег: v{new_ver}")
        except Exception as e:
            print_warning(f"Git-тег не создан: {e}")
        return

    print_header("📦 Информация о версии Viral Hunter")
    print(f"  Версия приложения: {Colors.BOLD}v{ver}{Colors.END}")
    print(f"  Git коммит:         {Colors.CYAN}{commit}{Colors.END}")
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        print(f"  Текущая ветка:      {branch}")
    except Exception:
        pass


def cmd_deploy(args: argparse.Namespace) -> None:
    """Packages project, backups remote database, uploads, rebuilds Docker container and checks health."""
    conn = resolve_connection(args)
    from src.version import get_version, get_git_commit
    ver = get_version()
    commit = get_git_commit()
    print_header(f"Развертывание на сервер: {conn['target']} | Релиз: v{ver} ({commit})")

    # Ensure git release tag exists for reproducibility
    try:
        tag_check = subprocess.run(["git", "tag", "-l", f"v{ver}"], capture_output=True, text=True)
        if not tag_check.stdout.strip():
            subprocess.run(["git", "tag", "-a", f"v{ver}", "-m", f"Deploy release v{ver}"], stderr=subprocess.DEVNULL)
            print_info(f"Создан релизный Git-тег: v{ver}")
    except Exception:
        pass

    temp_archive = Path(tempfile.gettempdir()) / "viral-hunter-deploy.tar.gz"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    remote_dir = conn["remote_dir"]

    try:
        # Step 1: Package local code
        print_info(f"[1/5] Упаковка исходного кода проекта (версия v{ver})...")
        if temp_archive.exists():
            temp_archive.unlink()
        create_project_archive(temp_archive)
        size_mb = temp_archive.stat().st_size / (1024 * 1024)
        print_success(f"Архив сформирован: {size_mb:.2f} MB")

        # Step 2: Backup remote data
        print_info("[2/5] Создание резервной копии данных на сервере...")
        backup_cmd = (
            f"set -e; mkdir -p {remote_dir}/data/backups; "
            f"if [ -d {remote_dir}/data ] && [ \"$(ls -A {remote_dir}/data 2>/dev/null)\" ]; then "
            f"tar --exclude=backups --exclude=video-temp -czf {remote_dir}/data/backups/data-{stamp}.tar.gz -C {remote_dir}/data . 2>/dev/null || true; "
            f"echo 'Резервная копия сохранена в data/backups/data-{stamp}.tar.gz'; "
            f"ls -t {remote_dir}/data/backups/data-*.tar.gz 2>/dev/null | tail -n +3 | xargs -r rm -f; "
            f"fi"
        )
        run_ssh(conn, backup_cmd)

        # Step 3: Upload archive
        print_info(f"[3/5] Загрузка архива на {conn['target']}...")
        run_scp_upload(conn, temp_archive, "~/viral-hunter-deploy.tar.gz")
        print_success("Архив успешно передан.")

        # Step 4: Extract and rebuild Docker
        print_info("[4/5] Распаковка и пересборка контейнеров через Docker Compose...")
        deploy_cmd = (
            f"set -e; "
            f"mkdir -p {remote_dir}/data; "
            f"tar -xzf ~/viral-hunter-deploy.tar.gz -C {remote_dir}; "
            f"rm -f ~/viral-hunter-deploy.tar.gz; "
            f"cd {remote_dir}; "
            f"touch .env; "
            f"if [ -f src/sanitize_env.py ]; then python3 src/sanitize_env.py .env || true; fi; "
            f"if ! docker compose version >/dev/null 2>&1; then "
            f"  echo 'Обновление Docker Compose до v2...'; "
            f"  apt-get update -y && apt-get install -y docker-compose-plugin 2>/dev/null || true; "
            f"  if ! docker compose version >/dev/null 2>&1; then "
            f"    ARCH=$(uname -m); "
            f"    mkdir -p /root/.docker/cli-plugins /usr/local/bin; "
            f"    curl -fsSL \"https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$ARCH\" -o /usr/local/bin/docker-compose || true; "
            f"    chmod +x /usr/local/bin/docker-compose 2>/dev/null || true; "
            f"    cp /usr/local/bin/docker-compose /root/.docker/cli-plugins/docker-compose 2>/dev/null || true; "
            f"    cp /usr/local/bin/docker-compose /usr/bin/docker-compose 2>/dev/null || true; "
            f"  fi; "
            f"fi; "
            f"systemctl stop apache2 2>/dev/null || true; "
            f"systemctl stop nginx 2>/dev/null || true; "
            f"if command -v ufw >/dev/null 2>&1; then "
            f"  ufw allow 80/tcp 2>/dev/null || true; "
            f"  ufw allow 443/tcp 2>/dev/null || true; "
            f"  ufw allow 8000/tcp 2>/dev/null || true; "
            f"fi; "
            f"docker ps -a --filter 'name=viral-hunter' -q | xargs -r docker rm -f 2>/dev/null || true; "
            f"docker image prune -f 2>/dev/null || true; "
            f"if docker compose version >/dev/null 2>&1; then "
            f"  docker compose up -d --build --remove-orphans; "
            f"else "
            f"  docker-compose up -d --build --remove-orphans; "
            f"fi; "
            f"docker image prune -f 2>/dev/null || true"
        )
        run_ssh(conn, deploy_cmd)

        # Step 5: Check container status
        print_info("[5/5] Проверка статуса запущенного приложения...")
        status_cmd = (
            f"set -e; cd {remote_dir}; "
            f"if docker compose version >/dev/null 2>&1; then docker compose ps; else docker-compose ps; fi"
        )
        run_ssh(conn, status_cmd)

        print_header("Развертывание завершено успешно! 🎉")
        print(f"  🔒 Защищенный сайт (HTTPS): {Colors.BOLD}https://viralhunter.duckdns.org{Colors.END}")
        print(f"  👉 Прямой IP (HTTP):         http://{conn['ip']}:8000")
        print_info("Данные базы данных и cookies были бережно сохранены в директории data/ на сервере.")
        print_info("Для просмотра логов в реальном времени используйте: python cli.py logs")

    finally:
        if temp_archive.exists():
            try:
                temp_archive.unlink()
            except Exception:
                pass


def cmd_status(args: argparse.Namespace) -> None:
    """Shows remote container status, system resources and HTTP check."""
    conn = resolve_connection(args)
    print_header(f"Статус сервера: {conn['target']}")

    status_script = f"""
cd {conn['remote_dir']} || exit 1
echo "=== 🐳 Docker Контейнеры ==="
if docker compose version >/dev/null 2>&1; then
    docker compose ps
else
    docker-compose ps
fi

echo ""
echo "=== 💾 Память и Swap ==="
free -h

echo ""
echo "=== 💽 Диск ==="
df -h / | awk 'NR==1 || NR==2'

echo ""
echo "=== 🌐 Проверка порта 8000 ==="
if curl -s -o /dev/null -w "%{{http_code}}" http://127.0.0.1:8000/ | grep -q "200\\|302"; then
    echo "✅ Веб-сервер отвечает (HTTP 200/302 OK)"
else
    echo "⚠️ Веб-сервер пока не отвечает или запускается"
fi
"""
    run_ssh(conn, status_script)


def cmd_logs(args: argparse.Namespace) -> None:
    """Streams live container logs from remote server."""
    conn = resolve_connection(args)
    tail = getattr(args, "tail", 100)
    print_info(f"Подключение к логам {conn['target']} (tail={tail}, Ctrl+C для выхода)...")
    logs_cmd = (
        f"cd {conn['remote_dir']} && "
        f"if docker compose version >/dev/null 2>&1; then "
        f"docker compose logs -f --tail={tail}; "
        f"else docker-compose logs -f --tail={tail}; fi"
    )
    run_ssh(conn, logs_cmd)


def cmd_restart(args: argparse.Namespace) -> None:
    """Restarts remote Docker container."""
    conn = resolve_connection(args)
    print_info(f"Перезапуск приложения на {conn['target']}...")
    restart_cmd = (
        f"cd {conn['remote_dir']} && "
        f"if docker compose version >/dev/null 2>&1; then docker compose restart; "
        f"else docker-compose restart; fi"
    )
    run_ssh(conn, restart_cmd)
    print_success("Приложение успешно перезапущено.")


def cmd_stop(args: argparse.Namespace) -> None:
    """Stops remote Docker container."""
    conn = resolve_connection(args)
    print_info(f"Остановка приложения на {conn['target']}...")
    stop_cmd = (
        f"cd {conn['remote_dir']} && "
        f"if docker compose version >/dev/null 2>&1; then docker compose down; "
        f"else docker-compose down; fi"
    )
    run_ssh(conn, stop_cmd)
    print_success("Приложение остановлено.")


def cmd_backup(args: argparse.Namespace) -> None:
    """Downloads remote SQLite database and backups archive to local directory."""
    conn = resolve_connection(args)
    print_header(f"Скачивание резервной копии: {conn['target']}")

    local_backup_dir = Path("backups")
    local_backup_dir.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    local_file = local_backup_dir / f"remote-data-{stamp}.tar.gz"

    archive_remote_cmd = (
        f"set -e; cd {conn['remote_dir']}; "
        f"tar -czf /tmp/backup-{stamp}.tar.gz -C data . 2>/dev/null"
    )
    print_info("Архивация данных на сервере...")
    run_ssh(conn, archive_remote_cmd)

    print_info(f"Скачивание в {local_file}...")
    run_scp_download(conn, f"/tmp/backup-{stamp}.tar.gz", local_file)

    cleanup_cmd = f"rm -f /tmp/backup-{stamp}.tar.gz"
    run_ssh(conn, cleanup_cmd, check=False)

    print_success(f"Резервная копия успешно сохранена: {local_file.resolve()}")


def cmd_config(args: argparse.Namespace) -> None:
    """Displays or resets current deploy configuration."""
    config = load_config()
    if args.reset:
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        print_success("Конфигурация подключения сброшена.")
        return

    print_header("Текущая конфигурация подключения (.deploy.json)")
    if not config:
        print_info("Конфигурация еще не создана. Запустите: python cli.py deploy")
    else:
        for k, v in config.items():
            print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# Argument Parser & Entry Point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="🚀 Universal Viral Hunter - CLI развертывания и управления сервером",
    )

    # Global options
    parser.add_argument("--ip", help="IP-адрес сервера (VPS)")
    parser.add_argument("-u", "--user", default=None, help="SSH пользователь (по умолчанию: root)")
    parser.add_argument("-p", "--port", type=int, default=None, help="SSH порт (по умолчанию: 22)")
    parser.add_argument("-k", "--key", default=None, help="Путь к приватному SSH-ключу (.pem / id_rsa / id_ed25519)")
    parser.add_argument("-d", "--dir", default=None, help="Удаленная папка (по умолчанию: ~/viral-hunter)")

    subparsers = parser.add_subparsers(dest="command", help="Команда для выполнения")

    # bootstrap
    sp_boot = subparsers.add_parser("bootstrap", help="Подготовить чистый сервер (Docker, Swap 2GB, Firewall)")
    sp_boot.add_argument("ip", nargs="?", help="IP-адрес сервера")

    # deploy
    sp_deploy = subparsers.add_parser("deploy", help="Развернуть/обновить проект на сервере")
    sp_deploy.add_argument("ip", nargs="?", help="IP-адрес сервера")

    # status
    sp_status = subparsers.add_parser("status", help="Проверить статус контейнера, ОЗУ и веб-интерфейса")

    # logs
    sp_logs = subparsers.add_parser("logs", help="Просмотр логов контейнера в реальном времени")
    sp_logs.add_argument("--tail", type=int, default=100, help="Количество последних строк лога")

    # restart
    subparsers.add_parser("restart", help="Перезапустить приложение на сервере")

    # stop
    subparsers.add_parser("stop", help="Остановить приложение на сервере")

    # backup
    subparsers.add_parser("backup", help="Скачать резервную копию базы данных SQLite на локальный ПК")

    # config
    sp_cfg = subparsers.add_parser("config", help="Показать или сбросить сохраненные настройки подключения")
    sp_cfg.add_argument("--reset", action="store_true", help="Сбросить сохраненную конфигурацию")

    # version
    sp_ver = subparsers.add_parser("version", help="Показать текущую версию проекта или повысить ее")
    sp_ver.add_argument("--bump", choices=["patch", "minor", "major"], help="Автоматически повысить версию и создать Git-тег")

    return parser


def main() -> None:
    # Enable ANSI escape colors on Windows console
    if sys.platform == "win32":
        os.system("")

    parser = build_parser()
    args = parser.parse_args()

    # Default to interactive deploy if no command is specified
    if not args.command:
        parser.print_help()
        print("\n" + f"{Colors.YELLOW}👉 Подсказка: запустите 'python cli.py deploy' для развертывания проекта.{Colors.END}\n")
        return

    commands = {
        "bootstrap": cmd_bootstrap,
        "deploy": cmd_deploy,
        "status": cmd_status,
        "logs": cmd_logs,
        "restart": cmd_restart,
        "stop": cmd_stop,
        "backup": cmd_backup,
        "config": cmd_config,
        "version": cmd_version,
    }

    handler = commands.get(args.command)
    if handler:
        try:
            handler(args)
        except KeyboardInterrupt:
            print("\n" + f"{Colors.YELLOW}Операция прервана пользователем.{Colors.END}")
            sys.exit(130)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
