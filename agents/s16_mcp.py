"""
s16: MCP Integration -- External Tool Protocol

See: docs/zh/s16-mcp.md | docs/en/s16-mcp.md

Adds MCP (Model Context Protocol) support: external processes can expose
tools that register into the same registry as built-in tools. The agent
can't tell the difference.

Key additions over s14:
  - MCPServerConnection     -- manages connection to an MCP server (simulated)
  - discover_mcp_tools()    -- connect, list_tools, register into registry
  - make_mcp_handler()      -- sync handler that bridges to async MCP calls
  - SimulatedMCPServer      -- in-process mock for testing without real servers

Usage (CLI mode):
    export OPENAI_API_KEY=sk-xxx
    python agents/s16_mcp.py

Unit tests:
    python agents/s16_mcp.py --test
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

import yaml
from openai import OpenAI


# ===========================================================================
# Configuration system (new in this chapter)
# ===========================================================================
# 默认配置是唯一的 ground truth；用户的 config.yaml 只需提供 override。
# 启动时流程：load_env() → load_config() deep_merge → _expand_env_vars()

DEFAULT_CONFIG = {
    "model": "anthropic/claude-sonnet-4",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    "fallback": {
        "model": "",
        "base_url": "",
        "api_key": "",
    },
    "limits": {
        "max_iterations": 30,
        "max_child_iterations": 15,
        "max_retries": 3,
        "max_continuations": 3,
    },
    "compression": {
        "threshold": 50000,
        "protect_first": 3,
        "keep_recent_tool_results": 3,
        "tail_token_budget": 20000,
    },
    "memory": {
        "memory_char_limit": 2200,
        "user_char_limit": 1375,
    },
    "db_path": "state.db",
    # --- new in s12 ---
    "gateway": {
        "session_idle_timeout": 86400,        # 24h: auto-reset after silence
        "agent_name": "main",                 # used in session key prefix
        "platforms": {},                       # platform configs go here
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge two dicts. Override values take precedence."""
    # 关键：dict vs dict 时递归合并；非 dict 时直接覆盖
    # 这样用户只写 limits.max_iterations=50 就能覆盖，其它嵌套字段仍走默认
    result = base.copy()
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_env_vars(value):
    """Recursively resolve ${VAR} references in config values."""
    # 允许在 YAML 里写 api_key: ${OPENAI_API_KEY}，避免把 secrets 提交进配置
    # 变量不存在时保留原 ${VAR}，让调用方感知（而不是静默变成空串）
    if isinstance(value, str):
        def replacer(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))
        return re.sub(r'\$\{(\w+)\}', replacer, value)

    elif isinstance(value, dict):
        return {
            key: _expand_env_vars(val)
            for key, val in value.items()
        }

    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]

    return value


def load_env(env_path: Path | None = None):
    """Read a .env file and set as environment variables (simple implementation)."""
    # 只处理 KEY=VALUE 格式，够用且不依赖 python-dotenv
    # 用 setdefault：真实环境变量优先，.env 只做缺省值
    if env_path is None:
        env_path = HERMES_HOME / ".env"

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def load_config(config_path: Path | None = None) -> dict:
    """Load config.yaml, deep merge with defaults, expand env vars."""
    if config_path is None:
        config_path = HERMES_HOME / "config.yaml"

    # 文件不存在直接返回默认值（仍要跑 env 展开，默认值里可能也用了 ${VAR}）
    if not config_path.exists():
        return _expand_env_vars(DEFAULT_CONFIG.copy())

    try:
        raw_text = config_path.read_text(encoding="utf-8")
        user_config = yaml.safe_load(raw_text) or {}
    except Exception:
        # YAML 解析异常就退回默认值；不让坏配置阻塞启动
        user_config = {}

    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    return _expand_env_vars(merged)


def save_config(config: dict, config_path: Path | None = None):
    """Save config to config.yaml with 0600 file permissions."""
    if config_path is None:
        config_path = HERMES_HOME / "config.yaml"

    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(
        config,
        default_flow_style=False,
        allow_unicode=True,
    )
    config_path.write_text(text, encoding="utf-8")
    # 0600：配置里可能含 api_key，只允许本人读写
    config_path.chmod(0o600)


# ===========================================================================
# HERMES_HOME and Profile
# ===========================================================================

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))

# Load .env (may set OPENAI_API_KEY etc.)
load_env()

# Load and apply configuration
_config = load_config()

BASE_URL = os.getenv("OPENAI_BASE_URL") or _config["base_url"]
API_KEY = os.getenv("OPENAI_API_KEY") or _config["api_key"]
MODEL = os.getenv("MODEL") or _config["model"]
MAX_ITERATIONS = int(
    os.getenv("MAX_ITERATIONS") or _config["limits"]["max_iterations"]
)
DB_PATH = os.getenv("DB_PATH") or _config["db_path"]

FALLBACK_MODEL = (
    os.getenv("FALLBACK_MODEL") or _config["fallback"]["model"]
)
FALLBACK_BASE_URL = (
    os.getenv("FALLBACK_BASE_URL")
    or _config["fallback"]["base_url"]
    or BASE_URL
)
FALLBACK_API_KEY = (
    os.getenv("FALLBACK_API_KEY")
    or _config["fallback"]["api_key"]
    or API_KEY
)

COMPRESSION_THRESHOLD = _config["compression"]["threshold"]
PROTECT_FIRST = _config["compression"]["protect_first"]
KEEP_RECENT_TOOL_RESULTS = _config["compression"]["keep_recent_tool_results"]
TAIL_TOKEN_BUDGET = _config["compression"]["tail_token_budget"]
MAX_RETRIES = _config["limits"]["max_retries"]
MAX_CONTINUATIONS = _config["limits"]["max_continuations"]
MAX_CHILD_ITERATIONS = _config["limits"]["max_child_iterations"]
MEMORY_CHAR_LIMIT = _config["memory"]["memory_char_limit"]
USER_CHAR_LIMIT = _config["memory"]["user_char_limit"]

CONTINUE_MESSAGE = "Please continue from where you left off."

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)


# ===========================================================================
# Tool registry (reused from s02)
# ===========================================================================


@dataclass
class ToolEntry:
    """A registered tool with its metadata and handler."""
    name: str
    toolset: str
    schema: dict
    handler: Callable


