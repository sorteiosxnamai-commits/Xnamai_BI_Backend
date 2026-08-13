"""Export the current FastAPI contract without starting the server."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402


def main() -> None:
    output = ROOT / "openapi.json"
    output.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
