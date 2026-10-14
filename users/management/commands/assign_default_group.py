"""One-time backfill: put pre-existing ordinary users into the default group.

    python manage.py assign_default_group
    python manage.py assign_default_group --dry-run
    python manage.py assign_default_group --group User

Only users who are in *no* group and are neither staff nor superusers are
touched, so privileged accounts and existing memberships are left exactly as
they are. The command is idempotent and safe to run repeatedly.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = (
        'Add existing ordinary users (no group, not staff, not superuser) to '
        'the default group. One-time backfill; idempotent.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--group',
            default='User',
            metavar='NAME',
            help='Group to assign (default: User).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would change without writing to the database.',
        )

    def handle(self, *args, **options):
        group_name = options['group']
        dry_run = options['dry_run']

        group = Group.objects.filter(name=group_name).first()
        if group is None:
            raise CommandError(
                f'Group {group_name!r} does not exist. Run '
                f'`python manage.py setup_groups` first.'
            )

        # Target only ordinary accounts that belong to no group at all. Staff
        # and superusers are skipped, and anyone already in a group is left
        # alone, so no privileged account or existing membership is changed.
        targets = list(
            User.objects
            .filter(is_staff=False, is_superuser=False, groups__isnull=True)
            .order_by('username')
        )

        if not targets:
            self.stdout.write('No ungrouped ordinary users to assign.')
            return

        if dry_run:
            for user in targets:
                self.stdout.write(f'would assign  {user.username} -> {group_name}')
            self.stdout.write(self.style.SUCCESS(
                f'Dry run: {len(targets)} user(s) would be assigned to '
                f'{group_name!r} - nothing written.'
            ))
            return

        with transaction.atomic():
            for user in targets:
                user.groups.add(group)
                self.stdout.write(f'assigned      {user.username} -> {group_name}')

        self.stdout.write(self.style.SUCCESS(
            f'Assigned {len(targets)} user(s) to {group_name!r}.'
        ))
