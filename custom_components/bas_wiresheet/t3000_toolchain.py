"""Runs T3000's own Control Basic compiler and interpreter (built from T3000 commit e2cc801d)."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

BIN_DIR = Path(__file__).parent / "bin"
MAX_CODE = 2000
TIMEOUT_S = 5


class ToolchainError(Exception):
    """The compiler rejected the program or could not run."""


def _tool(name: str) -> str:
    path = BIN_DIR / name
    if not path.is_file():
        raise ToolchainError(f"T3000 tool {name} is not installed")
    if not os.access(path, os.X_OK):
        path.chmod(0o755)  # file shares and archive tools can drop the execute bit
    return str(path)


def _run(tool: str, args: list[str], data: bytes) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([_tool(tool), *args], input=data, capture_output=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ToolchainError(f"{tool} failed to run: {err}") from err


def compile_source(source: str) -> bytes:
    res = _run("t3000c", ["compile"], source.encode("ascii"))
    out = res.stdout.decode("latin-1")
    if res.returncode != 0 or not out.startswith("OK "):
        detail = out.split("\n", 1)[1].strip() if out.startswith("ERR") and "\n" in out else f"exit {res.returncode}"
        raise ToolchainError(f"Control Basic compile error: {detail}")
    head, body = out.split("\n", 2)[:2]
    code = bytes.fromhex(body.strip())
    if len(code) != int(head.split()[1]):
        raise ToolchainError("Compiler output length mismatch")
    if len(code) >= MAX_CODE:
        raise ToolchainError(f"Program is {len(code)} bytes; the controller limit is below {MAX_CODE}")
    return code


def decompile(code: bytes) -> str:
    res = _run("t3000c", ["decompile"], code.hex().encode())
    if res.returncode != 0:
        raise ToolchainError(f"T3000 cannot disassemble this program (exit {res.returncode})")
    return res.stdout.decode("latin-1")


def build(source: str) -> dict:
    """Compile, then require decompile + recompile to give identical bytes."""
    code = compile_source(source)
    text = decompile(code)
    again = compile_source(text)
    if again != code:
        raise ToolchainError("Round-trip check failed: T3000's listing of this program would compile to different bytes")
    return {"code": code, "code_hex": code.hex(), "length": len(code), "sha256": hashlib.sha256(code).hexdigest(), "listing": text}


def simulate(code: bytes, script: str, workdir: str) -> list[str]:
    """Run byte code in T3000's interpreter. Script commands: set VAR n v / scan count ms / get VAR n / writes."""
    path = Path(workdir) / f"prog-{hashlib.sha256(code).hexdigest()[:12]}.hex"
    path.write_text(code.hex())
    res = _run("t3000vm", [str(path)], script.encode())
    if res.returncode != 0:
        raise ToolchainError(f"Interpreter failed (exit {res.returncode}): {res.stdout.decode('latin-1')[-200:]}")
    return res.stdout.decode().splitlines()
