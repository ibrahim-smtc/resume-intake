"""The deployment files agree with each other and with the code: Vercel installs from pyproject.toml (its dependency list must be
there, or the build fails with "No `project` table"), requirements.txt repeats it for pip, and the entrypoint it names exists."""
import importlib
import json
import re
import tomllib

import harness

harness.isolate()
from harness import ROOT, check, finish  # noqa: E402

pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def names(specs):
    return sorted(re.split(r"[<>=!~\s\[]", s.strip(), maxsplit=1)[0].lower() for s in specs)


def requirement_lines(path):
    return [re.sub(r"\s+#.*$", "", line).strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith(("#", "-r"))]


project = pyproject.get("project", {})
check("pyproject.toml has a [project] table with a name, a version and the Python it needs", bool(project.get("name") and project.get("version") and project.get("requires-python")), list(project))
check("...and its dependencies (Vercel installs from here)", len(project.get("dependencies", [])) >= 6, project.get("dependencies"))
check("requirements.txt lists exactly the same packages and version ranges",
      sorted(project["dependencies"]) == sorted(requirement_lines(ROOT / "requirements.txt")),
      (sorted(project["dependencies"]), sorted(requirement_lines(ROOT / "requirements.txt"))))
check("requirements-dev.txt builds on requirements.txt", "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8"))
check("the Python version in .python-version satisfies requires-python", (ROOT / ".python-version").read_text().strip().startswith("3.12") and project["requires-python"] == ">=3.12")
check("the app is an application, not a library (uv only installs its dependencies)", pyproject["tool"]["uv"]["package"] is False)

module, _, attribute = pyproject["tool"]["vercel"]["entrypoint"].partition(":")
check("the Vercel entrypoint names a FastAPI app that really exists", hasattr(importlib.import_module(module), attribute), pyproject["tool"]["vercel"])
vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
check("vercel.json configures the function of that same file", module.replace(".", "/") + ".py" in vercel["functions"], vercel)

ignore = (ROOT / ".vercelignore").read_text(encoding="utf-8").split()
gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
check("secrets and local data are never uploaded or committed",
      all(x in ignore for x in (".env", "data", ".venv", "tools")) and all(x in gitignore for x in (".env", "data/", ".venv/", "tools/")), (ignore, gitignore))
check("the internal documents of this public repository are not committed", "sources/" in gitignore and "docs/notes.md" in gitignore)
finish()
