"""Seed the built-in achievements onto real, existing categories.
    python manage.py seed_achievements
    python manage.py seed_achievements --dry-run

Idempotent: each definition is keyed by its stable ``slug`` and only created if
absent, so running it repeatedly makes no further changes. Only criteria that are
expressible with the four supported types are seeded; a ``category_solves`` entry
whose category does not exist is skipped and reported rather than guessed.

Definitions are never updated by this command - an administrator's edits are
respected.
"""

import copy

from django.core.management.base import BaseCommand
from django.db import transaction

from achievements.models import Achievement
from challenges.models import Category

# (slug, name, description, icon, gradient, rarity, category, reward, rule)
BUILT_INS = [
    {
        'slug': 'first-blood',
        'name': 'First Blood',
        'description': 'Solve your very first challenge.',
        'icon': 'Zap',
        'gradient': ['#4F1F71', '#7764E0'],
        'rarity': 'Legendary',
        'category': 'General',
        'reward': 100,
        'criteria_type': Achievement.CRITERIA_FIRST_SOLVE,
        'criteria_config': {},
    },
    {
        'slug': 'crypto-beginner',
        'name': 'Crypto Beginner',
        'description': 'Solve 3 challenges in the Cryptography category.',
        'icon': 'KeyRound',
        'gradient': ['#f6d365', '#fda085'],
        'rarity': 'Common',
        'category': 'Cryptography',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Cryptography', 'target': 3},
    },
    {
        'slug': 'web-hunter',
        'name': 'Web Hunter',
        'description': 'Solve 5 challenges in the Web Exploitation category.',
        'icon': 'Globe',
        'gradient': ['#61dafb', '#a78bfa'],
        'rarity': 'Common',
        'category': 'Web Exploitation',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Web Exploitation', 'target': 5},
    },
    {
        'slug': 'linux-explorer',
        'name': 'Linux Explorer',
        'description': 'Solve a challenge in the Binary Exploitation category.',
        'icon': 'Terminal',
        'gradient': ['#4ade80', '#16a34a'],
        'rarity': 'Uncommon',
        'category': 'Binary Exploitation',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Binary Exploitation', 'target': 1},
    },
    {
        'slug': 'forensics-investigator',
        'name': 'Forensics Investigator',
        'description': 'Solve 5 challenges in the Digital Forensics category.',
        'icon': 'FileSearch',
        'gradient': ['#f472b6', '#c084fc'],
        'rarity': 'Rare',
        'category': 'Digital Forensics',
        'reward': 200,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Digital Forensics', 'target': 5},
    },
    {
        'slug': 'blue-team-rookie',
        'name': 'Blue Team Rookie',
        'description': 'Solve a challenge in the Networking category.',
        'icon': 'ShieldCheck',
        'gradient': ['#38bdf8', '#22d3ee'],
        'rarity': 'Common',
        'category': 'Networking',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Networking', 'target': 1},
    },
    {
        'slug': 'osint-sleuth',
        'name': 'OSINT Sleuth',
        'description': 'Solve 3 challenges in the OSINT category.',
        'icon': 'Search',
        'gradient': ['#fbbf24', '#f97316'],
        'rarity': 'Uncommon',
        'category': 'OSINT',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'OSINT', 'target': 3},
    },
    {
        'slug': 'reverse-engineer',
        'name': 'Reverse Engineer',
        'description': 'Solve 5 challenges in the Reverse Engineering category.',
        'icon': 'Bug',
        'gradient': ['#34d399', '#06b6d4'],
        'rarity': 'Rare',
        'category': 'Reverse Engineering',
        'reward': 200,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Reverse Engineering', 'target': 5},
    },
    {
        'slug': 'stego-specialist',
        'name': 'Stego Specialist',
        'description': 'Solve a challenge in the Steganography category.',
        'icon': 'Image',
        'gradient': ['#a3e635', '#65a30d'],
        'rarity': 'Common',
        'category': 'Steganography',
        'reward': 150,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Steganography', 'target': 1},
    },
    {
        'slug': 'mobile-maven',
        'name': 'Mobile Maven',
        'description': 'Solve a challenge in the Mobile Security category.',
        'icon': 'Smartphone',
        'gradient': ['#fb7185', '#eab308'],
        'rarity': 'Rare',
        'category': 'Mobile Security',
        'reward': 200,
        'criteria_type': Achievement.CRITERIA_CATEGORY_SOLVES,
        'criteria_config': {'category_name': 'Mobile Security', 'target': 1},
    },
]


class Command(BaseCommand):
    help = (
        'Seed the built-in achievements onto real categories. Idempotent; '
        'safe to run repeatedly. Existing definitions are never modified.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be created without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        created = existing = skipped = 0

        for index, spec in enumerate(BUILT_INS):
            spec = copy.deepcopy(spec)
            slug = spec.pop('slug')
            config = spec.pop('criteria_config')
            criteria_type = spec['criteria_type']

            # Resolve the category reference for category rules.
            if criteria_type == Achievement.CRITERIA_CATEGORY_SOLVES:
                name = config.pop('category_name')
                category = Category.objects.filter(name=name).first()
                if category is None:
                    skipped += 1
                    self.stdout.write(self.style.WARNING(
                        f'skipped  {slug}: category {name!r} does not exist'
                    ))
                    continue
                config = {'category': category.pk, 'target': config['target']}

            # Respect existing rows, including soft-deleted ones.
            found = Achievement.all_objects.filter(slug=slug).first()
            if found is not None:
                existing += 1
                state = 'soft-deleted' if found.deleted_at else 'present'
                self.stdout.write(f'unchanged {slug} ({state})')
                continue

            if dry_run:
                created += 1
                self.stdout.write(f'would create {slug}')
                continue

            with transaction.atomic():
                Achievement.objects.create(
                    slug=slug,
                    criteria_config=config,
                    sort_order=index,
                    **spec,
                )
            created += 1
            self.stdout.write(f'created  {slug}')

        summary = (
            f'{created} created, {existing} already present, {skipped} skipped'
        )
        if dry_run:
            self.stdout.write(self.style.SUCCESS(
                f'Dry run complete - {summary}. No changes written.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f'Done - {summary}.'))
