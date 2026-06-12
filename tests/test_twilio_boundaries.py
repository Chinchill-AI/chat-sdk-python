"""Port of adapter-twilio/src/{api,format,voice,webhook}/boundary.test.ts.

Upstream keeps the ``api`` / ``format`` / ``voice`` / ``webhook`` subpaths
runtime-light: they must not import the full adapter ("../index") or the
``twilio`` npm package. The Python analog enforced here:

- no module in ``chat_sdk.adapters.twilio`` imports ``aiohttp`` at the top
  level (hazard #10: optional deps stay lazy — the upstream analog of
  keeping subpaths free of runtime-heavy imports);
- no module imports the official ``twilio`` SDK (the port hand-rolls the
  REST calls to mirror upstream's explicit no-`twilio`-dependency choice);
- the helper modules never import ``.adapter`` (upstream's "../index" rule),
  so voice/webhook/api helpers stay usable without the adapter.
"""

from __future__ import annotations

import ast
from pathlib import Path

import chat_sdk.adapters.twilio as twilio_pkg

TWILIO_PKG_DIR = Path(twilio_pkg.__file__).parent
TWILIO_MODULES = sorted(TWILIO_PKG_DIR.glob("*.py"))


def _top_level_imports(path: Path) -> set[str]:
    """Module names imported at the top level (lazy imports excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _imports_package(imports: set[str], package: str) -> bool:
    return any(name == package or name.startswith(f"{package}.") for name in imports)


class TestTwilioImportBoundaries:
    """Runtime-light import boundaries for the Twilio package."""

    def test_discovers_the_twilio_modules(self):
        # Guard the globbing itself: if the package moves, the boundary
        # tests below would silently assert over an empty list.
        names = {p.name for p in TWILIO_MODULES}
        assert "__init__.py" in names
        assert "types.py" in names
        assert "format_converter.py" in names

    def test_no_module_imports_aiohttp_at_top_level(self):
        offenders = [p.name for p in TWILIO_MODULES if _imports_package(_top_level_imports(p), "aiohttp")]
        assert offenders == []

    def test_no_module_imports_the_twilio_sdk(self):
        offenders = [p.name for p in TWILIO_MODULES if _imports_package(_top_level_imports(p), "twilio")]
        assert offenders == []

    def test_helper_modules_do_not_import_the_adapter(self):
        adapter_module = "chat_sdk.adapters.twilio.adapter"
        offenders = [
            p.name
            for p in TWILIO_MODULES
            if p.name not in ("__init__.py", "adapter.py") and _imports_package(_top_level_imports(p), adapter_module)
        ]
        assert offenders == []
