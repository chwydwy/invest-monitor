"""Explicit one-time cleanup. Caller must stop collectors/preprocess/AI/deliver first."""
import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from dashboard_sender import exclusive_lock
from ai_pipeline import Queue


def counts(db):
    return dict(jobs=[dict(r) for r in db.execute('SELECT source,state,count(*) n FROM jobs GROUP BY source,state')],
        discoveries=[dict(r) for r in db.execute("SELECT coalesce(json_extract(payload,'$.source'),'news') source,state,count(*) n FROM discoveries GROUP BY 1,2")],
        outputs=[dict(r) for r in db.execute('SELECT destination,state,count(*) n FROM outputs GROUP BY destination,state')])


def retire(queue, cutoff):
    """Conserve completed results/outputs/history. Scrub only unanalysed news IDs."""
    with queue.db() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM meta WHERE key='news_title_cutover'").fetchone():
            raise ValueError('cutover already recorded; do not retire new work')
        before=counts(db)
        completed={r[0][5:] for r in db.execute("SELECT id FROM jobs WHERE source='news' AND state='complete'")}
        targets={}
        for r in db.execute("SELECT * FROM jobs WHERE source='news' AND state IN ('pending','running','hold') AND created<=?",(cutoff,)):
            # Never retire a job with an existing delivery intent, even if an
            # inconsistent state suggests it was not completed.
            if db.execute('SELECT 1 FROM outputs WHERE job=?',(r['id'],)).fetchone():continue
            p=json.loads(r['payload']);targets[r['id'][5:]]=p.get('event',{}).get('url')
        for r in db.execute("SELECT * FROM discoveries WHERE coalesce(json_extract(payload,'$.source'),'news')='news' AND (first_seen IS NULL OR first_seen<=?)",(cutoff,)):
            p=json.loads(r['payload']);identifier=p.get('id',r['id'].removeprefix('news:'))
            if identifier in completed:continue
            job=db.execute('SELECT state FROM jobs WHERE id=?',('news:'+identifier,)).fetchone()
            if job and identifier not in targets:continue
            if r['state'] in ('pending','running','hold'):
                targets.setdefault(identifier,None)
        for identifier,url in targets.items():
            db.execute('INSERT OR IGNORE INTO news_tombstones VALUES (?,?,?,?)',(identifier,url,'pre_title_cutover_unanalysed',cutoff))
            # Retain an auditable minimal row; no future model reservation.
            db.execute("UPDATE jobs SET state='retired',payload='{}',result=NULL,reason='pre_title_cutover_unanalysed',due=0 WHERE id=? AND source='news' AND state!='complete'",('news:'+identifier,))
            minimal=json.dumps(dict(id=identifier,source='news'))
            db.execute("UPDATE discoveries SET state='retired',payload=?,reason='pre_title_cutover_unanalysed' WHERE id IN (?,?)",(minimal,identifier,'news:'+identifier))
            db.execute('DELETE FROM meta WHERE key=?',('body:'+identifier,))
        # Interrupted attempts still consume today's budget; never refund them.
        db.execute("UPDATE attempts SET outcome='retired_cutover_unknown' WHERE outcome='running' AND job IN (SELECT id FROM jobs WHERE state='retired' AND source='news')")
        db.execute('INSERT INTO meta VALUES (?,?)',('news_title_cutover',json.dumps(datetime.fromtimestamp(cutoff,timezone.utc).isoformat())))
        return dict(cutoff=datetime.fromtimestamp(cutoff,timezone.utc).isoformat(),retired_ids=len(targets),before=before,after=counts(db))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--settings',required=True)
    parser.add_argument('--backup-dir',required=True);parser.add_argument('--stopped-confirmed',action='store_true')
    args=parser.parse_args()
    if not args.stopped_confirmed:parser.error('stop news/preprocess/ai/deliver before cleanup')
    for role in ('news','preprocess','ai','deliver'):
        state=subprocess.run(['systemctl','is-active','monitor-pipeline@'+role+'.service'],capture_output=True,text=True).stdout.strip()
        if state!='inactive':parser.error('required service is not stopped: '+role)
    settings=json.loads(Path(args.settings).read_text())
    backup=Path(args.backup_dir);backup.mkdir(mode=0o700,parents=True,exist_ok=False)
    # Locks fence active workers as well as the required service stops.
    path=settings['ai_db']
    with exclusive_lock(path+'.role-news'),exclusive_lock(path+'.preprocess'),exclusive_lock(path+'.worker'),exclusive_lock(path+'.delivery'):
        for key in ('ai_db','dashboard_db'):
            src=sqlite3.connect(Path(settings[key]).as_uri()+'?mode=ro',uri=True)
            dst=sqlite3.connect(backup/(key+'.sqlite3'));src.backup(dst);src.close();dst.close()
            (backup/(key+'.sqlite3')).chmod(0o600)
        queue=Queue(path,limits=settings.get('limits'))
        result=retire(queue,queue.clock())
        (backup/'cutover.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        (backup/'cutover.json').chmod(0o600)
        print(json.dumps(result,ensure_ascii=False))
