"""Deploy the Trip-Planner app from hosted DEV to hosted PROD.

This is the trip copy of the Lost Miniatures hosted DEV to PROD promote flow.
On the Railway development environment, the trip page's "Deploy to production"
button uploads the app files that DEV is running to the production
environment of the same Railway project (Trip-Planner) with `railway up`.

Only app files are uploaded. Trip data (trip_itinerary.json, trip_files/,
trip_photos/, password and Gmail files) lives on each environment's volume at
DLM_DATA_DIR (usually /data) and is never uploaded, replaced, or deleted.
Lost Miniatures / ema is a different project and is never touched.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path


class RailwayDeployError(RuntimeError):
    """The app cannot start a Railway deploy."""


_LOCK = threading.Lock()
_JOB = {
    "running": False,
    "succeeded": None,
    "error": "",
    "log": "",
    "target": "",
}

_CLI_VERSION = "5.62.1"
_PROD_TOKEN_ENV = "RAILWAY_PROD_TOKEN"
_DEFAULT_SERVICE = "Trip-Planner"
_START_COMMAND = "python app/trip/server.py"
_LINK_ENV_KEYS = (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_ENVIRONMENT_NAME",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
    "RAILWAY_SERVICE_NAME",
    "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_REPLICA_ID",
    "RAILWAY_SNAPSHOT_ID",
    "RAILWAY_TOKEN",
    "RAILWAY_API_TOKEN",
)


def running_on_railway() -> bool:
    return bool(os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT"))


def app_environment() -> str:
    """DEV on the hosted development environment, PROD on Railway otherwise, LOCAL on a PC."""
    explicit = str(os.environ.get("DLM_ENV", "") or "").strip().upper()
    if explicit in {"DEV", "DEVELOPMENT"}:
        return "DEV"
    if explicit in {"PROD", "PRODUCTION"}:
        return "PROD"
    if explicit == "LOCAL":
        return "LOCAL"
    name = str(os.environ.get("RAILWAY_ENVIRONMENT_NAME", "") or "").strip().lower()
    if name in {"development", "dev", "staging"}:
        return "DEV"
    if running_on_railway():
        return "PROD"
    return "LOCAL"


def can_promote() -> bool:
    """True only on the hosted DEV trip service."""
    return running_on_railway() and app_environment() == "DEV"


def service_name() -> str:
    for key in ("RAILWAY_SERVICE_NAME", "TRIP_SERVICE"):
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return _DEFAULT_SERVICE


def normalize_target(raw: str) -> str:
    text = str(raw or "prod").strip().lower()
    if text in {"prod", "production"}:
        return "prod"
    if text in {"dev", "development"}:
        return "dev"
    raise RailwayDeployError("Choose DEV or PROD.")


def _https(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    return url if url.startswith("https://") else ""


def _read_host_file(data_dir: Path) -> dict:
    path = data_dir / "railway_host.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def hosted_targets(data_dir: Path) -> dict:
    payload = _read_host_file(data_dir)
    prod = _https(os.environ.get("TRIP_PROD_URL")) or _https(payload.get("prod")) or _https(payload.get("url"))
    dev = _https(payload.get("dev"))
    if running_on_railway():
        origin = _https(os.environ.get("DLM_PUBLIC_ORIGIN"))
        if not origin:
            domain = str(os.environ.get("RAILWAY_PUBLIC_DOMAIN", "") or "").strip().strip("/")
            origin = f"https://{domain}" if domain else ""
        if app_environment() == "DEV" and origin:
            dev = origin
        elif origin:
            prod = origin
    if prod.endswith("/trip"):
        prod = prod[: -len("/trip")]
    return {"dev": dev, "prod": prod, "url": prod}


def hosted_url(data_dir: Path) -> str:
    return hosted_targets(data_dir)["url"]


_TOKEN_FILE = "railway_prod_token"
_ACCOUNT_FILE = "railway_account_token"
_UPLOAD_SOURCE = ""
_REDACT_TOKEN = ""


def _clean_token(raw: str) -> str:
    token = str(raw or "").strip().strip("'\"")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def prod_token(data_dir: Path | None = None) -> str:
    env = _clean_token(os.environ.get(_PROD_TOKEN_ENV, "") or "")
    if not env:
        env = _clean_token(os.environ.get("RAILWAY_TOKEN", "") or "")
    if env:
        return env
    if data_dir is None:
        return ""
    try:
        return _clean_token((data_dir / _TOKEN_FILE).read_text(encoding="utf-8"))
    except OSError:
        return ""


def prod_token_source(data_dir: Path | None = None) -> str:
    """Name the place prod_token() read from, for error messages. Never the token itself."""
    if _clean_token(os.environ.get(_PROD_TOKEN_ENV, "") or ""):
        return _PROD_TOKEN_ENV
    if _clean_token(os.environ.get("RAILWAY_TOKEN", "") or ""):
        return "RAILWAY_TOKEN"
    if data_dir is not None:
        try:
            if _clean_token((data_dir / _TOKEN_FILE).read_text(encoding="utf-8")):
                return f"the saved {(data_dir / _TOKEN_FILE).as_posix()} file"
        except OSError:
            pass
    return ""


def _token_help() -> str:
    return (
        "Create a project token in Railway project Trip-Planner > Project Settings > Tokens "
        "with Environment = production, set it as RAILWAY_PROD_TOKEN on the "
        f"{service_name()} service in the development environment, then redeploy DEV. "
        "An account token (RAILWAY_API_TOKEN) cannot upload to production."
    )


def missing_prod_token_message() -> str:
    return "Deploy to production is not set up on this DEV app yet. " + _token_help()


def unauthorized_message(source: str) -> str:
    used = source or "no token"
    return f"Railway said Unauthorized to the production upload. The upload used {used}. " + _token_help()


_UNAUTHORIZED_RE = re.compile(r"unauthori[sz]ed|\b401\b|invalid (?:project )?token|not authorized", re.IGNORECASE)


def looks_unauthorized(text: str) -> bool:
    return bool(_UNAUTHORIZED_RE.search(str(text or "")))


def account_token(data_dir: Path | None = None) -> str:
    env = str(os.environ.get("RAILWAY_API_TOKEN", "") or "").strip()
    if env:
        return env
    if data_dir is None:
        return ""
    try:
        return (data_dir / _ACCOUNT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def deploy_credential(data_dir: Path | None = None) -> tuple[str, bool]:
    """Token for a DEV to PROD upload. railway up needs a production project token."""
    project = prod_token(data_dir)
    if project:
        return project, False
    return "", False


def hosted_prod_block_reason(on_railway: bool, environment: str, target: str, token: str) -> str:
    """Why this app cannot start a promote. Empty means the DEV to PROD upload can start."""
    if not on_railway:
        return "Deploy to production runs from the hosted DEV trip site."
    if environment != "DEV":
        return "This is production. Deploy to production from the hosted DEV trip site."
    if normalize_target(target) != "prod":
        return "Hosted DEV can only deploy to production."
    if not str(token or "").strip():
        return missing_prod_token_message()
    return ""


def status(data_dir: Path) -> dict:
    with _LOCK:
        job = dict(_JOB)
    targets = hosted_targets(data_dir)
    environment = app_environment()
    on_railway = running_on_railway()
    promote = on_railway and environment == "DEV"
    ready = promote and bool(deploy_credential(data_dir)[0])
    error = job["error"]
    if promote and not ready and not job["running"] and not error:
        error = missing_prod_token_message()
    return {
        "ok": True,
        "local": not on_railway,
        "environment": environment,
        "url": targets["url"],
        "devUrl": targets["dev"],
        "prodUrl": targets["prod"],
        "promoteProd": promote,
        "promoteReady": ready,
        "target": job["target"],
        "running": bool(job["running"]),
        "succeeded": job["succeeded"],
        "error": error,
        "log": job["log"],
    }


def _redact(line: str) -> str:
    text = str(line or "").rstrip()
    for token in (prod_token(), account_token(), _REDACT_TOKEN):
        if token and token in text:
            text = text.replace(token, "[token]")
    return text


def _append_log(line: str) -> None:
    text = _redact(line)
    if not text:
        return
    with _LOCK:
        lines = [item for item in _JOB["log"].splitlines() if item.strip()]
        lines.append(text)
        _JOB["log"] = "\n".join(lines[-30:])


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


_SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", ".railway", "data", "node_modules"}
_SKIP_NAMES = {"railway_account_token", "railway_prod_token", ".env", "railway_deploy.log"}


def stage_hosted_upload(project_root: Path) -> Path:
    """Copy the running app (repo root, the parent of app/) without data or the server venv.

    Railpack creates .venv while DEV runs. Uploading it breaks the PROD build.
    """

    def _ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in _SKIP_DIRS or name in _SKIP_NAMES or name.endswith(".pyc") or name.startswith(".env.")
        }

    root = Path(tempfile.mkdtemp(prefix="trip-prod-upload-"))
    destination = root / "upload"
    try:
        shutil.copytree(project_root, destination, ignore=_ignore, symlinks=True, ignore_dangling_symlinks=True)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    # Railpack fails the build when Python 3.14 has no precompiled binary.
    (destination / ".python-version").write_text("3.13.2\n", encoding="utf-8")
    railpack = {
        "$schema": "https://schema.railpack.com",
        "provider": "python",
        "packages": {"python": "3.13"},
        "deploy": {"startCommand": _START_COMMAND},
    }
    (destination / "railpack.json").write_text(json.dumps(railpack, indent=2) + "\n", encoding="utf-8")
    return destination


def upload_is_safe(project_root: Path, data_dir: Path) -> None:
    """Refuse an upload that could carry trip data to the other environment."""
    ignore = project_root / ".railwayignore"
    try:
        lines = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()]
    except OSError:
        lines = []
    if "data" not in lines and "data/" not in lines:
        raise RailwayDeployError("Refusing to deploy. .railwayignore must exclude data/.")
    root = project_root.resolve()
    data = data_dir.resolve()
    if data == root or data in root.parents:
        raise RailwayDeployError("Refusing to deploy. The app folder is inside the data folder.")
    try:
        relative = data.relative_to(root).as_posix()
    except ValueError:
        return
    if relative != "data" and not relative.startswith("data/"):
        raise RailwayDeployError("Refusing to deploy. The data folder is inside the app upload.")


def promote_subprocess_env(base: dict, token: str, project_id: str, service: str) -> dict:
    """Point the CLI at PROD only. The DEV process is linked to the development environment."""
    env = dict(base)
    for key in _LINK_ENV_KEYS:
        env.pop(key, None)
    env.pop(_PROD_TOKEN_ENV, None)
    env["RAILWAY_TOKEN"] = token
    env["RAILWAY_HTTP_TIMEOUT"] = "300"
    env["RAILWAY_PROJECT_ID"] = project_id
    env["RAILWAY_SERVICE_NAME"] = service
    return env


def _cli_url() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "x86_64-unknown-linux-gnu"
    elif machine in {"aarch64", "arm64"}:
        arch = "aarch64-unknown-linux-gnu"
    else:
        raise RailwayDeployError("This server cannot run the Railway CLI.")
    name = f"railway-v{_CLI_VERSION}-{arch}.tar.gz"
    return f"https://github.com/railwayapp/cli/releases/download/v{_CLI_VERSION}/{name}"


def _ensure_railway_cli() -> Path:
    found = shutil.which("railway")
    if found:
        return Path(found)
    dest = Path(tempfile.gettempdir()) / "trip-railway-cli"
    exe = dest / "railway"
    if exe.is_file() and os.access(exe, os.X_OK) and exe.stat().st_size > 1000:
        return exe
    dest.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(_cli_url(), headers={"User-Agent": "trip-deploy"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except Exception as exc:
        raise RailwayDeployError("Could not download the Railway CLI on DEV.") from exc
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        member = next(
            (item for item in archive.getmembers() if item.isfile() and Path(item.name).name == "railway"),
            None,
        )
        if member is None:
            raise RailwayDeployError("The Railway CLI download did not include the railway program.")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise RailwayDeployError("The Railway CLI download could not be read.")
        exe.write_bytes(extracted.read())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return exe


def _stream_process(command: list[str], env: dict, cwd: Path, log_path: Path, mode: str = "w") -> int:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.time() + 15 * 60
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open(mode, encoding="utf-8") as handle:
        assert process.stdout is not None
        for line in process.stdout:
            if time.time() > deadline:
                process.kill()
                process.wait(timeout=10)
                raise RailwayDeployError("The production deploy took too long.")
            handle.write(_redact(line) + "\n")
            handle.flush()
            _append_log(line)
    return process.wait()


def _log_says_unauthorized(log_path: Path) -> bool:
    try:
        return looks_unauthorized(log_path.read_text(encoding="utf-8", errors="replace")[-6000:])
    except OSError:
        return False


def _service_status(cli: Path, env: dict, service: str) -> dict:
    result = subprocess.run(
        [str(cli), "service", "status", "--service", service, "--environment", "production", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    payload = _extract_json(text)
    if not payload and result.returncode != 0 and looks_unauthorized(text):
        raise RailwayDeployError(unauthorized_message(_UPLOAD_SOURCE))
    return payload


def _append_build_logs(cli: Path, env: dict, service: str) -> None:
    """Pull build and runtime logs so the page shows why PROD failed."""
    commands = [
        [str(cli), "logs", "--service", service, "--environment", "production", "--lines", "60"],
        [str(cli), "logs", "--service", service, "--environment", "production", "--build", "--lines", "40"],
    ]
    for command in commands:
        try:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=40, check=False)
        except Exception as exc:
            _append_log(f"Could not read Railway logs: {exc}")
            continue
        body = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
        if not body:
            continue
        _append_log("Build log:" if "--build" in command else "Runtime log:")
        for line in body.splitlines()[-18:]:
            _append_log(line)


def _wait_until_live(cli: Path, env: dict, service: str, previous_id: str) -> None:
    for _attempt in range(60):
        time.sleep(10)
        parsed = _service_status(cli, env, service)
        state = str(parsed.get("status") or "")
        deployment_id = str(parsed.get("deploymentId") or "")
        _append_log(f"Deployment status: {state or 'waiting'}")
        if not deployment_id or deployment_id == previous_id:
            _append_log("Waiting for the new production deployment...")
            continue
        if "SUCCESS" in state:
            return
        if state and any(flag in state for flag in ("FAILED", "CRASHED")):
            _append_build_logs(cli, env, service)
            raise RailwayDeployError(f"The new production deployment failed ({state}).")
    raise RailwayDeployError("The Railway deployment did not finish in time.")


def _run_hosted_prod(project_root: Path, data_dir: Path, on_success) -> None:
    global _REDACT_TOKEN, _UPLOAD_SOURCE
    error = ""
    succeeded = False
    staged = None
    log_path = data_dir / "railway_deploy.log"
    try:
        upload_is_safe(project_root, data_dir)
        token, _account = deploy_credential(data_dir)
        _REDACT_TOKEN = token
        _UPLOAD_SOURCE = prod_token_source(data_dir)
        if not token:
            raise RailwayDeployError(missing_prod_token_message())
        project_id = str(os.environ.get("RAILWAY_PROJECT_ID", "") or "").strip()
        service = service_name()
        if not project_id:
            raise RailwayDeployError("This DEV app is missing its Railway project id.")
        _append_log(
            f"Uploading this DEV app to production ({service}). Trip data on the production volume is not replaced."
        )
        staged = stage_hosted_upload(project_root)
        cli = _ensure_railway_cli()
        env = promote_subprocess_env(os.environ, token, project_id, service)
        previous_id = str(_service_status(cli, env, service).get("deploymentId") or "")
        command = [
            str(cli),
            "up",
            str(staged),
            "--path-as-root",
            "--service",
            service,
            "--environment",
            "production",
            "--project",
            project_id,
            "-y",
            "--verbose",
            "-m",
            "Deployed from hosted DEV trip site",
        ]
        uploaded = False
        for attempt in (1, 2):
            code = _stream_process(command, env, project_root, log_path, "w" if attempt == 1 else "a")
            if code == 0:
                uploaded = True
                break
            if _log_says_unauthorized(log_path):
                raise RailwayDeployError(unauthorized_message(_UPLOAD_SOURCE))
            if attempt == 1:
                _append_log("Upload did not finish. Trying once more.")
                time.sleep(8)
        if not uploaded:
            raise RailwayDeployError("The upload to production failed.")
        _wait_until_live(cli, env, service, previous_id)
        _append_log("Production is live.")
        succeeded = True
    except Exception as exc:
        error = _redact(str(exc))
        _append_log(error)
    url = hosted_targets(data_dir)["prod"]
    with _LOCK:
        _JOB["running"] = False
        _JOB["succeeded"] = succeeded
        _JOB["error"] = "" if succeeded else error or "The deploy to production failed."
    if succeeded and on_success:
        try:
            on_success(url, "prod")
        except Exception:
            pass
    if staged is not None:
        shutil.rmtree(staged.parent, ignore_errors=True)
    _REDACT_TOKEN = ""


def start(project_root: Path, data_dir: Path, on_success=None, target: str = "prod") -> dict:
    """Start a hosted DEV to PROD app upload in the background and return status()."""
    chosen = normalize_target(target)
    reason = hosted_prod_block_reason(running_on_railway(), app_environment(), chosen, deploy_credential(data_dir)[0])
    if reason:
        raise RailwayDeployError(reason)
    with _LOCK:
        if _JOB["running"]:
            return status(data_dir)
        _JOB["running"] = True
        _JOB["succeeded"] = None
        _JOB["error"] = ""
        _JOB["target"] = chosen
        _JOB["log"] = f"Started {datetime.now().strftime('%H:%M:%S')} for PROD"
    threading.Thread(
        target=_run_hosted_prod,
        args=(project_root, data_dir, on_success),
        name="trip-railway-promote",
        daemon=True,
    ).start()
    return status(data_dir)
