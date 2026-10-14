"""Entry point that works from the repo root without installing:  python src/main.py <command>"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lexflow.cli import main  # noqa: E402

sys.exit(main())
