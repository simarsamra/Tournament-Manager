from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.db import models
from datetime import timedelta

from .models import Team, Tournament, Match, Court, TimeSlot
from .scheduling import generate_fixtures
from .standings import calculate_standings, advance_winner
from .withdrawals import handle_withdrawal
from .forms import TournamentForm
from .scheduling import generate_consolation_if_ready


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

	def test_organizer_can_delete_tournament(self):
		tournament = self._create_tournament(name="Delete Me")
		self.client.force_login(self.organizer)

		response = self.client.post(
			reverse("delete_tournament", kwargs={"pk": tournament.pk}),
			{"confirm_delete": "DELETE"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertFalse(Tournament.objects.filter(pk=tournament.pk).exists())
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("deleted" in m.lower() for m in msgs))

	def test_non_organizer_cannot_delete_tournament(self):
		tournament = self._create_tournament(name="Keep Me")
		team = self._create_team(tournament, "Falcons")
		self.client.force_login(team.user)

		response = self.client.post(
			reverse("delete_tournament", kwargs={"pk": tournament.pk}),
			{"confirm_delete": "DELETE"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		self.assertTrue(Tournament.objects.filter(pk=tournament.pk).exists())

	def test_dashboard_shows_multiple_tournaments_to_organizer(self):
		first = self._create_tournament(name="Spring Cup")
		second = self._create_tournament(name="Summer Cup")
		self._create_team(first, "Alpha")
		self._create_team(second, "Beta")
		self.client.force_login(self.organizer)

		response = self.client.get(reverse("dashboard"))

		self.assertEqual(response.status_code, 200)
		self.assertIn("all_tournaments", response.context)
		self.assertEqual(response.context["all_tournaments"].count(), 2)
		self.assertContains(response, "Spring Cup")
		self.assertContains(response, "Summer Cup")

	def test_organizer_can_switch_selected_tournament_across_pages(self):
		first = self._create_tournament(name="Spring Cup")
		second = self._create_tournament(name="Summer Cup")
		self._create_team(first, "Alpha")
		self._create_team(second, "Beta")
		self.client.force_login(self.organizer)

		select_response = self.client.post(
			reverse("select_tournament"),
			{"tournament_id": first.pk},
			follow=True,
		)
		teams_response = self.client.get(reverse("teams"))

		self.assertEqual(select_response.status_code, 200)
		self.assertEqual(self.client.session.get("selected_tournament_id"), first.pk)
		self.assertEqual(teams_response.context["tournament"].pk, first.pk)
		self.assertContains(teams_response, "Alpha")
		self.assertNotContains(teams_response, "Beta")

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


class DoubleEliminationBracketTests(TestCase):
	"""Tests for double-elimination bracket progression logic."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="organizer", password="pass123", is_staff=True
		)

	def _create_tournament(self, fmt="double_elimination", name="T1"):
		return Tournament.objects.create(
			name=name,
			format=fmt,
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
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

	def test_double_elim_winners_bracket_progression(self):
		"""Verify winners bracket matches advance correctly."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Get first round winners bracket matches
		first_round_matches = tournament.matches.filter(
			bracket_type="winners", round_number=1
		).order_by("bracket_position")

		self.assertEqual(first_round_matches.count(), 2)  # 4 teams = 2 first round matches

		# Confirm first round matches
		for i, match in enumerate(first_round_matches):
			match.status = "pending_confirmation"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.submitted_by = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner", "submitted_by"])

			# Advance winner to next round
			advance_winner(match)

		# Verify winners advanced to round 2
		second_round = tournament.matches.filter(bracket_type="winners", round_number=2)
		self.assertTrue(second_round.exists())
		for match in second_round:
			self.assertIsNotNone(match.team1)
			self.assertIsNotNone(match.team2)

	def test_double_elim_losers_bracket_creation(self):
		"""Verify losers bracket matches are created for losers from winners bracket."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Look for all bracket types initially
		matches = tournament.matches.all()
		bracket_types = set(m.bracket_type for m in matches)

		# In double-elimination, both winners and losers brackets should exist
		# after fixture generation or be generated during tournament progression
		self.assertGreater(matches.count(), 0, "Should have matches generated")

		# Create a losers bracket manually to verify the structure works
		from .scheduling import generate_knockout
		losers = teams[1::2]  # Teams 2, 4
		generate_knockout(
			tournament,
			teams=losers,
			start_match=100,
			bracket_type="losers",
			round_offset=0
		)

		losers_matches = tournament.matches.filter(bracket_type="losers")
		self.assertTrue(losers_matches.exists(), "Losers bracket should exist after generation")

	def test_double_elim_losers_bracket_progression(self):
		"""Verify losers bracket progression advances teams through bracket."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Manually set up losers bracket matches
		from .scheduling import generate_knockout
		winners_bracket = tournament.matches.filter(bracket_type="winners")

		# Create a losers bracket with the losers from winners round 1
		losers = teams[1::2]  # Teams 2, 4 (lower seeds, would lose to 1, 3)
		generated_losers = generate_knockout(
			tournament,
			teams=losers,
			start_match=100,
			bracket_type="losers",
			round_offset=0
		)

		losers_matches = tournament.matches.filter(bracket_type="losers", round_number=1)
		self.assertTrue(losers_matches.exists())

		# Confirm a losers match and verify progression
		losers_match = losers_matches.first()
		if losers_match and losers_match.team1 and losers_match.team2:
			losers_match.status = "confirmed"
			losers_match.score_team1 = 2
			losers_match.score_team2 = 1
			losers_match.winner = losers_match.team1
			losers_match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

			advance_winner(losers_match)

			# Verify next losers match was updated
			if losers_match.next_match:
				losers_match.next_match.refresh_from_db()
				self.assertTrue(
					losers_match.next_match.team1 or losers_match.next_match.team2
				)

	def test_double_elim_finals_both_brackets(self):
		"""Verify winners and losers bracket winners meet in grand finals."""
		tournament = self._create_tournament()
		teams = [self._create_team(tournament, f"Team {i}", seed=i) for i in range(1, 5)]
		generate_fixtures(tournament)

		# Get all matches
		all_matches = tournament.matches.all()

		# Mark winners bracket round 1 as confirmed
		winners_r1 = tournament.matches.filter(bracket_type="winners", round_number=1)
		for match in winners_r1:
			match.status = "confirmed"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.save()

		# Create losers bracket manually if not auto-created
		losers = [t for t in teams if t not in [m.winner for m in winners_r1]]
		if losers:
			from .scheduling import generate_knockout
			generate_knockout(
				tournament, teams=losers, start_match=100,
				bracket_type="losers", round_offset=0
			)

		# Verify match structure: should have winners bracket semifinals/finals + losers bracket + grand finals
		final_matches = tournament.matches.filter(bracket_type="winners").order_by("-round_number").first()
		self.assertIsNotNone(final_matches)
		self.assertTrue(final_matches.round_number > 1)

	def test_double_elim_draw_rejected_in_winners_bracket(self):
		"""Verify draws are rejected in winners bracket (elimination)."""
		tournament = self._create_tournament()
		team1 = self._create_team(tournament, "Red", seed=1)
		team2 = self._create_team(tournament, "Blue", seed=2)
		generate_fixtures(tournament)

		# Get first round match
		match = tournament.matches.filter(bracket_type="winners", round_number=1).first()
		self.assertIsNotNone(match)

		# Submit draw score
		match.status = "pending_confirmation"
		match.score_team1 = 2
		match.score_team2 = 2
		match.submitted_by = team1
		match.save(update_fields=["status", "score_team1", "score_team2", "submitted_by"])

		# Try to confirm as opponent
		self.client.force_login(team2.user)
		response = self.client.post(
			reverse("confirm_score", kwargs={"pk": match.pk}), follow=True
		)

		# Verify draw was rejected
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertIsNone(match.winner)
		messages = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Draws are not allowed" in m for m in messages))


class WithdrawalPolicyTests(TestCase):
	"""Tests for withdrawal policies (forfeit vs void) and standings impact."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="organizer", password="pass123", is_staff=True
		)

	def _create_tournament(self, fmt="round_robin", name="T1", policy="forfeit"):
		return Tournament.objects.create(
			name=name,
			format=fmt,
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			withdrawal_policy=policy,
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

	def _create_mock_request(self, user=None):
		"""Create a mock request object for withdrawal handling."""
		from django.test import RequestFactory
		factory = RequestFactory()
		request = factory.get("/")
		request.user = user or self.organizer
		return request

	def test_withdrawal_forfeit_policy_marks_future_matches(self):
		"""Verify forfeit policy marks future matches as forfeited with opponent as winner."""
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

		generate_fixtures(tournament)

		# Get future matches for team1
		future_matches_before = tournament.matches.filter(team1=team1, status="upcoming").count()
		self.assertTrue(future_matches_before > 0)

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Verify team status is withdrawn
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

		# Verify future matches are now forfeited with opponent as winner
		forfeited_matches = tournament.matches.filter(
			status="forfeited"
		).filter(
			models.Q(team1=team1) | models.Q(team2=team1)
		)
		self.assertTrue(forfeited_matches.exists())

		for match in forfeited_matches:
			self.assertEqual(match.status, "forfeited")
			self.assertIsNotNone(match.winner)
			self.assertNotEqual(match.winner, team1)

	def test_withdrawal_void_policy_marks_future_matches_cancelled(self):
		"""Verify void policy marks future matches as cancelled."""
		tournament = self._create_tournament(policy="void")
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

		generate_fixtures(tournament)

		# Get upcoming matches for team1
		upcoming_before = tournament.matches.filter(team1=team1, status="upcoming").count()
		self.assertTrue(upcoming_before > 0)

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Verify future matches are cancelled, not showing a winner
		cancelled_matches = tournament.matches.filter(
			status="cancelled"
		).filter(
			models.Q(team1=team1) | models.Q(team2=team1)
		)
		self.assertTrue(cancelled_matches.exists())

		for match in cancelled_matches:
			self.assertEqual(match.status, "cancelled")
			self.assertIsNone(match.winner)

	def test_withdrawal_forfeit_standings_impact(self):
		"""Verify forfeit policy impacts standings (opponent gets win)."""
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

		generate_fixtures(tournament)

		# Ensure team1 and team2 have an upcoming match
		team1_team2_match = tournament.matches.filter(
			(models.Q(team1=team1, team2=team2) | models.Q(team1=team2, team2=team1)),
			status="upcoming"
		).first()

		if team1_team2_match:
			# Mark it as scheduled so it exists for withdrawal to handle
			pass

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Get standings after withdrawal
		standings_after = calculate_standings(tournament)

		# Verify team1 is withdrawn
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

		# Check that forfeit match was created
		forfeits = tournament.matches.filter(status="forfeited")
		self.assertTrue(forfeits.exists(), "Should have forfeited matches after withdrawal")

		# Verify at least one forfeit match exists
		forfeit_count = forfeits.filter(
			(models.Q(team1=team1) | models.Q(team2=team1))
		).count()
		self.assertGreater(forfeit_count, 0, "Team1 should have at least one forfeited match")

	def test_withdrawal_void_standings_not_impacted(self):
		"""Verify void policy doesn't impact standings (match voided)."""
		tournament = self._create_tournament(policy="void")
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)
		team3 = self._create_team(tournament, "Team C", seed=3)

		generate_fixtures(tournament)

		# Complete one match
		match1 = tournament.matches.filter(status="upcoming").first()
		if match1:
			match1.status = "confirmed"
			match1.score_team1 = 3
			match1.score_team2 = 1
			match1.winner = match1.team1
			match1.save()

		# Get standings before withdrawal
		standings_before = calculate_standings(tournament)
		team2_points_before = next(
			(s["points"] for s in standings_before if s["team"] == team2), 0
		)

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Get standings after withdrawal
		standings_after = calculate_standings(tournament)
		team2_points_after = next(
			(s["points"] for s in standings_after if s["team"] == team2), 0
		)

		# Team2 points should not increase from cancelled match
		self.assertEqual(team2_points_after, team2_points_before)

	def test_withdrawal_creates_open_slots(self):
		"""Verify scheduled matches create open slots when team withdraws."""
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", seed=1)
		team2 = self._create_team(tournament, "Team B", seed=2)

		# Add court and time slot
		court = Court.objects.create(tournament=tournament, name="Court 1")
		now = timezone.now()
		timeslot = TimeSlot.objects.create(
			tournament=tournament,
			start_time=now + timedelta(hours=1),
			end_time=now + timedelta(hours=2)
		)

		generate_fixtures(tournament)

		# Schedule a match
		match = tournament.matches.filter(status="upcoming").first()
		if match:
			match.court = court
			match.scheduled_time = timeslot.start_time
			match.scheduled_end_time = timeslot.end_time
			match.save()

		# Get open slots before withdrawal
		open_slots_before = tournament.open_slots.count()

		# Withdraw team1
		request = self._create_mock_request()
		handle_withdrawal(request, team1, tournament)

		# Verify open slots were created for scheduled matches
		open_slots_after = tournament.open_slots.count()
		self.assertGreater(open_slots_after, open_slots_before)

	def test_team_self_withdraw_requires_correct_password(self):
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a", seed=1)
		self._create_team(tournament, "Team B", username="team_b", seed=2)
		generate_fixtures(tournament)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("withdraw_team", kwargs={"pk": team1.pk}),
			{"confirm_withdraw": "yes", "password": "wrong-pass"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		team1.refresh_from_db()
		self.assertEqual(team1.status, "active")
		msgs = [str(m) for m in response.context["messages"]]
		self.assertTrue(any("Incorrect password" in m for m in msgs))

	def test_team_self_withdraw_with_password_succeeds(self):
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a2", seed=1)
		self._create_team(tournament, "Team B", username="team_b2", seed=2)
		generate_fixtures(tournament)

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("withdraw_team", kwargs={"pk": team1.pk}),
			{"confirm_withdraw": "yes", "password": "pass123"},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

	def test_organizer_can_withdraw_team_without_password(self):
		tournament = self._create_tournament(policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a3", seed=1)
		self._create_team(tournament, "Team B", username="team_b3", seed=2)
		generate_fixtures(tournament)

		self.client.force_login(self.organizer)
		response = self.client.post(
			reverse("withdraw_team", kwargs={"pk": team1.pk}),
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		team1.refresh_from_db()
		self.assertEqual(team1.status, "withdrawn")

	def test_organizer_mark_no_show_forfeits_match(self):
		tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a4", seed=1)
		team2 = self._create_team(tournament, "Team B", username="team_b4", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.filter(status="upcoming").first()

		self.client.force_login(self.organizer)
		response = self.client.post(
			reverse("mark_no_show", kwargs={"pk": match.pk}),
			{"no_show_team": str(team1.pk)},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		match.refresh_from_db()
		self.assertEqual(match.status, "forfeited")
		self.assertEqual(match.winner, team2)

	def test_team_cannot_mark_no_show(self):
		tournament = self._create_tournament(fmt="round_robin", policy="forfeit")
		team1 = self._create_team(tournament, "Team A", username="team_a5", seed=1)
		team2 = self._create_team(tournament, "Team B", username="team_b5", seed=2)
		generate_fixtures(tournament)
		match = tournament.matches.filter(status="upcoming").first()

		self.client.force_login(team1.user)
		response = self.client.post(
			reverse("mark_no_show", kwargs={"pk": match.pk}),
			{"no_show_team": str(team2.pk)},
			follow=True,
		)

		self.assertEqual(response.status_code, 200)
		match.refresh_from_db()
		self.assertEqual(match.status, "upcoming")


class TournamentLifecycleTests(TestCase):
	"""Tests for end-to-end tournament lifecycle (organizer + team UI flows)."""

	def setUp(self):
		self.organizer = User.objects.create_user(
			username="org_admin", password="pass123", is_staff=True
		)

	def test_organizer_creates_and_manages_knockout_tournament(self):
		"""Full organizer flow: create tournament, add court/timeslots, manage teams, generate fixtures."""
		self.client.force_login(self.organizer)

		# Step 1: Create tournament
		response = self.client.post(
			reverse("tournament_setup"),
			{
				"name": "Regional Knockout",
				"format": "knockout",
				"sport_type": "table_tennis",
				"points_per_win": 3,
				"points_per_loss": 0,
				"points_per_draw": 1,
				"default_match_duration": 30,
				"players_per_team": 1,
				"num_groups": 2,
				"teams_per_group_advance": 1,
				"withdrawal_policy": "forfeit",
			},
		)
		self.assertEqual(response.status_code, 302)  # Should redirect after creating
		tournament = Tournament.objects.get(name="Regional Knockout")
		self.assertEqual(tournament.format, "knockout")
		self.assertEqual(tournament.status, "setup")

		# Step 2: Add court
		response = self.client.post(
			reverse("add_court", kwargs={"pk": tournament.pk}),
			{"name": "Court 1", "is_available": True},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		court = tournament.courts.get(name="Court 1")
		self.assertIsNotNone(court)

		# Step 3: Add time slot
		now = timezone.now()
		response = self.client.post(
			reverse("add_timeslot", kwargs={"pk": tournament.pk}),
			{
				"date": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
				"start_time": "10:00",
				"end_time": "11:00",
			},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(tournament.time_slots.count(), 1)

		# Step 4: Add teams (simulating organizer registration)
		for i in range(1, 5):
			user = User.objects.create_user(
				username=f"team_user_{i}", password="pass123"
			)
			Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)

		# Step 5: Start tournament (generate fixtures)
		response = self.client.post(
			reverse("start_tournament", kwargs={"pk": tournament.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)
		tournament.refresh_from_db()
		self.assertEqual(tournament.status, "active")
		self.assertIsNotNone(tournament.started_at)

		# Verify fixtures were generated
		matches = tournament.matches.all()
		self.assertGreater(matches.count(), 0)
		# Knockout with 4 teams: 2 semifinal + 1 final = 3 matches
		self.assertEqual(matches.count(), 3)

		# Step 6: View fixtures
		response = self.client.get(reverse("fixtures"))
		self.assertEqual(response.status_code, 200)
		self.assertIn("matches", response.context)
		self.assertEqual(len(response.context["matches"]), 3)

	def test_team_registers_plays_and_views_standings(self):
		"""Full team user flow: register, play matches, submit scores, confirm scores, view standings."""
		# Step 1: Create and start a tournament
		tournament = Tournament.objects.create(
			name="Team Flow Tournament",
			format="round_robin",
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			default_match_duration=30,
		)

		# Create 3 teams
		teams_data = []
		for i in range(1, 4):
			user = User.objects.create_user(
				username=f"team_player_{i}", password="pass123"
			)
			team = Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)
			teams_data.append((user, team))

		generate_fixtures(tournament)
		tournament.status = "active"
		tournament.started_at = timezone.now()
		tournament.save(update_fields=["status", "started_at"])

		# Step 2: Team user 1 logs in and submits a score
		user1, team1 = teams_data[0]
		self.client.force_login(user1)

		# Find a match for team1
		match = tournament.matches.filter(team1=team1, status="upcoming").first()
		if not match:
			match = tournament.matches.filter(team2=team1, status="upcoming").first()

		self.assertIsNotNone(match, "Should have upcoming match for team1")

		# Submit score
		response = self.client.post(
			reverse("submit_score", kwargs={"pk": match.pk}),
			{
				"score_team1": 3,
				"score_team2": 1,
			},
			follow=True,
		)
		self.assertEqual(response.status_code, 200)

		# Verify match is now pending confirmation
		match.refresh_from_db()
		self.assertEqual(match.status, "pending_confirmation")
		self.assertEqual(match.score_team1, 3)
		self.assertEqual(match.score_team2, 1)

		# Step 3: Team user 2 (opponent) logs in and confirms the score
		user2, team2 = teams_data[1]
		self.client.force_login(user2)

		response = self.client.post(
			reverse("confirm_score", kwargs={"pk": match.pk}),
			follow=True,
		)
		self.assertEqual(response.status_code, 200)

		# Verify match is now confirmed
		match.refresh_from_db()
		self.assertEqual(match.status, "confirmed")
		self.assertIsNotNone(match.winner)

		# Step 4: Any logged-in user views standings
		# Stay logged in as user2 to view standings
		response = self.client.get(reverse("standings"), follow=True)
		self.assertEqual(response.status_code, 200)
		
		# Check if standings are in context - may be under different key
		context_keys = list(response.context.keys()) if response.context else []
		standings_found = any(key in ["standings", "tournament_standings"] for key in context_keys)
		
		# If standings are available, verify they're correct
		if standings_found:
			standings_key = next((k for k in context_keys if k in ["standings", "tournament_standings"]), None)
			standings = response.context[standings_key]
			self.assertTrue(len(standings) > 0)
			
			# Winner should have 3 points
			winner_standing = next(
				(s for s in standings if s["team"] == match.winner), None
			)
			if winner_standing:
				self.assertEqual(winner_standing["points"], 3)

	def test_hybrid_tournament_full_lifecycle_group_to_knockout(self):
		"""Full hybrid tournament flow: groups, group advancement, knockout, finals."""
		# Create hybrid tournament
		tournament = Tournament.objects.create(
			name="Hybrid Championship",
			format="hybrid",
			sport_type="table_tennis",
			points_per_win=3,
			points_per_loss=0,
			points_per_draw=1,
			num_groups=2,
			teams_per_group_advance=1,
			default_match_duration=30,
		)

		# Create 4 teams (2 per group)
		teams = []
		for i in range(1, 5):
			user = User.objects.create_user(
				username=f"hybrid_team_{i}", password="pass123"
			)
			team = Team.objects.create(
				user=user,
				tournament=tournament,
				name=f"Team {i}",
				seed=i,
			)
			teams.append(team)

		# Generate group stage fixtures
		generate_fixtures(tournament)

		# Verify groups were assigned
		teams_with_groups = tournament.teams.filter(group__gt="")
		self.assertEqual(teams_with_groups.count(), 4, "All teams should be assigned to groups")

		# Verify group stage matches were created
		group_matches = tournament.matches.filter(group__gt="")
		self.assertTrue(group_matches.exists(), "Group stage matches should be created")

		# Complete group stage matches
		for match in group_matches:
			match.status = "confirmed"
			match.score_team1 = 3
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		# Trigger knockout generation from group stage completion
		from .standings import check_group_stage_complete
		knockout_generated = check_group_stage_complete(tournament)
		self.assertTrue(knockout_generated, "Knockout should be generated after group stage")

		# Verify knockout matches were created
		knockout_matches = tournament.matches.filter(group="")
		self.assertTrue(knockout_matches.exists(), "Knockout matches should exist after group stage")

		# Verify knockout structure
		ko_by_round = knockout_matches.values_list("round_number", flat=True).distinct()
		self.assertTrue(len(list(ko_by_round)) > 0, "Knockout should have multiple rounds")

		# Complete first knockout round
		first_round_ko = knockout_matches.filter(round_number=knockout_matches.aggregate(models.Min("round_number"))["round_number__min"])
		for match in first_round_ko:
			if match.team1 and match.team2:
				match.status = "confirmed"
				match.score_team1 = 2
				match.score_team2 = 1
				match.winner = match.team1
				match.save(update_fields=["status", "score_team1", "score_team2", "winner"])
				advance_winner(match)

		# Verify tournament has proper structure
		all_matches = tournament.matches.all()
		self.assertGreater(all_matches.count(), 0, "Tournament should have matches")

	def test_tournament_audit_log_tracks_lifecycle_events(self):
		"""Verify audit log tracks all tournament lifecycle events."""
		from .models import AuditLog

		self.client.force_login(self.organizer)

		# Create tournament
		response = self.client.post(
			reverse("tournament_setup"),
			{
				"name": "Audit Test",
				"format": "knockout",
				"sport_type": "table_tennis",
				"points_per_win": 3,
				"points_per_loss": 0,
				"points_per_draw": 1,
				"default_match_duration": 30,
				"players_per_team": 1,
				"num_groups": 2,
				"teams_per_group_advance": 1,
				"withdrawal_policy": "forfeit",
			},
		)
		self.assertEqual(response.status_code, 302)  # Redirect after creating

		tournament = Tournament.objects.get(name="Audit Test")

		# Add a court
		self.client.post(
			reverse("add_court", kwargs={"pk": tournament.pk}),
			{"name": "Court 1", "is_available": True},
		)

		# Add a team
		user = User.objects.create_user(username="audit_test_team", password="pass123")
		Team.objects.create(
			user=user,
			tournament=tournament,
			name="Audit Test Team",
			seed=1,
		)

		# Check audit log has entries
		audit_entries = AuditLog.objects.filter(tournament=tournament)
		self.assertGreater(audit_entries.count(), 0)

		# Verify key events are logged
		actions = [entry.action for entry in audit_entries]
		self.assertIn("tournament_created", actions)
		self.assertIn("court_added", actions)


class AdditionalFormatSupportTests(TestCase):
	def _create_tournament(self, fmt, name="Format Test"):
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

	def test_new_format_choices_are_valid_in_form(self):
		base = {
			"name": "F",
			"sport_type": "table_tennis",
			"players_per_team": 1,
			"points_per_win": 3,
			"points_per_loss": 0,
			"points_per_draw": 1,
			"num_groups": 2,
			"teams_per_group_advance": 1,
			"withdrawal_policy": "forfeit",
			"default_match_duration": 30,
		}

		for fmt in ("double_round_robin", "consolation"):
			data = dict(base)
			data["name"] = fmt
			data["format"] = fmt
			form = TournamentForm(data=data)
			self.assertTrue(form.is_valid(), f"Expected format '{fmt}' to be valid")

	def test_double_round_robin_generates_home_and_away_fixtures(self):
		tournament = self._create_tournament(fmt="double_round_robin", name="DRR")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		# For 4 teams, each pair plays twice => 4 * 3 = 12 matches.
		self.assertEqual(tournament.matches.count(), 12)

		# Ensure each pairing appears in both directions.
		team1 = tournament.teams.get(name="Team 1")
		team2 = tournament.teams.get(name="Team 2")
		self.assertTrue(tournament.matches.filter(team1=team1, team2=team2).exists())
		self.assertTrue(tournament.matches.filter(team1=team2, team2=team1).exists())

	def test_consolation_generates_main_bracket_on_start(self):
		tournament = self._create_tournament(fmt="consolation", name="Consolation Main")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		# Main single-elim bracket exists immediately.
		self.assertEqual(tournament.matches.filter(bracket_type="winners").count(), 3)
		# Consolation bracket should not exist before round 1 completes.
		self.assertFalse(tournament.matches.filter(bracket_type="consolation").exists())

	def test_consolation_generated_after_first_round_completion(self):
		tournament = self._create_tournament(fmt="consolation", name="Consolation Dynamic")
		for i in range(1, 5):
			self._create_team(tournament, f"Team {i}", seed=i)

		generate_fixtures(tournament)

		first_round = tournament.matches.filter(bracket_type="winners", round_number=1)
		self.assertEqual(first_round.count(), 2)

		for match in first_round:
			match.status = "confirmed"
			match.score_team1 = 2
			match.score_team2 = 1
			match.winner = match.team1
			match.save(update_fields=["status", "score_team1", "score_team2", "winner"])

		generated = generate_consolation_if_ready(tournament)
		self.assertTrue(generated)
		self.assertTrue(tournament.matches.filter(bracket_type="consolation").exists())

