#!/bin/bash
# Скрипт для развертывания проекта на Raspberry Pi по SSH

PI_USER="vtt"
PI_HOST="192.168.10.43" # IP-адрес вашей Raspberry Pi
PROJECT_DIR="/home/$PI_USER/viral-hunter"

echo "🚀 Начинаем деплой на Raspberry Pi ($PI_USER@$PI_HOST)..."
echo "ℹ️  (Вас попросят ввести пароль для копирования файлов и для запуска Docker)"

# 1. Архивация на лету и отправка на сервер (заменяет rsync)
echo "📦 Копируем файлы проекта на сервер..."
tar -czf - --exclude='.git' \
           --exclude='.venv' \
           --exclude='__pycache__' \
           --exclude='.pytest_cache' \
           --exclude='data' \
           --exclude='.agent' \
           --exclude='.kilo' \
           --exclude='.env' \
           --exclude='*.sqlite' \
           --exclude='*.sqlite3' \
           . | ssh $PI_USER@$PI_HOST "mkdir -p $PROJECT_DIR/data && tar -xzf - -C $PROJECT_DIR"

# 2. Запускаем Docker Compose на Raspberry Pi
echo "🐳 Запуск Docker Compose на Raspberry Pi..."
ssh $PI_USER@$PI_HOST "cd $PROJECT_DIR && (docker compose up -d --build || docker-compose up -d --build)"

echo "✅ Скрипт завершил работу!"
echo "Если выше были ошибки — скопируйте их."
read -p "Нажмите Enter для выхода из окна..."
