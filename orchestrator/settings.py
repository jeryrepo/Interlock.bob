"""
orchestrator/settings.py
=========================
One place that reads the environment.

Interlock previously read `os.environ` from six different modules, which made it
impossible to answer "what does this deployment actually have configured?"
without grepping. That question matters now: every IBM integration is optional,
and the honest answer to "is watsonx wired up?" has to be checkable rather than
assumed.

Nothing here fails on missing configuration. Absent IBM credentials mean the
IBM features are off — not that Interlock is broken. The CLI, the MCP server,
the PR review and the deterministic gate all work with an empty environment.

`.env` is loaded if present, without adding a dependency: a five-line parser is
cheaper than python-dotenv for a file this simple, and it keeps the runtime
dependency list honest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_FILE = ".env"
_TRUTHY = {"1", "true", "yes", "on"}


def installation_root() -> Path:
    """The directory Interlock is installed in — where its own `.env` lives."""
    return Path(__file__).resolve().parent.parent


def _env_candidates(path: str | Path) -> list[Path]:
    """
    Where to look for `.env`, in order: the working directory, then Interlock's.

    A bare relative `.env` resolves against the process working directory, and
    for the MCP server that directory is *your* repository — `interlock init`
    writes `"cwd": "<your-repo>"` into `.bob/mcp.json` so the tools run against
    your services. The credentials, though, live in Interlock's own checkout,
    which is where `.gitignore` protects them.

    The result was that a correctly filled `.env` was read by the CLI and
    ignored by Bob: narration, the security model pass, LLM consumer discovery
    and `campaign --request` all silently off, with `interlock doctor` reporting
    them fine because doctor runs from the other directory.

    Falling back to the installation keeps the secrets in one gitignored place
    and makes them work from anywhere. A `.env` in the working directory still
    wins, so a per-repository override remains possible.
    """
    candidates = [Path(path)]
    if not Path(path).is_absolute():
        candidates.append(installation_root() / Path(path).name)
    return candidates


def load_dotenv(path: str | Path = _ENV_FILE) -> None:
    """
    Load `.env` into os.environ without overwriting anything already set.

    Real environment variables win over the file, which is what makes the same
    image behave correctly in a container where secrets arrive as env vars.

    Searches the working directory first, then Interlock's own installation —
    see `_env_candidates` for why the second one is load-bearing.
    """
    file = next((c for c in _env_candidates(path) if c.is_file()), None)
    if file is None:
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class WatsonxSettings:
    """watsonx.ai inference configuration. All optional."""

    api_key: str = ""
    url: str = "https://us-south.ml.cloud.ibm.com"
    project_id: str = ""
    space_id: str = ""
    model_id: str = "ibm/granite-4-h-small"
    max_new_tokens: int = 300
    api_version: str = "2024-10-08"
    iam_url: str = "https://iam.cloud.ibm.com/identity/token"
    narration_enabled: bool = False

    @property
    def configured(self) -> bool:
        """
        True only when a model call could actually succeed.

        watsonx.ai requires a project OR a space to scope the request, so a key
        alone is not enough. Returning False here is what makes narration
        degrade to silence instead of erroring at request time.
        """
        return bool(self.api_key) and bool(self.project_id or self.space_id)

    @property
    def enabled(self) -> bool:
        """Narration runs only when explicitly switched on AND configured."""
        return self.narration_enabled and self.configured

    def why_disabled(self) -> str | None:
        """A human-readable reason, for surfacing in `interlock doctor`."""
        if not self.narration_enabled:
            return "INTERLOCK_ENABLE_NARRATION is not set to 1"
        if not self.api_key:
            return "IBM_CLOUD_API_KEY is not set"
        if not (self.project_id or self.space_id):
            return "neither WATSONX_PROJECT_ID nor WATSONX_SPACE_ID is set"
        return None


@dataclass(frozen=True)
class OrchestrateSettings:
    """watsonx Orchestrate configuration. All optional."""

    instance_url: str = ""
    api_key: str = ""
    env_name: str = "hackathon"
    external_agent_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.instance_url and self.api_key)

    @property
    def external_agent_enabled(self) -> bool:
        """
        The /chat/completions endpoint serves only when a key is configured.

        It refuses rather than defaulting to open: an unauthenticated endpoint
        that runs test suites against a repository is a remote code execution
        surface, not a convenience.
        """
        return bool(self.external_agent_key)


@dataclass(frozen=True)
class Settings:
    db_path: str = "interlock.db"
    api_url: str = "http://localhost:8000"
    workspace: str = ".interlock_work"
    components_root: str = "fixtures"
    watsonx: WatsonxSettings = WatsonxSettings()
    orchestrate: OrchestrateSettings = OrchestrateSettings()


def load(env_file: str | Path = _ENV_FILE) -> Settings:
    """Read settings from the environment, loading `.env` first if present."""
    load_dotenv(env_file)
    return Settings(
        db_path=os.environ.get("INTERLOCK_DB_PATH", "interlock.db"),
        api_url=os.environ.get("ORCHESTRATOR_API_URL")
        or os.environ.get("INTERLOCK_API_URL", "http://localhost:8000"),
        workspace=os.environ.get("INTERLOCK_WORKSPACE", ".interlock_work"),
        components_root=os.environ.get("INTERLOCK_COMPONENTS_ROOT", "fixtures"),
        watsonx=WatsonxSettings(
            api_key=os.environ.get("IBM_CLOUD_API_KEY", ""),
            url=os.environ.get("WATSONX_URL", "https://us-south.ml.cloud.ibm.com"),
            project_id=os.environ.get("WATSONX_PROJECT_ID", ""),
            space_id=os.environ.get("WATSONX_SPACE_ID", ""),
            model_id=os.environ.get("WATSONX_MODEL_ID", "ibm/granite-4-h-small"),
            max_new_tokens=_int("WATSONX_MAX_NEW_TOKENS", 300),
            api_version=os.environ.get("WATSONX_API_VERSION", "2024-10-08"),
            iam_url=os.environ.get(
                "IBM_CLOUD_IAM_URL", "https://iam.cloud.ibm.com/identity/token"
            ),
            narration_enabled=_flag("INTERLOCK_ENABLE_NARRATION"),
        ),
        orchestrate=OrchestrateSettings(
            instance_url=os.environ.get("WATSONX_ORCHESTRATE_INSTANCE_URL", ""),
            api_key=os.environ.get("WATSONX_ORCHESTRATE_API_KEY", ""),
            env_name=os.environ.get("WATSONX_ORCHESTRATE_ENV", "hackathon"),
            external_agent_key=os.environ.get("INTERLOCK_EXTERNAL_AGENT_KEY", ""),
        ),
    )
