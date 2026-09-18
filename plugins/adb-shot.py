#!/usr/bin/env python3
"""SwiftBar plugin: 截取当前 adb 设备屏幕，可保存到下载文件夹或上传后复制链接。

菜单两项：
  1. 截图到下载文件夹        -> --shot
  2. 截图并上传 · 复制链接   -> --upload

上传流程对应抓包样例：
  66: POST https://s.utui.cc/get_upload_url  (Cookie: session_id=...)
      -> {"urls":[{"upload":{"url":...,"fields":{...}},"view_url":...}]}
  67: POST <S3 upload url> multipart/form-data，回显 fields，最后追加 file 字段
      -> 204 No Content
  68: GET view_url 得到图片（本插件不做校验，只认状态码）

配置见仓库根 config.json 的 utui / adb_shot 两段；session_id 失效时菜单会红字提示，
并把 https://s.utui.cc/ 的“更新 session_id”入口放到菜单里，需要手动改 config.json。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
STATE_PATH = Path.home() / ".cache" / "adb-shot-state.json"
LOCK_PATH = Path.home() / ".cache" / "adb-shot.lock"
LOCK_STALE_SECONDS = 60
ADB_TIMEOUT = 15
HTTP_TIMEOUT = 30
SIPS = "/usr/bin/sips"
PBCOPY = "/usr/bin/pbcopy"

TITLE_ICON = "sfimage=camera.viewfinder"
SITE_URL = "https://s.utui.cc/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) SwiftBar-ADB-Shot/1.0"

ADB_CANDIDATES = (
    Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
    Path("/opt/homebrew/bin/adb"),
    Path("/usr/local/bin/adb"),
)


class PluginError(Exception):
    """本插件可直接展示给用户的错误。"""


class AuthError(PluginError):
    """session_id 失效或缺失。"""


# --------------------------------------------------------------------------- #
# 配置与状态
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    try:
        with CONFIG_PATH.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError as error:
        raise PluginError(f"找不到配置文件: {CONFIG_PATH}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise PluginError(f"config.json 无法读取: {error}") from error

    if not isinstance(config, dict):
        raise PluginError("config.json 顶层必须是对象")
    return config


def section(config: dict, name: str) -> dict:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def shot_options(config: dict) -> dict:
    options = section(config, "adb_shot")
    return {
        "adb_path": str(options.get("adb_path") or "").strip(),
        "screenshot_dir": str(options.get("screenshot_dir") or "~/Downloads").strip(),
        "notify": bool(options.get("notify", True)),
        "jpeg_quality": max(1, min(int(options.get("jpeg_quality") or 85), 100)),
    }


def upload_options(config: dict) -> dict:
    options = section(config, "utui")
    return {
        "session_id": str(options.get("session_id") or "").strip(),
        "endpoint": str(
            options.get("endpoint") or "https://s.utui.cc/get_upload_url"
        ).strip(),
    }


def load_state() -> dict:
    try:
        with STATE_PATH.open(encoding="utf-8") as state_file:
            state = json.load(state_file)
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(**values) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(values)
    payload["time"] = datetime.now().strftime("%H:%M:%S")
    temporary = STATE_PATH.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False)
        os.replace(temporary, STATE_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def safe_menu_text(value: object) -> str:
    return str(value).replace("|", "-").replace("\n", " ").strip()


def expand_path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def quote_osascript(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    """弹一条 macOS 通知；失败静默忽略，不影响主流程。"""
    script = [
        "-e",
        "on run argv",
        "-e",
        'display notification (item 1 of argv) with title (item 2 of argv) sound name "default"',
        "-e",
        "end run",
    ]
    try:
        subprocess.run(
            ["/usr/bin/osascript", *script, message[:400], title],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def copy_to_clipboard(text: str) -> None:
    subprocess.run([PBCOPY], input=text.encode("utf-8"), check=True, timeout=5)


# --------------------------------------------------------------------------- #
# 锁
# --------------------------------------------------------------------------- #


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (0, 1):
        try:
            descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt or not lock_is_stale():
                raise PluginError("上一次操作还在进行，稍后再试")
            LOCK_PATH.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            lock_file.write(f"{os.getpid()} {int(time.time())}\n")
        return


def lock_is_stale() -> bool:
    try:
        return time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return True


def release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# adb
# --------------------------------------------------------------------------- #


def resolve_adb(config: dict) -> Path | None:
    configured = shot_options(config)["adb_path"]
    if configured:
        candidate = expand_path(configured)
        return candidate if os.access(candidate, os.X_OK) else None
    for candidate in ADB_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("adb")
    return Path(found) if found else None


def run_adb(adb: Path, arguments: list[str], timeout: int = ADB_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(adb), *arguments],
        check=False,
        timeout=timeout,
        capture_output=True,
    )


def list_devices(adb: Path) -> tuple[list[dict], list[dict]]:
    """返回 (可用设备, 非 device 状态的设备)。"""
    result = run_adb(adb, ["devices", "-l"])
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise PluginError(f"adb devices 失败: {message or result.returncode}")

    ready: list[dict] = []
    others: list[dict] = []
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = next(
            (token.split(":", 1)[1] for token in parts[2:] if token.startswith("model:")),
            "",
        )
        device = {"serial": serial, "state": state, "model": model.replace("_", " ")}
        (ready if state == "device" else others).append(device)
    return ready, others


def capture_png(adb: Path, serial: str) -> bytes:
    result = subprocess.run(
        [str(adb), "-s", serial, "exec-out", "screencap", "-p"],
        check=False,
        timeout=ADB_TIMEOUT,
        capture_output=True,
    )
    if result.returncode == 0 and result.stdout:
        return result.stdout

    # 老版本 adb 不支持 exec-out 时回退到设备内落盘 + pull。
    remote = f"/sdcard/adb-shot-{uuid.uuid4().hex}.png"
    try:
        shell = run_adb(adb, ["-s", serial, "shell", "screencap", "-p", remote])
        if shell.returncode != 0:
            message = (shell.stderr or shell.stdout).decode("utf-8", "replace").strip()
            raise PluginError(f"screencap 失败: {message or shell.returncode}")
        pulled = run_adb(adb, ["-s", serial, "pull", remote, "-"])
        if pulled.returncode != 0 or not pulled.stdout:
            raise PluginError("adb pull 截图失败")
        return pulled.stdout
    finally:
        run_adb(adb, ["-s", serial, "shell", "rm", "-f", remote])


def save_screenshot(adb: Path, serial: str, options: dict) -> Path:
    png_bytes = capture_png(adb, serial)
    if not png_bytes:
        raise PluginError("截图内容为空")

    screenshot_dir = expand_path(options["screenshot_dir"])
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PluginError(f"无法创建目录 {screenshot_dir}: {error}") from error

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = screenshot_dir / f"adb-{serial}-{stamp}.jpg"
    counter = 1
    while target.exists():
        target = screenshot_dir / f"adb-{serial}-{stamp}-{counter}.jpg"
        counter += 1

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as png_file:
        png_file.write(png_bytes)
        png_path = Path(png_file.name)
    try:
        converted = subprocess.run(
            [
                SIPS,
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                str(options["jpeg_quality"]),
                str(png_path),
                "--out",
                str(target),
            ],
            check=False,
            timeout=30,
            capture_output=True,
        )
        if converted.returncode != 0 or not target.exists():
            message = (converted.stderr or converted.stdout).decode(
                "utf-8", "replace"
            ).strip()
            raise PluginError(f"PNG 转 JPEG 失败: {message or converted.returncode}")
    finally:
        png_path.unlink(missing_ok=True)
    return target


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #


def http_post(url: str, body: bytes, headers: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        raise PluginError(f"网络请求失败: {error.reason}") from error
    except TimeoutError as error:
        raise PluginError("网络请求超时") from error


def request_upload_ticket(
    endpoint: str, session_id: str, size: int
) -> tuple[str, dict, str]:
    payload = json.dumps(
        [{"index": 0, "size": size, "type": "image/jpeg", "file": {}}],
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Cookie": f"session_id={session_id}",
        "Origin": SITE_URL.rstrip("/"),
        "Referer": SITE_URL,
        "User-Agent": USER_AGENT,
    }
    status, body = http_post(endpoint, payload, headers)
    if status in (401, 403):
        raise AuthError(f"session_id 已失效（HTTP {status}）")
    if status >= 400:
        raise PluginError(f"get_upload_url 返回 HTTP {status}: {summarize(body)}")

    try:
        parsed = json.loads(body.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as error:
        raise PluginError("get_upload_url 返回了非 JSON 内容") from error

    errors = parsed.get("errors")
    if errors:
        raise PluginError(f"服务端返回错误: {summarize(json.dumps(errors, ensure_ascii=False).encode('utf-8'))}")

    entries = parsed.get("urls") or []
    entry = next((item for item in entries if item.get("index") == 0), None)
    if entry is None and entries:
        entry = entries[0]
    if not isinstance(entry, dict):
        raise PluginError("get_upload_url 响应缺少 urls[0]")

    upload = entry.get("upload") or {}
    upload_url = str(upload.get("url") or "").strip()
    fields = upload.get("fields") or {}
    view_url = str(entry.get("view_url") or entry.get("preview_url") or "").strip()
    if not upload_url or not isinstance(fields, dict):
        raise PluginError("get_upload_url 响应缺少上传地址或签名字段")
    if not view_url:
        raise PluginError("get_upload_url 响应缺少 view_url")
    return upload_url, fields, view_url


def summarize(body: bytes, limit: int = 160) -> str:
    text = body.decode("utf-8", "replace").strip()
    return text[:limit] if text else "(空响应)"


def build_multipart(
    fields: dict, file_bytes: bytes, filename: str, content_type: str
) -> tuple[bytes, str]:
    boundary = f"----SwiftBarADBShot{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def upload_image(image_path: Path, config: dict) -> str:
    options = upload_options(config)
    if not options["session_id"]:
        raise AuthError("未配置 utui.session_id")

    size = image_path.stat().st_size
    upload_url, fields, view_url = request_upload_ticket(
        options["endpoint"], options["session_id"], size
    )

    body, content_type = build_multipart(
        fields, image_path.read_bytes(), "image.jpg", "image/jpeg"
    )
    status, response = http_post(
        upload_url,
        body,
        {
            "Content-Type": content_type,
            "Accept": "*/*",
            "Origin": SITE_URL.rstrip("/"),
            "Referer": SITE_URL,
            "User-Agent": USER_AGENT,
        },
    )
    if status >= 300:
        raise PluginError(f"S3 上传返回 HTTP {status}: {summarize(response)}")
    return view_url


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #


def resolve_device(adb: Path, serial: str | None) -> dict:
    ready, others = list_devices(adb)
    if serial:
        for device in ready:
            if device["serial"] == serial:
                return device
        raise PluginError(f"设备 {serial} 不可用")
    if not ready:
        if others:
            detail = "、".join(f"{item['serial']}({item['state']})" for item in others)
            raise PluginError(f"没有可用设备: {detail}")
        raise PluginError("未连接设备")
    return ready[0]


def prepare_screenshot(config: dict, serial: str | None) -> tuple[Path, dict]:
    adb = resolve_adb(config)
    if adb is None:
        raise PluginError("找不到 adb，请在 config.json 的 adb_shot.adb_path 指定")
    device = resolve_device(adb, serial)
    options = shot_options(config)
    return save_screenshot(adb, device["serial"], options), options


def do_shot(config: dict, serial: str | None) -> int:
    acquire_lock()
    try:
        image_path, _ = prepare_screenshot(config, serial)
        save_state(action="shot", ok=True, detail="截图已保存", file=str(image_path))
        return 0
    except PluginError as error:
        save_state(action="shot", ok=False, detail=str(error))
        return 1
    except subprocess.TimeoutExpired:
        save_state(action="shot", ok=False, detail="adb 执行超时")
        return 1
    except Exception as error:  # noqa: BLE001 - 兜底，避免插件崩溃丢状态
        save_state(action="shot", ok=False, detail=f"{type(error).__name__}: {error}")
        return 1
    finally:
        release_lock()


def do_upload(config: dict, serial: str | None) -> int:
    acquire_lock()
    try:
        image_path, options = prepare_screenshot(config, serial)
        view_url = upload_image(image_path, config)
        copy_to_clipboard(view_url)
        save_state(
            action="upload",
            ok=True,
            detail="上传成功，链接已复制",
            file=str(image_path),
            url=view_url,
        )
        if options["notify"]:
            notify("截图已上传", f"链接已复制到剪贴板\n{view_url}")
        return 0
    except AuthError as error:
        save_state(action="upload", ok=False, detail=str(error), auth_error=True)
        if shot_options(config)["notify"]:
            notify("截图上传失败", str(error))
        return 1
    except PluginError as error:
        save_state(action="upload", ok=False, detail=str(error))
        if shot_options(config)["notify"]:
            notify("截图上传失败", str(error))
        return 1
    except subprocess.TimeoutExpired:
        save_state(action="upload", ok=False, detail="adb 执行超时")
        return 1
    except Exception as error:  # noqa: BLE001
        save_state(action="upload", ok=False, detail=f"{type(error).__name__}: {error}")
        if shot_options(config)["notify"]:
            notify("截图上传失败", str(error))
        return 1
    finally:
        release_lock()


# --------------------------------------------------------------------------- #
# 菜单
# --------------------------------------------------------------------------- #


def plugin_command(action: str, serial: str | None = None) -> str:
    script = str(Path(__file__).resolve())
    command = f'bash="{script}" param1={action}'
    if serial:
        command += f" param2={serial}"
    return f"{command} terminal=false refresh=true"


def device_label(device: dict) -> str:
    model = safe_menu_text(device.get("model") or "")
    return f"{device['serial']} ({model})" if model else str(device["serial"])


def print_state_line(state: dict) -> None:
    if not state:
        print("最近一次: 还没有记录 | color=#8E8E93")
        return

    stamp = safe_menu_text(state.get("time") or "")
    detail = safe_menu_text(state.get("detail") or "")
    if state.get("ok"):
        if state.get("action") == "upload":
            print(f"最近一次: {stamp} 上传成功 · 链接已复制 | color=#8E8E93")
        else:
            name = Path(state.get("file") or "").name
            file_url = urllib.parse.quote(str(state.get("file") or ""), safe="/:")
            print(
                f"最近一次: {stamp} 截图已保存 · {safe_menu_text(name)}"
                f' | color=#8E8E93 href="file://{file_url}"'
            )
    else:
        print(f"最近一次: {stamp} 失败 · {detail} | color=red")


def print_auth_hint(config: dict, state: dict) -> None:
    missing = not upload_options(config)["session_id"]
    if not (state.get("auth_error") or missing):
        return
    print(
        '更新 session_id | href="https://s.utui.cc/" '
        'tooltip="登录后在 DevTools → Application → Cookies 复制 session_id，'
        '写入 config.json 的 utui.session_id"'
    )
    if state.get("auth_error"):
        print("（上传失败时记得先更新 session_id） | color=#8E8E93")


def render_menu() -> int:
    try:
        config = load_config()
    except PluginError as error:
        print("ADB: ! | color=red sfimage=exclamationmark.triangle")
        print("---")
        print(f"错误: {safe_menu_text(error)}")
        print("立即刷新 | refresh=true")
        return 1

    options = shot_options(config)
    state = load_state()
    adb = resolve_adb(config)

    print(f"📱 | {TITLE_ICON}")
    print("---")

    if adb is None:
        print("找不到 adb，可在 config.json 指定 adb_path | color=red")
        print("截图到下载文件夹 | color=#8E8E93")
        print("截图并上传 · 复制链接 | color=#8E8E93")
    else:
        try:
            ready, others = list_devices(adb)
            error = None
        except (PluginError, subprocess.TimeoutExpired) as caught:
            ready, others, error = [], [], caught

        if error is not None:
            print(f"adb 异常: {safe_menu_text(error)} | color=red")
            print("截图到下载文件夹 | color=#8E8E93")
        elif not ready:
            print("未连接设备 | color=#8E8E93")
            print("截图到下载文件夹 | color=#8E8E93")
            print("截图并上传 · 复制链接 | color=#8E8E93")
        elif len(ready) == 1:
            device = ready[0]
            print(f"截图到下载文件夹 | {plugin_command('--shot')}")
            print(f"截图并上传 · 复制链接 | {plugin_command('--upload')}")
            print(f"当前设备: {device_label(device)} | color=#8E8E93")
        else:
            for device in ready:
                print(f"{device_label(device)} | sfimage=camera")
                print(f"--截图到下载文件夹 | {plugin_command('--shot', device['serial'])}")
                print(
                    f"--截图并上传 · 复制链接 | {plugin_command('--upload', device['serial'])}"
                )

        for device in others:
            print(f"{device_label(device)} · {device['state']} | color=orange")

    if not upload_options(config)["session_id"]:
        print("未配置 utui.session_id | color=#8E8E93")

    print_state_line(state)
    print_auth_hint(config, state)
    print("---")
    if options["screenshot_dir"]:
        directory = expand_path(options["screenshot_dir"])
        print(f'下载文件夹: {safe_menu_text(directory)} | href="file://{directory}"')
    print("立即刷新 | refresh=true")
    return 0


def main() -> int:
    arguments = sys.argv[1:]
    action = arguments[0] if arguments else ""
    serial = arguments[1] if len(arguments) > 1 else None

    if action in ("--shot", "--upload"):
        try:
            config = load_config()
        except PluginError as error:
            save_state(action=action.strip("-"), ok=False, detail=str(error))
            return 1
        if action == "--shot":
            return do_shot(config, serial)
        return do_upload(config, serial)

    return render_menu()


if __name__ == "__main__":
    sys.exit(main())
