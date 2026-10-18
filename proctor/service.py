"""Service-manager abstraction for systemd, OpenRC, runit, s6, SysV and BSD rc."""

from __future__ import print_function

import os
import shlex
import time

from .util import run, which


class ServiceController(object):
    def __init__(self, host, name="prosody", config=None):
        self.host = host
        self.name = name or "prosody"
        self.config = config or {}
        self.manager = self._resolve_manager(self.config.get("manager", "auto"))

    def _resolve_manager(self, value):
        value = (value or "auto").lower()
        if value not in ("auto", ""):
            return value
        return self.host.init

    @property
    def available(self):
        if self._custom("stop") and self._custom("start"):
            return True
        return self.manager in ("systemd", "openrc", "runit", "s6", "sysv", "rc", "rcctl", "rcd")

    def _custom(self, action):
        commands = (self.config.get("commands") or {})
        command = commands.get(action)
        if not command:
            return None
        if isinstance(command, str):
            command = shlex.split(command)
        return [str(part).replace("{name}", self.name) for part in command]

    def command(self, action):
        custom = self._custom(action)
        if custom:
            return custom
        name = self.name
        if self.manager == "systemd":
            if action == "status":
                return ["systemctl", "is-active", "--quiet", name]
            return ["systemctl", action, name]
        if self.manager == "openrc":
            return ["rc-service", name, action]
        if self.manager == "runit":
            if action == "status":
                return ["sv", "status", name]
            return ["sv", action, name]
        if self.manager == "s6":
            service_path = os.path.join("/run/service", name)
            if action == "status":
                return ["s6-svstat", service_path]
            flag = {"start": "-u", "stop": "-d"}.get(action)
            if not flag:
                raise ValueError("unsupported s6 action: %s" % action)
            return ["s6-svc", flag, service_path]
        if self.manager == "sysv":
            return ["service", name, action]
        if self.manager == "rc":
            return ["service", name, action]
        if self.manager == "rcctl":
            if action == "status":
                return ["rcctl", "check", name]
            return ["rcctl", action, name]
        if self.manager == "rcd":
            script = "/etc/rc.d/%s" % name
            return [script, action]
        raise ValueError("no service manager available for %s" % name)

    def is_active(self):
        if not self.available:
            return None
        if self._custom("status") is None and self.manager not in (
                "systemd", "openrc", "runit", "s6", "sysv", "rc", "rcctl", "rcd"):
            return None
        if self.manager == "runit":
            proc = run(["sv", "status", self.name], check=False)
            output = (proc.stdout or b"").decode("utf-8", "replace")
            return proc.returncode == 0 and output.strip().startswith("run:")
        if self.manager == "s6":
            proc = run(self.command("status"), check=False)
            output = (proc.stdout or b"").decode("utf-8", "replace")
            return "up" in output
        proc = run(self.command("status"), check=False)
        return proc.returncode == 0

    def stop(self, timeout=45.0):
        if not self.available:
            return False
        active = self.is_active()
        if active is False:
            return False
        run(self.command("stop"), timeout=timeout)
        if active is None:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_active() is False:
                return True
            time.sleep(0.3)
        return False

    def start(self, timeout=45.0):
        if not self.available:
            return False
        run(self.command("start"), timeout=timeout)
        if self.is_active() is None:
            # A configured start command without a status command cannot be
            # polled; trust the command's exit status.
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_active() is True:
                return True
            time.sleep(0.3)
        return self.is_active() is True

    def restart(self, timeout=45.0):
        if not self.available:
            return False
        run(self.command("restart"), timeout=timeout)
        if self.is_active() is None:
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_active() is True:
                return True
            time.sleep(0.3)
        return self.is_active() is True

    def reload(self):
        if not self.available:
            return False
        try:
            if self.manager in ("runit", "s6"):
                return self.restart()
            run(self.command("reload"), timeout=60)
            return True
        except Exception:
            try:
                return self.restart()
            except Exception:
                return False

    def enable(self):
        if not self.available:
            return False
        custom = self._custom("enable")
        if custom:
            run(custom)
            return True
        name = self.name
        try:
            if self.manager == "systemd":
                run(["systemctl", "enable", name], check=True)
            elif self.manager == "openrc":
                run(["rc-update", "add", name, "default"], check=True)
            elif self.manager == "runit":
                source = os.path.join("/etc/sv", name)
                target = os.path.join("/var/service", name)
                if os.path.isdir(source) and os.path.isdir("/var/service"):
                    if not os.path.lexists(target):
                        os.symlink(source, target)
                else:
                    return False
            elif self.manager == "rc":
                run(["sysrc", "%s_enable=YES" % name], check=True)
            elif self.manager == "rcctl":
                run(["rcctl", "enable", name], check=True)
            elif self.manager == "sysv":
                if which("chkconfig"):
                    run(["chkconfig", name, "on"], check=True)
                elif which("update-rc.d"):
                    run(["update-rc.d", name, "defaults"], check=True)
                else:
                    return False
            elif self.manager == "s6":
                return True
            else:
                return False
            return True
        except Exception:
            return False

    def wait_until_stopped(self, timeout=30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_active() is False:
                return True
            time.sleep(0.3)
        return False


def validate_prosody_config(config_file=None, timeout=60):
    """Run prosodyctl's config check if available.

    Returns (status, output) where status is True/False/None (unavailable).
    """
    if not which("prosodyctl"):
        return None, "prosodyctl not found"
    prefix = ["prosodyctl"]
    if config_file:
        prefix.extend(["--config", config_file])
    command = prefix + ["check", "config"]
    proc = run(command, check=False, timeout=timeout)
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()
    if proc.returncode == 127:
        return None, output
    if proc.returncode != 0 and "unknown command" in output.lower():
        command = prefix + ["check"]
        proc = run(command, check=False, timeout=timeout)
        output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()
    return proc.returncode == 0, output
