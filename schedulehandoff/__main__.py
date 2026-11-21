import argparse
import json
import tempfile
from pathlib import Path

from . import changes, operations, pickup, planner, report

COMMANDS = ['room-import', 'substitute-assign', 'notice-evaluate', 'substitute-withdraw',
            'notice-resend', 'plan-reschedule', 'approve-reschedule', 'pickup-import',
            'pickup-merge']


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('demo')
    for command in COMMANDS:
        arg = sub.add_parser(command)
        arg.add_argument('--db', required=True)
        nargs = '+' if command == 'pickup-merge' else None
        arg.add_argument('file' if nargs is None else 'files', nargs=nargs)
    arg = sub.add_parser('report')
    arg.add_argument('--db', required=True)
    arg.add_argument('--out', required=True)
    x = parser.parse_args()
    if x.command == 'demo':
        root = Path(tempfile.mkdtemp(prefix=__package__ + '-'))
        result = operations.demo(root)
        print(json.dumps(result, ensure_ascii=False))
        print(report.build(root / 'ledger.db', root / 'report.json'))
    elif x.command == 'report':
        print(report.build(x.db, x.out))
    elif x.command == 'pickup-merge':
        print(json.dumps(pickup.run_many(x.db, x.files), ensure_ascii=False))
    else:
        fn = [operations.first, operations.second, operations.third, changes.withdraw,
              changes.resend, planner.plan, planner.approve, pickup.run][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db, x.file), ensure_ascii=False))


if __name__ == '__main__':
    main()
