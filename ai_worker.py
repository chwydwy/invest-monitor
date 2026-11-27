"""Single analysis call per tick; callbacks build and validate existing prompts."""
from email.utils import parsedate_to_datetime
from dashboard_sender import exclusive_lock
from operations_metrics import mark


class DeliveryHold(Exception):
    pass


def retry_seconds(headers, now):
    value = headers.get('Retry-After', '')
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 60


def tick(queue, transport, parse, preflight=None):
    """transport gets durable payload; parse returns result and delivery intents."""
    with exclusive_lock(queue.path + '.worker'):
        queue.recover()
        if preflight:
            preflight()
        job = queue.reserve()
        if job is None:
            return 'waiting'
        try:
            response = transport(job)
        except Exception:
            queue.finish(job, 599)
            return 'network_retry'
        status = response.status_code
        if status == 200:
            try:
                content = response.json()['choices'][0]['message']['content']
                if not isinstance(content, str) or not content.strip():
                    raise ValueError('empty content')
                if (job['payload'].get('kind')=='title_selection' and
                    response.json()['choices'][0].get('finish_reason') not in (None,'stop')):
                    raise ValueError('incomplete content')
                result, outputs = parse(job, content)
            except (KeyError, IndexError, ValueError, TypeError):
                queue.finish(job, 598, result={'invalid_content':locals().get('content')})
                return 'format_retry'
            if isinstance(result,dict):
                result['usage']=response.json().get('usage')
            queue.finish(job, 200, result, outputs)
            return 'complete'
        headers = response.headers
        delay = retry_seconds(headers, queue.clock())
        try:
            reset = float(headers.get('X-RateLimit-Reset', 0))
            if reset > 1e12:
                reset /= 1000
            delay = max(delay, reset - queue.clock())
        except ValueError:
            pass
        # Persist classification only, never error bodies (may contain prompts).
        daily = False
        if status == 429:
            try:
                error = response.json().get('error', {})
                message=str(error.get('message','')).lower()
                daily = any(word in message for word in ('daily','per day','per-day'))
            except (ValueError, TypeError):
                pass
        queue.finish(job, status, result={'http_status':status,'retry_after_seconds':delay,'daily_limit':daily},retry_after=delay, daily=daily)
        return 'http_' + str(status)


def deliver_one(queue, send):
    """Independent delivery destinations; uncertain Telegram requires review."""
    with exclusive_lock(queue.path + '.delivery'):
        with queue.db() as db:
            db.execute("UPDATE outputs SET state='uncertain',reason='interrupted_delivery' WHERE state='running'")
            row = db.execute("SELECT * FROM outputs WHERE state='pending' AND due<=? ORDER BY job,destination LIMIT 1", (queue.clock(),)).fetchone()
            if not row:
                return 'waiting'
            db.execute("UPDATE outputs SET state='running',attempts=attempts+1 WHERE job=? AND destination=?", (row['job'],row['destination']))
        import json
        try:
            success = send(row['destination'], json.loads(row['payload']))
            state = 'delivered' if success else 'pending'
            reason = None if success else 'delivery_rejected'
        except DeliveryHold:
            state, reason = 'hold', 'delivery_permanent_error'
        except Exception:
            # A timeout may mean Telegram accepted the message; no automatic
            # resend without a provider idempotency guarantee.
            state, reason = ('uncertain', 'delivery_unknown') if row['destination'].startswith('telegram') else ('pending', 'delivery_retry')
        if state=='pending' and row['attempts']>=4:
            state='hold'
        with queue.db() as db:
            db.execute('UPDATE outputs SET state=?,reason=?,due=? WHERE job=? AND destination=?',
                       (state,reason,queue.clock() if state=='delivered' else queue.clock()+min(3600,60*2**min(row['attempts'],6)),row['job'],row['destination']))
            source=row['job'].split(':',1)[0]
            stage='dashboard_enqueue' if row['destination']=='dashboard' else 'telegram'
            if state=='delivered':
                mark(db,queue.clock(),success=stage)
                mark(db,queue.clock(),success='deliver')
            else:
                mark(db,queue.clock(),source,'failed',row['job'])
                if state=='hold':mark(db,queue.clock(),source,'held',row['job'])
        return state
