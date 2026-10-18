"""Keep per-layer pull progress out of job logs.

`docker compose pull` writes a line for every progress tick of every image layer
("3f4ca6d5a1b2 Downloading 12.5MB", "… Extracting 4B", hundreds per pull). The
meaningful steps – "Image x Pulling", "Image x Pulled", the commands run and the
container lines – are kept. The full output is still used to spot errors such as
rate limits; only what's written to the log is filtered.
"""
import re

_LAYER_STATES = (
    "Pulling fs layer", "Waiting", "Downloading", "Download complete", "Verifying Checksum",
    "Extracting", "Pull complete", "Already exists",
)
_PROGRESS_RE = re.compile(
    r"^\s*[0-9a-f]{12}\s+(?:" + "|".join(re.escape(s) for s in _LAYER_STATES) + r")\b[^\n]*$"
)


def is_pull_progress(line: str) -> bool:
    return bool(_PROGRESS_RE.match(line.rstrip("\r\n")))


def strip_pull_progress(text: str) -> str:
    """Remove progress lines from a whole log (keeps line endings of everything else)."""
    return "".join(line for line in text.splitlines(keepends=True) if not is_pull_progress(line))


class ProgressFilter:
    """For output that arrives in arbitrary chunks: passes on whole lines that aren't
    progress, holding back a trailing partial line until it is complete."""

    def __init__(self) -> None:
        self._partial = ""

    def feed(self, text: str) -> str:
        lines = (self._partial + text).splitlines(keepends=True)
        self._partial = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
        return "".join(line for line in lines if not is_pull_progress(line))

    def flush(self) -> str:
        rest, self._partial = self._partial, ""
        return "" if is_pull_progress(rest) else rest
