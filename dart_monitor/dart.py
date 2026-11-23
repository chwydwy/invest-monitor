"""DART collection entry point; all AI calls go through the shared worker."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if __name__ == '__main__':
    from pipeline_runtime import main
    main(default_role='dart')
