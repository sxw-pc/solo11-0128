import argparse,json,tempfile
from pathlib import Path
from . import operations,report,changes,planner,power
COMMANDS=['room-import', 'substitute-assign', 'notice-evaluate','substitute-withdraw','notice-resend','plan-reschedule','approve-reschedule','room-correct','power-import','power-topology','power-fault','power-service','power-plan','power-execution','power-correct','power-proof']
def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('demo')
    sub.add_parser('power-demo')
    for command in COMMANDS:
        arg=sub.add_parser(command);arg.add_argument('--db',required=True);arg.add_argument('file')
    arg=sub.add_parser('report');arg.add_argument('--db',required=True);arg.add_argument('--out',required=True)
    x=parser.parse_args()
    if x.command=='demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        result=operations.demo(root)
        print(json.dumps(result,ensure_ascii=False))
        print(report.build(root/'ledger.db',root/'report.json'))
    elif x.command=='power-demo':
        root=Path(tempfile.mkdtemp(prefix=__package__+'-'))
        print(json.dumps(power.demo(root),ensure_ascii=False))
    elif x.command=='report':print(report.build(x.db,x.out))
    else:
        fn=[operations.first,operations.second,operations.third,changes.withdraw,changes.resend,
            planner.plan,planner.approve,operations.correct,power.run,power.topology,
            power.fault,power.service,power.plan,power.execution,power.correct,
            power.proof][COMMANDS.index(x.command)]
        print(json.dumps(fn(x.db,x.file),ensure_ascii=False))
if __name__=='__main__':main()
