"""
macOS 웹 기반 원격 데스크탑 서버
"""
from __future__ import annotations

import io
import os
import platform
import secrets
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import bcrypt
import httpx
import pyautogui
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── 경로 및 환경 설정 ───

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"
STATIC_DIR = BASE_DIR / "static"

# .env가 없으면 .env.example에서 자동 생성
if not ENV_FILE.exists() and ENV_EXAMPLE.exists():
    shutil.copy(ENV_EXAMPLE, ENV_FILE)

load_dotenv(ENV_FILE)


# ─── .env 읽기/쓰기 헬퍼 ───
# 사용자 친화적: 코멘트와 순서를 최대한 유지하면서 키만 업데이트


def env_read_all() -> dict[str, str]:
    """현재 .env 파일의 키/값 전체 반환"""
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        result[k.strip()] = v.strip()
    return result


def env_update(updates: dict[str, str]) -> None:
    """주어진 키들을 .env에 반영. 기존 줄은 자리에서 교체, 없으면 끝에 추가."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            new_lines.append(f"{k}={updates[k]}")
            seen.add(k)
        else:
            new_lines.append(line)

    for k, v in updates.items():
        if k not in seen:
            new_lines.append(f"{k}={v}")

    # 원자적 쓰기 (.env는 dotfile이라 with_suffix가 오작동하므로 부모 디렉토리 기준으로 생성)
    tmp = ENV_FILE.parent / ".env.tmp"
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)

    # 환경 변수도 즉시 업데이트
    for k, v in updates.items():
        os.environ[k] = v


def env_get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ─── 비밀번호 해싱 ───


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def is_setup_required() -> bool:
    """비밀번호 해시가 없으면 초기 설정이 필요한 상태."""
    return not env_get("PASSWORD_HASH")


PORT = int(env_get("PORT", "8000") or "8000")

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.05

# ─── 로깅 ───

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

access_logger = logging.getLogger("access")
access_logger.propagate = False
access_logger.setLevel(logging.INFO)
_access_handler = logging.FileHandler(BASE_DIR / "access.log", encoding="utf-8")
_access_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
access_logger.addHandler(_access_handler)

# ─── 텔레그램 알림 ───


def send_telegram(message: str, *, token: str | None = None, chat_id: str | None = None) -> tuple[bool, str]:
    """성공 여부와 에러 메시지 반환. (테스트용으로도 사용)"""
    bot = token or env_get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or env_get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        return False, "토큰 또는 채팅 ID가 설정되지 않았습니다"
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            json={"chat_id": chat, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            try:
                detail = resp.json().get("description", f"HTTP {resp.status_code}")
            except Exception:
                detail = f"HTTP {resp.status_code}"
            logger.error(f"텔레그램 전송 실패: {detail}")
            return False, detail
        return True, ""
    except Exception as e:
        logger.error(f"텔레그램 전송 오류: {e}")
        return False, str(e)


# ─── ngrok 터널 ───

_ngrok_tunnel = None


def start_ngrok() -> str | None:
    global _ngrok_tunnel
    token = env_get("NGROK_AUTHTOKEN")
    if not token:
        return None
    try:
        from pyngrok import ngrok, conf
        conf.get_default().auth_token = token
        for t in ngrok.get_tunnels():
            ngrok.disconnect(t.public_url)
        _ngrok_tunnel = ngrok.connect(PORT, "http")
        logger.info(f"ngrok 터널: {_ngrok_tunnel.public_url}")
        return _ngrok_tunnel.public_url
    except Exception as e:
        logger.error(f"ngrok 시작 실패: {e}")
        return None


def stop_ngrok():
    try:
        from pyngrok import ngrok
        ngrok.kill()
    except Exception:
        pass


def ngrok_watchdog():
    from pyngrok import ngrok
    while True:
        time.sleep(30)
        try:
            if not ngrok.get_tunnels():
                logger.warning("ngrok 끊김 — 재시작")
                url = start_ngrok()
                if url:
                    send_telegram(f"🔄 <b>ngrok 재연결</b>\n<code>{url}</code>")
        except Exception as e:
            logger.error(f"ngrok 감시 오류: {e}")


def test_ngrok_token(token: str) -> tuple[bool, str]:
    """주어진 토큰으로 임시 터널을 열어 검증.
    이미 활성 터널이 있으면 그것과 충돌을 피하기 위해 임의의 빈 포트를 사용."""
    if not token:
        return False, "토큰이 비어있습니다"

    # 사용 가능한 임의의 포트 찾기 (실제 PORT를 건드리지 않음)
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        test_port = s.getsockname()[1]

    try:
        from pyngrok import ngrok, conf
        conf.get_default().auth_token = token
        tunnel = ngrok.connect(test_port, "http")
        url = tunnel.public_url
        try:
            ngrok.disconnect(url)
        except Exception:
            pass
        # 운영 중인 터널이 있다면 토큰이 바뀌었으니 다시 설정
        if _ngrok_tunnel:
            conf.get_default().auth_token = env_get("NGROK_AUTHTOKEN")
        return True, url
    except Exception as e:
        return False, str(e)


# ─── macOS 권한 점검 ───


def check_screen_recording() -> bool:
    """screencapture를 시도해 화면 기록 권한 여부를 추정."""
    if platform.system() != "Darwin":
        return True  # 비-macOS는 체크 의미 없음
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
            r = subprocess.run(
                ["screencapture", "-x", tmp.name],
                capture_output=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            return os.path.getsize(tmp.name) > 1000  # 권한 없으면 0바이트 또는 매우 작음
    except Exception:
        return False


def check_accessibility() -> bool:
    """pyautogui로 마우스 위치 읽기 시도. 손쉬운 사용 권한이 없으면 실패할 수 있음."""
    if platform.system() != "Darwin":
        return True
    try:
        pyautogui.position()
        return True
    except Exception:
        return False


def open_system_settings(which: str) -> bool:
    """macOS 시스템 설정의 권한 페이지를 연다."""
    if platform.system() != "Darwin":
        return False
    urls = {
        "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    }
    url = urls.get(which)
    if not url:
        return False
    try:
        subprocess.Popen(["open", url])
        return True
    except Exception:
        return False


# ─── FastAPI lifespan ───


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("\n" + "=" * 50)
    print("  macOS Remote Desktop Server")
    print("=" * 50)
    print(f"  로컬:  http://localhost:{PORT}")

    if is_setup_required():
        print("  상태:  ⚙️  초기 설정 필요 — 브라우저에서 설정 페이지를 여세요")
        print("=" * 50 + "\n")
        yield
        return

    if env_get("NGROK_AUTHTOKEN"):
        url = start_ngrok()
        if url:
            print(f"  외부:  {url}")
            send_telegram(
                f"🖥 <b>원격 데스크탑 시작</b>\n\n"
                f"🌐 <code>{url}</code>\n"
                f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            threading.Thread(target=ngrok_watchdog, daemon=True).start()
    else:
        print("  외부:  (NGROK_AUTHTOKEN 미설정 — 로컬 전용)")

    if env_get("TELEGRAM_BOT_TOKEN") and env_get("TELEGRAM_CHAT_ID"):
        print("  알림:  텔레그램 활성화")
    else:
        print("  알림:  (텔레그램 미설정)")

    print("=" * 50 + "\n")

    yield

    if env_get("NGROK_AUTHTOKEN"):
        send_telegram(f"⚠️ <b>서버 종료</b> {datetime.now().strftime('%H:%M:%S')}")
        stop_ngrok()


app = FastAPI(title="macOS Remote Desktop", lifespan=lifespan)

# ─── 인증 ───

sessions: dict[str, dict] = {}
login_attempts: dict[str, dict] = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 10


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def is_localhost(request: Request) -> bool:
    ip = get_client_ip(request)
    return ip in ("127.0.0.1", "::1", "localhost", "unknown")


def check_ip_lock(ip: str):
    info = login_attempts.get(ip)
    if not info or not info.get("locked_until"):
        return
    if datetime.now() < info["locked_until"]:
        raise HTTPException(status_code=429, detail="너무 많은 시도. 잠시 후 다시 시도하세요.")
    login_attempts.pop(ip, None)


def record_login_failure(ip: str) -> int:
    entry = login_attempts.setdefault(ip, {"count": 0, "locked_until": None})
    entry["count"] += 1
    if entry["count"] >= MAX_LOGIN_ATTEMPTS:
        entry["locked_until"] = datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)
        logger.warning(f"IP {ip} 잠금 ({LOCKOUT_MINUTES}분)")
    return max(0, MAX_LOGIN_ATTEMPTS - entry["count"])


def verify_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="인증 토큰이 필요합니다")
    token = auth[7:]
    session = sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다")
    if datetime.now() > session["expires_at"]:
        sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="토큰이 만료되었습니다")
    return token


def require_setup_or_auth(request: Request) -> None:
    """초기 설정 중에는 localhost에서만 허용, 그 외에는 토큰 필요."""
    if is_setup_required():
        if not is_localhost(request):
            raise HTTPException(status_code=403, detail="초기 설정은 로컬에서만 가능합니다")
        return
    verify_token(request)


# ─── 요청 모델 ───


class LoginRequest(BaseModel):
    password: str


class ClickRequest(BaseModel):
    x: float
    y: float
    double: bool = False
    right: bool = False


class DragRequest(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float


class ScrollRequest(BaseModel):
    x: float
    y: float
    direction: str
    amount: int = 3


class KeyRequest(BaseModel):
    key: str
    modifiers: list[str] = []


class TypeRequest(BaseModel):
    text: str


class ConfigUpdate(BaseModel):
    password: str | None = None
    port: int | None = None
    token_expire_hours: int | None = None
    ngrok_authtoken: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class TokenTest(BaseModel):
    token: str


class TelegramTest(BaseModel):
    bot_token: str
    chat_id: str


class OpenSettings(BaseModel):
    which: str  # "screen" | "accessibility"


# ─── 헬퍼 ───

_MODIFIER_MAP = {
    "command": "command", "cmd": "command",
    "ctrl": "ctrl", "control": "ctrl",
    "shift": "shift",
    "alt": "alt", "option": "alt",
}


def clamp_coords(x: float, y: float) -> tuple[int, int]:
    w, h = pyautogui.size()
    return max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))


def capture_screen():
    """화면 캡처. PNG(무손실)로 캡처하여 JPEG 이중 압축 방지."""
    try:
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
            subprocess.run(
                ["screencapture", "-x", "-C", tmp.name],
                check=True, timeout=5,
            )
            return Image.open(tmp.name).copy()
    except Exception:
        return pyautogui.screenshot()


# ─── 엔드포인트 ───
# 블로킹 호출(pyautogui 등)은 def(동기)로 선언 → FastAPI 스레드풀 자동 실행


@app.get("/")
async def root():
    if is_setup_required():
        return RedirectResponse(url="/setup")
    return RedirectResponse(url="/static/index.html")


@app.get("/setup")
async def setup_page():
    return FileResponse(STATIC_DIR / "setup.html")


# ─── 설정 API ───


@app.get("/api/status")
def api_status():
    """현재 서버 상태 (인증 없이 호출 가능)."""
    return {
        "setup_required": is_setup_required(),
        "platform": platform.system(),
    }


@app.get("/api/config")
def api_get_config(request: Request):
    require_setup_or_auth(request)
    cfg = env_read_all()
    # 비밀번호 해시는 노출하지 않음 — 설정 여부만 알려줌
    return {
        "has_password": bool(cfg.get("PASSWORD_HASH")),
        "port": int(cfg.get("PORT", "8000") or "8000"),
        "token_expire_hours": int(cfg.get("TOKEN_EXPIRE_HOURS", "24") or "24"),
        "ngrok_authtoken": cfg.get("NGROK_AUTHTOKEN", ""),
        "telegram_bot_token": cfg.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": cfg.get("TELEGRAM_CHAT_ID", ""),
        "ngrok_url": _ngrok_tunnel.public_url if _ngrok_tunnel else None,
    }


@app.post("/api/config")
def api_set_config(body: ConfigUpdate, request: Request):
    require_setup_or_auth(request)

    updates: dict[str, str] = {}

    if body.password is not None:
        if len(body.password) < 4:
            raise HTTPException(status_code=400, detail="비밀번호는 4자 이상이어야 합니다")
        updates["PASSWORD_HASH"] = hash_password(body.password)
    if body.port is not None:
        if not (1 <= body.port <= 65535):
            raise HTTPException(status_code=400, detail="포트 범위가 잘못됨")
        updates["PORT"] = str(body.port)
    if body.token_expire_hours is not None:
        updates["TOKEN_EXPIRE_HOURS"] = str(max(1, body.token_expire_hours))
    if body.ngrok_authtoken is not None:
        updates["NGROK_AUTHTOKEN"] = body.ngrok_authtoken.strip()
    if body.telegram_bot_token is not None:
        updates["TELEGRAM_BOT_TOKEN"] = body.telegram_bot_token.strip()
    if body.telegram_chat_id is not None:
        updates["TELEGRAM_CHAT_ID"] = body.telegram_chat_id.strip()

    if updates:
        env_update(updates)

    return {"ok": True, "setup_required": is_setup_required()}


@app.post("/api/test-ngrok")
def api_test_ngrok(body: TokenTest, request: Request):
    require_setup_or_auth(request)
    ok, msg = test_ngrok_token(body.token)
    return {"ok": ok, "message": msg}


@app.post("/api/test-telegram")
def api_test_telegram(body: TelegramTest, request: Request):
    require_setup_or_auth(request)
    ok, msg = send_telegram(
        "✅ <b>Remote Desktop</b> 알림 테스트가 성공했어요.",
        token=body.bot_token,
        chat_id=body.chat_id,
    )
    return {"ok": ok, "message": msg or "전송 성공"}


@app.get("/api/permissions")
def api_permissions(request: Request):
    require_setup_or_auth(request)
    return {
        "platform": platform.system(),
        "screen_recording": check_screen_recording(),
        "accessibility": check_accessibility(),
    }


@app.post("/api/open-settings")
def api_open_settings(body: OpenSettings, request: Request):
    require_setup_or_auth(request)
    ok = open_system_settings(body.which)
    return {"ok": ok}


# ─── 로그인 / 일반 엔드포인트 ───


@app.post("/login")
async def login(body: LoginRequest, request: Request):
    if is_setup_required():
        raise HTTPException(status_code=503, detail="초기 설정이 필요합니다")

    ip = get_client_ip(request)
    check_ip_lock(ip)

    stored_hash = env_get("PASSWORD_HASH")
    if not verify_password(body.password, stored_hash):
        remaining = record_login_failure(ip)
        access_logger.info(f"LOGIN_FAIL ip={ip} remaining={remaining}")
        raise HTTPException(status_code=401, detail=f"비밀번호 오류 (남은 시도: {remaining}회)")

    login_attempts.pop(ip, None)
    token = secrets.token_hex(32)
    expire_hours = int(env_get("TOKEN_EXPIRE_HOURS", "24") or "24")
    sessions[token] = {
        "ip": ip,
        "created_at": datetime.now(),
        "expires_at": datetime.now() + timedelta(hours=expire_hours),
    }
    access_logger.info(f"LOGIN_OK ip={ip}")
    return {"token": token, "expires_in": expire_hours * 3600}


@app.get("/info")
def info(request: Request):
    verify_token(request)
    w, h = pyautogui.size()
    return {"width": w, "height": h}


@app.get("/screenshot")
def screenshot(
    request: Request,
    quality: int = Query(default=50, ge=1, le=100),
    scale: float = Query(default=0.5, gt=0, le=1.0),
):
    verify_token(request)
    img = capture_screen()

    if scale < 1.0:
        img = img.resize(
            (int(img.width * scale), int(img.height * scale)),
            resample=1,  # BILINEAR — 속도 우선
        )

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/click")
def click(body: ClickRequest, request: Request):
    verify_token(request)
    x, y = clamp_coords(body.x, body.y)
    if body.right:
        pyautogui.rightClick(x, y)
    elif body.double:
        pyautogui.doubleClick(x, y)
    else:
        pyautogui.click(x, y)
    return {"status": "ok"}


@app.post("/drag")
def drag(body: DragRequest, request: Request):
    verify_token(request)
    dx, dy = body.x2 - body.x1, body.y2 - body.y1
    x1, y1 = clamp_coords(body.x1, body.y1)
    pyautogui.moveTo(x1, y1)
    pyautogui.drag(int(dx), int(dy), duration=0.3)
    return {"status": "ok"}


@app.post("/scroll")
def scroll(body: ScrollRequest, request: Request):
    verify_token(request)
    x, y = clamp_coords(body.x, body.y)
    pyautogui.moveTo(x, y)
    pyautogui.scroll(body.amount if body.direction == "up" else -body.amount)
    return {"status": "ok"}


@app.post("/key")
def key(body: KeyRequest, request: Request):
    verify_token(request)
    keys = [_MODIFIER_MAP[m.lower()] for m in body.modifiers if m.lower() in _MODIFIER_MAP]
    keys.append(body.key)
    if len(keys) == 1:
        pyautogui.press(keys[0])
    else:
        pyautogui.hotkey(*keys)
    return {"status": "ok"}


@app.post("/type")
def type_text(body: TypeRequest, request: Request):
    verify_token(request)
    try:
        body.text.encode("ascii")
        pyautogui.write(body.text, interval=0.02)
    except UnicodeEncodeError:
        subprocess.run(["pbcopy"], input=body.text.encode("utf-8"), check=True)
        pyautogui.hotkey("command", "v")
    return {"status": "ok"}


# 정적 파일
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
