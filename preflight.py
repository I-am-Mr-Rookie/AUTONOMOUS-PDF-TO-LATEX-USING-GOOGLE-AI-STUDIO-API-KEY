"""Phase 1 — environment verification + dependency bootstrap (no Gemini requests).

Confirms the host can actually run the pipeline before any API budget is spent:
pip-installs any missing Python packages and locates the external binaries
pdflatex/latexmk and Poppler's pdftoppm (printing OS-specific install hints if
absent). The Gemini API key is NOT handled here — it is requested at runtime by
main.py after preflight passes, and never stored.
"""
from __future__ import annotations

import importlib
import platform
import shutil
import subprocess
import sys

from config import REQUIRED_PACKAGES, ensure_dirs

_INSTALL_HINTS = {
    "pdflatex": {
        "Windows": "Install MiKTeX (https://miktex.org) or TeX Live, then reopen the shell.",
        "Darwin": "brew install --cask mactex-no-gui   (or install the full MacTeX).",
        "Linux": "sudo apt-get install texlive-latex-extra latexmk   (Debian/Ubuntu).",
    },
    "pdftoppm": {
        "Windows": "winget install oschwartz10612.Poppler   then add its bin/ folder to PATH.",
        "Darwin": "brew install poppler",
        "Linux": "sudo apt-get install poppler-utils",
    },
}


def _install_missing_packages() -> bool:
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(import_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"  installing missing Python packages: {', '.join(missing)} …")
        res = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing], check=False
        )
        if res.returncode != 0:
            print("  ✗ Failed to run pip. Ensure pip is installed in this Python environment.")
        
        still_missing = []
        for import_name, pip_name in REQUIRED_PACKAGES.items():
            try:
                importlib.import_module(import_name)
            except ImportError:
                still_missing.append(pip_name)
        if still_missing:
            print(f"  ✗ Missing Python packages could not be installed: {', '.join(still_missing)}")
            return False
        else:
            print("  ✓ Python dependencies successfully installed.")
            return True
    else:
        print("  ✓ Python dependencies present.")
        return True


def _check_binary(name: str) -> bool:
    found = shutil.which(name)
    if found:
        print(f"  ✓ {name}: {found}")
        return True
    hint = _INSTALL_HINTS.get(name, {}).get(platform.system(), "Install it and add it to PATH.")
    print(f"  ✗ {name} NOT found — {hint}")
    return False



def run() -> bool:
    print(
        f"→ Phase 1: preflight on {platform.system()} {platform.release()} / "
        f"Python {platform.python_version()}"
    )
    ensure_dirs()
    has_deps = _install_missing_packages()

    # pdflatex OR latexmk is sufficient for compilation.
    has_tex = _check_binary("pdflatex")
    has_tex = _check_binary("latexmk") or has_tex
    has_poppler = _check_binary("pdftoppm")  # required by pdf2image for rasterizing

    ok = has_deps and has_tex and has_poppler
    print("✓ Preflight passed.\n" if ok else "✗ Preflight found blocking issues (see above).\n")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
