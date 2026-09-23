#!/usr/bin/env python3
"""Sequential SGF batch runner, filtered by SGF DT (not filename)."""
import argparse
from collections import Counter
from pathlib import Path
import re
import subprocess
import sys

from sgfmill import sgf

PROJECT = Path(__file__).resolve().parents[1]


def earliest_year(dt):
    """Conservatively handle SGF full and abbreviated dates; reject unknown dates."""
    years = []
    for part in dt.strip().split(','):
        part = part.strip()
        if re.fullmatch(r'\d{4}(?:-\d{2}){0,2}', part):
            years.append(int(part[:4]))
        elif years and re.fullmatch(r'\d{2}(?:-\d{2})?', part):
            pass  # Abbreviated date inherits its preceding year.
        else:
            return None
    return min(years) if years else None


def select_games(directory, min_year, limit):
    stats = Counter()
    selected = []
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or path.suffix.lower() != '.sgf':
            continue
        try:
            root = sgf.Sgf_game.from_bytes(path.read_bytes()).get_root()
            dt = root.get('DT') if root.has_property('DT') else ''
            year = earliest_year(dt)
        except (ValueError, OSError) as exc:
            stats['unreadable'] += 1
            print(f'Skip unreadable: {path}: {exc}', file=sys.stderr)
            continue
        if year is None:
            stats['unknown_date'] += 1
        elif year < min_year:
            stats['before_min_year'] += 1
        else:
            selected.append((path, dt))
            if limit is not None and len(selected) >= limit:
                break
    stats['selected'] = len(selected)
    return selected, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', nargs='?', type=Path, default=PROJECT / 'games')
    parser.add_argument('--min-year', type=int, default=2000, help='Earliest SGF year, inclusive (default: 2000)')
    parser.add_argument('--max-games', type=int, help='Maximum games to run; omitted means unlimited')
    parser.add_argument('--config', type=Path, default=PROJECT / 'configs/find-atari.json')
    parser.add_argument('--output-dir', type=Path, default=PROJECT / 'outputs')
    parser.add_argument('--dry-run', action='store_true', help='List eligible games without running KataGo')
    args = parser.parse_args()
    if not 1 <= args.min_year <= 9999:
        parser.error('--min-year must be in 1..9999')
    if args.max_games is not None and args.max_games < 1:
        parser.error('--max-games must be positive')
    if not args.directory.is_dir():
        parser.error('SGF directory does not exist')
    # Validate the shared mining configuration before launching any games.
    from find_atari import load_config
    load_config(args.config)
    selected, stats = select_games(args.directory, args.min_year, args.max_games)
    print(dict(stats), flush=True)
    if args.dry_run:
        for index, (path, dt) in enumerate(selected, 1):
            print(f'[{index}/{len(selected)}] DT={dt}: {path}', flush=True)
        return 0

    failed = 0
    for index, (path, dt) in enumerate(selected, 1):
        print(f'[{index}/{len(selected)}] DT={dt}: {path}', flush=True)
        result = subprocess.run([sys.executable, str(PROJECT / 'scripts/find_atari.py'), str(path),
                                 '--config', str(args.config), '--resume-root', str(args.output_dir)],
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            failed += 1
            print(f'Failed ({result.returncode}), continuing: {path}', file=sys.stderr)
    print(f'Batch complete: {len(selected)-failed} completed/skipped, {failed} failed -> {args.output_dir / "games"}')
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