class ToolRegistry:
    """Central registry for all agent tools."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
    ):
        """Register a tool by name with its schema and handler."""
        self._tools[name] = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
        )

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Look up a tool by name and execute its handler."""
        entry = self._tools.get(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        return entry.handler(args, **kwargs)

    def get_definitions(
        self,
        enabled_toolsets: list[str] | None = None,
    ) -> list[dict]:
        """Return OpenAI-format tool definitions filtered by toolset."""
        definitions = []
        for entry in self._tools.values():
            if enabled_toolsets and entry.toolset not in enabled_toolsets:
                continue
            definitions.append({
                "type": "function",
                "function": entry.schema,
            })
        return definitions


registry = ToolRegistry()


# ===========================================================================
# Dangerous command detection and approval (reused from s09)
# ===========================================================================

DANGEROUS_PATTERNS = [
    (
        r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|.*--no-preserve-root)",
        "Recursive/force delete",
    ),
    (r"rm\s+-[a-zA-Z]*r", "Recursive delete"),
    (r"mkfs\.", "Filesystem format"),
    (r"dd\s+if=", "Raw disk write"),
    (r">\s*/dev/sd[a-z]", "Direct device write"),
    (r"chmod\s+(-R\s+)?777", "World-writable permissions"),
    (r"chown\s+-R\s+", "Recursive ownership change"),
    (r"shutdown|reboot|poweroff|init\s+[06]", "System shutdown/reboot"),
    (r"kill\s+-9\s+(-1|1\b)", "Kill all processes"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;", "Fork bomb"),
    (r"DROP\s+(TABLE|DATABASE|INDEX)", "SQL destructive"),
    (r"TRUNCATE\s+TABLE", "SQL truncate"),
    (r"DELETE\s+FROM\s+\w+\s*;?\s*$", "SQL delete without WHERE"),
    (r"curl\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
    (r"wget\s+.*\|\s*(bash|sh|zsh)", "Pipe to shell"),
]

_compiled_patterns = [
    (re.compile(pattern, re.IGNORECASE), description)
    for pattern, description in DANGEROUS_PATTERNS
]
_session_approved: set[int] = set()
_ALLOWLIST_FILE = HERMES_HOME / "allowlist.json"


def _load_allowlist() -> set[str]:
    """Load the permanent allowlist."""
    if _ALLOWLIST_FILE.exists():
        try:
            return set(
                json.loads(_ALLOWLIST_FILE.read_text(encoding="utf-8"))
            )
        except Exception:
            pass
    return set()


def _save_allowlist(allowlist: set[str]):
    """Save the permanent allowlist to disk."""
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    _ALLOWLIST_FILE.write_text(
        json.dumps(sorted(allowlist)),
        encoding="utf-8",
    )


_permanent_allowlist: set[str] = _load_allowlist()


def detect_dangerous_command(
    command: str,
) -> list[tuple[int, str, str]]:
    """Detect if a command matches dangerous patterns."""
    matches = []
    for index, (regex, description) in enumerate(_compiled_patterns):
        if regex.search(command):
            matches.append((
                index,
                DANGEROUS_PATTERNS[index][0],
                description,
            ))
    return matches


def approve_command(
    command: str,
    matches: list[tuple[int, str, str]],
) -> bool:
    """Prompt the user to approve a dangerous command."""
    global _permanent_allowlist

    unapproved = [
        (index, pattern_str, description)
        for index, pattern_str, description in matches
        if index not in _session_approved
        and pattern_str not in _permanent_allowlist
    ]
    if not unapproved:
        return True

    print(f"\n  *** DANGEROUS COMMAND ***\n  Command: {command}")
    for _, _, description in unapproved:
        print(f"  - {description}")
    print("  [o]nce / [s]ession / [a]lways / [d]eny")

    choice = input("  Approve? ").strip().lower()

    if choice in ("o", "once"):
        return True

    if choice in ("s", "session"):
        for index, _, _ in unapproved:
            _session_approved.add(index)
        return True

    if choice in ("a", "always"):
        for _, pattern_str, _ in unapproved:
            _permanent_allowlist.add(pattern_str)
        _save_allowlist(_permanent_allowlist)
        for index, _, _ in unapproved:
            _session_approved.add(index)
        return True

    return False


# ===========================================================================
# Execution environment abstraction (new in s14)
# ===========================================================================
# BaseExecutionEnvironment 定义了两个抽象方法：_run_bash 和 cleanup。
# 命令包装、状态恢复、超时处理、CWD 追踪全在基类里。
# 子类只管"怎么启动 bash"和"怎么收拾"。

# Hermes 的 API key 不能泄露给 agent 执行的命令
_SECRET_BLOCKLIST = frozenset([
    "OPENAI_API_KEY", "ANTHROPIC_TOKEN", "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY", "GITHUB_TOKEN",
])


class BaseExecutionEnvironment(ABC):
    """
    The contract every terminal backend must fulfil.

    Subclasses implement _run_bash() and cleanup().
    Everything else -- command wrapping, snapshot restore, CWD tracking,
    timeout handling -- is shared.
    """

    def __init__(self, cwd: str, timeout: int = 180):
        self.cwd = cwd
        self.timeout = timeout
        self._session_id = uuid.uuid4().hex[:12]
        self._snapshot_path = f"/tmp/hermes-snap-{self._session_id}.sh"
        self._cwd_file = f"/tmp/hermes-cwd-{self._session_id}.txt"
        self._snapshot_ready = False

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        """Spawn a bash process to execute the wrapped command."""
        ...

    @abstractmethod
    def cleanup(self):
        """Release backend-specific resources."""
        ...

    def init_session(self):
        """Capture the current shell environment into a snapshot file."""
        # 首次使用时跑一次，把 login shell 的环境变量存下来
        init_cmd = (
            f"export -p > {self._snapshot_path} 2>/dev/null; "
            f"pwd -P > {self._cwd_file}"
        )
        proc = self._run_bash(init_cmd, timeout=10)
        proc.wait(timeout=10)
        self._snapshot_ready = True

    def execute(self, command: str, timeout: int | None = None) -> dict:
        """Wrap, run, wait, update CWD. Returns {"output": str, "returncode": int}."""
        if not self._snapshot_ready:
            self.init_session()

        timeout = timeout or self.timeout
        wrapped = self._wrap_command(command)
        proc = self._run_bash(wrapped, timeout=timeout)

        # 等输出
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return {"output": "(timed out)", "returncode": 124}

        # 更新 CWD
        self._update_cwd()

        output = stdout or ""
        return {"output": output[:10000], "returncode": proc.returncode or 0}

    def _wrap_command(self, command: str) -> str:
        """Wrap a bare command into: restore env → cd → run → save env → save CWD."""
        import shlex
        parts = []
        if self._snapshot_ready:
            parts.append(f"source {self._snapshot_path} 2>/dev/null")
        parts.append(f"cd {shlex.quote(self.cwd)} 2>/dev/null")
        parts.append(command)
        # 保存执行后的环境（给下一条命令用）
        parts.append(f"_exit=$?; export -p > {self._snapshot_path} 2>/dev/null; "
                      f"pwd -P > {self._cwd_file} 2>/dev/null; exit $_exit")
        return "; ".join(parts)

    def _update_cwd(self):
        """Read the CWD file to track directory changes."""
        try:
            new_cwd = Path(self._cwd_file).read_text().strip()
            if new_cwd:
                self.cwd = new_cwd
        except FileNotFoundError:
            pass


class LocalBackend(BaseExecutionEnvironment):
    """Execute commands directly on the host via subprocess."""

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        # 过滤掉 Hermes 的 API key
        env = {k: v for k, v in os.environ.items() if k not in _SECRET_BLOCKLIST}
        return subprocess.Popen(
            ["bash", "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

    def cleanup(self):
        for path in [self._snapshot_path, self._cwd_file]:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class DockerBackend(BaseExecutionEnvironment):
    """Execute commands inside a Docker container."""

    def __init__(self, image: str = "python:3.11-slim", **kwargs):
        super().__init__(**kwargs)
        self._image = image
        self._container_id: str | None = None

    def _ensure_container(self):
        """Start a long-lived container (once)."""
        if self._container_id:
            return
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", f"hermes-{self._session_id}",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--pids-limit", "256",
                "--cpus", "1",
                "--memory", "512m",
                "--tmpfs", "/tmp:rw,nosuid,size=256m",
                self._image,
                "sleep", "infinity",
            ],
            capture_output=True, text=True,
        )
        self._container_id = result.stdout.strip()
        if not self._container_id:
            raise RuntimeError(f"Docker start failed: {result.stderr}")

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        self._ensure_container()
        return subprocess.Popen(
            ["docker", "exec", "-i", self._container_id, "bash", "-c", cmd_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _update_cwd(self):
        """Docker: CWD file is inside the container; read via docker exec."""
        if not self._container_id:
            return
        result = subprocess.run(
            ["docker", "exec", self._container_id, "cat", self._cwd_file],
            capture_output=True, text=True,
        )
        new_cwd = result.stdout.strip()
        if new_cwd:
            self.cwd = new_cwd

    def cleanup(self):
        if self._container_id:
            subprocess.run(["docker", "rm", "-f", self._container_id],
                           capture_output=True)
            self._container_id = None


class SSHBackend(BaseExecutionEnvironment):
    """Execute commands on a remote machine via SSH with ControlMaster."""

    def __init__(self, host: str, user: str, key_path: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self._host = host
        self._user = user
        self._key_path = key_path
        ctrl_dir = Path("/tmp/hermes-ssh")
        ctrl_dir.mkdir(exist_ok=True)
        self._control_socket = str(ctrl_dir / f"{user}@{host}.sock")

    def _ssh_args(self) -> list[str]:
        args = [
            "ssh",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_socket}",
            "-o", "ControlPersist=300",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
        ]
        if self._key_path:
            args += ["-i", self._key_path]
        args.append(f"{self._user}@{self._host}")
        return args

    def _run_bash(self, cmd_string: str, *, timeout: int) -> subprocess.Popen:
        import shlex
        return subprocess.Popen(
            self._ssh_args() + ["bash", "-c", shlex.quote(cmd_string)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def _update_cwd(self):
        """SSH: read CWD file on remote via ssh cat."""
        result = subprocess.run(
            self._ssh_args() + ["cat", self._cwd_file],
            capture_output=True, text=True, timeout=5,
        )
        new_cwd = result.stdout.strip()
        if new_cwd:
            self.cwd = new_cwd

    def cleanup(self):
        subprocess.run(
            ["ssh", "-O", "exit",
             "-o", f"ControlPath={self._control_socket}",
             f"{self._user}@{self._host}"],
            capture_output=True,
        )


# --- Backend factory ---

def create_backend(config: dict) -> BaseExecutionEnvironment:
    """Create the right backend based on config."""
    backend_type = config.get("terminal", {}).get("backend", "local")
    terminal_cfg = config.get("terminal", {})

    if backend_type == "docker":
        image = terminal_cfg.get("docker_image", "python:3.11-slim")
        return DockerBackend(image=image, cwd="/workspace")
    elif backend_type == "ssh":
        return SSHBackend(
            host=terminal_cfg["ssh_host"],
            user=terminal_cfg["ssh_user"],
            key_path=terminal_cfg.get("ssh_key"),
            cwd="~",
        )
    else:
        return LocalBackend(cwd=os.getcwd())


# --- Global backend instance (lazy init) ---

_backend: BaseExecutionEnvironment | None = None


def get_backend() -> BaseExecutionEnvironment:
    """Get or create the global backend instance."""
    global _backend
    if _backend is None:
        _backend = create_backend(_config)
    return _backend


# ===========================================================================
# Tool implementations (terminal updated to use backend)
# ===========================================================================


def run_terminal(args, **kwargs):
    """Terminal tool handler: approval check → backend.execute()."""
    command = args.get("command", "")

    matches = detect_dangerous_command(command)
    if matches and not approve_command(command, matches):
        return json.dumps({"error": "Command denied by user."})

    backend = get_backend()
    result = backend.execute(command)

    output = result["output"]
    if result["returncode"] != 0:
        output += f"\n(exit code: {result['returncode']})"

    return output if output.strip() else "(no output)"


registry.register(
    name="terminal",
    toolset="terminal",
    schema={
        "name": "terminal",
        "description": "Run a shell command.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    handler=run_terminal,
)


def handle_read_file(args, **kwargs):
    """Read a file and return its contents."""
    try:
        with open(args["path"], "r", encoding="utf-8") as file_handle:
            return file_handle.read(100_000) or "(empty)"
    except Exception as exc:
        return f"(error: {exc})"


registry.register(
    name="read_file",
    toolset="file",
    schema={
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    handler=handle_read_file,
)


def handle_write_file(args, **kwargs):
    """Write content to a file, creating directories as needed."""
    try:
        os.makedirs(os.path.dirname(args["path"]) or ".", exist_ok=True)
        with open(args["path"], "w", encoding="utf-8") as file_handle:
            file_handle.write(args["content"])
        return f"Written {len(args['content'])} chars"
    except Exception as exc:
        return f"(error: {exc})"


registry.register(
    name="write_file",
    toolset="file",
    schema={
        "name": "write_file",
        "description": "Write content to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    handler=handle_write_file,
)


# ===========================================================================
# SQLite persistence (reused from s03, simplified)
# ===========================================================================


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize the SQLite database with WAL mode and required tables."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            started_at REAL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            timestamp REAL
        );
    """)
    return conn


def create_session(conn: sqlite3.Connection) -> str:
    """Create a new session and return its ID."""
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?)",
        (session_id, "cli", time.time()),
    )
    conn.commit()
    return session_id


def add_message(
    conn: sqlite3.Connection,
    session_id: str,
    msg: dict,
):
    """Persist a message to the database."""
    tool_calls_json = None
    if msg.get("tool_calls"):
        tool_calls_json = json.dumps(msg["tool_calls"])

    conn.execute(
        """
        INSERT INTO messages
            (session_id, role, content, tool_calls, tool_call_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            msg["role"],
            msg.get("content", ""),
            tool_calls_json,
            msg.get("tool_call_id"),
            time.time(),
        ),
    )
    conn.commit()


def get_session_messages(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict]:
    """Load all messages for a session from the database."""
    rows = conn.execute(
        """
        SELECT role, content, tool_calls, tool_call_id
        FROM messages
        WHERE session_id = ?
        ORDER BY id
        """,
        (session_id,),
    ).fetchall()

    messages = []
    for role, content, tool_calls_json, tool_call_id in rows:
        msg: dict = {"role": role, "content": content or ""}
        if tool_calls_json:
            msg["tool_calls"] = json.loads(tool_calls_json)
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        messages.append(msg)
    return messages


# ===========================================================================
# Compression + error recovery (reused from s05/s06)
# ===========================================================================


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: character count / 4."""
    return sum(len(str(msg.get("content", ""))) for msg in messages) // 4


def compress(messages: list[dict]) -> list[dict]:
    """Perform one round of context compression."""
    tool_indices = [
        index for index, msg in enumerate(messages)
        if msg.get("role") == "tool"
    ]
    for index in tool_indices[:-KEEP_RECENT_TOOL_RESULTS]:
        messages[index] = {
            **messages[index],
            "content": "[Old tool output cleared]",
        }

    head_end = PROTECT_FIRST
    tail_start = len(messages)
    tail_tokens = 0

    for index in range(len(messages) - 1, head_end - 1, -1):
        msg_tokens = len(str(messages[index].get("content", ""))) // 4
        if tail_tokens + msg_tokens > TAIL_TOKEN_BUDGET:
            break
        tail_tokens += msg_tokens
        tail_start = index

    if tail_start <= head_end:
        return messages

    middle = messages[head_end:tail_start]
    summary_parts = [
        f"[{msg['role']}] {str(msg.get('content', ''))[:500]}"
        for msg in middle
    ]
    prompt = "Summarize concisely:\n" + "\n".join(summary_parts)

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        summary = response.choices[0].message.content or "(summary failed)"
    except Exception as exc:
        summary = f"(summary error: {exc})"

    print(f"  [compress] {len(middle)} messages -> summary")

    return (
        messages[:head_end]
        + [{"role": "user", "content": f"[CONTEXT COMPACTION]\n{summary}"}]
        + messages[tail_start:]
    )


def classify_error(
    status_code: int | None,
    error_message: str,
) -> dict:
    """Classify an API error into an actionable decision."""
    if status_code == 429:
        return {
            "reason": "rate_limit",
            "retryable": True,
            "should_compress": False,
            "should_fallback": False,
        }
    if status_code == 400 and "context" in error_message.lower():
        return {
            "reason": "context_overflow",
            "retryable": True,
            "should_compress": True,
            "should_fallback": False,
        }
    if status_code in (500, 502, 503):
        return {
            "reason": "server_error",
            "retryable": True,
            "should_compress": False,
            "should_fallback": False,
        }
    if status_code in (401, 403):
        return {
            "reason": "auth",
            "retryable": False,
            "should_compress": False,
            "should_fallback": True,
        }
    if status_code == 404:
        return {
            "reason": "model_not_found",
            "retryable": False,
            "should_compress": False,
            "should_fallback": True,
        }
    return {
        "reason": "unknown",
        "retryable": False,
        "should_compress": False,
        "should_fallback": False,
    }


def jittered_backoff(
    attempt: int,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
) -> float:
    """Calculate exponential backoff with random jitter."""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    return delay + random.uniform(0, delay * 0.5)


def switch_to_fallback():
    """Switch to the fallback model. Returns (client, model) or (None, None)."""
    if not FALLBACK_MODEL:
        return None, None
    print(f"  [fallback] -> {FALLBACK_MODEL}")
    fallback_client = OpenAI(
        base_url=FALLBACK_BASE_URL,
        api_key=FALLBACK_API_KEY,
    )
    return fallback_client, FALLBACK_MODEL


# ===========================================================================
# Memory system (reused from s07, simplified)
# ===========================================================================

MEMORY_DIR = HERMES_HOME / "memories"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"
ENTRY_SEP = "\n\n\u00a7\n\n"


def parse_entries(text: str) -> list[str]:
    """Split section-mark-delimited text into a list of entries."""
    if not text.strip():
        return []
    return [entry.strip() for entry in text.split("\u00a7") if entry.strip()]


def render_entries(entries: list[str]) -> str:
    """Join entries back into section-mark-delimited text."""
    return ENTRY_SEP.join(entries)


def load_memory(file_path: Path) -> list[str]:
    """Load memory entries from a file."""
    if not file_path.exists():
        return []
    return parse_entries(file_path.read_text(encoding="utf-8"))


def save_memory(
    file_path: Path,
    entries: list[str],
    char_limit: int,
) -> str:
    """Save memory entries to a file, trimming if over the char limit."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    text = render_entries(entries)
    warning = ""

    if len(text) > char_limit:
        while entries and len(render_entries(entries)) > char_limit:
            entries.pop()
        text = render_entries(entries)
        warning = f"Trimmed to {len(entries)} entries."

    file_path.write_text(text, encoding="utf-8")
    return warning


def handle_memory(args, **kwargs):
    """Handle the memory tool: add / remove / read operations."""
    action = args.get("action", "")
    target = args.get("target", "memory")
    content = args.get("content", "")

    file_path = USER_FILE if target == "user" else MEMORY_FILE
    char_limit = USER_CHAR_LIMIT if target == "user" else MEMORY_CHAR_LIMIT

    if action == "read":
        entries = load_memory(file_path)
        if not entries:
            return f"({target} is empty)"
        return (
            f"=== {target.upper()} ({len(entries)} entries) ===\n"
            + render_entries(entries)
        )

    elif action == "add":
        if not content:
            return "(error: content required)"
        entries = load_memory(file_path)
        entries.append(content)
        warning = save_memory(file_path, entries, char_limit)
        message = f"Added to {target}. Total: {len(entries)}."
        if warning:
            message += f" {warning}"
        return message

    elif action == "remove":
        if not content:
            return "(error: keyword required)"
        entries = load_memory(file_path)
        before_count = len(entries)
        entries = [
            entry for entry in entries
            if content.lower() not in entry.lower()
        ]
        if before_count == len(entries):
            return f"No match for '{content}'."
        save_memory(file_path, entries, char_limit)
        removed_count = before_count - len(entries)
        return (
            f"Removed {removed_count}. Remaining: {len(entries)}."
        )

    return f"(error: unknown action '{action}')"


registry.register(
    name="memory",
    toolset="memory",
    schema={
        "name": "memory",
        "description": (
            "Manage persistent memory. Actions: add/remove/read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "remove", "read"],
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                },
                "content": {
                    "type": "string",
                },
            },
            "required": ["action"],
        },
    },
    handler=handle_memory,
)


# ===========================================================================
# Skill system (reused from s08, simplified)
# ===========================================================================

SKILLS_DIR = HERMES_HOME / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse SKILL.md frontmatter and body."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    metadata = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()

    return metadata, parts[2].strip()


def _render_skill(name: str, description: str, body: str) -> str:
    """Render frontmatter + body into SKILL.md content."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def discover_skills() -> list[dict]:
    """Scan the skills directory and return a list of skill summaries."""
    if not SKILLS_DIR.exists():
        return []

    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        metadata, _ = _parse_frontmatter(
            skill_file.read_text(encoding="utf-8")
        )
        skills.append({
            "name": metadata.get("name", skill_dir.name),
            "description": metadata.get("description", ""),
        })

    return skills


def handle_skill_view(args, **kwargs):
    """Load and return the full content of a skill by name."""
    skill_file = SKILLS_DIR / args.get("name", "") / "SKILL.md"
    if not skill_file.exists():
        return "(error: skill not found)"

    metadata, body = _parse_frontmatter(
        skill_file.read_text(encoding="utf-8")
    )
    return (
        f"=== Skill: {metadata.get('name', '')} ===\n"
        f"{metadata.get('description', '')}\n\n{body}"
    )


def handle_skill_manage(args, **kwargs):
    """Manage skills: create / edit / delete."""
    action = args.get("action", "")
    name = args.get("name", "")

    if not name:
        return "(error: name required)"

    skill_dir = SKILLS_DIR / name
    skill_file = skill_dir / "SKILL.md"

    if action == "create":
        if skill_file.exists():
            return "(error: exists)"
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = _render_skill(
            name,
            args.get("description", ""),
            args.get("body", ""),
        )
        skill_file.write_text(content, encoding="utf-8")
        return f"Created '{name}'."

    elif action == "edit":
        if not skill_file.exists():
            return "(error: not found)"
        metadata, old_body = _parse_frontmatter(
            skill_file.read_text(encoding="utf-8")
        )
        new_description = (
            args.get("description")
            or metadata.get("description", "")
        )
        new_body = args.get("body") or old_body
        content = _render_skill(name, new_description, new_body)
        skill_file.write_text(content, encoding="utf-8")
        return f"Updated '{name}'."

    elif action == "delete":
        if not skill_file.exists():
            return "(error: not found)"
        shutil.rmtree(skill_dir)
        return f"Deleted '{name}'."

    return "(error: unknown action)"


registry.register(
    name="skill_view",
    toolset="skill",
    schema={
        "name": "skill_view",
        "description": "Load full skill content.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    handler=handle_skill_view,
)

registry.register(
    name="skill_manage",
    toolset="skill",
    schema={
        "name": "skill_manage",
        "description": "Manage skills: create/edit/delete.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "delete"],
                },
                "name": {"type": "string"},
                "description": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["action", "name"],
        },
    },
    handler=handle_skill_manage,
)


# ===========================================================================
# Subagent delegation (reused from s10)
# ===========================================================================

DELEGATE_BLOCKED_TOOLS = {"delegate_task", "memory", "skill_manage"}


def build_child_agent(
    goal: str,
    context: str,
    toolsets: list[str],
) -> dict:
    """Build a child agent environment: isolated messages, restricted tools."""
    child_tools = [
        tool_def
        for tool_def in registry.get_definitions(toolsets)
        if tool_def["function"]["name"] not in DELEGATE_BLOCKED_TOOLS
    ]

    child_prompt = (
        "You are a focused sub-agent. "
        "Complete the task and report results.\n"
        f"# Task\n{goal}\n\n"
    )
    if context:
        child_prompt += f"# Context\n{context}\n\n"

    child_prompt += (
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {os.getcwd()}"
    )

    return {
        "system_prompt": child_prompt,
        "messages": [{"role": "user", "content": goal}],
        "tools": child_tools,
    }


def run_child_conversation(child_env: dict) -> str:
    """Run the child agent's conversation loop and return the final response."""
    messages = child_env["messages"]
    tools = child_env["tools"]
    system_prompt = child_env["system_prompt"]

    for iteration in range(MAX_CHILD_ITERATIONS):
        api_messages = (
            [{"role": "system", "content": system_prompt}] + messages
        )

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=api_messages,
                tools=tools if tools else None,
            )
        except Exception as exc:
            return f"(child error: {exc})"

        assistant_msg = response.choices[0].message

        msg_dict: dict = {
            "role": "assistant",
            "content": assistant_msg.content or "",
        }
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_msg.tool_calls
            ]
        messages.append(msg_dict)

        if not assistant_msg.tool_calls:
            return assistant_msg.content or "(empty)"

        for tool_call in assistant_msg.tool_calls:
            tool_name = tool_call.function.name

            if tool_name in DELEGATE_BLOCKED_TOOLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": (
                        f"(error: '{tool_name}' blocked for sub-agents)"
                    ),
                })
            else:
                tool_args = json.loads(tool_call.function.arguments)
                print(
                    f"    [child-tool] {tool_name}: "
                    f"{json.dumps(tool_args, ensure_ascii=False)[:100]}"
                )
                output = registry.dispatch(tool_name, tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": output,
                })

    return "(child: max iterations)"


