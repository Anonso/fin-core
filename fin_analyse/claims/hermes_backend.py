"""Hermes Agent backend for LLM claim extraction.

This backend delegates LLM calls to the local Hermes agent via subprocess,
allowing fin-analyse to use the Kimi Coding Plan API key that Hermes already
has access to.

Usage:
    from fin_analyse.claims.hermes_backend import HermesBackend
    from fin_analyse.claims.llm_extractor import LLMClaimExtractor

    backend = HermesBackend(model="kimi-for-coding")
    extractor = LLMClaimExtractor(backend=backend)
    claims = extractor.extract(evidence)
"""

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from .llm_extractor import LLMBackend


class HermesBackend:
    """Delegate LLM calls to the local Hermes agent.

    Hermes must be installed and configured with a valid API key.
    The key is read from Hermes's profile .env, not from the environment.
    """

    def __init__(
        self,
        model: str = "kimi-for-coding",
        profile: str = "fin",
        timeout: int = 600,
    ):
        self.model = model
        self.profile = profile
        self.timeout = timeout
        self._hermes_bin = self._find_hermes()

    def _find_hermes(self) -> str:
        """Locate the hermes binary."""
        # Try common locations
        candidates = [
            os.path.expanduser("~/.local/bin/hermes"),
            "/usr/local/bin/hermes",
            "/usr/bin/hermes",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        # Try PATH
        result = subprocess.run(["which", "hermes"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        raise RuntimeError(
            "Hermes binary not found. Install Hermes first: "
            "https://github.com/NousResearch/hermes-agent"
        )

    def _read_hermes_env(self) -> dict[str, str]:
        """Read API keys from Hermes profile .env file.

        Uses `hermes config env-path` to find the correct location,
        respecting the HERMES_HOME / profile layout.
        """
        import subprocess

        # Ask hermes CLI where .env is
        result = subprocess.run(
            [self._hermes_bin, "config", "env-path"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            env_path = Path(result.stdout.strip())
        else:
            # Fallback to hardcoded path
            env_path = Path.home() / ".hermes" / "profiles" / self.profile / ".env"

        env = {}
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        key, val = line.split("=", 1)
                        env[key] = val
        return env

    def complete(self, prompt: str) -> str:
        """Send prompt to Hermes and return the response.

        Uses `hermes chat -q` for non-interactive one-shot queries.
        """
        hermes_env = self._read_hermes_env()

        # Build the command
        cmd = [
            self._hermes_bin,
            "chat",
            "-q",
            prompt,
            "-m",
            self.model,
            "-Q",  # quiet mode
        ]

        # Merge environment: Hermes .env + current env
        env = {**os.environ, **hermes_env}

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
            if result.returncode != 0:
                stderr = result.stderr[:500] if result.stderr else ""
                return f"[] // ERROR: Hermes exited {result.returncode}: {stderr}"
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return "[] // ERROR: Hermes timeout"
        except Exception as e:
            return f"[] // ERROR: {e}"

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_executor=None,
        max_turns: int = 10,
    ) -> str:
        """Multi-turn conversation with tool calling (not supported by Hermes)."""
        raise NotImplementedError("Hermes backend does not support tool calling")


class HermesFileBackend:
    """Alternative backend using file-based communication with Hermes.

    More robust for long prompts: writes prompt to a temp file,
    asks Hermes to process it, reads result from output file.
    """

    def __init__(
        self,
        model: str = "kimi-for-coding",
        profile: str = "fin",
        timeout: int = 600,
    ):
        self.model = model
        self.profile = profile
        self.timeout = timeout
        self._hermes_bin = self._find_hermes()

    def _find_hermes(self) -> str:
        candidates = [
            os.path.expanduser("~/.local/bin/hermes"),
            "/usr/local/bin/hermes",
            "/usr/bin/hermes",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
        result = subprocess.run(["which", "hermes"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        raise RuntimeError("Hermes binary not found")

    def _read_hermes_env(self) -> dict[str, str]:
        env_path = Path.home() / ".hermes" / "profiles" / self.profile / ".env"
        if not env_path.exists():
            env_path = Path.home() / ".hermes" / ".env"
        env = {}
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        key, val = line.split("=", 1)
                        env[key] = val
        return env

    def complete(self, prompt: str) -> str:
        """Write prompt to file, ask Hermes to process, read result."""
        hermes_env = self._read_hermes_env()
        env = {**os.environ, **hermes_env}

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            input_path = f.name

        output_path = input_path + ".out"

        # Construct a query that reads the file and writes output
        query = (
            f"Read the file at {input_path}. It contains a prompt for you. "
            f"Process it and write ONLY your raw response (no markdown, no explanations) "
            f"to {output_path}. Do not include any other text in your response."
        )

        cmd = [
            self._hermes_bin,
            "chat",
            "-q",
            query,
            "-m",
            self.model,
            "-Q",
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
            if result.returncode != 0:
                stderr = result.stderr[:500] if result.stderr else ""
                return f"[] // ERROR: {stderr}"

            # Read output file
            if Path(output_path).exists():
                with open(output_path, encoding="utf-8") as f:
                    return f.read().strip()
            return result.stdout.strip()

        except subprocess.TimeoutExpired:
            return "[] // ERROR: timeout"
        except Exception as e:
            return f"[] // ERROR: {e}"
        finally:
            # Cleanup
            for p in [input_path, output_path]:
                with contextlib.suppress(Exception):
                    Path(p).unlink(missing_ok=True)

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_executor=None,
        max_turns: int = 10,
    ) -> str:
        """Multi-turn conversation with tool calling (not supported by Hermes)."""
        raise NotImplementedError("Hermes file backend does not support tool calling")


def create_hermes_backend(
    model: str = "kimi-for-coding",
    profile: str = "fin",
    use_file_mode: bool = False,
) -> LLMBackend:
    """Factory function to create a Hermes backend.

    Args:
        model: Model name to use (must be available in Hermes config)
        profile: Hermes profile name (default: "fin")
        use_file_mode: Use file-based communication for long prompts
    """
    if use_file_mode:
        return HermesFileBackend(model=model, profile=profile)
    return HermesBackend(model=model, profile=profile)
