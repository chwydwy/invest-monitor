"""Parse Npay stock news and retain a one-time source migration boundary."""

import os
from datetime import datetime
from pathlib import Path


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
            try:
                article_date = datetime.strptime(raw_date, "%Y%m%d%H%M")
            except ValueError:
                continue
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
