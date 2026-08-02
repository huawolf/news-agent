"""Install and control the local service at user-login on each desktop OS."""

import os
import subprocess
import sys
from pathlib import Path

from src.runtime import PROJECT_ROOT


SERVICE_NAME = "news-agent"


def _python_path() -> Path:
    candidate = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate if candidate.exists() else Path(sys.executable)


def install() -> str:
    executable = str(_python_path())
    if sys.platform == "darwin":
        directory = Path.home() / "Library" / "LaunchAgents"
        target = directory / "com.news-agent.local.plist"
        directory.mkdir(parents=True, exist_ok=True)
        target.write_text(f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\"><dict><key>Label</key><string>com.news-agent.local</string>
<key>ProgramArguments</key><array><string>{executable}</string><string>-m</string><string>src.main</string><string>serve</string></array>
<key>WorkingDirectory</key><string>{PROJECT_ROOT}</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>""", encoding="utf-8")
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=False, capture_output=True)
        return f"installed LaunchAgent: {target}"
    if os.name == "nt":
        # Task Scheduler does not support a WorkingDirectory field. Run through
        # cmd so Python can import the project package and load the local .env.
        command = f'cmd /d /c "cd /d \"{PROJECT_ROOT}\" && \"{executable}\" -m src.main serve"'
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "News Agent", "/TR", command], check=True)
        return "installed Windows Task Scheduler task: News Agent"
    directory = Path.home() / ".config" / "systemd" / "user"
    target = directory / f"{SERVICE_NAME}.service"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(f"""[Unit]
Description=News Agent local service
After=network-online.target

[Service]
Type=simple
WorkingDirectory={PROJECT_ROOT}
ExecStart={executable} -m src.main serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""", encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"], check=True)
    return f"installed user systemd service: {target}"


def uninstall() -> str:
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / "com.news-agent.local.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.news-agent.local"], check=False, capture_output=True)
        target.unlink(missing_ok=True)
        return "removed LaunchAgent"
    if os.name == "nt":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "News Agent"], check=False)
        return "removed Windows Task Scheduler task"
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"], check=False)
    target = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
    target.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return "removed user systemd service"


def start() -> str:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.news-agent.local"], check=True)
        return "started LaunchAgent"
    if os.name == "nt":
        subprocess.run(["schtasks", "/Run", "/TN", "News Agent"], check=True)
        return "started Windows Task Scheduler task"
    subprocess.run(["systemctl", "--user", "start", f"{SERVICE_NAME}.service"], check=True)
    return "started user systemd service"


def stop() -> str:
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "kill", "SIGTERM", f"gui/{os.getuid()}/com.news-agent.local"], check=False)
        return "stopped LaunchAgent"
    if os.name == "nt":
        subprocess.run(["schtasks", "/End", "/TN", "News Agent"], check=False)
        return "stopped Windows Task Scheduler task"
    subprocess.run(["systemctl", "--user", "stop", f"{SERVICE_NAME}.service"], check=False)
    return "stopped user systemd service"


def status() -> str:
    if sys.platform == "darwin":
        return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.news-agent.local"], text=True, capture_output=True).stdout
    if os.name == "nt":
        return subprocess.run(["schtasks", "/Query", "/TN", "News Agent", "/FO", "LIST"], text=True, capture_output=True).stdout
    return subprocess.run(["systemctl", "--user", "status", f"{SERVICE_NAME}.service", "--no-pager"], text=True, capture_output=True).stdout
