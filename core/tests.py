from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Team, Tournament
from .scheduling import generate_fixtures


class UXAndLogicRegressionTests(TestCase):
	def setUp(self):
		self.organizer = User.objects.create_user(
			username="organizer", password="pass123", is_staff=True
		)

	def _create_tournament(self, fmt="round_robin", name="T1"):
		return Tournament.objects.create(
			name=name,
			format=fmt,
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			teams_per_group_advance=1,
			num_groups=2,
			default_match_duration=30,
		)

	def _create_team(self, tournament, team_name, username=None, seed=0):
		username = username or team_name.lower().replace(" ", "_")
		user = User.objects.create_user(username=username, password="pass123")
		return Team.objects.create(
			user=user,
			tournament=tournament,
			name=team_name,
			seed=seed,
		)

	def test_register_duplicate_team_name_shows_form_error(self):
		tournament = self._create_tournament()
		self._create_team(tournament, "Falcons", username="existing_user")

		response = self.client.post(
			reverse("register"),
			{
				"team_name": "Falcons",
				"username": "new_user",
				"password": "abc12345",
				"password_confirm": "abc12345",
			},
		)

		self.assertEqual(response.status_code, 200)
		self.assertContains(response, "Team name already exists")

	def test_fixtures_invalid_page_query_does_not_crash(self):
		tournament = self._create_tournament()
		self._create_team(tournament, "A")
		self._create_team(tournament, "B")
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("fixtures"), {"page": "abc"})

		self.assertEqual(response.status_code, 200)
		self.assertIn("matches", response.context)

	def test_audit_log_invalid_page_query_does_not_crash(self):
		self._create_tournament()
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("audit_log"), {"page": "bad"})

		self.assertEqual(response.status_code, 200)
		self.assertIn("logs", response.context)

	def test_add_timeslot_rejects_end_before_start(self):
		tournament = self._create_tournament()
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("add_timeslot", kwargs={"pk": tournament.pk}),
			{
				"date": "2026-04-20",
				"start_time": "11:00",
				"end_time": "10:00",
			},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertEqual(tournament.time_slots.count(), 0)
		messages = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("End time must be after start time" in m for m in messages))

	def test_knockout_disallows_draw_on_confirm(self):
		tournament = self._create_tournament(fmt="knockout")
		team1 = self._create_team(tournament, "Red", seed=1)
		team2 = self._create_team(tournament, "Blue", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.first()
		match.status = "pending_confirmation"
		match.score_team1 = 2
		match.score_team2 = 2
		match.submitted_by = team1
		match.save(update_fields=["status", "score_team1", "score_team2", "submitted_by"])

		self.client.force_login(team2.user)
		response = self.client.post(reverse("confirm_score", kwargs={"pk": match.pk}), follow=True)

		self.assertEqual(response.status_code, 200)
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertIsNone(match.winner)
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Draws are not allowed" in m for m in msgs))

	def test_public_hybrid_standings_includes_bracket_after_group_stage(self):
		tournament = self._create_tournament(fmt="hybrid")
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Complete group stage with non-draw confirmed scores.
		for match in tournament.matches.filter(group__gt=""):
			match.status = "confirmed"
			match.score_team1 = 3
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		# Trigger knockout generation from completed group stage.
		from .standings import check_group_stage_complete

		check_group_stage_complete(tournament)

		response = self.client.get(reverse("public_standings"))

		self.assertEqual(response.status_code, 200)
		self.assertIn("bracket", response.context)
		self.assertTrue(response.context["bracket"])
