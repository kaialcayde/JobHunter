"""Load browser-side JavaScript assets from the automation package."""

from functools import lru_cache
from pathlib import Path


SCRIPT_ROOT = Path(__file__).resolve().parent
_MISSING = object()


@lru_cache(maxsize=None)
def load_script(relative_path: str) -> str:
    """Return the contents of a browser script file.

    Scripts should contain a single function expression compatible with
    Playwright's page.evaluate(script, arg) signature.
    """
    script_path = (SCRIPT_ROOT / relative_path).resolve()
    if SCRIPT_ROOT not in script_path.parents:
        raise ValueError(f"Script path escapes browser script root: {relative_path}")
    return script_path.read_text(encoding="utf-8").strip()


def evaluate_script(target, relative_path: str, arg=_MISSING):
    """Evaluate a cached browser script on a Playwright page/frame."""
    script = load_script(relative_path)
    if arg is _MISSING:
        return target.evaluate(script)
    return target.evaluate(script, arg)
