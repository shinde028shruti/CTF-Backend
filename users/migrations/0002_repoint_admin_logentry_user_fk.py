from django.db import migrations

REPOINT_FORWARD = """
PRAGMA foreign_keys=OFF;
CREATE TABLE "django_admin_log__new" (
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "object_id" text NULL,
    "object_repr" varchar(200) NOT NULL,
    "action_flag" smallint unsigned NOT NULL CHECK ("action_flag" >= 0),
    "change_message" text NOT NULL,
    "content_type_id" integer NULL REFERENCES "django_content_type" ("id") DEFERRABLE INITIALLY DEFERRED,
    "user_id" bigint NOT NULL REFERENCES "users_user" ("id") DEFERRABLE INITIALLY DEFERRED,
    "action_time" datetime NOT NULL
);
INSERT INTO "django_admin_log__new" ("id", "object_id", "object_repr", "action_flag", "change_message", "content_type_id", "user_id", "action_time")
    SELECT "id", "object_id", "object_repr", "action_flag", "change_message", "content_type_id", "user_id", "action_time" FROM "django_admin_log";
DROP TABLE "django_admin_log";
ALTER TABLE "django_admin_log__new" RENAME TO "django_admin_log";
CREATE INDEX "django_admin_log_content_type_id_c4bce8eb" ON "django_admin_log" ("content_type_id");
CREATE INDEX "django_admin_log_user_id_c564eba6" ON "django_admin_log" ("user_id");
PRAGMA foreign_keys=ON;
"""

REPOINT_REVERSE = """
PRAGMA foreign_keys=OFF;
CREATE TABLE "django_admin_log__old" (
    "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT,
    "object_id" text NULL,
    "object_repr" varchar(200) NOT NULL,
    "action_flag" smallint unsigned NOT NULL CHECK ("action_flag" >= 0),
    "change_message" text NOT NULL,
    "content_type_id" integer NULL REFERENCES "django_content_type" ("id") DEFERRABLE INITIALLY DEFERRED,
    "user_id" integer NOT NULL REFERENCES "auth_user" ("id") DEFERRABLE INITIALLY DEFERRED,
    "action_time" datetime NOT NULL
);
INSERT INTO "django_admin_log__old" ("id", "object_id", "object_repr", "action_flag", "change_message", "content_type_id", "user_id", "action_time")
    SELECT "id", "object_id", "object_repr", "action_flag", "change_message", "content_type_id", "user_id", "action_time" FROM "django_admin_log";
DROP TABLE "django_admin_log";
ALTER TABLE "django_admin_log__old" RENAME TO "django_admin_log";
CREATE INDEX "django_admin_log_content_type_id_c4bce8eb" ON "django_admin_log" ("content_type_id");
CREATE INDEX "django_admin_log_user_id_c564eba6" ON "django_admin_log" ("user_id");
PRAGMA foreign_keys=ON;
"""


class Migration(migrations.Migration):
    """
    Repoints django_admin_log.user_id from auth_user to users_user.

    This project ran `migrate` with the default auth.User before AUTH_USER_MODEL
    was switched. Django's own autodetector cannot detect this: the LogEntry.user
    field in admin/migrations/0001_initial.py is declared as
    `to=settings.AUTH_USER_MODEL`, which is re-evaluated against current settings
    whenever migration state is built. Both the recorded state and the live model
    therefore already agree on users.User, so no AlterField is ever generated and
    the stale SQLite foreign key survives.

    SQLite cannot alter a foreign key in place, so the table is rebuilt.
    """

    dependencies = [
        ('admin', '0003_logentry_add_action_flag_choices'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            sql=REPOINT_FORWARD,
            reverse_sql=REPOINT_REVERSE,
        ),
    ]