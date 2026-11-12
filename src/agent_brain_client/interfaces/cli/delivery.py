"""Verified detached delivery entry; arguments never carry message content."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / 'src'), str(_ENTRY_ROOT)]


def main():
    import argparse
    from agent_brain_client.runtime.delivery import run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device-home', type=Path, required=True)
    args = parser.parse_args()
    run(args.device_home)
    return 0


if __name__ == '__main__':
    sys.exit(main())
