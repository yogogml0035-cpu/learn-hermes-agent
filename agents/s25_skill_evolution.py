"""
s25: Self-Evolution -- Improving Agent Capabilities Through Text Optimization

See: docs/zh/s25-skill-evolution.md | docs/en/s25-skill-evolution.md

Agent capabilities = model × context text quality. s23 improves the model
(RL training). This chapter improves the context text: systematically
optimizing skills, tool descriptions, prompts, and code WITHOUT retraining.

Key additions over s23:
  - EvalExample / EvalDataset    -- evaluation dataset with train/val/holdout
  - SyntheticDatasetBuilder      -- LLM generates test cases from skill text
  - FitnessScore / evaluate_skill -- LLM-as-judge scoring on rubrics
  - ConstraintValidator          -- size, growth, structure constraint gates
  - SkillOptimizer               -- feedback→mutate→evaluate loop (teaches GEPA core idea)
  - evolve_skill()               -- full evolution pipeline entry point

Usage (CLI mode):
    export OPENAI_API_KEY=sk-xxx
    python agents/s25_skill_evolution.py

Evolve a skill:
    python agents/s25_skill_evolution.py --evolve <skill_name>

Unit tests:
    python agents/s25_skill_evolution.py --test
"""

from __future__ import annotations

import asyncio
import importlib.util
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
# Browser Automation (new in s17)
# ===========================================================================
# agent 通过 accessibility tree "看"网页，通过元素引用操作网页。
# SimulatedBrowser 用内存中的假页面演示完整流程，不需要真浏览器。


@dataclass
class PageElement:
    """An interactive element on a simulated page."""
    ref: str          # "e1", "e2", ...
    role: str         # "link", "button", "textbox", "heading", ...
    name: str         # visible text / label
    value: str = ""   # current value (for inputs)


@dataclass
class SimulatedPage:
    """A fake web page with a title, URL, and interactive elements."""
    url: str
    title: str
    elements: list[PageElement] = field(default_factory=list)


class SimulatedBrowser:
    """
    An in-memory mock browser for testing the browser tool flow.

    No real Chromium, no CDP, no Node.js -- just Python objects simulating
    pages with accessibility trees. Demonstrates how browser_navigate,
    browser_click, browser_type, and browser_snapshot work end-to-end.
    """

    def __init__(self):
        self._current_page: SimulatedPage | None = None
        self._history: list[str] = []
        self._cookies: dict[str, str] = {}

        # Pre-defined pages
        self._pages: dict[str, SimulatedPage] = {
            "https://github.com": SimulatedPage(
                url="https://github.com",
                title="GitHub",
                elements=[
                    PageElement("e1", "link", "Sign in"),
                    PageElement("e2", "link", "Sign up"),
                    PageElement("e3", "search", "Search GitHub"),
                    PageElement("e4", "heading", "Let's build from here"),
                ],
            ),
            "https://github.com/login": SimulatedPage(
                url="https://github.com/login",
                title="Sign in to GitHub",
                elements=[
                    PageElement("e1", "textbox", "Username"),
                    PageElement("e2", "textbox", "Password"),
                    PageElement("e3", "button", "Sign in"),
                ],
            ),
            "https://github.com/search": SimulatedPage(
                url="https://github.com/search",
                title="Search results",
                elements=[
                    PageElement("e1", "link", "NousResearch/hermes-agent"),
                    PageElement("e2", "text", "Self-improving AI agent"),
                    PageElement("e3", "link", "NousResearch/hermes-agent-ui"),
                    PageElement("e4", "text", "Web UI for Hermes Agent"),
                ],
            ),
        }

    def navigate(self, url: str) -> str:
        """Navigate to URL, return accessibility tree snapshot."""
        if self._current_page:
            self._history.append(self._current_page.url)

        page = self._pages.get(url)
        if not page:
            # Unknown URL → generate minimal page
            page = SimulatedPage(
                url=url, title=f"Page: {url}",
                elements=[PageElement("e1", "text", f"Content of {url}")],
            )

        self._current_page = page
        return self.snapshot()

    def snapshot(self) -> str:
        """Return the accessibility tree of the current page."""
        if not self._current_page:
            return "(no page loaded)"

        lines = [f'page "{self._current_page.title}" url="{self._current_page.url}"']
        for el in self._current_page.elements:
            value_part = f' value="{el.value}"' if el.value else ""
            lines.append(f'  {el.role} "{el.name}" [ref={el.ref}]{value_part}')
        return "\n".join(lines)

    def click(self, ref: str) -> str:
        """Click an element by ref. May trigger navigation."""
        if not self._current_page:
            return "(no page loaded)"

        element = self._find_element(ref)
        if not element:
            return f"(error: element {ref} not found)"

        # Simulate: clicking "Sign in" link navigates to login page
        if element.role == "link" and element.name == "Sign in":
            return self.navigate("https://github.com/login")

        # Simulate: clicking "Sign in" button on login page
        if element.role == "button" and element.name == "Sign in":
            self._cookies["session"] = "logged_in"
            return f'Clicked "{element.name}". Login successful.'

        return f'Clicked "{element.name}".'

    def type_text(self, ref: str, text: str) -> str:
        """Type text into an input element."""
        if not self._current_page:
            return "(no page loaded)"

        element = self._find_element(ref)
        if not element:
            return f"(error: element {ref} not found)"

        element.value = text
        return f'Typed "{text}" into "{element.name}".'

    def press_key(self, key: str) -> str:
        """Press a keyboard key. Enter on search → navigate to results."""
        if (self._current_page and
                self._current_page.url == "https://github.com" and
                key.lower() == "enter"):
            return self.navigate("https://github.com/search")
        return f"Pressed {key}."

    def back(self) -> str:
        """Go back in history."""
        if not self._history:
            return "(no history)"
        url = self._history.pop()
        return self.navigate(url)

    def console(self, expression: str = "") -> str:
        """Evaluate a JS expression (simulated)."""
        if not expression:
            return "(no console errors)"
        if expression == "document.title":
            return self._current_page.title if self._current_page else ""
        if expression == "document.cookie":
            return "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return f"(eval: {expression})"

    def _find_element(self, ref: str) -> PageElement | None:
        if not self._current_page:
            return None
        for el in self._current_page.elements:
            if el.ref == ref:
                return el
        return None


