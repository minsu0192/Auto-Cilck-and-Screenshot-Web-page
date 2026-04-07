#!/bin/bash
# RemoteDesktop.app 빌드 스크립트
# 실행하면 /Applications/RemoteDesktop.app 가 생성됩니다.
# 그 후로는 Spotlight나 Launchpad에서 "Remote Desktop"으로 실행할 수 있습니다.

set -e
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

APP_NAME="RemoteDesktop"
INSTALL_PATH="/Applications/$APP_NAME.app"
# 빌드는 임시 위치에서 (sudo 없이), 마지막에 한 번에 /Applications/로 이동
BUILD_PATH="$(mktemp -d)/$APP_NAME.app"

# 정리 트랩 — 빌드 실패해도 임시 디렉토리 청소
trap 'rm -rf "$(dirname "$BUILD_PATH")"' EXIT

# 빌드 중에는 BUILD_PATH를 사용 (스크립트 나머지 부분과 호환)
APP_PATH="$BUILD_PATH"

echo ""
echo "🛠  $APP_NAME.app 빌드 중..."
echo "   프로젝트: $PROJECT_DIR"
echo "   설치 위치: $INSTALL_PATH"
echo ""

mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

# ─── Info.plist ───
cat > "$APP_PATH/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Remote Desktop</string>
    <key>CFBundleDisplayName</key>
    <string>Remote Desktop</string>
    <key>CFBundleIdentifier</key>
    <string>com.local.remote-desktop</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleSignature</key>
    <string>????</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSScreenCaptureUsageDescription</key>
    <string>원격 데스크탑 화면 캡처를 위해 화면 기록 권한이 필요합니다.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>마우스/키보드 제어를 위해 손쉬운 사용 권한이 필요합니다.</string>
</dict>
</plist>
EOF

# ─── 런처 스크립트 ───
cat > "$APP_PATH/Contents/MacOS/launcher" <<LAUNCHER_EOF
#!/bin/bash
# Remote Desktop 런처
# build_app.sh가 자동 생성함. 직접 수정하지 마세요.

PROJECT_DIR="$PROJECT_DIR"
cd "\$PROJECT_DIR"

# 로그 파일
LOG_FILE="\$PROJECT_DIR/server.log"

# 알림 헬퍼
notify() {
    osascript -e "display notification \"\$2\" with title \"Remote Desktop\" subtitle \"\$1\"" 2>/dev/null || true
}

alert() {
    osascript -e "display alert \"Remote Desktop\" message \"\$1\" buttons {\"확인\"} default button \"확인\"" 2>/dev/null
}

# Python3 찾기 (Homebrew, system Python 등 다양한 경로 탐색)
find_python() {
    for cmd in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if command -v "\$cmd" >/dev/null 2>&1; then
            echo "\$cmd"
            return 0
        fi
    done
    return 1
}

SYSTEM_PYTHON=\$(find_python)
if [ -z "\$SYSTEM_PYTHON" ]; then
    alert "Python 3가 설치되어 있지 않습니다.\n\n터미널에서 다음을 실행하세요:\nbrew install python3"
    exit 1
fi

# 가상환경 (PEP 668 회피, 시스템 Python 격리)
VENV_DIR="\$PROJECT_DIR/.venv"
if [ ! -x "\$VENV_DIR/bin/python" ]; then
    notify "초기 설정" "가상환경 만드는 중..."
    if ! \$SYSTEM_PYTHON -m venv "\$VENV_DIR" 2>>"\$LOG_FILE"; then
        alert "가상환경 생성 실패\n\n로그: \$LOG_FILE"
        open "\$LOG_FILE"
        exit 1
    fi
fi
PYTHON="\$VENV_DIR/bin/python"

# 의존성 확인 + 자동 설치
if ! "\$PYTHON" -c "import fastapi, pyautogui, dotenv, PIL, pyngrok, httpx, bcrypt" 2>/dev/null; then
    notify "초기 설정" "의존성 설치 중... (1~2분 소요)"
    "\$PYTHON" -m pip install --quiet --upgrade pip 2>>"\$LOG_FILE"
    if ! "\$PYTHON" -m pip install --quiet -r requirements.txt 2>>"\$LOG_FILE"; then
        alert "의존성 설치 실패\n\n로그: \$LOG_FILE"
        open "\$LOG_FILE"
        exit 1
    fi
fi

# .env 확인 및 자동 생성 (비밀번호 미설정이면 서버가 /setup으로 안내)
if [ ! -f .env ]; then
    cp .env.example .env 2>/dev/null || true
fi

