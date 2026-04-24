from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Team, TeamTournamentParticipation, TeamTournamentCourtPreference


class Command(BaseCommand):
    help = "Backfill TeamTournamentParticipation and TeamTournamentCourtPreference from existing Team records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without committing writes.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created_participations = 0
        updated_participations = 0
        created_preferences = 0

        with transaction.atomic():
            teams = Team.objects.select_related("tournament").prefetch_related("preferred_courts")
            for team in teams:
                participation, created = TeamTournamentParticipation.objects.get_or_create(
                    team=team,
                    tournament=team.tournament,
                    defaults={
                        "status": team.status,
                        "withdrawn_at": team.withdrawn_at,
                        "group": team.group,
                        "seed": team.seed,
                        "availability_notes": team.availability_notes,
                    },
                )
                if created:
                    created_participations += 1
                else:
                    fields_to_update = []
                    if participation.status != team.status:
                        participation.status = team.status
                        fields_to_update.append("status")
                    if participation.withdrawn_at != team.withdrawn_at:
                        participation.withdrawn_at = team.withdrawn_at
                        fields_to_update.append("withdrawn_at")
                    if participation.group != team.group:
                        participation.group = team.group
                        fields_to_update.append("group")
                    if participation.seed != team.seed:
                        participation.seed = team.seed
                        fields_to_update.append("seed")
                    if participation.availability_notes != team.availability_notes:
                        participation.availability_notes = team.availability_notes
                        fields_to_update.append("availability_notes")
                    if fields_to_update:
                        fields_to_update.append("updated_at")
                        participation.save(update_fields=fields_to_update)
                        updated_participations += 1

                for court in team.preferred_courts.all():
                    _, pref_created = TeamTournamentCourtPreference.objects.get_or_create(
                        participation=participation,
                        court=court,
                    )
                    if pref_created:
                        created_preferences += 1

            if dry_run:
                transaction.set_rollback(True)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run complete (no changes committed): "
                    f"created_participations={created_participations}, "
                    f"updated_participations={updated_participations}, "
                    f"created_preferences={created_preferences}"
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                "Backfill complete: "
                f"created_participations={created_participations}, "
                f"updated_participations={updated_participations}, "
                f"created_preferences={created_preferences}"
            )
        )
