"""Backfill achievement awards from existing solve history.

    python manage.py backfill_achievements
    python manage.py backfill_achievements --dry-run
    python manage.py backfill_achievements --username alice

For every user with at least one solve, re-run the same evaluator used by the
live solve path and persist any awards they had already qualified for. The
command is:

* **idempotent** - already-earned awards are skipped, so re-running is a no-op;
* **non-destructive** - it only ever INSERTs new ``UserAchievement`` rows; it
  never updates or deletes anything;
* **safe to dry-run** - ``--dry-run`` reports how many awards *would* be created
  (using the live criteria logic) without writing or notifying.

The number of awards created is always reported.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from achievements.models import Achievement
from achievements.services import evaluate_achievements, pending_achievements
from submissions.models import Solve

User = get_user_model()


class Command(BaseCommand):
    help = (
        'Award achievements users already qualified for, from stored solve '
        'history. Idempotent and non-destructive.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--username',
            help='Backfill a single user instead of everyone with a solve.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be awarded without writing to the database.',
        )

    def handle(self, *args, **options):
        username = options.get('username')
        dry_run = options['dry_run']

        definitions = list(Achievement.objects.active())
        if not definitions:
            self.stdout.write(self.style.WARNING(
                'No active achievements exist. Run `seed_achievements` or '
                'create some in the admin first.'
            ))
            return

        solved_user_ids = (
            Solve.objects.values_list('user_id', flat=True).distinct()
        )
        users = User.objects.filter(pk__in=list(solved_user_ids))
        if username:
            users = users.filter(username=username)
            if not users.exists():
                raise CommandError(
                    f'No user with solves matched {username!r}.'
                )
        users = users.order_by('pk')

        processed = 0
        total_awarded = 0
        per_user = []

        for user in users.iterator():
            processed += 1
            if dry_run:
                count = len(pending_achievements(user, definitions))
            else:
                count = len(evaluate_achievements(user, definitions))
            if count:
                per_user.append((user.username, count))
            total_awarded += count

        for name, count in per_user:
            self.stdout.write(f'  {name}: +{count}')

        summary = (
            f'{total_awarded} award(s) '
            f'{"would be created" if dry_run else "created"} '
            f'for {processed} user(s) with solve history'
        )
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry run complete - {summary}. No changes written.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f'Done - {summary}.'))
