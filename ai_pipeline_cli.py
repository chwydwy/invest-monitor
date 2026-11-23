"""Read-only metadata inspection for the separate analysis queue."""
import argparse
import json
from pathlib import Path
from ai_pipeline import Queue


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--release-job')
    actions.add_argument('--release-discovery',nargs=2,metavar=('ID','COMPANY'))
    actions.add_argument('--retry-output',nargs=2,metavar=('JOB','DESTINATION'))
    actions.add_argument('--resume-auth',action='store_true')
    parser.add_argument('--ack-duplicate-risk',action='store_true')
    args = parser.parse_args()
    if not Path(args.db).is_file():
        parser.error('analysis database does not exist; no database created')
    queue=Queue(args.db)
    with queue.db() as db:
        if args.release_job:
            db.execute("UPDATE jobs SET state='pending',due=0,reason='manual_release' WHERE id=? AND state='hold'",(args.release_job,))
        if args.release_discovery:
            row=db.execute('SELECT payload FROM discoveries WHERE id=? AND company=?',args.release_discovery).fetchone()
            if row:
                payload=json.loads(row[0]);payload.pop('_failures',None);payload.pop('_retry_at',None)
                db.execute("UPDATE discoveries SET state='pending',reason='manual_release',payload=? WHERE id=? AND company=?",(json.dumps(payload,ensure_ascii=False),*args.release_discovery))
        if args.retry_output:
            row=db.execute('SELECT state FROM outputs WHERE job=? AND destination=?',args.retry_output).fetchone()
            if row and row[0]=='uncertain' and not args.ack_duplicate_risk:
                parser.error('uncertain Telegram delivery requires --ack-duplicate-risk after checking prior delivery')
            db.execute("UPDATE outputs SET state='pending',attempts=0,due=0 WHERE job=? AND destination=? AND state IN ('hold','uncertain')",args.retry_output)
        if args.resume_auth:
            db.execute("DELETE FROM meta WHERE key='auth_hold'")
    print(json.dumps(queue.status(), ensure_ascii=False, indent=2))
