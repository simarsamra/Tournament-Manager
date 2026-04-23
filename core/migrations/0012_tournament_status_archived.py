from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_match_dispute_window_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tournament",
            name="status",
            field=models.CharField(
                choices=[
                    ("setup", "Setup"),
                    ("registration_open", "Registration Open"),
                    ("ready", "Ready for Scheduling"),
                    ("scheduled", "Schedule Draft Ready"),
                    ("active", "Active"),
                    ("completed", "Completed"),
                    ("archived", "Archived"),
                ],
                default="setup",
                max_length=20,
            ),
        ),
    ]
