"""Parse Npay stock news and retain a one-time source migration boundary."""

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))


def parse_npay_registration(raw_date):
    """Npay compact local time or an explicitly offset ISO time."""
    try:
        if len(raw_date) == 12 and raw_date.isdigit():
            return datetime.strptime(raw_date, "%Y%m%d%H%M").replace(tzinfo=KST)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", raw_date):
            return None
        parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=KST)
        return parsed
    except (AttributeError, TypeError, ValueError):
        return None


def parse_stock_news(payload, cutoff):
    """Return the existing news.py record shape, newest first."""
    if not isinstance(payload, dict) or not isinstance(payload.get("clusters"), list):
        raise ValueError("Npay news clusters are missing")

    news = []
    seen = set()
    for cluster in payload["clusters"]:
        if not isinstance(cluster, dict) or not isinstance(cluster.get("items"), list):
            continue
        for item in cluster["items"]:
            if not isinstance(item, dict):
                continue
            oid, aid = item.get("officeId"), item.get("articleId")
            title, raw_date = item.get("title"), item.get("datetime")
            if not all(isinstance(value, str) and value for value in (oid, aid, title, raw_date)):
                continue
            registration = parse_npay_registration(raw_date)
            if registration is None:
                continue
            article_date = registration.astimezone(KST).replace(tzinfo=None)
            unique_id = f"{oid}_{aid}"
            if unique_id in seen or article_date < cutoff:
                continue
            seen.add(unique_id)
            news.append({
                "id": unique_id,
                "title": title,
                "press": item.get("officeName") or "알수없음",
                "office_id": oid,
                "article_id": aid,
                "date": article_date,
                "date_raw": article_date.strftime("%Y.%m.%d %H:%M"),
                "npay_datetime_raw": raw_date,
                "published_at": registration.isoformat(timespec="seconds"),
            })
    return sorted(news, key=lambda item: item["date"], reverse=True)


def load_or_create_cutover(path, now=None):
    """Persist the first Npay start time outside Git to avoid a bulk backlog."""
    path = Path(path)
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        pass

    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    first_start = now or datetime.now()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    with os.fdopen(fd, "w", encoding="utf-8") as target:
        target.write(first_start.isoformat(timespec="seconds") + "\n")
    return first_start