def handle_delegate(args, **kwargs):
    """Handle the delegate_task tool: spawn a child agent."""
    goal = args.get("goal", "")
    if not goal:
        return "(error: goal required)"

    print(f"  [delegate] child: {goal[:80]}")

    child_env = build_child_agent(
        goal,
        args.get("context", ""),
        args.get("toolsets", ["terminal", "file"]),
    )
    result = run_child_conversation(child_env)

    print(f"  [delegate] done ({len(result)} chars)")
    return result


registry.register(
    name="delegate_task",
    toolset="delegate",
    schema={
        "name": "delegate_task",
        "description": (
            "Delegate a task to a sub-agent with isolated context. "
            "Returns final result text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Task description",
                },
                "context": {
                    "type": "string",
                    "description": "Relevant context",
                },
                "toolsets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Available tool sets",
                },
            },
            "required": ["goal"],
        },
    },
    handler=handle_delegate,
)


# ===========================================================================
# System prompt
# ===========================================================================


def find_project_context(cwd: str) -> str:
    """Find and load the project configuration file by priority."""
    for name in [".hermes.md", "HERMES.md"]:
        path = Path(cwd) / name
        if path.exists():
            return path.read_text(encoding="utf-8")[:20000]

    for name in ["AGENTS.md", "CLAUDE.md", ".cursorrules"]:
        path = Path(cwd) / name
        if path.exists():
            return path.read_text(encoding="utf-8")[:20000]

    return ""


