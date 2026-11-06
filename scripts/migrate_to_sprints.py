"""One-off: convert weekly "Done ..." columns into closed sprints and open a first active sprint.

    PERKAN_DATA_DIR=/path/to/data python scripts/migrate_to_sprints.py            # dry run (rolled back)
    PERKAN_DATA_DIR=/path/to/data python scripts/migrate_to_sprints.py --apply    # write it

Back up the database first. See db.migrate_to_sprints for the rules.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import app  # noqa: E402,F401  (creates/upgrades the database on import)
import db  # noqa: E402


class _Rollback(Exception):
    pass


def main():
    apply = '--apply' in sys.argv
    report = None
    try:
        with db.write() as conn:
            report = db.migrate_to_sprints(conn)
            if not apply:
                raise _Rollback()
    except _Rollback:
        pass
    print(json.dumps(report, indent=2))
    print('APPLIED' if apply else 'DRY RUN: nothing was written (pass --apply to write)')


if __name__ == '__main__':
    main()