# --- Global browser instance (per task_id in real Hermes, global here) ---
_browser: SimulatedBrowser | None = None


def _get_browser() -> SimulatedBrowser:
    global _browser
    if _browser is None:
        _browser = SimulatedBrowser()
    return _browser


# --- Browser tool handlers ---

def handle_browser_navigate(args, **kwargs):
    return _get_browser().navigate(args.get("url", ""))

def handle_browser_snapshot(args, **kwargs):
    return _get_browser().snapshot()

def handle_browser_click(args, **kwargs):
    return _get_browser().click(args.get("ref", ""))

def handle_browser_type(args, **kwargs):
    return _get_browser().type_text(args.get("ref", ""), args.get("text", ""))

def handle_browser_press(args, **kwargs):
    return _get_browser().press_key(args.get("key", ""))

def handle_browser_back(args, **kwargs):
    return _get_browser().back()

def handle_browser_console(args, **kwargs):
    return _get_browser().console(args.get("expression", ""))


# --- Register browser tools ---

for tool_name, handler, desc, params in [
    ("browser_navigate", handle_browser_navigate, "Navigate to URL.",
     {"url": {"type": "string", "description": "URL to navigate to"}}),
    ("browser_snapshot", handle_browser_snapshot, "Get page accessibility tree.", {}),
    ("browser_click", handle_browser_click, "Click an element by ref.",
     {"ref": {"type": "string", "description": "Element reference (e.g. e1)"}}),
    ("browser_type", handle_browser_type, "Type text into an input.",
     {"ref": {"type": "string"}, "text": {"type": "string"}}),
    ("browser_press", handle_browser_press, "Press a keyboard key.",
     {"key": {"type": "string", "description": "Key name (Enter, Tab, etc.)"}}),
    ("browser_back", handle_browser_back, "Go back in browser history.", {}),
    ("browser_console", handle_browser_console, "Evaluate JavaScript or get console logs.",
     {"expression": {"type": "string"}}),
]:
    required = list(params.keys()) if params else []
    registry.register(
        name=tool_name,
        toolset="browser",
        schema={
            "name": tool_name,
            "description": desc,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
        handler=handler,
    )


# ===========================================================================
# Voice & Vision (new in s18)
# ===========================================================================
# 辅助模型：视觉用单独的模型，和主模型分开。
# STT 在适配器层做，TTS 在工具层做 + Gateway 层投递。


class SimulatedVisionModel:
    """
    Mock vision model for testing without real API calls.

    Given an image description (simulating base64 analysis), returns a
    text analysis. In real Hermes, this would call Gemini Flash or Claude.
    """

    def analyze(self, image_base64: str, question: str) -> str:
        # 模拟视觉分析：根据图片"内容"返回描述
        if "error" in image_base64.lower() or "traceback" in image_base64.lower():
            return (
                "The image shows a Python traceback with a TypeError: "
                "'NoneType' object is not iterable. This typically happens "
                "when you try to loop over a variable that is None."
            )
        if "chart" in image_base64.lower() or "graph" in image_base64.lower():
            return (
                "The image shows a bar chart with 5 categories. "
                "The tallest bar is the third one, approximately 85 units."
            )
        return f"The image appears to be a general screenshot. Question: {question}"


_vision_model = SimulatedVisionModel()


def _image_to_base64(image_path: str) -> str:
    """Read an image file and return a base64 data URL (simplified)."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    import base64
    data = path.read_bytes()

    # 大小检查（真实实现里是 20MB）
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Image too large (>20MB)")

    # 格式检测
    if data[:4] == b'\x89PNG':
        mime = "image/png"
    elif data[:2] == b'\xff\xd8':
        mime = "image/jpeg"
    else:
        mime = "image/png"  # fallback

    encoded = base64.b64encode(data).decode()
    return f"data:{mime};base64,{encoded}"


def handle_vision_analyze(args, **kwargs):
    """Vision tool: analyze an image with the auxiliary vision model."""
    image_url = args.get("image_url", "")
    question = args.get("question", "Describe this image.")

    try:
        if image_url.startswith(("http://", "https://")):
            # 真实实现：下载图片 → 验证 → base64
            # 教学简化：用 URL 本身作为"内容"传给模拟模型
            image_data = image_url
        else:
            # 本地文件
            image_data = _image_to_base64(image_url)

        analysis = _vision_model.analyze(image_data, question)
        return json.dumps({"result": analysis})

    except Exception as exc:
        return json.dumps({"error": str(exc)})


registry.register(
    name="vision_analyze",
    toolset="vision",
    schema={
        "name": "vision_analyze",
        "description": "Analyze an image using AI vision.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_url": {
                    "type": "string",
                    "description": "Image URL or local file path",
                },
                "question": {
                    "type": "string",
                    "description": "Question about the image",
                },
            },
            "required": ["image_url"],
        },
    },
    handler=handle_vision_analyze,
)


# --- Text-to-Speech (TTS) ---

def handle_text_to_speech(args, **kwargs):
    """TTS tool: convert text to audio file, return MEDIA tag."""
    text = args.get("text", "")
    if not text:
        return json.dumps({"error": "No text provided"})

    # 模拟 TTS：创建一个假音频文件
    audio_dir = HERMES_HOME / "cache" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    filename = f"tts_{uuid.uuid4().hex[:8]}.ogg"
    audio_path = audio_dir / filename

    # 真实实现会调 Edge TTS / OpenAI TTS 生成真音频
    # 这里写一个占位文件
    audio_path.write_text(f"[simulated audio: {text[:50]}]")

    return json.dumps({
        "success": True,
        "file_path": str(audio_path),
        "media_tag": f"MEDIA:{audio_path}",
    })


registry.register(
    name="text_to_speech",
    toolset="voice",
    schema={
        "name": "text_to_speech",
        "description": "Convert text to speech audio.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to speak"},
            },
            "required": ["text"],
        },
    },
    handler=handle_text_to_speech,
)


# --- Speech-to-Text (STT) ---
# STT 在适配器层调用（不注册为 agent 工具），这里只提供函数。

def transcribe_audio(audio_path: str) -> dict:
    """
    Transcribe audio to text (simulated).

    In real Hermes, this would call local Whisper, Groq, or OpenAI STT.
    Called by platform adapters when they receive voice messages.
    """
    path = Path(audio_path)
    if not path.exists():
        return {"success": False, "error": f"File not found: {audio_path}"}

    # 模拟转写
    return {
        "success": True,
        "transcript": f"[simulated transcript of {path.name}]",
        "provider": "simulated",
    }


# ===========================================================================
# Skill Creation Loop (new in s21)
# ===========================================================================
# 后台审视：对话结束后 fork 一个副本分析"有没有值得保存的经验"。
# 如果有，调 skill_manage 创建技能。下次对话时技能出现在可用列表里。

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and consider saving or updating a skill "
    "if appropriate.\n\n"
    "Focus on: was a non-trivial approach used to complete a task that required "
    "trial and error, or changing course due to experiential findings along "
    "the way, or did the user expect or desire a different method or outcome?\n\n"
    "If a relevant skill already exists, update it with what you learned. "
    "Otherwise, create a new skill if the approach is reusable.\n"
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)


class BackgroundReviewer:
    """
    Analyzes a completed conversation for reusable patterns.

    In real Hermes, this forks an AIAgent with max_iterations=8,
    running in a daemon thread. Here we simulate the review logic.
    """

    def __init__(self, skill_nudge_interval: int = 10):
        self.skill_nudge_interval = skill_nudge_interval
        self._iters_since_skill = 0
        self._reviews_triggered = 0
        self._skills_created: list[str] = []

    def on_tool_call(self):
        """Called after each tool call. Increments the counter."""
        self._iters_since_skill += 1

    def should_review(self) -> bool:
        """Check if it's time to trigger a background review."""
        if self.skill_nudge_interval <= 0:
            return False
        return self._iters_since_skill >= self.skill_nudge_interval

    def review(self, messages: list[dict]) -> dict:
        """
        Simulate a background review of the conversation.

        In real Hermes, this would:
        1. Fork an AIAgent with the messages + _SKILL_REVIEW_PROMPT
        2. Let it run (max 8 iterations)
        3. Extract skill_manage tool calls from its output

        Here we simulate by analyzing the messages for patterns.
        """
        self._reviews_triggered += 1
        self._iters_since_skill = 0  # reset counter

        # Simulate analysis: look for tool call diversity and errors
        tool_calls = []
        has_errors = False
        has_retries = False

        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tool_calls.append(tc["function"]["name"])
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if "error" in content.lower() or "failed" in content.lower():
                    has_errors = True
                if "retry" in content.lower() or "try again" in content.lower():
                    has_retries = True

        # Non-trivial heuristic: errors + retries suggest trial-and-error
        unique_tools = set(tool_calls)
        is_nontrivial = (has_errors or has_retries) and len(unique_tools) >= 3

        if not is_nontrivial:
            return {"action": "skip", "reason": "Nothing to save."}

        # Generate a skill name from the tools used
        skill_name = f"workflow-{'_'.join(sorted(unique_tools)[:3])}"
        skill_content = self._generate_skill_content(messages, tool_calls, skill_name)

        # Create the skill via the existing skill_manage handler
        result = handle_skill_manage({
            "action": "create",
            "name": skill_name,
            "description": f"Workflow pattern using {', '.join(sorted(unique_tools)[:3])}",
            "body": skill_content,
        })

        self._skills_created.append(skill_name)
        return {
            "action": "created",
            "skill_name": skill_name,
            "result": result,
        }

    def _generate_skill_content(
        self, messages: list[dict], tool_calls: list[str], name: str
    ) -> str:
        """Generate skill body from conversation analysis."""
        # Extract the sequence of tools used
        unique = list(dict.fromkeys(tool_calls))  # ordered unique

        lines = [f"# {name}", "", "## Steps", ""]
        for i, tool in enumerate(unique, 1):
            lines.append(f"{i}. Use `{tool}` tool")

        # Add pitfalls from error messages
        lines.extend(["", "## Pitfalls", ""])
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if "error" in content.lower():
                    # Extract first line of error
                    first_line = content.split("\n")[0][:100]
                    lines.append(f"- Watch out: {first_line}")

        return "\n".join(lines)


# --- Global reviewer instance ---
_reviewer = BackgroundReviewer(skill_nudge_interval=10)


def build_skills_index() -> str:
    """
    Build the skills index for the system prompt.

    Only includes name + description (not full content).
    Full content loaded on-demand via skill_view.
    """
    skills = discover_skills()
    if not skills:
        return ""

    lines = ["# Available Skills"]
    lines.append("Load a skill with skill_view(name) before following its instructions.\n")
    for skill in skills:
        lines.append(f"- **{skill['name']}**: {skill['description']}")

    return "\n".join(lines)


# ===========================================================================
# Hook system (new in s22)
# ===========================================================================
# 两套 hook 系统：Gateway hooks（事件驱动）和 Plugin hooks（回调驱动）。
# 异常永远不传播——一个坏 hook 不能搞崩核心循环。


# --- Gateway hooks: async event emitter ---

class HookRegistry:
    """
    Async event emitter for Gateway lifecycle events.

    Supports wildcard matching: a handler registered for "command:*"
    fires for "command:reset", "command:model", etc.
    Errors are caught and logged, never propagated.
    """

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}
        self._loaded_hooks: list[dict] = []

    def register(self, event_type: str, handler: Callable):
        """Register a handler for an event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    async def emit(self, event_type: str, context: dict | None = None):
        """Fire all handlers for an event. Supports wildcard matching."""
        handlers = list(self._handlers.get(event_type, []))
        # Wildcard: "command:*" matches any "command:xxx"
        if ":" in event_type:
            base = event_type.split(":")[0]
            wildcard_key = f"{base}:*"
            handlers.extend(self._handlers.get(wildcard_key, []))

        for fn in handlers:
            try:
                result = fn(event_type, context or {})
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                print(f"  [hooks] Error in handler for '{event_type}': {e}")

    def discover_and_load(self, hooks_dir: Path):
        """
        Scan hooks directory for HOOK.yaml + handler.py pairs.

        Each subdirectory under hooks_dir that contains both files
        gets dynamically loaded and registered.
        """
        # Always register built-in hooks first
        self._register_builtin_hooks()

        if not hooks_dir.exists():
            return

        for hook_dir in sorted(hooks_dir.iterdir()):
            if not hook_dir.is_dir():
                continue
            manifest_path = hook_dir / "HOOK.yaml"
            handler_path = hook_dir / "handler.py"
            if not manifest_path.exists() or not handler_path.exists():
                continue

            try:
                meta = yaml.safe_load(manifest_path.read_text())
                if not meta or not meta.get("events"):
                    continue

                # Dynamic import of handler.py
                spec = importlib.util.spec_from_file_location(
                    meta.get("name", hook_dir.name), handler_path
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                handle_fn = getattr(module, "handle", None)
                if handle_fn is None:
                    print(f"  [hooks] {hook_dir.name}: no 'handle' function")
                    continue

                for event in meta["events"]:
                    self.register(event, handle_fn)

                self._loaded_hooks.append({
                    "name": meta.get("name", hook_dir.name),
                    "description": meta.get("description", ""),
                    "events": meta["events"],
                    "path": str(hook_dir),
                })
            except Exception as e:
                print(f"  [hooks] Failed to load {hook_dir.name}: {e}")

    def _register_builtin_hooks(self):
        """Register the built-in BOOT.md hook."""
        self.register("gateway:startup", handle_boot_md)
        self._loaded_hooks.append({
            "name": "boot-md",
            "description": "Run ~/.hermes/BOOT.md on gateway startup",
            "events": ["gateway:startup"],
            "path": "(builtin)",
        })

    def list_hooks(self) -> list[dict]:
        """Return metadata for all loaded hooks."""
        return list(self._loaded_hooks)


# --- BOOT.md handler (built-in gateway hook) ---

def handle_boot_md(event_type: str, context: dict):
    """
    Built-in hook: run BOOT.md as an agent prompt on gateway startup.

    Executes in a background thread so it doesn't block startup.
    If BOOT.md doesn't exist, does nothing (zero overhead).
    """
    boot_path = HERMES_HOME / "BOOT.md"
    if not boot_path.exists():
        return

    content = boot_path.read_text(encoding="utf-8").strip()
    if not content:
        return

    def _run_boot():
        conn = init_db(DB_PATH)
        session_id = create_session(conn)
        prompt = build_system_prompt(os.getcwd())
        try:
            result = run_conversation(
                user_message=content,
                conn=conn,
                session_id=session_id,
                cached_prompt=prompt,
            )
            response = result.get("final_response", "")
            # [SILENT] means the boot agent has nothing to report
            if response and "[SILENT]" not in response:
                print(f"  [boot] {response[:200]}")
        except Exception:
            pass  # best-effort
        finally:
            conn.close()

    thread = threading.Thread(target=_run_boot, daemon=True, name="boot-md")
    thread.start()
    return thread  # for testing


# --- Plugin hooks: sync callback registry ---

class PluginHookRegistry:
    """
    Sync callback registry for agent-level lifecycle events.

    Works in both CLI and Gateway mode. Callbacks are registered
    via register_hook() and invoked via invoke_hook().
    Errors are caught and logged, never propagated.
    """

    # Valid hook names
    VALID_HOOKS = frozenset([
        "pre_tool_call", "post_tool_call",
        "pre_llm_call", "post_llm_call",
        "on_session_start", "on_session_end",
    ])

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = {}

    def register_hook(self, hook_name: str, callback: Callable):
        """Register a callback for a hook. Raises ValueError for unknown hooks."""
        if hook_name not in self.VALID_HOOKS:
            raise ValueError(
                f"Unknown hook: {hook_name}. "
                f"Valid: {sorted(self.VALID_HOOKS)}"
            )
        self._hooks.setdefault(hook_name, []).append(callback)

    def invoke_hook(self, hook_name: str, **kwargs) -> list:
        """Invoke all callbacks for a hook. Returns list of non-None results."""
        results = []
        for cb in self._hooks.get(hook_name, []):
            try:
                ret = cb(**kwargs)
                if ret is not None:
                    results.append(ret)
            except Exception as e:
                print(f"  [hook] {hook_name} error in {cb.__name__}: {e}")
        return results

    def list_hooks(self) -> dict[str, int]:
        """Return hook names and their callback counts."""
        return {name: len(cbs) for name, cbs in self._hooks.items() if cbs}


# --- Global plugin hook instance ---

_plugin_hooks = PluginHookRegistry()


def get_plugin_hooks() -> PluginHookRegistry:
    return _plugin_hooks


# ===========================================================================
# Trajectory system (new in s23)
# ===========================================================================
# 对话轨迹的收集、格式化、压缩和奖励打分。
# 这是 Hermes Agent 的离线进化机制——把对话经验变成训练数据。


def convert_to_trajectory(messages: list[dict]) -> list[dict]:
    """
    Convert OpenAI-format messages to ShareGPT trajectory format.

    Role mapping: system→system, user→human, assistant→gpt, tool→tool.
    Tool calls wrapped in <tool_call> tags, tool results in <tool_response>.
    """
    trajectory = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        if role == "system":
            from_field = "system"
        elif role == "user":
            from_field = "human"
        elif role == "assistant":
            from_field = "gpt"
            # Wrap tool calls in <tool_call> tags
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = fn.get("arguments", "")
                    tc_text = json.dumps(
                        {"name": fn.get("name", ""), "arguments": args},
                        ensure_ascii=False,
                    )
                    content += f"\n<tool_call>\n{tc_text}\n</tool_call>"
        elif role == "tool":
            from_field = "tool"
            tc_id = msg.get("tool_call_id", "")
            content = (
                f"<tool_response>\n"
                f'{{"tool_call_id": "{tc_id}", '
                f'"content": {json.dumps(content, ensure_ascii=False)}}}\n'
                f"</tool_response>"
            )
        else:
            continue

        if content:
            trajectory.append({"from": from_field, "value": content})

    return trajectory


def extract_tool_stats(messages: list[dict]) -> dict:
    """Extract per-tool success/failure counts from messages."""
    stats: dict[str, dict] = {}
    # Map tool_call_id → tool_name
    tc_map: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                tc_map[tc["id"]] = fn.get("name", "unknown")
        elif msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            tool_name = tc_map.get(tc_id, "unknown")
            if tool_name not in stats:
                stats[tool_name] = {"count": 0, "success": 0, "failure": 0}
            stats[tool_name]["count"] += 1
            content = (msg.get("content") or "")[:200].lower()
            if "error" in content or "failed" in content:
                stats[tool_name]["failure"] += 1
            else:
                stats[tool_name]["success"] += 1
    return stats


def extract_reasoning_stats(messages: list[dict]) -> dict:
    """Count how many assistant turns include reasoning (<think> blocks)."""
    total = 0
    with_reasoning = 0
    for msg in messages:
        if msg.get("role") == "assistant":
            total += 1
            content = msg.get("content") or ""
            if "<think>" in content or "<REASONING" in content:
                with_reasoning += 1
    return {
        "total_assistant_turns": total,
        "turns_with_reasoning": with_reasoning,
        "turns_without_reasoning": total - with_reasoning,
        "has_any_reasoning": with_reasoning > 0,
    }


# --- Trajectory compression ---

def _estimate_tokens(text: str) -> int:
    """Rough token estimate (1 token ≈ 4 chars)."""
    return len(text) // 4


def estimate_tokens_trajectory(trajectory: list[dict]) -> int:
    """Estimate total tokens in a trajectory."""
    return sum(_estimate_tokens(t.get("value", "")) for t in trajectory)


def compress_trajectory(
    trajectory: list[dict],
    target_tokens: int = 15250,
    protect_last_n: int = 4,
) -> tuple[list[dict], dict]:
    """
    Compress a trajectory to fit within target token budget.

    Strategy: protect head (system + first human + first gpt) and
    tail (last N turns). Replace middle with a rule-based summary.

    Returns (compressed_trajectory, metrics_dict).
    """
    original_tokens = estimate_tokens_trajectory(trajectory)
    if original_tokens <= target_tokens:
        return trajectory, {
            "was_compressed": False,
            "original_tokens": original_tokens,
            "compressed_tokens": original_tokens,
            "turns_removed": 0,
        }

    # Protect head: system + first human + first gpt
    head: list[dict] = []
    rest = list(trajectory)
    for role in ("system", "human", "gpt"):
        for i, turn in enumerate(rest):
            if turn["from"] == role:
                head.append(rest.pop(i))
                break

    # Protect tail
    if len(rest) > protect_last_n:
        tail = rest[-protect_last_n:]
        middle = rest[:-protect_last_n]
    else:
        tail = rest
        middle = []

    # Summarize middle
    if middle:
        summary = _summarize_turns(middle)
        compressed_middle = [{
            "from": "system",
            "value": f"[Summary of {len(middle)} middle turns]\n{summary}",
        }]
    else:
        compressed_middle = []

    compressed = head + compressed_middle + tail
    compressed_tokens = estimate_tokens_trajectory(compressed)

    return compressed, {
        "was_compressed": True,
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "turns_removed": len(middle),
    }


def _summarize_turns(turns: list[dict]) -> str:
    """Rule-based summary of middle turns (production uses LLM)."""
    tools_used: set[str] = set()
    errors = 0
    for t in turns:
        value = t.get("value", "")
        if "<tool_call>" in value:
            for match in re.findall(r'"name":\s*"(\w+)"', value):
                tools_used.add(match)
        if t["from"] == "tool" and "error" in value.lower()[:200]:
            errors += 1

    parts = [f"Agent worked through {len(turns)} turns."]
    if tools_used:
        parts.append(f"Tools: {', '.join(sorted(tools_used))}.")
    if errors:
        parts.append(f"Hit {errors} error(s), recovered and continued.")
    return " ".join(parts)


# --- Reward functions ---

def correctness_reward(completions: list[str], expected: list[str]) -> list[float]:
    """
    2.0 if expected answer found in completion, 0.0 otherwise.
    Highest-priority reward signal.
    """
    rewards = []
    for completion, answer in zip(completions, expected):
        if answer and answer in completion:
            rewards.append(2.0)
        else:
            rewards.append(0.0)
    return rewards


def format_reward(completions: list[str]) -> list[float]:
    """
    Up to 0.5 for proper formatting (think tags + tool_call tags).
    Auxiliary reward to encourage structured behavior.
    """
    rewards = []
    for c in completions:
        score = 0.0
        if "<think>" in c and "</think>" in c:
            score += 0.25
        if "<tool_call>" in c:
            score += 0.25
        rewards.append(score)
    return rewards


# --- Batch runner (simplified) ---

def run_batch(prompts: list[str], output_path: str) -> list[dict]:
    """
    Run the agent on each prompt, collect trajectories to JSONL.

    Simplified teaching version. Production batch_runner adds:
    parallelism, checkpointing, toolset sampling, reasoning filtering.
    """
    results = []
    for i, prompt in enumerate(prompts):
        conn = init_db(":memory:")
        session_id = create_session(conn)
        cached = build_system_prompt(os.getcwd())

        try:
            result = run_conversation(prompt, conn, session_id, cached)
            messages = result.get("messages", [])
            trajectory = convert_to_trajectory(messages)
            tool_stats = extract_tool_stats(messages)
            reasoning = extract_reasoning_stats(messages)

            entry = {
                "prompt_index": i,
                "trajectory": trajectory,
                "tool_stats": tool_stats,
                "reasoning_stats": reasoning,
                "completed": result.get("final_response") is not None,
                "api_calls": sum(
                    1 for m in messages if m.get("role") == "assistant"
                ),
            }
            # Filter: discard zero-reasoning samples
            if not reasoning["has_any_reasoning"]:
                entry["filtered"] = "no_reasoning"

            results.append(entry)
        except Exception as e:
            results.append({
                "prompt_index": i,
                "trajectory": [],
                "completed": False,
                "error": str(e),
            })
        finally:
            conn.close()

        status = "OK" if results[-1].get("completed") else "FAIL"
        print(f"  [{i+1}/{len(prompts)}] {status}")

    # Write JSONL
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = sum(1 for r in results if r.get("completed"))
    print(f"\nBatch: {ok}/{len(prompts)} succeeded → {output_path}")
    return results


# ===========================================================================
# Skill Evolution system (new in s25)
# ===========================================================================
# s23 改进模型（RL 训练），s25 改进文本（技能优化）。
# 核心洞察：agent 能力 = 模型 × 上下文文本。优化文本 = 提升能力。
# 不改模型权重，只改 skill body 文本，纯 API 调用，~$2-10/次。
#
# 实际的 Hermes Agent 用 DSPy + GEPA 做优化。
# 这里用最简实现教核心思路：feedback → mutate → evaluate → select。


@dataclass
class EvalExample:
    """一个评估用例：任务输入 + 期望行为描述（rubric）。

    注意 expected_behavior 是"好的回答应该包含什么"的描述，
    不是精确的期望输出文本。这让评估更灵活。
    """
    task_input: str
    expected_behavior: str
    difficulty: str = "medium"


@dataclass
class EvalDataset:
    """评估数据集，分为训练/验证/测试三个子集。

    train: 优化器用来评估和改进（可以"看到"）
    val:   优化器用来选择最佳版本（防止过拟合 train）
    holdout: 最终评估用，优化过程中从不使用
    """
    train: list[EvalExample] = field(default_factory=list)
    val: list[EvalExample] = field(default_factory=list)
    holdout: list[EvalExample] = field(default_factory=list)

    @property
    def all_examples(self) -> list[EvalExample]:
        return self.train + self.val + self.holdout

    def save(self, path: Path):
        """Save splits to JSONL files."""
        path.mkdir(parents=True, exist_ok=True)
        for split_name, split_data in [
            ("train", self.train), ("val", self.val), ("holdout", self.holdout),
        ]:
            with open(path / f"{split_name}.jsonl", "w", encoding="utf-8") as f:
                for ex in split_data:
                    f.write(json.dumps({
                        "task_input": ex.task_input,
                        "expected_behavior": ex.expected_behavior,
                        "difficulty": ex.difficulty,
                    }, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, path: Path) -> "EvalDataset":
        """Load splits from JSONL files."""
        dataset = cls()
        for split_name in ["train", "val", "holdout"]:
            split_file = path / f"{split_name}.jsonl"
            if split_file.exists():
                examples = []
                for line in split_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        d = json.loads(line)
                        examples.append(EvalExample(**d))
                setattr(dataset, split_name, examples)
        return dataset


# SyntheticDatasetBuilder, FitnessScore, ConstraintValidator → s26
# SkillOptimizer, EvolutionResult, evolve_skill() → s27



# ===========================================================================
# Entry point
# ===========================================================================

def run_cli():
    """CLI mode (s25)."""
    print("=== s25: Self-Evolution Overview (CLI mode) ===")
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
    """Quick self-tests for s18 + s21 + s22 + s23."""
    print("=== s23: Unit Tests ===\n")

    # --- s18 smoke ---
    model = SimulatedVisionModel()
    assert "TypeError" in model.analyze("traceback error", "?")
    print("  Vision smoke ........ OK")

    # --- s21 BackgroundReviewer tests (reused) ---

    reviewer = BackgroundReviewer(skill_nudge_interval=3)

    assert reviewer._iters_since_skill == 0
    reviewer.on_tool_call()
    reviewer.on_tool_call()
    assert reviewer._iters_since_skill == 2
    assert not reviewer.should_review()

    reviewer.on_tool_call()
    assert reviewer.should_review()

    trivial_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result = reviewer.review(trivial_messages)
    assert result["action"] == "skip"
    assert reviewer._iters_since_skill == 0

    nontrivial_messages = [
        {"role": "user", "content": "Help me set up CI"},
        {"role": "assistant", "content": "Let me try...", "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "1", "content": "error: command failed"},
        {"role": "assistant", "content": "Let me fix...", "tool_calls": [
            {"id": "2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "2", "content": "file contents..."},
        {"role": "assistant", "content": "Try again...", "tool_calls": [
            {"id": "3", "type": "function", "function": {"name": "write_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "3", "content": "retry succeeded after error"},
    ]

    for _ in range(3):
        reviewer.on_tool_call()
    result = reviewer.review(nontrivial_messages)
    assert result["action"] == "created"
    skill_name = result["skill_name"]
    skill_file = SKILLS_DIR / skill_name / "SKILL.md"
    assert skill_file.exists()

    import shutil as _shutil
    _shutil.rmtree(SKILLS_DIR / skill_name, ignore_errors=True)
    print("  s21 reviewer ........ OK")

    # --- s22 tests (new) ---

    # HookRegistry: register and emit
    hook_log = []

    hr = HookRegistry()
    hr._loaded_hooks.clear()  # remove built-in for clean test

    def sync_handler(event_type, context):
        hook_log.append(("sync", event_type, context))

    hr.register("agent:start", sync_handler)
    asyncio.get_event_loop().run_until_complete(
        hr.emit("agent:start", {"user": "alice"})
    )
    assert len(hook_log) == 1
    assert hook_log[0] == ("sync", "agent:start", {"user": "alice"})
    print("  HookRegistry sync ... OK")

    # Async handler
    async def async_handler(event_type, context):
        hook_log.append(("async", event_type, context))

    hr.register("agent:end", async_handler)
    asyncio.get_event_loop().run_until_complete(
        hr.emit("agent:end", {"user": "bob"})
    )
    assert hook_log[-1] == ("async", "agent:end", {"user": "bob"})
    print("  HookRegistry async .. OK")

    # Wildcard matching
    wildcard_log = []
    hr.register("command:*", lambda et, ctx: wildcard_log.append(et))
    asyncio.get_event_loop().run_until_complete(
        hr.emit("command:reset", {})
    )
    asyncio.get_event_loop().run_until_complete(
        hr.emit("command:model", {})
    )
    assert wildcard_log == ["command:reset", "command:model"]
    print("  HookRegistry wild ... OK")

    # Error isolation
    def bad_handler(event_type, context):
        raise RuntimeError("hook crash!")

    hr.register("agent:start", bad_handler)
    # Should not raise, just print error
    asyncio.get_event_loop().run_until_complete(
        hr.emit("agent:start", {})
    )
    # sync_handler still fired (it was registered first)
    assert hook_log[-1][0] == "sync"
    print("  HookRegistry error .. OK")

    # Unmatched event → no crash
    asyncio.get_event_loop().run_until_complete(
        hr.emit("nonexistent:event", {})
    )
    print("  HookRegistry noop ... OK")

    # PluginHookRegistry: register and invoke
    phr = PluginHookRegistry()
    tool_log = []

    def audit_tool(tool_name, args, **kw):
        tool_log.append(f"{tool_name}:{args}")

    phr.register_hook("pre_tool_call", audit_tool)
    phr.invoke_hook("pre_tool_call", tool_name="terminal", args="ls -la")
    assert tool_log == ["terminal:ls -la"]
    print("  PluginHook basic .... OK")

    # Return value collection
    def add_context(**kw):
        return {"context": "extra info"}

    phr.register_hook("pre_llm_call", add_context)
    results = phr.invoke_hook("pre_llm_call")
    assert len(results) == 1
    assert results[0]["context"] == "extra info"
    print("  PluginHook return ... OK")

    # Error isolation
    def bad_plugin(tool_name, args, **kw):
        raise ValueError("plugin crash!")

    phr.register_hook("post_tool_call", bad_plugin)
    phr.invoke_hook("post_tool_call", tool_name="test", args={})
    # Should not raise
    print("  PluginHook error .... OK")

    # Invalid hook name → ValueError
    try:
        phr.register_hook("invalid_hook", lambda: None)
        assert False, "should have raised"
    except ValueError:
        pass
    print("  PluginHook valid .... OK")

    # list_hooks
    hooks = phr.list_hooks()
    assert "pre_tool_call" in hooks
    assert hooks["pre_tool_call"] == 1
    print("  PluginHook list ..... OK")

    # BOOT.md handler: no file → noop
    result = handle_boot_md("gateway:startup", {})
    assert result is None
    print("  s22 hooks ........... OK")

    # --- s23 tests (new) ---

    # convert_to_trajectory: basic conversion
    test_msgs = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    traj = convert_to_trajectory(test_msgs)
    assert len(traj) == 3
    assert traj[0] == {"from": "system", "value": "You are helpful."}
    assert traj[1] == {"from": "human", "value": "Hello"}
    assert traj[2] == {"from": "gpt", "value": "Hi there!"}
    print("  convert basic ....... OK")

    # convert_to_trajectory: tool calls
    tool_msgs = [
        {"role": "user", "content": "Run ls"},
        {"role": "assistant", "content": "Sure", "tool_calls": [
            {"id": "tc1", "type": "function",
             "function": {"name": "terminal", "arguments": '{"command": "ls"}'}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": "file1.txt\nfile2.txt"},
        {"role": "assistant", "content": "Here are the files."},
    ]
    traj2 = convert_to_trajectory(tool_msgs)
    assert any("<tool_call>" in t["value"] for t in traj2)
    assert any("<tool_response>" in t["value"] for t in traj2)
    assert any('"terminal"' in t["value"] for t in traj2)
    print("  convert tools ....... OK")

    # extract_tool_stats
    stats = extract_tool_stats(tool_msgs)
    assert "terminal" in stats
    assert stats["terminal"]["count"] == 1
    assert stats["terminal"]["success"] == 1
    assert stats["terminal"]["failure"] == 0
    print("  tool_stats .......... OK")

    # extract_tool_stats with errors
    error_msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "e1", "type": "function",
             "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "e1", "content": "error: command not found"},
    ]
    err_stats = extract_tool_stats(error_msgs)
    assert err_stats["terminal"]["failure"] == 1
    print("  tool_stats errors ... OK")

    # extract_reasoning_stats
    reasoning_msgs = [
        {"role": "assistant", "content": "<think>let me think</think> ok"},
        {"role": "assistant", "content": "no thinking here"},
        {"role": "assistant", "content": "<think>more thought</think> done"},
    ]
    rs = extract_reasoning_stats(reasoning_msgs)
    assert rs["total_assistant_turns"] == 3
    assert rs["turns_with_reasoning"] == 2
    assert rs["has_any_reasoning"] is True
    print("  reasoning_stats ..... OK")

    # compress_trajectory: short trajectory → no compression
    short_traj = [
        {"from": "system", "value": "sys"},
        {"from": "human", "value": "hi"},
        {"from": "gpt", "value": "hello"},
    ]
    compressed, metrics = compress_trajectory(short_traj, target_tokens=1000)
    assert metrics["was_compressed"] is False
    assert compressed == short_traj
    print("  compress short ...... OK")

    # compress_trajectory: long trajectory → compressed
    long_traj = [
        {"from": "system", "value": "s" * 1000},
        {"from": "human", "value": "h" * 1000},
    ]
    # Add 20 middle turns of ~500 chars each
    for i in range(20):
        long_traj.append({"from": "gpt", "value": f"turn {i} " + "x" * 500})
    long_traj.append({"from": "gpt", "value": "final answer " + "y" * 500})

    compressed2, metrics2 = compress_trajectory(
        long_traj, target_tokens=2000, protect_last_n=2
    )
    assert metrics2["was_compressed"] is True
    assert metrics2["turns_removed"] > 0
    assert len(compressed2) < len(long_traj)
    # Head and tail preserved
    assert compressed2[0]["from"] == "system"
    assert "final answer" in compressed2[-1]["value"]
    print("  compress long ....... OK")

    # correctness_reward
    rewards = correctness_reward(
        ["the answer is 42", "wrong answer", "42 is correct"],
        ["42", "42", "42"],
    )
    assert rewards == [2.0, 0.0, 2.0]
    print("  correctness_reward .. OK")

    # format_reward
    f_rewards = format_reward([
        "<think>thought</think> <tool_call>call</tool_call>",  # both
        "<think>thought</think> no tools",  # think only
        "no formatting at all",  # neither
    ])
    assert f_rewards[0] == 0.5
    assert f_rewards[1] == 0.25
    assert f_rewards[2] == 0.0
    print("  format_reward ....... OK")

    # estimate_tokens
    assert _estimate_tokens("hello world") == 2  # 11 chars / 4
    assert estimate_tokens_trajectory([
        {"from": "gpt", "value": "x" * 400}
    ]) == 100
    print("  token estimation .... OK")

    print("\nAll s23 unit tests passed.")

    # ---- s25 tests: Self-Evolution data structures ----
    print("\n--- s25: Self-Evolution Overview ---\n")

    # EvalExample creation
    ex1 = EvalExample("write a hello world", "should print hello", "easy")
    ex2 = EvalExample("sort a list", "use sorted() or .sort()", "medium")
    ex3 = EvalExample("parse JSON", "use json.loads", "medium")
    ex4 = EvalExample("read CSV", "use csv module", "hard")
    ex5 = EvalExample("connect to DB", "use sqlite3", "hard")
    assert ex1.difficulty == "easy"
    assert ex3.expected_behavior == "use json.loads"
    print("  EvalExample ......... OK")

    # EvalDataset basic
    ds = EvalDataset(
        train=[ex1, ex2, ex3],
        val=[ex4],
        holdout=[ex5],
    )
    assert len(ds.all_examples) == 5
    assert len(ds.train) == 3
    assert len(ds.holdout) == 1
    print("  EvalDataset basic ... OK")

    # EvalDataset save/load roundtrip
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        save_path = Path(tmpdir) / "test_ds"
        ds.save(save_path)
        loaded = EvalDataset.load(save_path)
        assert len(loaded.train) == 3
        assert len(loaded.val) == 1
        assert len(loaded.holdout) == 1
        assert loaded.train[0].task_input == ex1.task_input
    print("  EvalDataset save/load OK")

    # EvalDataset empty
    empty_ds = EvalDataset()
    assert len(empty_ds.all_examples) == 0
    print("  EvalDataset empty ... OK")

    print("\nAll s25 unit tests passed.")


if __name__ == "__main__":
    if "--gateway" in sys.argv:
        asyncio.run(run_gateway_console())
    elif "--simulate" in sys.argv:
        asyncio.run(run_gateway_simulated())
    elif "--test" in sys.argv:
        run_unit_tests()
    else:
        run_cli()
