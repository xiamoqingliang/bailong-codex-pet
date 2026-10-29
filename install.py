"""Development-directory installer; release packages use the standalone copy."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'packaging'))
from compact_install import main
if __name__ == '__main__':
    raise SystemExit(main())