# 포트 읽기
PORT_LINE=\$(grep '^PORT=' .env 2>/dev/null || echo "PORT=8000")
PORT=\${PORT_LINE#PORT=}
PORT=\${PORT:-8000}

# 이미 실행 중인지 확인
if lsof -i :\$PORT >/dev/null 2>&1; then
    EXISTING_PID=\$(lsof -ti :\$PORT)
    RESPONSE=\$(osascript -e "display alert \"Remote Desktop\" message \"포트 \$PORT가 이미 사용 중입니다.\n\n기존 서버를 종료하고 새로 시작할까요?\" buttons {\"취소\", \"새로 시작\"} default button \"새로 시작\"" 2>/dev/null)
    if echo "\$RESPONSE" | grep -q "새로 시작"; then
        kill \$EXISTING_PID 2>/dev/null
        sleep 1
    else
        # 기존 서버에 그대로 접속
        open "http://localhost:\$PORT"
        exit 0
    fi
fi

# 서버 시작
notify "서버 시작 중" "잠시만 기다려 주세요..."
"\$PYTHON" server.py > "\$LOG_FILE" 2>&1 &
SERVER_PID=\$!

# 서버 종료 시 자식 프로세스 정리
trap "kill \$SERVER_PID 2>/dev/null; exit" EXIT INT TERM

# 서버 준비 대기 (최대 10초)
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -s -o /dev/null "http://localhost:\$PORT/" 2>/dev/null; then
        break
    fi
    if ! kill -0 \$SERVER_PID 2>/dev/null; then
        # 서버가 죽음
        alert "서버 시작 실패\n\n로그를 확인하세요: \$LOG_FILE"
        open "\$LOG_FILE"
        exit 1
    fi
    sleep 1
done

# 브라우저 열기
open "http://localhost:\$PORT"

# 알림
notify "서버 실행 중" "http://localhost:\$PORT"

# 서버가 끝날 때까지 대기 (Dock에서 종료할 때까지)
wait \$SERVER_PID
LAUNCHER_EOF

chmod +x "$APP_PATH/Contents/MacOS/launcher"

# ─── 아이콘 (간단한 SVG → ICNS 생성, 실패해도 무시) ───
ICON_PATH="$APP_PATH/Contents/Resources/icon.icns"
if command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
    # 간단한 사각형 PNG 생성 후 icns 변환
    TMP_DIR=$(mktemp -d)
    ICONSET="$TMP_DIR/icon.iconset"
    mkdir -p "$ICONSET"
    # 1024x1024 베이스 이미지 만들기 (Python으로)
    PYTHON_CMD=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "")
    if [ -n "$PYTHON_CMD" ]; then
        $PYTHON_CMD - <<PYEOF 2>/dev/null && {
from PIL import Image, ImageDraw, ImageFont
img = Image.new("RGBA", (1024, 1024), (74, 158, 255, 255))
draw = ImageDraw.Draw(img)
# 모니터 모양
draw.rounded_rectangle([180, 220, 844, 700], radius=40, fill=(255, 255, 255, 255))
draw.rounded_rectangle([220, 260, 804, 660], radius=20, fill=(26, 26, 46, 255))
# 받침대
draw.rectangle([460, 700, 564, 780], fill=(255, 255, 255, 255))
draw.rounded_rectangle([350, 780, 674, 820], radius=20, fill=(255, 255, 255, 255))
img.save("$TMP_DIR/icon_1024.png")
PYEOF
            sips -z 16 16     "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_16x16.png" >/dev/null 2>&1
            sips -z 32 32     "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_16x16@2x.png" >/dev/null 2>&1
            sips -z 32 32     "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_32x32.png" >/dev/null 2>&1
            sips -z 64 64     "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_32x32@2x.png" >/dev/null 2>&1
            sips -z 128 128   "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_128x128.png" >/dev/null 2>&1
            sips -z 256 256   "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_128x128@2x.png" >/dev/null 2>&1
            sips -z 256 256   "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_256x256.png" >/dev/null 2>&1
            sips -z 512 512   "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_256x256@2x.png" >/dev/null 2>&1
            sips -z 512 512   "$TMP_DIR/icon_1024.png" --out "$ICONSET/icon_512x512.png" >/dev/null 2>&1
            cp "$TMP_DIR/icon_1024.png" "$ICONSET/icon_512x512@2x.png"
            iconutil -c icns "$ICONSET" -o "$ICON_PATH" 2>/dev/null
            # Info.plist에 아이콘 등록
            /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string icon" "$APP_PATH/Contents/Info.plist" 2>/dev/null || true
        }
    fi
    rm -rf "$TMP_DIR"
fi

# ─── /Applications/ 로 설치 ───
# /Applications/는 시스템 폴더라 sudo가 필요함. 한 번만 비밀번호 묻고 끝.
echo ""
echo "📦 /Applications/ 로 설치합니다..."
echo "   (시스템 폴더라서 macOS 비밀번호를 한 번 요청합니다)"
echo ""

if ! sudo rm -rf "$INSTALL_PATH"; then
    echo "❌ 기존 앱 제거 실패"
    exit 1
fi

if ! sudo mv "$BUILD_PATH" "$INSTALL_PATH"; then
    echo "❌ 설치 실패 — 권한을 확인하세요"
    exit 1
fi

# 소유권을 현재 사용자로 (앱 안의 launcher가 사용자 권한으로 동작해야 함)
sudo chown -R "$(whoami):staff" "$INSTALL_PATH" 2>/dev/null || true

# Gatekeeper의 quarantine 속성 제거 — 첫 실행 시 "확인되지 않은 개발자" 경고 방지
sudo xattr -dr com.apple.quarantine "$INSTALL_PATH" 2>/dev/null || true

echo ""
echo "✅ 빌드 + 설치 완료!"
echo ""
echo "   위치: $INSTALL_PATH"
echo ""
echo "   사용법:"
echo "   1. Spotlight (Cmd+Space) → 'Remote Desktop' 검색 → Enter"
echo "   2. 또는 Finder → 응용 프로그램 → Remote Desktop 더블클릭"
echo "   3. 또는 Launchpad에서 Remote Desktop 클릭"
echo ""
echo "   Dock에 추가하려면: 처음 실행 후 Dock의 아이콘을 우클릭 → 옵션 → Dock에 유지"
echo ""
echo "   첫 실행 시 macOS가 권한을 묻습니다:"
echo "   - 화면 기록 권한: 허용"
echo "   - 손쉬운 사용 권한: 허용"
echo "   허용 후 한 번 종료했다가 다시 실행하면 정상 동작합니다."
echo ""
