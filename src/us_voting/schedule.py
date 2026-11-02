"""Decide whether a configured source is due on this hourly run."""

from datetime import UTC, datetime, time, timedelta

from us_voting.config import Source

# Half the hourly cron period, so drift between consecutive runs neither skips an
# interval nor runs one twice.
TOLERANCE = timedelta(minutes=30)


def is_due(source: Source, last_run: datetime | None, now: datetime) -> bool:
    schedule = source.schedule
    e0 = datetime.combine(source.election.date, time(0), tzinfo=UTC)

    if schedule.start_hour is not None and now < e0 + timedelta(hours=schedule.start_hour):
        return False
    if schedule.end_hour is not None and now >= e0 + timedelta(hours=schedule.end_hour):
        return False

    every = schedule.every_hours
    for window in schedule.windows:
        if e0 + timedelta(hours=window.start_hour) <= now < e0 + timedelta(hours=window.end_hour):
            every = window.every_hours
            break

    if last_run is None:
        return True

    anchor_hour = source.daily_at_utc if source.daily_at_utc is not None else schedule.daily_at_utc
    if every == 24 and anchor_hour is not None:
        anchor = now.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)
        if anchor > now:
            anchor -= timedelta(days=1)
        return last_run < anchor
    return now - last_run >= timedelta(hours=every) - TOLERANCE
