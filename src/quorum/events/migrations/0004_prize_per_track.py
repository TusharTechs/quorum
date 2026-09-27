from django.db import migrations, models


def backfill(apps, schema_editor):
    # events seeded before the flag existed: the fixture's "Best in track" prize is per track
    apps.get_model("events", "Prize").objects.filter(name="Best in track", track__isnull=True).update(per_track=True)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0003_track_pairwise"),
    ]

    operations = [
        migrations.AddField(
            model_name="prize",
            name="per_track",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
