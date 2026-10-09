"""Offline structural validation for the HROne assignment repository.

Run from the repository root: python validate_submission.py
This does not test MongoDB connectivity, aggregation semantics, or HTTP behavior.
"""
from pathlib import Path
import ast
import sys

ROOT = Path(__file__).resolve().parent
REQUIRED = [
    "app/main.py",
    "requirements.txt",
    "README.md",
    "REVIEW.md",
    "DECISIONS.md",
]
FORBIDDEN = [".env", "Dockerfile"]
errors = []

for rel in REQUIRED:
    if not (ROOT / rel).is_file():
        errors.append(f"Missing required file: {rel}")

for rel in FORBIDDEN:
    if (ROOT / rel).exists():
        errors.append(f"Forbidden file present: {rel}")

source_path = ROOT / "app/main.py"
if source_path.exists():
    try:
        source = source_path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(source_path))
        print("PASS: app/main.py parses as Python")
    except (SyntaxError, UnicodeDecodeError) as exc:
        errors.append(f"Python syntax validation failed: {exc}")

if errors:
    for err in errors:
        print(f"FAIL: {err}")
    sys.exit(1)

for rel in REQUIRED:
    print(f"PASS: required file exists: {rel}")
for rel in FORBIDDEN:
    print(f"PASS: forbidden file absent: {rel}")
print("\nOffline structural checks passed.")
print("This script does NOT establish runtime or MongoDB integration correctness.")
