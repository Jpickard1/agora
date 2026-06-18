"""Spawn a brand-new live agent: a claude session in tmux + its hub bridge.

This backs the web UI's "➕ New agent" button. Given a name, a working path, a
target machine + tmux session, and a summary of initial tasks, it:

  1. starts ``claude`` in a detached tmux session at the working path,
  2. starts that agent's hub *bridge* (``hubcli listen``) in its own tmux session
     so the new agent shows online and receives hub messages, and
  3. after a short delay (claude needs to come up), types a bootstrap prompt into
     the agent (its name + initial tasks) and tells it to announce itself.

Works locally or on another machine over ssh (the hub lives on the shared
filesystem, so the target host sees the same hub root).

Users-only (note, not a hard boundary): spawning exists only as this server-side
helper behind the token-gated HTTP endpoint + the browser UI. There is no
``hubcli``/``HubClient``/bridge path to it, so a connected agent following the
normal protocol has no tool with which to spawn another agent. (Under a single
shared token this is a convention, not cryptographic enforcement.) Every spawn
is logged server-side.

Safety on the shell-out: LOCAL spawns run as argv arrays with NO shell, so a
name/path/task value can never be interpreted as a command. Remote (ssh) spawns
build the command with shlex quoting. Inputs are validated: name/session are
sanitised to a safe charset, the machine must be a bare hostname, and the path
may not contain control characters.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time

from .store import _safe_name

# A machine is either empty/local or a plain hostname (alnum . _ - and user@host).
_MACHINE_RE = re.compile(r"^[A-Za-z0-9._@-]+$")


def _is_local(machine: str | None) -> bool:
    if not machine:
        return True
    m = machine.strip().lower()
    host = socket.gethostname().split(".")[0].lower()
    return m in ("", "local", "localhost", "127.0.0.1", "::1", host)


def _hubcli_bin() -> str:
    return os.environ.get("AGORA_HUBCLI") or shutil.which("hubcli") or "hubcli"


def _claude_bin() -> str:
    return os.environ.get("AGORA_CLAUDE_BIN") or "claude"


def bootstrap_prompt(name: str, tasks: str, bridge_session: str) -> str:
    """The first message typed into the new agent so it knows who it is."""
    tasks = " ".join((tasks or "").split()) or "(none specified yet — wait for instructions)"
    return (
        f"You are agent '{name}' on the agora communication hub. You are already "
        f"connected: a bridge (tmux session {bridge_session}) delivers hub messages "
        f"to you as lines like '[HUB #general from someone]: ...', and you reply with "
        f"shell commands, e.g.  hubcli post -c general --author {name} \"your reply\". "
        f"Your initial tasks: {tasks}. "
        f"First, announce yourself by running: "
        f"hubcli post -c general --author {name} \"{name} online — ready to help\"  "
        f"then begin. Stay connected and respond to hub messages."
    )


def build_spawn_plan(name: str, path: str, machine: str, session: str,
                     tasks: str, *, hub_root: str,
                     hubcli_bin: str | None = None, claude_bin: str | None = None,
                     seed_delay: int = 4) -> dict:
    """Build (don't run) the spawn plan: the exact argv steps to execute. Pure +
    side-effect-free, so it's unit-testable. Raises ValueError on bad input.

    Returns a dict with:
      immediate -> [argv, ...]   run right away (start claude + its bridge)
      delayed   -> [argv, ...]   run after seed_delay (type the bootstrap prompt)
      local     -> bool          target is this host (argv exec, no shell)
      plus name/session/bridge_session/target/machine/seed_delay.
    """
    if not (name or "").strip():
        raise ValueError("agent name is required")
    name = _safe_name(name)
    path = (path or "").strip()
    if not path:
        raise ValueError("creation path is required")
    if any(c in path for c in "\n\r\x00"):
        raise ValueError("invalid characters in path")
    machine = (machine or "").strip()
    if machine and not _MACHINE_RE.match(machine):
        raise ValueError("invalid machine/hostname")
    session = _safe_name(session or name)
    bridge_session = f"{session}-bridge"
    hubcli_bin = hubcli_bin or _hubcli_bin()
    claude_bin = claude_bin or _claude_bin()

    prompt = bootstrap_prompt(name, tasks, bridge_session)
    # tmux runs a session's command through a shell, so the bridge command is a
    # shell string — built only from server-controlled values + sanitised name/
    # session, and shlex-quoted regardless.
    bridge_cmd = (f"AGENT_HUB_ROOT={shlex.quote(hub_root)} {shlex.quote(hubcli_bin)} "
                  f"listen --name {shlex.quote(name)} --pane {shlex.quote(session)}")

    immediate = [
        ["tmux", "new-session", "-d", "-s", session, "-c", path, claude_bin],
        ["tmux", "new-session", "-d", "-s", bridge_session, bridge_cmd],
    ]
    delayed = [
        ["tmux", "send-keys", "-t", session, "-l", "--", prompt],
        ["tmux", "send-keys", "-t", session, "Enter"],
    ]
    local = _is_local(machine)
    return {
        "name": name,
        "session": session,
        "bridge_session": bridge_session,
        "machine": machine,
        "local": local,
        "target": "local" if local else f"ssh:{machine}",
        "immediate": immediate,
        "delayed": delayed,
        "seed_delay": int(seed_delay),
    }


def _remote_argv(machine: str, steps: list[list[str]], seed_delay: int) -> list[str]:
    """One ssh invocation that runs all steps on the remote host, shlex-safe."""
    imm = "; ".join(shlex.join(s) for s in steps[:2])
    seed = "; ".join(shlex.join(s) for s in steps[2:])
    script = f"{imm}; sleep {int(seed_delay)}; {seed}"
    return ["ssh", machine, f"bash -lc {shlex.quote(script)}"]


def run_spawn(name: str, path: str, machine: str, session: str, tasks: str, *,
              hub_root: str, **kw) -> dict:
    """Build the plan and launch it. LOCAL: run the immediate argv steps now (no
    shell), then type the bootstrap prompt from a background timer. REMOTE: one
    detached ssh runs the whole sequence. Raises ValueError on bad input."""
    plan = build_spawn_plan(name, path, machine, session, tasks,
                            hub_root=hub_root, **kw)
    print(f"[spawn] launching agent '{plan['name']}' on {plan['target']} "
          f"(session={plan['session']}, path={path})", flush=True)

    if plan["local"]:
        for argv in plan["immediate"]:
            subprocess.run(argv, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

        def _seed():
            time.sleep(plan["seed_delay"])
            for argv in plan["delayed"]:
                subprocess.run(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        threading.Thread(target=_seed, daemon=True).start()
    else:
        argv = _remote_argv(plan["machine"],
                            plan["immediate"] + plan["delayed"], plan["seed_delay"])
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        plan["launcher_pid"] = proc.pid
    return plan
