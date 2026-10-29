"""Compatibility entry for the version-local v051 builder."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_v051 import main
if __name__ == "__main__":
    main()
