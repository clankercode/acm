"""Deploying a new version from the dashboard.

The button is a convenience over ``just update``, and it is the most dangerous
thing in the program: it runs a shell script that pulls code and installs it. Two
rules follow from that, both enforced here rather than in the UI, because a UI
guard is a suggestion.

1. Only from loopback. The dashboard binds every interface by default so it can
   be read from another machine on the LAN, and an unauthenticated "run this
   script" endpoint on that interface is remote code execution for anyone who can
   reach the port. Opting out is possible (``CCM_UPDATE_FROM_LAN=1``) and is
   nobody's default.
2. Only a checkout that looks like this project. The script is run *in* that
   directory and pulls into it, so the path is not a place to be relaxed.

The update outlives the server that starts it, since restarting the service is
its last step. It therefore runs as its own transient systemd unit where that is
available -- restarting ``ccm.service`` kills everything in that service's
cgroup, a detached child included.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger("ccm.selfupdate")

#: The script does the work; this module only decides whether it may run.
SCRIPT_NAME = "self-update.sh"

#: An attempt that has neither finished nor been heard from in this long is
#: treated as gone, so a killed update cannot wedge the button forever.
STALE_AFTER = 30 * 60.0

#: How much of the transcript the dashboard is given.
LOG_TAIL_BYTES = 16 * 1024


def _script_for(checkout: Path) -> Path | None:
    """The checkout's own copy of the script, which is the one being installed.

    Deliberately not the installed copy: a wheel does not carry ``packaging/``,
    and reaching for a script from somewhere other than the tree being built
    would run one version's steps over another version's checkout.
    """
    script = checkout / "packaging" / SCRIPT_NAME
    return script if script.is_file() else None


@dataclass(frozen=True)
class Status:
    """What the dashboard needs to draw the button and the progress panel."""

    #: True when an update could be started right now.
    available: bool
    #: Why not, in words fit to put in a tooltip. None when it is available.
    reason: str | None
    running: bool
    #: "ok", "failed", or None when nothing has finished yet.
    outcome: str | None
    #: Tail of the transcript, or "" when there has never been an attempt.
    log: str
    checkout: str | None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "reason": self.reason,
            "running": self.running,
            "outcome": self.outcome,
            "log": self.log,
            "checkout": self.checkout,
        }


class Updater:
    """Guards, starts and reports on a self-update."""

    def __init__(self, settings: Settings):
        self.settings = settings
        state_dir = settings.db_path.parent
        self.log_path = state_dir / "update.log"
        self.done_path = state_dir / "update.log.done"
        self.marker_path = state_dir / "update.running"

    # -- checkout ----------------------------------------------------------

    def checkout_problem(self) -> str | None:
        """Why this copy cannot update itself, or None when it can."""
        path = self.settings.checkout_path
        if path is None:
            return (
                "No checkout configured. Set CCM_CHECKOUT to the git checkout "
                "to build from, e.g. in ~/.config/ccm/env"
            )
        if not path.is_dir():
            return f"{path} is not a directory"
        if not (path / ".git").exists():
            return f"{path} is not a git checkout"
        # Named, so pointing this at some other repo does not pull that repo and
        # install it as this program.
        if not (path / "justfile").is_file() or not (path / "pyproject.toml").is_file():
            return f"{path} does not look like a ccm checkout"
        if _script_for(path) is None:
            return f"{path} has no packaging/{SCRIPT_NAME}"
        return None

    # -- state -------------------------------------------------------------

    def running(self) -> bool:
        """True while an attempt is in flight.

        The marker is a file rather than memory on purpose: the update restarts
        the server, so the process that started it is not the process asked about
        it afterwards.
        """
        try:
            started = self.marker_path.stat().st_mtime
        except OSError:
            return False
        if self.done_path.is_file() and self.done_path.stat().st_mtime >= started:
            return False
        return (time.time() - started) < STALE_AFTER

    def outcome(self) -> str | None:
        try:
            text = self.done_path.read_text()
        except OSError:
            return None
        _, _, value = text.strip().partition("=")
        return value or None

    def tail(self) -> str:
        try:
            with self.log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - LOG_TAIL_BYTES))
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def status(self) -> Status:
        problem = self.checkout_problem()
        running = self.running()
        reason = problem or ("An update is already running" if running else None)
        return Status(
            available=reason is None,
            reason=reason,
            running=running,
            outcome=None if running else self.outcome(),
            log=self.tail(),
            checkout=str(self.settings.checkout_path) if self.settings.checkout_path else None,
        )

    # -- start -------------------------------------------------------------

    def may_start_from(self, client_host: str | None) -> str | None:
        """Why a request from this address may not update, or None."""
        if self.settings.update_from_lan:
            return None
        # By parsed address rather than by string: 127.0.0.1 is not the only
        # loopback address, and a hostname would be the client's to choose. An
        # address that cannot be read at all -- no peer, or something exotic in
        # front of us -- is refused rather than assumed local.
        try:
            local = ipaddress.ip_address(client_host or "").is_loopback
        except ValueError:
            local = False
        if local:
            return None
        return (
            "Updates are only allowed from the machine running the monitor. "
            "Set CCM_UPDATE_FROM_LAN=1 to allow them over the network"
        )

    @staticmethod
    def cross_site_problem(headers) -> str | None:
        """Refuse a request a browser says came from somewhere else.

        The loopback guard above does nothing here: a browser *is* on loopback.
        Any page in any tab can POST to ``http://localhost:8808/api/update`` --
        no token, no CORS preflight for a simple request -- and the update runs.
        That is the realistic attack on this endpoint, not someone on the LAN.

        ``Sec-Fetch-Site`` is the defence, because it is set by the browser and
        cannot be set by the page. Absent means the caller is not a browser at
        all (curl, a script), which is allowed: the point is not to authenticate,
        it is to stop a foreign page from spending this endpoint.
        """
        site = headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return f"Refusing an update requested from another site ({site})"
        origin = headers.get("origin")
        host = headers.get("host")
        if origin and host and origin.split("//")[-1] != host:
            return f"Refusing an update requested from {origin}"
        return None

    def start(self) -> Status:
        """Launch the script. Raises RuntimeError if it must not run."""
        problem = self.checkout_problem()
        if problem:
            raise RuntimeError(problem)
        if self.running():
            raise RuntimeError("An update is already running")

        checkout = self.settings.checkout_path
        assert checkout is not None  # checkout_problem would have said otherwise
        script = _script_for(checkout)
        assert script is not None

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Cleared first: a stale "status=ok" beside a running attempt would read
        # as an update that had already succeeded.
        self.done_path.unlink(missing_ok=True)
        self.log_path.write_text("[starting] update requested\n")
        self.marker_path.write_text(f"{time.time():.0f}\n")

        argv = ["bash", str(script), str(checkout), str(self.log_path)]
        runner = shutil.which("systemd-run")
        if runner:
            # Its own transient unit, or restarting ccm.service part-way through
            # would kill the update along with the server.
            argv = [
                runner,
                "--user",
                "--collect",
                "--quiet",
                f"--unit=ccm-self-update-{int(time.time())}",
                *argv,
            ]
        try:
            subprocess.Popen(
                argv,
                cwd=str(checkout),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Only reached without systemd-run: a new session at least
                # survives the parent, even if the cgroup kill still finds it.
                start_new_session=True,
            )
        except OSError as exc:
            self.marker_path.unlink(missing_ok=True)
            raise RuntimeError(f"could not start the update: {exc}") from exc

        log.warning("self-update started in %s", checkout)
        return self.status()
