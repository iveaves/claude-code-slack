"""Job scheduler for recurring agent tasks.

Wraps APScheduler's AsyncIOScheduler and publishes ScheduledEvents
to the event bus when jobs fire.

IMPORTANT — day-of-week convention:
    Standard crontab:  0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat
    APScheduler native: 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

    APScheduler's CronTrigger.from_crontab() does NOT convert the
    day-of-week field — it passes the raw number through.  This means
    ``from_crontab("0 10 * * 3")`` fires on *Thursday*, not Wednesday.
    We therefore parse the crontab ourselves and convert explicitly.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.asyncio import (
    AsyncIOScheduler,  # type: ignore[import-untyped]
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from ..events.bus import EventBus
from ..events.types import ScheduledEvent
from ..storage.database import DatabaseManager

logger = structlog.get_logger()

# How long after a missed fire we'll still execute the job (seconds).
MISFIRE_GRACE_SECONDS = 3 * 60 * 60  # 3 hours

# Default timezone for cron triggers.
DEFAULT_TIMEZONE = "America/Los_Angeles"


# ── Crontab → CronTrigger helper ─────────────────────────────────────


# Named days APScheduler understands directly (case-insensitive).
_NAMED_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Mapping from crontab numeric dow (0=Sun … 6=Sat) to APScheduler
# numeric dow (0=Mon … 6=Sun).
_CRONTAB_DOW_TO_APSCHEDULER = {
    0: 6,  # Sun → 6
    1: 0,  # Mon → 0
    2: 1,  # Tue → 1
    3: 2,  # Wed → 2
    4: 3,  # Thu → 3
    5: 4,  # Fri → 4
    6: 5,  # Sat → 5
    7: 6,  # Sun (alt) → 6
}


def _convert_dow_token(token: str) -> str:
    """Convert a single crontab day-of-week token to APScheduler convention.

    Handles: plain numbers (``3``), ranges (``1-5``), stepped ranges
    (``1-5/2``), and named days (``mon``, ``WED``).  Named days are
    passed through unchanged because APScheduler handles them correctly.
    """
    # Named days — pass through
    if token.lower() in _NAMED_DAYS:
        return token

    # Wildcard / every — pass through
    if token in ("*", "?"):
        return token

    # Stepped wildcard like */2
    if token.startswith("*/") or token.startswith("?/"):
        return token

    # Range with optional step: e.g. "1-5" or "0-6/2"
    range_match = re.match(r"^(\d+)-(\d+)(/\d+)?$", token)
    if range_match:
        lo = _CRONTAB_DOW_TO_APSCHEDULER.get(int(range_match.group(1)))
        hi = _CRONTAB_DOW_TO_APSCHEDULER.get(int(range_match.group(2)))
        if lo is None or hi is None:
            return token  # out-of-range, let APScheduler error out
        step = range_match.group(3) or ""
        # If the converted range wraps (e.g. crontab 0-4 → AP 6,0-3),
        # fall back to a list.
        if lo <= hi:
            return f"{lo}-{hi}{step}"
        else:
            # Enumerate the days explicitly
            step_val = int(step[1:]) if step else 1
            orig_lo = int(range_match.group(1))
            orig_hi = int(range_match.group(2))
            days = []
            for d in range(orig_lo, orig_hi + 1, step_val):
                converted = _CRONTAB_DOW_TO_APSCHEDULER.get(d)
                if converted is not None:
                    days.append(str(converted))
            return ",".join(days) if days else token

    # Plain number
    try:
        num = int(token)
        converted = _CRONTAB_DOW_TO_APSCHEDULER.get(num)
        return str(converted) if converted is not None else token
    except ValueError:
        pass

    # Fallback: return as-is (let APScheduler validate)
    return token


def _convert_dow_field(field: str) -> str:
    """Convert a full crontab day-of-week field (may be comma-separated)."""
    parts = field.split(",")
    return ",".join(_convert_dow_token(p.strip()) for p in parts)


def parse_crontab(
    expression: str,
    tz: Optional[ZoneInfo] = None,
) -> CronTrigger:
    """Parse a standard 5-field crontab expression into a CronTrigger.

    Unlike ``CronTrigger.from_crontab()``, this correctly converts the
    day-of-week field from crontab convention (0=Sun) to APScheduler
    convention (0=Mon).
    """
    fields = expression.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"Expected 5-field crontab expression, got {len(fields)}: {expression!r}"
        )

    minute, hour, day, month, dow = fields
    converted_dow = _convert_dow_field(dow)

    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=converted_dow,
        timezone=tz,
    )


# ── JobScheduler ─────────────────────────────────────────────────────


class JobScheduler:
    """Cron scheduler that publishes ScheduledEvents to the event bus."""

    def __init__(
        self,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        default_working_directory: Path,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> None:
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.default_working_directory = default_working_directory
        self.tz = ZoneInfo(timezone)
        self._scheduler = AsyncIOScheduler(
            job_defaults={"misfire_grace_time": MISFIRE_GRACE_SECONDS},
            timezone=self.tz,
        )

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Load persisted jobs, start the scheduler, then fire any missed jobs."""
        missed_jobs = await self._load_jobs_from_db()
        self._scheduler.start()
        logger.info("Job scheduler started")

        # Fire jobs that were missed during downtime (within grace window).
        for job_info in missed_jobs:
            logger.info(
                "Firing missed job",
                job_name=job_info["job_name"],
                expected_fire=str(job_info["expected_fire"]),
                last_fired_at=str(job_info.get("last_fired_at")),
            )
            await self._fire_event(
                job_name=job_info["job_name"],
                prompt=job_info["prompt"],
                working_directory=job_info["working_directory"],
                target_channel_ids=job_info["target_channel_ids"],
                skill_name=job_info.get("skill_name"),
            )

    async def stop(self) -> None:
        """Shutdown the scheduler gracefully."""
        self._scheduler.shutdown(wait=False)
        logger.info("Job scheduler stopped")

    # ── Public API ────────────────────────────────────────────────

    async def add_job(
        self,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_channel_ids: Optional[List[str]] = None,
        working_directory: Optional[Path] = None,
        skill_name: Optional[str] = None,
        created_by: str = "",
    ) -> str:
        """Add a new scheduled job.

        Args:
            job_name: Human-readable job name.
            cron_expression: Standard 5-field crontab (0=Sun for day-of-week).
            prompt: The prompt to send to Claude when the job fires.
            target_channel_ids: Slack channel IDs to send the response to.
            working_directory: Working directory for Claude execution.
            skill_name: Optional skill to invoke.
            created_by: Slack user ID of the creator.

        Returns:
            The job ID.
        """
        trigger = parse_crontab(cron_expression, tz=self.tz)
        work_dir = working_directory or self.default_working_directory

        job = self._scheduler.add_job(
            self._fire_event,
            trigger=trigger,
            kwargs={
                "job_name": job_name,
                "prompt": prompt,
                "working_directory": str(work_dir),
                "target_channel_ids": target_channel_ids or [],
                "skill_name": skill_name,
            },
            name=job_name,
            max_instances=1,
            coalesce=True,
        )

        # Persist to database
        await self._save_job(
            job_id=job.id,
            job_name=job_name,
            cron_expression=cron_expression,
            prompt=prompt,
            target_channel_ids=target_channel_ids or [],
            working_directory=str(work_dir),
            skill_name=skill_name,
            created_by=created_by,
        )

        next_fire = job.next_run_time
        logger.info(
            "Scheduled job added",
            job_id=job.id,
            job_name=job_name,
            cron=cron_expression,
            next_fire=str(next_fire) if next_fire else "none",
            timezone=str(self.tz),
        )
        return str(job.id)

    async def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            logger.warning("Job not found in scheduler", job_id=job_id)

        await self._delete_job(job_id)
        logger.info("Scheduled job removed", job_id=job_id)
        return True

    async def list_jobs(self) -> List[Dict[str, Any]]:
        """List all scheduled jobs from the database."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduled_jobs WHERE is_active = 1 ORDER BY created_at"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ── Event firing ──────────────────────────────────────────────

    async def _fire_event(
        self,
        job_name: str,
        prompt: str,
        working_directory: str,
        target_channel_ids: List[str],
        skill_name: Optional[str],
    ) -> None:
        """Called by APScheduler when a job triggers. Publishes a ScheduledEvent."""
        event = ScheduledEvent(
            job_name=job_name,
            prompt=prompt,
            working_directory=Path(working_directory),
            target_channel_ids=target_channel_ids,
            skill_name=skill_name,
        )

        logger.info(
            "Scheduled job fired",
            job_name=job_name,
            event_id=event.id,
        )

        # Record last fire time in the DB for misfire detection on restart.
        await self._update_last_fired(job_name)

        await self.event_bus.publish(event)

    async def _update_last_fired(self, job_name: str) -> None:
        """Update last_fired_at for a job (matched by name)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            async with self.db_manager.get_connection() as conn:
                await conn.execute(
                    "UPDATE scheduled_jobs SET last_fired_at = ? "
                    "WHERE job_name = ? AND is_active = 1",
                    (now, job_name),
                )
                await conn.commit()
        except Exception:
            logger.exception("Failed to update last_fired_at", job_name=job_name)

    # ── Startup: load from DB + detect misfires ───────────────────

    async def _load_jobs_from_db(self) -> List[Dict[str, Any]]:
        """Load persisted jobs, re-register with APScheduler, detect misfires.

        Returns a list of job-info dicts for jobs that were missed during
        downtime and should be fired immediately.
        """
        missed_jobs: List[Dict[str, Any]] = []
        now = datetime.now(self.tz)

        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM scheduled_jobs WHERE is_active = 1"
                )
                rows = list(await cursor.fetchall())

            for row in rows:
                row_dict = dict(row)
                try:
                    trigger = parse_crontab(row_dict["cron_expression"], tz=self.tz)

                    # Parse channel IDs from stored comma-separated string
                    # DB column is target_chat_ids (legacy name from Telegram)
                    chat_ids_str = row_dict.get("target_chat_ids", "")
                    channel_ids = (
                        [x.strip() for x in chat_ids_str.split(",") if x.strip()]
                        if chat_ids_str
                        else []
                    )

                    kwargs: Dict[str, Any] = {
                        "job_name": row_dict["job_name"],
                        "prompt": row_dict["prompt"],
                        "working_directory": row_dict["working_directory"],
                        "target_channel_ids": channel_ids,
                        "skill_name": row_dict.get("skill_name"),
                    }

                    self._scheduler.add_job(
                        self._fire_event,
                        trigger=trigger,
                        kwargs=kwargs,
                        id=row_dict["job_id"],
                        name=row_dict["job_name"],
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                    )

                    # Log computed next fire time for observability
                    next_fire = trigger.get_next_fire_time(None, now)
                    logger.info(
                        "Loaded scheduled job",
                        job_id=row_dict["job_id"],
                        job_name=row_dict["job_name"],
                        cron=row_dict["cron_expression"],
                        next_fire=str(next_fire) if next_fire else "none",
                        next_fire_day=(
                            next_fire.strftime("%A") if next_fire else "unknown"
                        ),
                    )

                    # ── Misfire detection ────────────────────────────
                    missed = self._detect_misfire(
                        trigger=trigger,
                        last_fired_raw=row_dict.get("last_fired_at"),
                        now=now,
                    )
                    if missed:
                        missed_jobs.append({**kwargs, "expected_fire": missed})

                except Exception:
                    logger.exception(
                        "Failed to load scheduled job",
                        job_id=row_dict.get("job_id"),
                    )

            logger.info("Loaded scheduled jobs from database", count=len(rows))
        except Exception:
            # Table might not exist yet on first run
            logger.debug("No scheduled_jobs table found, starting fresh")

        return missed_jobs

    def _detect_misfire(
        self,
        trigger: CronTrigger,
        last_fired_raw: Any,
        now: datetime,
    ) -> Optional[datetime]:
        """Check if a job should have fired during downtime.

        Walks forward from ``last_fired_at`` through expected fire times.
        If any expected fire is in the past *and* within the misfire grace
        window, return it (caller should fire the job).

        Returns the missed fire time, or None if no misfire detected.
        """
        if not last_fired_raw:
            return None

        # Parse last_fired_at (stored as ISO string or datetime)
        if isinstance(last_fired_raw, str):
            last_fired = datetime.fromisoformat(last_fired_raw)
        elif isinstance(last_fired_raw, datetime):
            last_fired = last_fired_raw
        else:
            return None

        # Ensure timezone-aware
        if last_fired.tzinfo is None:
            last_fired = last_fired.replace(tzinfo=timezone.utc)

        # Convert to scheduler timezone for comparison
        last_fired = last_fired.astimezone(self.tz)

        # Walk forward from last fire, looking for missed firings
        expected = trigger.get_next_fire_time(last_fired, last_fired)
        latest_missed: Optional[datetime] = None

        # Safety bound: don't walk more than 1000 iterations
        # (covers ~3 years of daily jobs, more than enough)
        for _ in range(1000):
            if expected is None or expected > now:
                break
            # This expected fire time is in the past — check grace window
            seconds_late = (now - expected).total_seconds()
            if seconds_late <= MISFIRE_GRACE_SECONDS:
                latest_missed = expected
            expected = trigger.get_next_fire_time(expected, expected)

        return latest_missed

    # ── DB persistence ────────────────────────────────────────────

    async def _save_job(
        self,
        job_id: str,
        job_name: str,
        cron_expression: str,
        prompt: str,
        target_channel_ids: List[str],
        working_directory: str,
        skill_name: Optional[str],
        created_by: str,
    ) -> None:
        """Persist a job definition to the database."""
        chat_ids_str = ",".join(str(cid) for cid in target_channel_ids)
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                """
                INSERT OR REPLACE INTO scheduled_jobs
                (job_id, job_name, cron_expression, prompt, target_chat_ids,
                 working_directory, skill_name, created_by, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    job_id,
                    job_name,
                    cron_expression,
                    prompt,
                    chat_ids_str,
                    working_directory,
                    skill_name,
                    created_by,
                ),
            )
            await conn.commit()

    async def _delete_job(self, job_id: str) -> None:
        """Soft-delete a job from the database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET is_active = 0 WHERE job_id = ?",
                (job_id,),
            )
            await conn.commit()
