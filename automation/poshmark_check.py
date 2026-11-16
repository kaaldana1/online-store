"""Old name, kept so earlier commands still work. Everything now lives in poshmark.py.

    python automation/poshmark_check.py login      ==  python automation/poshmark.py login
    python automation/poshmark_check.py check URL  ==  python automation/poshmark.py check URL
"""
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.argv[0] = str(Path(__file__).with_name("poshmark.py"))
    runpy.run_path(sys.argv[0], run_name="__main__")
