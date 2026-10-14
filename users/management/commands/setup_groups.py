"""
Create and maintain the platform's authorization groups.

Uses Django's built-in ``Group`` / ``Permission`` models only - no custom
group or permission framework is introduced here.

    python manage.py setup_groups
    python manage.py setup_groups --assign h4ck3r_x=User --assign admin=Admin
    python manage.py setup_groups --dry-run

The command is idempotent: groups are fetched with ``get_or_create`` and each
group's permission set is reconciled with ``set()``, so running it twice
produces no further changes.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()

# Canonical permission sets, expressed as 'app_label.codename'.
#
# Model permissions only, so the set is derived from models that actually
# exist. The `challenges.Challenge` model now exists, so its view/add/change
# permissions are granted to `Challenge Creator`, and the Admin group also gets
# `delete_challenge`. The `User` group stays empty: the public challenge
# endpoints are open (published-only) and must never expose drafts.
GRANTED = {
    'User': [
        # Deliberately empty. Published challenges are readable through the
        # public, AllowAny endpoints; granting view_challenge here would also
        # expose the admin (drafts-included) surface.
    ],
    'Challenge Creator': [
        'challenges.view_challenge',
        'challenges.add_challenge',
        'challenges.change_challenge',
    ],
    'Admin': [
        'users.view_user',
        'users.add_user',
        'users.change_user',
        'users.delete_user',
        'auth.view_group',
        'auth.add_group',
        'auth.change_group',
        'auth.delete_group',
        'challenges.view_challenge',
        'challenges.add_challenge',
        'challenges.change_challenge',
        'challenges.delete_challenge',
    ],
}

# Permissions the permission model *should* carry but does not, because the
# underlying model has not been built. Reported on every run; never granted.
# Empty now that the Challenge model exists; kept so future models (e.g.
# Submission) can be declared here instead of being invented.
MISSING = {
    'User': [],
    'Challenge Creator': [],
    'Admin': [],
}

MISSING_REASON = (
    'the referenced model does not exist yet, so Django has not generated '
    'these model permissions. Add the model, run makemigrations/migrate, then '
    're-run setup_groups.'
)


class Command(BaseCommand):
    help = (
        'Create or update the User, Challenge Creator and Admin groups and '
        'sync their permissions. Idempotent; safe to run repeatedly.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--assign',
            action='append',
            default=[],
            metavar='USERNAME=GROUP',
            help=(
                'Add a user to a group via the standard m2m relation. '
                'Repeatable. Example: --assign h4ck3r_x=User'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would change without writing to the database.',
        )

    # ------------------------------------------------------------------ #
    # Permission resolution
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve(refs):
        """Map 'app.codename' strings to Permission objects."""
        resolved, missing = [], []
        for ref in refs:
            app_label, sep, codename = ref.partition('.')
            if not sep or not app_label or not codename:
                raise CommandError(
                    f'Malformed permission {ref!r}; expected "app_label.codename".'
                )
            perm = Permission.objects.filter(
                content_type__app_label=app_label,
                codename=codename,
            ).first()
            (resolved if perm else missing).append(perm if perm else ref)
        return resolved, missing

    # ------------------------------------------------------------------ #
    # Assignment validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_assignments(specs):
        """Validate --assign values before anything is written."""
        if not specs:
            return []

        valid_groups = list(GRANTED)
        known = set()
        for raw in specs:
            username, sep, group_name = raw.partition('=')
            username, group_name = username.strip(), group_name.strip()
            if not sep or not username or not group_name:
                raise CommandError(
                    f'Invalid --assign value {raw!r}; expected '
                    f'USERNAME=GROUP, e.g. --assign h4ck3r_x=User.'
                )
            if group_name not in valid_groups:
                raise CommandError(
                    f'Unknown group {group_name!r}. Valid groups: '
                    f'{", ".join(valid_groups)}.'
                )
            key = (username, group_name)
            if key in known:
                continue
            known.add(key)

        pairs = []
        for username, group_name in sorted(known):
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(
                    f'Unknown user {username!r}. Pass an existing username, '
                    f'e.g. --assign h4ck3r_x=User.'
                )
            pairs.append((user, group_name))
        return pairs

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        assignments = self._validate_assignments(options['assign'])

        # Validate every declared permission before touching the database, so
        # a typo or an unapplied migration fails loudly instead of silently
        # leaving a group under-permissioned.
        desired = {}
        for name, refs in GRANTED.items():
            resolved, unresolved = self._resolve(refs)
            if unresolved:
                raise CommandError(
                    f'Permission(s) not found for group {name!r}: '
                    f'{", ".join(unresolved)}. Has `manage.py migrate` run?'
                )
            desired[name] = resolved

        if not dry_run:
            with transaction.atomic():
                self._sync_groups(dry_run, desired)
                self._sync_assignments(dry_run, assignments)
        else:
            self._sync_groups(dry_run, desired)
            self._sync_assignments(dry_run, assignments)

        self._report_missing()
        self.stdout.write(self.style.SUCCESS(
            'Dry run complete - no changes written.'
            if dry_run else 'Groups and permissions are up to date.'
        ))

    # ------------------------------------------------------------------ #
    # Group + permission sync
    # ------------------------------------------------------------------ #

    def _sync_groups(self, dry_run, desired):
        for name, refs in GRANTED.items():
            group = Group.objects.filter(name=name).first()
            created = group is None

            current = set(group.permissions.all()) if group else set()
            wanted = set(desired[name])
            added, removed = wanted - current, current - wanted

            if created:
                verb = 'create' if dry_run else 'created'
                self.stdout.write(f'{verb:<7} group  {name}')
            elif not (added or removed):
                self.stdout.write(f'unchanged group  {name} '
                                  f'({len(wanted)} permission(s))')
            else:
                verb = 'would set' if dry_run else 'updated'
                self.stdout.write(
                    f'{verb:<9} group {name} '
                    f'(+{len(added)} / -{len(removed)})'
                )

            for perm in sorted(added, key=lambda p: p.codename):
                self.stdout.write(f'          + {perm.content_type.app_label}.{perm.codename}')
            for perm in sorted(removed, key=lambda p: p.codename):
                self.stdout.write(f'          - {perm.content_type.app_label}.{perm.codename}')

            if dry_run:
                continue

            if created:
                group = Group.objects.create(name=name)
            group.permissions.set(desired[name])

    # ------------------------------------------------------------------ #
    # User <-> group assignment
    # ------------------------------------------------------------------ #

    def _sync_assignments(self, dry_run, assignments):
        if not assignments:
            self.stdout.write('no assignments requested (use --assign USERNAME=GROUP)')
            return

        for user, group_name in assignments:
            # In a dry run the group may not exist yet (nothing was written),
            # so membership is only checkable once we know the row is there.
            group = Group.objects.filter(name=group_name).first()
            if group is not None and user.groups.filter(pk=group.pk).exists():
                self.stdout.write(f'unchanged {user.username} -> {group_name}')
                continue

            verb = 'would assign' if dry_run else 'assigned'
            self.stdout.write(f'{verb:<13} {user.username} -> {group_name}')
            if not dry_run:
                # Standard m2m; never touches is_staff, is_superuser or role.
                user.groups.add(Group.objects.get(name=group_name))

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def _report_missing(self):
        report = {name: perms for name, perms in MISSING.items() if perms}
        if not report:
            return

        self.stdout.write(self.style.WARNING('\nMISSING / NOT GRANTED'))
        for name, perms in report.items():
            self.stdout.write(f'  {name}')
            for ref in perms:
                self.stdout.write(f'    {ref}')
        self.stdout.write(f'  reason: {MISSING_REASON}')
