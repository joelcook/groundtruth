"""groundtruth: give your LLM a single source of truth."""

import warnings

# The MCP SDK's settings model trips a harmless pydantic-settings warning on import.
warnings.filterwarnings("ignore", message=".*has an incomplete definition.*")

__version__ = "2.0.0"
