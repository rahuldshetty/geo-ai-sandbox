"""Entry script for the frozen bundle and for `python app.py`.

packaging/spatial-intelligence.spec points PyInstaller at this file, so it must
stay a plain top-level script with no package-relative imports.
"""

from spatial_intelligence.server import run

if __name__ == "__main__":
    run()
