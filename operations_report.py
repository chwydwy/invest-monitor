"""Read-only pipeline snapshot and latest-only half-hour status reporting."""
import argparse
import json
import os
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from ai_worker import retry_seconds
from dashboard_sender import exclusive_lock

KST=timezone(timedelta(hours=9))
METRICS=('collected','ai_completed','excluded','failed','held')
BUCKETS=('news','dart','retry')


def iso(at):
    return datetime.fromtimestamp(at,timezone.utc).isoformat(timespec='milliseconds') if at is not None else None


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    db.row_factory=sqlite3.Row
    db.execute('BEGIN')
    return db


def delivery_counts(rows, dashboard=False):
    result=dict(delivered=0,pending=0,retry=0,hold=0,uncertain=0,conflict=0,failed=None)
    for row in rows:
        state=row['state'];n=row['n']
        if state in ('delivered','uncertain','conflict'):result[state]+=n
        elif state.startswith('hold') or state=='hold':result['hold']+=n
        elif state in ('pending','running'):
            result['retry' if row['attempts']>0 else 'pending']+=n
    return result


def snapshot(settings, now=None):
    now=now if now is not None else datetime.now(timezone.utc).timestamp()
    local=datetime.fromtimestamp(now,KST);utc=datetime.fromtimestamp(now,timezone.utc)
    report=dict(schema_version=1,report_id='pi-status-'+str(uuid.uuid4()),reported_at=iso(now),
        stale_after_seconds=4500,today_kst=dict(date=local.date().isoformat(),
        sources={s:{m:None for m in METRICS} for s in ('dart','news')}),
        ai_budget=None,queue=dict(waiting=None,running=None,held=None,failed=None,oldest_wait_seconds=None,state='unknown'),
        last_success={k:None for k in ('dart_collect','news_collect','preprocess','ai','dashboard_enqueue','dashboard_stored','telegram')},
        delivery=dict(dashboard_enqueue=None,dashboard_stored=None,telegram=None),issues=[])
    issues=report['issues']
    try:
        with closing(readonly(settings['ai_db'])) as db:
            keys=('operations_metrics_started','operations_metrics_fault','budget_baseline','auth_hold','paused_until','pause_reason')
            meta={r['key']:json.loads(r['value']) for r in db.execute('SELECT key,value FROM meta WHERE key IN ('+','.join('?' for _ in keys)+')',keys)}
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            begin=local.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            covered=(meta.get('operations_metrics_started') is not None and meta['operations_metrics_started']<=begin
                     and meta.get('operations_metrics_fault',0)<begin and 'operations_daily' in tables)
            if covered:
                for s in ('dart','news'):
                    counts=dict(db.execute('SELECT metric,n FROM operations_daily WHERE day=? AND source=?',(local.date().isoformat(),s)))
                    report['today_kst']['sources'][s]={m:counts.get(m,0) for m in METRICS}
            else:
                issues.append(dict(source='pi',stage='activity',state='partial_day',message='오늘 전체의 전이 시각을 복원할 수 없어 당일 처리 수는 미확인입니다. 집계 시작 후 다음 한국 날짜부터 제공합니다.'))
            if 'operations_success' in tables:
                for row in db.execute('SELECT stage,at FROM operations_success'):
                    if row['stage'] in report['last_success']:report['last_success'][row['stage']]=iso(row['at'])
            if 'operations_errors' in tables:
                successes=dict(db.execute('SELECT stage,at FROM operations_success'))
                for row in db.execute('SELECT stage,source,at,code FROM operations_errors'):
                    if row['at']>successes.get(row['stage'],0):
                        issues.append(dict(source=row['source'],stage=row['stage'],state='error',message='최근 작업 실패 종류: '+row['code']))
            if not report['last_success']['news_collect']:
                report['last_success']['news_collect']=iso(db.execute('SELECT max(last_success) FROM scans').fetchone()[0])
            local_usage={k:0 for k in BUCKETS}
            if settings.get('limits',{}).get('total',900)!=900:
                raise ValueError('report contract requires the existing 900 limit')
            local_usage.update(dict(db.execute('SELECT bucket,count(*) FROM attempts WHERE day=? GROUP BY bucket',(utc.date().isoformat(),))))
            baseline=meta.get('budget_baseline',{})
            baseline={k:baseline.get(k,0) for k in BUCKETS} if baseline.get('day')==utc.date().isoformat() else {k:0 for k in BUCKETS}
            usage={k:local_usage[k]+baseline[k] for k in BUCKETS};used=sum(usage.values())
            retries={k:0 for k in ('news','dart')}
            retries.update(dict(db.execute("SELECT j.source,count(*) FROM attempts a JOIN jobs j ON j.id=a.job WHERE a.day=? AND a.bucket='retry' GROUP BY j.source",(utc.date().isoformat(),))))
            report['ai_budget']=dict(utc_date=utc.date().isoformat(),limit=900,used=used,remaining=max(0,900-used),
                usage=usage,local_usage=local_usage,budget_baseline=baseline,retries_by_source=retries)
            jobs=dict(db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
            waiting=jobs.get('pending',0);running=jobs.get('running',0)
            oldest=db.execute("SELECT min(created) FROM jobs WHERE state='pending'").fetchone()[0]
            state='processing' if running else 'waiting'
            if meta.get('auth_hold'):state='auth_hold'
            elif meta.get('paused_until',0)>now:state='budget_wait' if meta.get('pause_reason')=='daily_limit' else 'rate_limit_wait'
            elif waiting:
                limits=settings.get('limits',dict(total=900,news=600,dart=200,retry=100))
                buckets=[r[0] for r in db.execute("SELECT DISTINCT CASE WHEN attempts>0 THEN 'retry' ELSE source END FROM jobs WHERE state='pending'")]
                if used>=900 or all(usage[k]>=limits[k] for k in buckets):state='budget_wait'
            report['queue']=dict(waiting=waiting,running=running,held=jobs.get('hold',0),failed=None,
                oldest_wait_seconds=int(max(0,now-oldest)) if oldest is not None else 0 if not waiting else None,state=state)
            rows=db.execute('SELECT destination,state,attempts,count(*) n FROM outputs GROUP BY destination,state,attempts').fetchall()
            report['delivery']['dashboard_enqueue']=delivery_counts([r for r in rows if r['destination']=='dashboard'])
            report['delivery']['telegram']=delivery_counts([r for r in rows if r['destination'].startswith('telegram:')])
            discoveries=dict(db.execute('SELECT state,count(*) FROM discoveries GROUP BY state'))
            if 'news_candidates' in tables and db.execute("SELECT 1 FROM meta WHERE key='news_title_cutover'").fetchone():
                from news_selection import stage_status
                stages=stage_status(db,now)
                c=stages['candidates'];b=stages['batches'];s=stages['summaries'];calls=stages['calls_utc']
                issues.append(dict(source='news',stage='title_selection',state='stage_counts',
                    message=f"제목 대기 {c.get('waiting',0)}, 묶음 배정 {c.get('batched',0)}, 완료 묶음 {b.get('complete',0)}, 보류 묶음 {b.get('hold',0)}, 누적 선택 {c.get('selected',0)}, 미선정 {c.get('not_selected',0)}."))
                issues.append(dict(source='news',stage='individual_summary',state='stage_counts',
                    message=f"새 경로 요약 대기 {s.get('pending',0)}, 처리 {s.get('running',0)}, 완료 {s.get('complete',0)}, 보류 {s.get('hold',0)}, 재시도 대기 {stages['summary_retry']}. UTC 당일 호출은 제목 선별 {calls.get('title_selection',0)}, 개별 요약 {calls.get('summary',0)} (재시도 포함)."))
            gaps=db.execute('SELECT count(*) FROM scans WHERE gap IS NOT NULL').fetchone()[0]
            if gaps:issues.append(dict(source='news',stage='collect',state='coverage_gap',message=f'현재 {gaps}개 기업의 조회 구간이 상한 또는 오류로 미확인입니다.'))
            for stage,held in [('ai',jobs.get('hold',0)),('preprocess',discoveries.get('hold',0))]:
                if held:issues.append(dict(source='pi',stage=stage,state='held',message=f'현재 확인이 필요한 보류 {held}건'))
            uncertain=report['delivery']['telegram']['uncertain']
            if uncertain:issues.append(dict(source='telegram',stage='delivery',state='uncertain',message=f'발송 여부가 불확실한 {uncertain}건은 자동 재전송하지 않습니다.'))
    except (sqlite3.Error,ValueError,TypeError,KeyError,OSError):
        issues.append(dict(source='pi',stage='snapshot',state='unknown',message='분석 대기열 상태를 확인하지 못했습니다. 재수집하거나 재분석하지 않았습니다.'))
    try:
        with closing(readonly(settings['dashboard_db'])) as db:
            rows=db.execute('SELECT state,attempts,count(*) n FROM outbox GROUP BY state,attempts').fetchall()
            report['delivery']['dashboard_stored']=delivery_counts(rows,True)
            report['last_success']['dashboard_stored']=db.execute("SELECT max(delivered_at) FROM outbox WHERE state='delivered'").fetchone()[0]
    except (sqlite3.Error,OSError):
        issues.append(dict(source='dashboard',stage='stored_ack',state='unknown',message='기존 전송 대기열의 수신 ACK 상태가 미확인입니다.'))
    return report


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_suffix(path.suffix+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as handle:json.dump(value,handle,ensure_ascii=False)
    os.replace(temporary,path)


def load_json(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default


def run_report(settings, verify=False, now=None, post=None):
    now=now if now is not None else datetime.now(timezone.utc).timestamp()
    state_path=Path(settings['state_file']);snapshot_path=state_path.with_suffix('.snapshot.json')
    with exclusive_lock(state_path):
        state=load_json(state_path,{})
        if state.get('auth_hold') or state.get('configuration_hold'):
            return dict(status='auth_hold' if state.get('auth_hold') else 'configuration_hold')
        if not verify and (state.get('slot')==int(now//1800) or state.get('next_allowed',0)>now):
            return dict(status='waiting')
        if verify:
            if state.get('status') not in ('stored','duplicate'):
                raise ValueError('stored report required before duplicate verification')
            report=load_json(snapshot_path,{})
            if report.get('report_id')!=state.get('report_id'):raise ValueError('snapshot mismatch')
        else:
            report=snapshot(settings,now)
            atomic_json(snapshot_path,report) # replace, never append a report backlog
        encoded=json.dumps(report,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        if len(encoded)>65536:raise ValueError('status report exceeds contract limit')
        endpoint=settings['endpoint']
        if urlparse(endpoint).scheme!='https':raise ValueError('HTTPS status endpoint required')
        token_path=Path(settings['token_file'])
        if os.name=='posix' and (token_path.stat().st_uid!=os.getuid() or token_path.stat().st_mode&0o077):
            raise ValueError('private service-account token file required')
        token=token_path.read_text(encoding='utf-8').strip()
        if len(token)<32:raise ValueError('status token missing')
        state.update(slot=int(now//1800),report_id=report['report_id'],reported_at=report['reported_at'],attempted_at=iso(now),status='attempting')
        atomic_json(state_path,state) # persist before HTTP; no same-slot loop after crash
        try:
            response=(post or requests.post)(endpoint,data=encoded,headers={'Authorization':'Bearer '+token,
                'Content-Type':'application/json','User-Agent':'invest-monitor-operations-report/1.0'},timeout=20,allow_redirects=False)
            state['http_status']=response.status_code
            try:ack=response.json()
            except ValueError:ack={}
            if response.status_code==200 and ack.get('report_id')==report['report_id'] and ack.get('status') in ('stored','duplicate','superseded'):
                state.update(status=ack['status'],received_at=ack.get('received_at'),last_success=iso(now))
            else:
                state['status']='rejected'
                if response.status_code in (401,403):state['auth_hold']=True
                if response.status_code in (400,405,409,413,415,422) or (response.status_code==503 and ack.get('error')=='report_not_configured'):
                    state['configuration_hold']=True
                if response.status_code==429 or response.status_code>=500:
                    state['next_allowed']=now+max(1800,retry_seconds(response.headers,now))
        except (requests.RequestException,ValueError,TypeError):
            state.update(status='request_failed',next_allowed=now+1800)
        atomic_json(state_path,state)
        return {k:state.get(k) for k in ('report_id','reported_at','http_status','status','received_at')}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--settings',required=True)
    parser.add_argument('--verify-duplicate',action='store_true')
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--resume-auth',action='store_true')
    parser.add_argument('--resume-after-fix',action='store_true')
    args=parser.parse_args()
    try:
        settings=load_json(args.settings,{})
        if args.status:result=load_json(settings['state_file'],{})
        elif args.resume_auth or args.resume_after_fix:
            with exclusive_lock(settings['state_file']):
                result=load_json(settings['state_file'],{});result.pop('auth_hold',None)
                if args.resume_after_fix:result.pop('configuration_hold',None)
                atomic_json(settings['state_file'],result)
            result={'status':'auth_hold_cleared'}
        else:result=run_report(settings,args.verify_duplicate)
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'report_error','type':type(exc).__name__}))
        raise SystemExit(1)