def build_system_prompt(cwd: str) -> str:
    """Assemble the full system prompt."""
    parts = []

    soul_path = HERMES_HOME / "SOUL.md"
    if soul_path.exists():
        parts.append(soul_path.read_text(encoding="utf-8")[:20000])
    else:
        parts.append("You are a helpful assistant.")

    memory_entries = load_memory(MEMORY_FILE)
    if memory_entries:
        parts.append("# Memory\n" + render_entries(memory_entries))

    user_entries = load_memory(USER_FILE)
    if user_entries:
        parts.append("# User Profile\n" + render_entries(user_entries))

    skills = discover_skills()
    if skills:
        lines = [
            f"- **{skill['name']}**: {skill['description']}"
            for skill in skills
        ]
        parts.append("# Available Skills\n" + "\n".join(lines))

    parts.append(
        "# Permissions\n"
        "Dangerous commands require user approval."
    )

    project = find_project_context(cwd)
    if project:
        parts.append(f"# Project Context\n{project}")

    parts.append(
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"Working directory: {cwd}"
    )

    return "\n\n".join(parts)


# ===========================================================================
# Core conversation loop
# ===========================================================================

ENABLED_TOOLSETS = ["terminal", "file", "memory", "skill", "delegate"]


def run_conversation(
    user_message: str,
    conn: sqlite3.Connection,
    session_id: str,
    cached_prompt: str,
) -> dict:
    """Run a conversation loop with all features enabled."""
    messages = get_session_messages(conn, session_id)
    user_msg = {"role": "user", "content": user_message}
    messages.append(user_msg)
    add_message(conn, session_id, user_msg)

    tools = registry.get_definitions(ENABLED_TOOLSETS)
    active_client = client
    active_model = MODEL
    retry_count = 0
    continuation_count = 0

    for iteration in range(MAX_ITERATIONS):
        if estimate_tokens(messages) >= COMPRESSION_THRESHOLD:
            messages = compress(messages)

        api_messages = (
            [{"role": "system", "content": cached_prompt}] + messages
        )

        try:
            response = active_client.chat.completions.create(
                model=active_model,
                messages=api_messages,
                tools=tools,
            )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            classified = classify_error(status, str(exc))
            print(f"  [error] {classified['reason']} (status={status})")

            if classified["should_compress"]:
                messages = compress(messages)
                continue

            if classified["should_fallback"]:
                fallback_client, fallback_model = switch_to_fallback()
                if fallback_client:
                    active_client = fallback_client
                    active_model = fallback_model
                    continue
                raise

            if classified["retryable"] and retry_count < MAX_RETRIES:
                retry_count += 1
                time.sleep(jittered_backoff(retry_count))
                continue

            raise

        retry_count = 0
        assistant_msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        msg_dict: dict = {
            "role": "assistant",
            "content": assistant_msg.content or "",
        }
        if assistant_msg.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in assistant_msg.tool_calls
            ]
        messages.append(msg_dict)
        add_message(conn, session_id, msg_dict)

        if finish_reason == "length" and continuation_count < MAX_CONTINUATIONS:
            continuation_count += 1
            cont_msg = {"role": "user", "content": CONTINUE_MESSAGE}
            messages.append(cont_msg)
            add_message(conn, session_id, cont_msg)
            continue

        if not assistant_msg.tool_calls:
            return {
                "final_response": assistant_msg.content,
                "messages": messages,
            }

        continuation_count = 0
        for tool_call in assistant_msg.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)
            print(
                f"  [tool] {tool_name}: "
                f"{json.dumps(tool_args, ensure_ascii=False)[:120]}"
            )
            output = registry.dispatch(tool_name, tool_args)
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            }
            messages.append(tool_msg)
            add_message(conn, session_id, tool_msg)

    return {
        "final_response": "(max iterations reached)",
        "messages": messages,
    }


