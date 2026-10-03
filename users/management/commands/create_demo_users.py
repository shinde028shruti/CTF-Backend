"""
Creates the demo accounts used by the Login page's Demo User / Demo Admin
buttons, so the React frontend has real credentials to authenticate against.

The command is idempotent: an account that already exists is left untouched
unless --reset-password is passed.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

User = get_user_model()

DEMO_ACCOUNTS = [
    {
        'username': 'h4ck3r_x',
        'email': 'hacker@cyberforge.io',
        'password': 'password123',
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'role': User.ROLE_USER,
        'points': 1450,
        'bio': 'Cybersecurity enthusiast. CTF competitor. Always learning.',
    },
    {
        'username': 'admin',
        'email': 'admin@cyberforge.io',
        'password': 'admin123',
        'first_name': 'Site',
        'last_name': 'Administrator',
        'role': User.ROLE_ADMIN,
        'points': 0,
        'bio': 'Platform administrator.',
    },
]


class Command(BaseCommand):
    help = 'Create or update the demo user and admin accounts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Also reset the password of accounts that already exist.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        reset = options['reset_password']

        for spec in DEMO_ACCOUNTS:
            user = User.objects.filter(username=spec['username']).first()

            if user is None:
                user = User.objects.create_user(
                    username=spec['username'],
                    email=spec['email'],
                    password=spec['password'],
                )
                user.first_name = spec['first_name']
                user.last_name = spec['last_name']
                user.role = spec['role']
                user.points = spec['points']
                user.bio = spec['bio']
                # Demo admin needs Django admin access as well as the API role.
                if spec['role'] == User.ROLE_ADMIN:
                    user.is_staff = True
                    user.is_superuser = True
                user.save()
                self.stdout.write(self.style.SUCCESS(
                    f'created  {spec["username"]} / {spec["password"]} ({spec["role"]})'
                ))
                continue

            changed = []
            if user.email != spec['email']:
                user.email = spec['email']
                changed.append('email')
            if user.role != spec['role']:
                user.role = spec['role']
                changed.append('role')
            if user.points != spec['points']:
                user.points = spec['points']
                changed.append('points')
            if spec['role'] == User.ROLE_ADMIN and not user.is_staff:
                user.is_staff = True
                user.is_superuser = True
                changed.append('is_staff')

            if reset:
                user.set_password(spec['password'])
                changed.append('password')

            if changed:
                user.save()
                self.stdout.write(self.style.WARNING(
                    f'updated  {spec["username"]} ({", ".join(changed)})'
                ))
            else:
                self.stdout.write(f'unchanged {spec["username"]}')

        self.stdout.write(self.style.SUCCESS('Demo accounts ready.'))