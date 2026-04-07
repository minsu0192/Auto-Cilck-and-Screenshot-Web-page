#!/bin/bash
# macOS 원격 데스크탑 — 원커맨드 실행 스크립트
set -e

cd "$(dirname "$0")"

echo ""
echo "🖥  macOS Remote Desktop 시작 준비"
echo ""

# Python 확인
if ! command -v python3 &>/dev/null; then
    echo "❌ python3이 설치되어 있지 않습니다."
    echo "   brew install python3"
    exit 1
fi

# 가상환경 (PEP 668 회피)
VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "🔧 가상환경 만드는 중..."
    python3 -m venv "$VENV_DIR"
fi
PYTHON="$VENV_DIR/bin/python"

# 의존성 설치 (이미 설치된 경우 빠르게 통과)
if ! "$PYTHON" -c "import fastapi, pyautogui, dotenv, PIL, pyngrok, httpx, bcrypt" 2>/dev/null; then
    echo "📦 의존성 설치 중..."
    "$PYTHON" -m pip install -q --upgrade pip
    "$PYTHON" -m pip install -q -r requirements.txt
fi

# .env 파일 확인 (없으면 자동 생성 — 서버가 알아서 처리)
if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
fi

# 서버 실행 — 비밀번호가 없으면 브라우저 설정 페이지로 자동 안내
echo "🚀 서버 시작..."
echo "   (처음이라면 브라우저에서 설정 페이지가 열립니다)"
echo ""
"$PYTHON" server.py