# ===========================================================================
# Gateway: unified message format (new in s12)
# ===========================================================================
# 所有平台来的消息都翻译成 MessageEvent，下游代码只看这个结构。
# SessionSource 标识"这条消息从哪来"，用来生成 session key。


class MessageType(Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    DOCUMENT = "document"


@dataclass
class SessionSource:
    """Where the message came from."""
    platform: str    # "console", "telegram", "wecom", ...
    chat_id: str     # unique chat identifier
    chat_type: str   # "dm" or "group"
    user_id: str     # who sent this message
    user_name: str = ""


@dataclass
class MessageEvent:
    """A platform-agnostic inbound message."""
    message_id: str
    text: str
    source: SessionSource
    message_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)


def build_session_key(source: SessionSource, agent_name: str = "main") -> str:
    """
    Build the session key that uniquely identifies a conversation.

    格式: agent:{name}:{platform}:{chat_type}:{chat_id}[:user_id]
    群聊按 user_id 隔离 → 同一群里的张三和李四各自有独立对话。
    """
    parts = [f"agent:{agent_name}:{source.platform}:{source.chat_type}:{source.chat_id}"]
    if source.chat_type == "group":
        parts.append(source.user_id)
    return ":".join(parts)


# ===========================================================================
# Gateway: platform adapter base class (new in s12)
# ===========================================================================
# 每个平台适配器继承 BasePlatformAdapter，实现 connect/disconnect/send。
# _on_message 回调由 GatewayRunner 在启动时注入。


