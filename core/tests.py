from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Court, Team, TimeSlot, Tournament
from .scheduling import generate_fixtures


class BracketSchedulingTests(TestCase):
    def _create_team(self, tournament, name, username):
        user = User.objects.create_user(username=username, password="pass123")
        return Team.objects.create(user=user, tournament=tournament, name=name)

    def _create_slot(self, tournament):
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        end = start + timedelta(hours=3)
        return TimeSlot.objects.create(tournament=tournament, start_time=start, end_time=end)

    def test_knockout_respects_timeslots_and_mutual_court_preferences(self):
        tournament = Tournament.objects.create(name="KO", format="knockout", default_match_duration=30)
        Court.objects.create(tournament=tournament, name="Court A")
        court_b = Court.objects.create(tournament=tournament, name="Court B")
        slot = self._create_slot(tournament)

        teams = [
            self._create_team(tournament, "T1", "t1"),
            self._create_team(tournament, "T2", "t2"),
            self._create_team(tournament, "T3", "t3"),
            self._create_team(tournament, "T4", "t4"),
        ]
        for team in teams:
            team.preferred_courts.add(court_b)

        generate_fixtures(tournament)

        round_one = tournament.matches.filter(round_number=1, status="upcoming").order_by("match_number")
        self.assertEqual(round_one.count(), 2)
        for match in round_one:
            self.assertEqual(match.court, court_b)
            self.assertIsNotNone(match.scheduled_time)
            self.assertGreaterEqual(match.scheduled_time, slot.start_time)
            self.assertLessEqual(match.scheduled_end_time, slot.end_time)

    def test_hybrid_group_matches_use_configured_slots_and_preferences(self):
        tournament = Tournament.objects.create(
            name="Hybrid",
            format="hybrid",
            num_groups=2,
            default_match_duration=30,
        )
        Court.objects.create(tournament=tournament, name="Court A")
        court_b = Court.objects.create(tournament=tournament, name="Court B")
        slot = self._create_slot(tournament)

        teams = [
            self._create_team(tournament, "H1", "h1"),
            self._create_team(tournament, "H2", "h2"),
            self._create_team(tournament, "H3", "h3"),
            self._create_team(tournament, "H4", "h4"),
        ]
        for team in teams:
            team.preferred_courts.add(court_b)

        generate_fixtures(tournament)

        group_matches = tournament.matches.filter(status="upcoming").order_by("match_number")
        self.assertEqual(group_matches.count(), 2)
        for match in group_matches:
            self.assertEqual(match.court, court_b)
            self.assertGreaterEqual(match.scheduled_time, slot.start_time)
            self.assertLessEqual(match.scheduled_end_time, slot.end_time)
