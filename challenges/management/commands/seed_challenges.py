"""Seed development categories and sample challenges.

Intended for local development / testing only. It is idempotent (safe to run
repeatedly) and never overwrites or deletes rows a user created: existing
categories and challenges are matched by their natural key and otherwise left
untouched.

Safety guard: the command refuses to run unless Django is in DEBUG mode, or the
operator passes ``--allow-production`` explicitly.

    python manage.py seed_challenges
    python manage.py seed_challenges --allow-production

Sample flags are development-only values, and are stored as hashes through the
same ``ChallengeFlag.set_flag`` path the API uses - the plaintext is printed to
this command's stdout for convenience and is never available from any API.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from challenges.models import Category, Challenge, ChallengeFlag, ChallengeHint

# The frontend's existing category definitions (name, icon, color, description).
CATEGORY_DEFINITIONS = [
    ('Web Exploitation', 'Globe', '#3b82f6', 'Attack and defend web applications. SQL injection, XSS, CSRF, SSRF, and more.'),
    ('Cryptography', 'KeyRound', '#8b5cf6', 'Break ciphers, crack hashes, and defeat poorly implemented encryption.'),
    ('Reverse Engineering', 'Cpu', '#f59e0b', 'Analyze compiled binaries, understand algorithms, and defeat protections.'),
    ('Digital Forensics', 'ScanSearch', '#10b981', 'Investigate disk images, memory dumps, and network captures for evidence.'),
    ('OSINT', 'Eye', '#06b6d4', 'Gather intelligence from open sources to track targets and find hidden info.'),
    ('Networking', 'Network', '#f97316', 'Analyze protocols, sniff traffic, and exploit network misconfigurations.'),
    ('Binary Exploitation', 'Bug', '#ef4444', 'Exploit memory corruption vulnerabilities in compiled programs.'),
    ('Mobile Security', 'Smartphone', '#ec4899', 'Analyze Android/iOS apps, bypass protections, and intercept traffic.'),
    ('Steganography', 'Image', '#84cc16', 'Discover data hidden in images, audio, video, and other media files.'),
    ('Miscellaneous', 'Puzzle', '#6366f1', "Challenges that don't fit neatly into other categories. Expect the unexpected."),
]

# Sample challenges. `flag` values are development-only.
SAMPLE_CHALLENGES = [
    {
        'slug': 'sample-sql-injection-101',
        'title': 'SQL Injection 101',
        'category': 'Web Exploitation',
        'difficulty': Challenge.DIFFICULTY_EASY,
        'points': 100,
        'featured': True,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'FlagHunt Team',
        'solves': 2847,
        'tags': ['sql', 'injection', 'authentication', 'web'],
        'description': 'Find the hidden flag inside the vulnerable authentication system.',
        'scenario': 'A startup launched their new admin portal but forgot to sanitize their SQL queries.',
        'objectives': [
            'Identify the SQL injection vulnerability in the login form',
            'Bypass authentication using a crafted payload',
            'Retrieve the flag from the admin profile',
        ],
        'instructions': 'Authenticate as the admin user without knowing the password.',
        'connection_info': {'url': 'http://web-sqli.challenge.local', 'port': 8080},
        'cheatsheet': [
            {'command': "admin' OR '1'='1' --", 'description': 'Basic authentication bypass', 'category': 'SQLi'},
        ],
        'flag': 'CTF{s1mpl3_sql_bypass_w1ns}',
        'hints': [
            {'text': 'What happens when you add a single quote to the username field?', 'cost': 10},
            {'text': "Try the payload admin'-- with any password.", 'cost': 20},
        ],
    },
    {
        'slug': 'sample-caesars-secret',
        'title': "Caesar's Secret",
        'category': 'Cryptography',
        'difficulty': Challenge.DIFFICULTY_EASY,
        'points': 75,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'CipherSage',
        'solves': 4102,
        'tags': ['caesar', 'classical', 'cipher', 'crypto'],
        'description': 'An ancient cipher hides a modern secret. Decrypt the message to reveal the flag.',
        'scenario': 'A leaked message from a threat actor was intercepted.',
        'instructions': 'Decrypt the ciphertext: WTY{p3q3_p1cm3a_1f_qbb_3pfj}.',
        'flag': 'CTF{c3s3r_c1ph3r_1s_too_3asy}',
        'hints': [
            {'text': 'The flag format is CTF{}. Count the shift.', 'cost': 5},
        ],
    },
    {
        'slug': 'sample-memory-dump-mayhem',
        'title': 'Memory Dump Mayhem',
        'category': 'Digital Forensics',
        'difficulty': Challenge.DIFFICULTY_MEDIUM,
        'points': 275,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'PhishFinder',
        'solves': 654,
        'tags': ['forensics', 'memory', 'volatility', 'artifacts'],
        'description': 'A memory dump from a compromised machine contains hidden evidence.',
        'scenario': 'A forensic analyst captured a memory dump before the machine was wiped.',
        'instructions': 'Use forensic analysis tools to extract the flag from the dump.',
        'flag': 'CTF{v0l4t1l1ty_v1ct0ry}',
        'hints': [
            {'text': "Use Volatility's strings plugin and grep for CTF{", 'cost': 25},
        ],
    },
    {
        'slug': 'sample-xss-playground',
        'title': 'XSS Playground',
        'category': 'Web Exploitation',
        'difficulty': Challenge.DIFFICULTY_MEDIUM,
        'points': 200,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'NullByte',
        'solves': 2430,
        'tags': ['xss', 'javascript', 'web', 'cookies'],
        'description': 'A comment system is vulnerable to Cross-Site Scripting.',
        'scenario': 'A vulnerable blog platform does not sanitize user input.',
        'instructions': 'Craft an XSS payload to exfiltrate the admin cookie.',
        'connection_info': {'url': 'http://xss-blog.challenge.local', 'port': 8000},
        'flag': 'CTF{x55_c00k13_th13f}',
        'hints': [
            {'text': 'The comment field is vulnerable. Try a simple payload first.', 'cost': 10},
        ],
    },
    {
        'slug': 'sample-crackme-zero',
        'title': 'Crackme Zero',
        'category': 'Reverse Engineering',
        'difficulty': Challenge.DIFFICULTY_MEDIUM,
        'points': 300,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'ByteWitch',
        'solves': 876,
        'tags': ['binary', 'reversing', 'disassembly', 'elf'],
        'description': 'A compiled binary checks a secret passphrase. Reverse engineer it.',
        'scenario': 'A suspicious executable was found on a compromised machine.',
        'instructions': 'Download the binary and find the correct password.',
        'flag': 'CTF{r3v3rs3_3ng1n33r1ng_ftw}',
        'hints': [
            {'text': 'Use strings on the binary first.', 'cost': 25},
        ],
    },
    {
        'slug': 'sample-warmup',
        'title': 'Welcome to the Platform',
        'category': 'Miscellaneous',
        'difficulty': Challenge.DIFFICULTY_EASY,
        'points': 50,
        'status': Challenge.STATUS_PUBLISHED,
        'author': 'FlagHunt Team',
        'solves': 12480,
        'tags': ['warmup', 'beginner', 'misc'],
        'description': 'A warm-up challenge. The flag is hidden in the challenge description itself.',
        'scenario': 'Not every challenge requires advanced tools.',
        'instructions': 'The flag is CTF{w3lc0m3_t0_cyb3rf0rg3}. Submit it to claim your first points!',
        'flag': 'CTF{w3lc0m3_t0_cyb3rf0rg3}',
        'hints': [],
    },
    {
        # A draft, so the admin workflow (edit/publish/delete) can be exercised.
        'slug': 'sample-kernel-panic',
        'title': 'Kernel Panic',
        'category': 'Binary Exploitation',
        'difficulty': Challenge.DIFFICULTY_INSANE,
        'points': 750,
        'status': Challenge.STATUS_DRAFT,
        'author': 'ShadowRoot',
        'solves': 0,
        'tags': ['kernel', 'uaf', 'privesc', 'pwn', 'linux'],
        'description': 'A Linux kernel module has a use-after-free vulnerability.',
        'scenario': 'A custom kernel module has a memory management flaw.',
        'instructions': 'Escalate to root and read /root/flag.txt.',
        'connection_info': {'url': 'ssh ctf@kernel.challenge.local', 'port': 2222},
        'flag': 'CTF{k3rn3l_pwn3d_0wn3d}',
        'hints': [
            {'text': 'The ioctl handler is missing proper locking.', 'cost': 75},
        ],
    },
]


class Command(BaseCommand):
    help = (
        'Seed development categories and sample challenges. Idempotent; '
        'refuses to run outside DEBUG without --allow-production.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--allow-production',
            action='store_true',
            help='Run even when DEBUG is False (deliberate override).',
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['allow_production']:
            raise CommandError(
                'Refusing to seed: DEBUG is False. This command is for local '
                'development/testing. Pass --allow-production to override.'
            )

        with transaction.atomic():
            categories_created = self._seed_categories()
            challenges_created = self._seed_challenges()

        self.stdout.write(self.style.SUCCESS(
            f'Categories: {categories_created} created, '
            f'{Category.objects.count()} total.'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'Challenges: {challenges_created} created, '
            f'{Challenge.objects.count()} total.'
        ))
        self.stdout.write(
            'Development flags (not retrievable from any API):'
        )
        for sample in SAMPLE_CHALLENGES:
            self.stdout.write(f'  {sample["title"]}: {sample["flag"]}')

    # ------------------------------------------------------------------ #

    def _seed_categories(self):
        created = 0
        for order, (name, icon, color, description) in enumerate(
            CATEGORY_DEFINITIONS
        ):
            _, was_created = Category.objects.get_or_create(
                name=name,
                defaults={
                    'icon': icon,
                    'color': color,
                    'description': description,
                    'order': order,
                },
            )
            created += int(was_created)
        return created

    def _seed_challenges(self):
        created = 0
        for sample in SAMPLE_CHALLENGES:
            data = dict(sample)
            flag = data.pop('flag')
            hints = data.pop('hints', [])
            category = Category.objects.get(name=data.pop('category'))

            challenge, was_created = Challenge.objects.get_or_create(
                slug=data['slug'],
                defaults={**data, 'category': category},
            )
            if not was_created:
                continue
            created += 1

            stored = ChallengeFlag(challenge=challenge)
            stored.set_flag(flag)
            stored.save()

            # Replace any hints that may have come along with the defaults.
            challenge.hints.all().delete()
            ChallengeHint.objects.bulk_create([
                ChallengeHint(
                    challenge=challenge,
                    text=hint['text'],
                    cost=hint.get('cost', 0),
                    order=index,
                )
                for index, hint in enumerate(hints)
            ])
        return created