class BasePlatformAdapter(ABC):
    """
    The contract every platform adapter must fulfil.

    Subclasses implement:
      - connect()      开始接收消息
      - disconnect()   停止
      - send()         把回复发回平台
    """

    def __init__(self, platform_name: str):
        self.platform_name = platform_name
        self._on_message: Callable | None = None  # injected by GatewayRunner
        self._running = False

    @abstractmethod
    async def connect(self) -> bool:
        """Start receiving messages. Return True if successful."""
        ...

    @abstractmethod
    async def disconnect(self):
        """Stop receiving messages and clean up."""
        ...

    @abstractmethod
    async def send(self, chat_id: str, content: str) -> bool:
        """Send a reply to the given chat. Return True if successful."""
        ...

    async def handle_message(self, event: MessageEvent):
        """Forward a translated event to the GatewayRunner callback."""
        if self._on_message:
            await self._on_message(event)


# ===========================================================================
# Gateway: console adapter (new in s12) -- a minimal adapter for testing
# ===========================================================================
# 不连任何外部平台，直接从终端读输入。用来验证 Gateway 流程。


class ConsoleAdapter(BasePlatformAdapter):
    """
    A trivial adapter that reads from stdin.

    This lets you test the full Gateway pipeline without any external platform.
    Every line you type becomes a MessageEvent from user 'console_user'.
    """

    def __init__(self):
        super().__init__("console")
        self._task: asyncio.Task | None = None

    async def connect(self) -> bool:
        self._running = True
        self._task = asyncio.create_task(self._read_loop())
        return True

    async def disconnect(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def send(self, chat_id: str, content: str) -> bool:
        # 直接打印到终端
        print(f"\n[{self.platform_name}] Assistant: {content}\n")
        return True

    async def _read_loop(self):
        """Read lines from stdin in a thread, post as MessageEvents."""
        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(
                    None, lambda: input("[console] You: ")
                )
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                self._running = False
                break

            event = MessageEvent(
                message_id=str(uuid.uuid4())[:8],
                text=line,
                source=SessionSource(
                    platform="console",
                    chat_id="console_user",
                    chat_type="dm",
                    user_id="console_user",
                    user_name="Console User",
                ),
            )
            await self.handle_message(event)


# ===========================================================================
# Platform utilities (new in s13)
# ===========================================================================
# 三个共性工具：UTF-16 计数、消息去重、消息分片合并。
# 每个真实适配器都会用到其中至少两个。


def utf16_len(text: str) -> int:
    """
    Count UTF-16 code units (what Telegram uses for length limits).

    大部分字符 = 1 unit，但很多 emoji = 2 units (surrogate pair)。
    Python 的 len() 返回 code points，不是 code units。
    """
    return len(text.encode("utf-16-le")) // 2


def truncate_utf16(text: str, max_units: int) -> str:
    """Truncate text to fit within max_units UTF-16 code units."""
    if utf16_len(text) <= max_units:
        return text
    # 逐字符累加，直到超限
    result = []
    total = 0
    for ch in text:
        ch_units = len(ch.encode("utf-16-le")) // 2
        if total + ch_units > max_units:
            break
        result.append(ch)
        total += ch_units
    return "".join(result)


class MessageDeduplicator:
    """
    Prevents processing the same message twice.

    Discord RESUME 和网络抖动都可能重推消息。
    用 message_id 做去重，FIFO 淘汰旧记录。
    """

    def __init__(self, max_size: int = 1000):
        self._seen: set[str] = set()
        self._order: list[str] = []
        self._max_size = max_size

    def is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen.add(message_id)
        self._order.append(message_id)
        if len(self._order) > self._max_size:
            old_id = self._order.pop(0)
            self._seen.discard(old_id)
        return False


class TextBatcher:
    """
    Merges rapid text chunks from the same session into one message.

    当平台客户端拆分长文本时，多条消息在毫秒级内到达。
    TextBatcher 缓冲文本片段，等安静期过后合并成一条 MessageEvent。
    """

    def __init__(self, callback: Callable):
        self._callback = callback  # async def callback(event: MessageEvent)
        self._buffers: dict[str, list[str]] = {}
        self._events: dict[str, MessageEvent] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def enqueue(
        self,
        session_key: str,
        text: str,
        event: MessageEvent,
        split_threshold: int = 3900,
    ):
        """Buffer a text chunk. Flush after quiet period."""
        if session_key not in self._buffers:
            self._buffers[session_key] = []
        self._buffers[session_key].append(text)
        self._events[session_key] = event  # keep latest event metadata

        # 取消之前的刷新任务
        old_task = self._tasks.get(session_key)
        if old_task and not old_task.done():
            old_task.cancel()

        # 接近截断阈值 → 等更久（后面几乎肯定还有）
        delay = 2.0 if len(text) >= split_threshold else 0.6

        self._tasks[session_key] = asyncio.create_task(
            self._flush_after(session_key, delay)
        )

    async def _flush_after(self, session_key: str, delay: float):
        await asyncio.sleep(delay)

        chunks = self._buffers.pop(session_key, [])
        event = self._events.pop(session_key, None)
        self._tasks.pop(session_key, None)

        if chunks and event:
            event.text = "".join(chunks)
            await self._callback(event)


# ===========================================================================
# Media cache (new in s13)
# ===========================================================================
# 平台媒体 URL 通常是临时的。适配器收到媒体消息时立刻下载到本地。

MEDIA_CACHE_DIR = HERMES_HOME / "cache"


def cache_image(data: bytes, filename: str) -> str:
    """Save image bytes to local cache, return the local path."""
    img_dir = MEDIA_CACHE_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / filename
    path.write_bytes(data)
    return str(path)


def cache_audio(data: bytes, filename: str) -> str:
    """Save audio bytes to local cache, return the local path."""
    audio_dir = MEDIA_CACHE_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / filename
    path.write_bytes(data)
    return str(path)


# ===========================================================================
# SimulatedAdapter (new in s13) -- replays scripted messages for testing
# ===========================================================================
# 用预定义的消息列表模拟一个平台。用来验证消息合并、去重等逻辑。


class SimulatedAdapter(BasePlatformAdapter):
    """
    Replays a list of scripted messages, then stops.

    Useful for testing text batching, deduplication, and the full
    Gateway pipeline without any real platform connection.
    """

    def __init__(self, messages: list[dict] | None = None):
        super().__init__("simulated")
        self._dedup = MessageDeduplicator()
        self._batcher: TextBatcher | None = None
        self._replies: list[tuple[str, str]] = []  # (chat_id, content) log

        # 默认脚本：演示分片合并
        self._script = messages or [
            # 正常消息
            {"text": "你好，帮我查个东西", "user": "alice", "delay": 0},
            # 模拟分片：两条消息间隔 0.1 秒，第一条接近 4000 字符
            {"text": "这是一段很长的文本" + "。" * 500, "user": "bob", "delay": 1.0},
            {"text": "这是被拆开的第二部分", "user": "bob", "delay": 0.1},
            # 重复消息（同一个 message_id）
            {"text": "你好", "user": "alice", "delay": 1.0, "msg_id": "dup_001"},
            {"text": "你好", "user": "alice", "delay": 0.05, "msg_id": "dup_001"},
        ]

    async def connect(self) -> bool:
        self._running = True
        self._batcher = TextBatcher(callback=self.handle_message)
        asyncio.create_task(self._replay_script())
        return True

    async def disconnect(self):
        self._running = False

    async def send(self, chat_id: str, content: str) -> bool:
        self._replies.append((chat_id, content))
        print(f"\n[simulated] Reply to {chat_id}: {content[:120]}...\n")
        return True

    async def _replay_script(self):
        """Play scripted messages with specified delays."""
        for i, msg in enumerate(self._script):
            if not self._running:
                break
            await asyncio.sleep(msg.get("delay", 0.5))

            msg_id = msg.get("msg_id", f"sim_{i}")
            if self._dedup.is_duplicate(msg_id):
                print(f"  [simulated] dedup: skipped {msg_id}")
                continue

            event = MessageEvent(
                message_id=msg_id,
                text=msg["text"],
                source=SessionSource(
                    platform="simulated",
                    chat_id=msg.get("user", "user1"),
                    chat_type="dm",
                    user_id=msg.get("user", "user1"),
                    user_name=msg.get("user", "user1"),
                ),
            )

            session_key = build_session_key(event.source)
            # 所有文本消息都过 batcher
            await self._batcher.enqueue(session_key, event.text, event)

        # 等 batcher 的最后一次刷新完成
        await asyncio.sleep(3.0)
        self._running = False


# ===========================================================================
# Gateway: GatewayRunner (reused from s12)
# ===========================================================================
# 三件事：启动适配器、路由消息、管理活跃会话。
# _handle_message 是所有适配器的汇聚点，不关心消息从哪个平台来。


class GatewayRunner:
    """
    Starts adapters, routes inbound messages to the right session,
    calls the core loop, and sends replies back.
    """

    def __init__(self, config: dict, db_path: str):
        self.config = config
        self.db_path = db_path
        self.adapters: dict[str, BasePlatformAdapter] = {}
        self.agent_name = config.get("gateway", {}).get("agent_name", "main")

        # session key → agent 运行状态
        self._active_sessions: dict[str, asyncio.Event] = {}
        self._pending_messages: dict[str, MessageEvent] = {}
        # session key → cached system prompt
        self._prompts: dict[str, str] = {}

    def add_adapter(self, adapter: BasePlatformAdapter):
        """Register an adapter (call before start)."""
        adapter._on_message = self._handle_message
        self.adapters[adapter.platform_name] = adapter

    async def start(self):
        """Connect all registered adapters."""
        for name, adapter in self.adapters.items():
            ok = await adapter.connect()
            if ok:
                print(f"  [gateway] {name} connected")
            else:
                print(f"  [gateway] {name} FAILED to connect")

    async def stop(self):
        """Disconnect all adapters."""
        for adapter in self.adapters.values():
            await adapter.disconnect()

    # ----- the core routing function -----

    async def _handle_message(self, event: MessageEvent):
        """
        All platforms converge here. This function:
        1. Builds a session key
        2. If a session is already active → interrupt it, queue the new message
        3. Otherwise → process the message in background
        """
        session_key = build_session_key(event.source, self.agent_name)

        if session_key in self._active_sessions:
            # 正在处理中 → 暂存新消息（只保留最后一条），发中断信号
            self._pending_messages[session_key] = event
            self._active_sessions[session_key].set()  # interrupt signal
            print(f"  [gateway] {session_key}: queued (agent busy)")
            return

        # 没有活跃 agent → 启动后台处理
        self._active_sessions[session_key] = asyncio.Event()
        asyncio.create_task(self._process_in_background(event, session_key))

    async def _process_in_background(
        self, event: MessageEvent, session_key: str
    ):
        """Process one message, then check for pending follow-ups."""
        try:
            response = await self._run_agent(event, session_key)

            # 发回复
            adapter = self.adapters.get(event.source.platform)
            if adapter and response:
                await adapter.send(event.source.chat_id, response)

        except Exception as exc:
            print(f"  [gateway] error: {exc}")

        # 检查是否有暂存的消息
        if session_key in self._pending_messages:
            next_event = self._pending_messages.pop(session_key)
            # 重置中断信号，继续处理下一条
            self._active_sessions[session_key] = asyncio.Event()
            await self._process_in_background(next_event, session_key)
        else:
            # 没有了，清除活跃标记
            del self._active_sessions[session_key]

    async def _run_agent(
        self, event: MessageEvent, session_key: str
    ) -> str | None:
        """
        Run the core conversation loop for one message.

        这里复用 s01-s11 的 run_conversation()。
        Gateway 不修改核心循环，只是换了一个"消息从哪来"。
        """
        # 用 session_key 作为 SQLite session_id
        conn = init_db(self.db_path)
        try:
            # 确保 session 存在
            existing = get_session_messages(conn, session_key)
            if not existing:
                # 首次对话，创建 session（用 session_key 作为 id）
                conn.execute(
                    "INSERT OR IGNORE INTO sessions (id, created_at) VALUES (?, ?)",
                    (session_key, datetime.now().isoformat()),
                )
                conn.commit()

            # 组装 system prompt（按 session 缓存）
            if session_key not in self._prompts:
                self._prompts[session_key] = build_system_prompt(os.getcwd())

            result = run_conversation(
                event.text, conn, session_key, self._prompts[session_key]
            )
            return result.get("final_response")
        finally:
            conn.close()


# ===========================================================================
# MCP Integration (new in s16)
# ===========================================================================
# MCP 工具通过外部进程提供。Hermes 启动外部进程 → 问它有哪些工具 →
# 注册进 registry → agent 调用时，handler 转发给外部进程。
#
# 这里用 SimulatedMCPServer 模拟外部进程，演示完整的注册和调用流程。
# 真实场景下，MCPServerConnection 会通过 stdio 或 HTTP 和外部进程通信。


class SimulatedMCPServer:
    """
    A mock MCP server that lives in-process.

    Simulates what a real MCP server does: advertise tools and handle calls.
    Used for testing the MCP registration and dispatch flow without
    needing npx, Node.js, or any external dependencies.
    """

    def __init__(self, name: str, tools: dict[str, Callable]):
        self.name = name
        self._tools = tools  # tool_name → handler function

    def list_tools(self) -> list[dict]:
        """MCP list_tools: return schema for each tool."""
        return [
            {
                "name": name,
                "description": f"Simulated tool: {name}",
                "inputSchema": {
                    "type": "object",
                    "properties": {"input": {"type": "string"}},
                },
            }
            for name in self._tools
        ]

    def call_tool(self, name: str, arguments: dict) -> dict:
        """MCP call_tool: dispatch to the tool handler."""
        handler = self._tools.get(name)
        if not handler:
            return {"isError": True, "content": f"Unknown tool: {name}"}
        try:
            result = handler(arguments)
            return {"isError": False, "content": result}
        except Exception as exc:
            return {"isError": True, "content": str(exc)}


# --- Global MCP server registry ---
_mcp_servers: dict[str, SimulatedMCPServer] = {}


def register_mcp_server(server: SimulatedMCPServer, config: dict | None = None):
    """
    Discover tools from an MCP server and register them into the tool registry.

    This is the s16 equivalent of discover_mcp_tools() in the real Hermes Agent.
    """
    config = config or {}
    include = config.get("tools", {}).get("include")
    exclude = config.get("tools", {}).get("exclude", [])

    _mcp_servers[server.name] = server

    tools = server.list_tools()
    registered = []

    for tool in tools:
        name = tool["name"]

        # 白名单 / 黑名单
        if include and name not in include:
            continue
        if name in exclude:
            continue

        # 加前缀
        prefixed = f"mcp_{server.name}_{name}"

        # 创建 handler：调用时转发给 MCP server
        handler = _make_mcp_handler(server.name, name)

        registry.register(
            name=prefixed,
            toolset=f"mcp-{server.name}",
            schema={
                "name": prefixed,
                "description": tool["description"],
                "parameters": tool["inputSchema"],
            },
            handler=handler,
        )
        registered.append(prefixed)

    return registered


def _make_mcp_handler(server_name: str, tool_name: str) -> Callable:
    """Create a sync handler that forwards calls to an MCP server."""

    def handler(args: dict, **kwargs) -> str:
        server = _mcp_servers.get(server_name)
        if not server:
            return json.dumps({"error": f"MCP server '{server_name}' not connected"})

        result = server.call_tool(tool_name, args)

        if result.get("isError"):
            return json.dumps({"error": result["content"]})
        return json.dumps({"result": result["content"]})

    return handler


def unregister_mcp_server(server_name: str):
    """Remove all tools from an MCP server."""
    server = _mcp_servers.pop(server_name, None)
    if not server:
        return
    for tool in server.list_tools():
        prefixed = f"mcp_{server_name}_{tool['name']}"
        # registry doesn't have deregister in our teaching impl,
        # but real Hermes does: registry.deregister(prefixed)


# ===========================================================================
# Entry point
# ===========================================================================

def run_cli():
    """CLI mode with MCP tools available."""
    print("=== s16: MCP Integration (CLI mode) ===")
    print(f"Profile (HERMES_HOME): {HERMES_HOME}")
    print(f"Model: {MODEL} | Base URL: {BASE_URL}")

    conn = init_db(DB_PATH)
    session_id = create_session(conn)
    cached_prompt = build_system_prompt(os.getcwd())
    print(f"System prompt: {len(cached_prompt)} chars")
    print("Type 'quit' to exit.\n")

    while True:
        user_input = input("You: ").strip()
        if not user_input or user_input.lower() in ("quit", "exit"):
            break
        result = run_conversation(
            user_input, conn, session_id, cached_prompt
        )
        print(f"\nAssistant: {result['final_response']}\n")

    conn.close()


async def run_gateway_console():
    """Gateway mode with ConsoleAdapter (interactive, like s12)."""
    print("=== s13: Platform Adapters (Console Gateway) ===")
    print(f"Model: {MODEL}")
    print("All messages flow through GatewayRunner → adapter → core loop.\n")

    runner = GatewayRunner(config=_config, db_path=DB_PATH)
    runner.add_adapter(ConsoleAdapter())

    await runner.start()

    try:
        while runner.adapters.get("console") and runner.adapters["console"]._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()


async def run_gateway_simulated():
    """Gateway mode with SimulatedAdapter (demos batching + dedup)."""
    print("=== s13: Platform Adapters (Simulated Gateway) ===")
    print(f"Model: {MODEL}")
    print("Replaying scripted messages to demo batching + dedup...\n")

    runner = GatewayRunner(config=_config, db_path=DB_PATH)
    sim = SimulatedAdapter()
    runner.add_adapter(sim)

    await runner.start()

    try:
        while sim._running:
            await asyncio.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        await runner.stop()

    # Report what happened
    print("\n--- Simulation Summary ---")
    print(f"Replies sent: {len(sim._replies)}")
    for chat_id, content in sim._replies:
        print(f"  → {chat_id}: {content[:80]}...")


def run_unit_tests():
    """Quick self-tests for s13 + s14 + s16."""
    print("=== s16: Unit Tests ===\n")

    # --- s13 tests ---
    assert utf16_len("hello") == 5
    assert utf16_len("😀") == 2
    print("  utf16_len ........... OK")

    dedup = MessageDeduplicator(max_size=3)
    assert not dedup.is_duplicate("a")
    assert dedup.is_duplicate("a")
    print("  MessageDeduplicator . OK")

    # --- s14 tests ---
    backend = LocalBackend(cwd=os.getcwd())
    result = backend.execute("echo hello_from_s14")
    assert "hello_from_s14" in result["output"]
    print("  LocalBackend ........ OK")

    backend.execute("export TEST_VAR=persistent")
    result2 = backend.execute("echo $TEST_VAR")
    assert "persistent" in result2["output"]
    print("  Session snapshot .... OK")
    backend.cleanup()

    # --- s16 tests (new) ---

    # Create a simulated MCP server with two tools
    def mock_weather(args):
        city = args.get("input", "unknown")
        return f"Weather in {city}: sunny, 25°C"

    def mock_translate(args):
        text = args.get("input", "")
        return f"Translated: [{text}]"

    server = SimulatedMCPServer("demo", {
        "get_weather": mock_weather,
        "translate": mock_translate,
    })

    # list_tools returns correct schemas
    tools = server.list_tools()
    assert len(tools) == 2
    assert tools[0]["name"] == "get_weather"
    print("  MCP list_tools ...... OK")

    # call_tool dispatches correctly
    result = server.call_tool("get_weather", {"input": "Beijing"})
    assert not result["isError"]
    assert "Beijing" in result["content"]
    print("  MCP call_tool ....... OK")

    # call_tool handles unknown tool
    result = server.call_tool("nonexistent", {})
    assert result["isError"]
    print("  MCP unknown tool .... OK")

    # register_mcp_server puts tools into registry with prefix
    registered = register_mcp_server(server)
    assert "mcp_demo_get_weather" in registered
    assert "mcp_demo_translate" in registered
    print("  MCP register ........ OK")

    # Dispatch through registry works end-to-end
    output = registry.dispatch("mcp_demo_get_weather", {"input": "Shanghai"})
    parsed = json.loads(output)
    assert "Shanghai" in parsed["result"]
    print("  MCP dispatch ........ OK")

    # Tool filtering: include only get_weather
    server2 = SimulatedMCPServer("filtered", {
        "tool_a": lambda a: "a",
        "tool_b": lambda a: "b",
        "tool_c": lambda a: "c",
    })
    filtered = register_mcp_server(server2, {"tools": {"include": ["tool_a"]}})
    assert filtered == ["mcp_filtered_tool_a"]
    print("  MCP filtering ....... OK")

    print("\nAll s16 unit tests passed.")


if __name__ == "__main__":
    if "--gateway" in sys.argv:
        asyncio.run(run_gateway_console())
    elif "--simulate" in sys.argv:
        asyncio.run(run_gateway_simulated())
    elif "--test" in sys.argv:
        run_unit_tests()
    else:
        run_cli()
