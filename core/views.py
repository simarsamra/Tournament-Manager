"""Core views for tournament management."""
import json
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models as db_models
from django.db.models import Q, Count, Avg, F
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Tournament, Court, TimeSlot, Team, Match,
    RescheduleRequest, OpenSlot, AuditLog, BackupRecord, Player, CourtAvailability,
)
from .forms import (
    TournamentForm, CourtForm, TimeSlotForm, TeamRegistrationForm,
    ScoreSubmitForm, RescheduleForm, TeamPreferencesForm, BulkTeamForm,
    BulkTeamFileForm, CourtAvailabilityForm,
)
from .scheduling import (
    generate_fixtures,
    generate_consolation_if_ready,
    estimate_required_matches,
    count_available_slots,
)
from .standings import calculate_standings, advance_winner, get_bracket_data, check_group_stage_complete
from .withdrawals import handle_withdrawal
from .backup import create_backup, validate_backup, restore_backup, list_backups, delete_backup
from .audit import log_action


def _get_available_tournaments():
    return Tournament.objects.annotate(
        team_count=Count("teams", distinct=True),
        match_count=Count("matches", distinct=True),
    ).annotate(
        status_rank=db_models.Case(
            db_models.When(status="active", then=db_models.Value(0)),
            db_models.When(status="registration_open", then=db_models.Value(1)),
            db_models.When(status="ready", then=db_models.Value(2)),
            db_models.When(status="scheduled", then=db_models.Value(3)),
            db_models.When(status="setup", then=db_models.Value(4)),
            db_models.When(status="completed", then=db_models.Value(5)),
            default=db_models.Value(6),
            output_field=db_models.IntegerField(),
        )
    ).order_by("status_rank", "-created_at")


def _get_tournament(request=None):
    tournaments = Tournament.objects.all()
    if request and getattr(request, "user", None) and request.user.is_authenticated:
        team = _get_team(request.user)
        if team:
            return team.tournament
        if _is_organizer(request.user):
            selected_id = request.GET.get("tournament") or request.session.get("selected_tournament_id")
            if selected_id and tournaments.filter(pk=selected_id).exists():
                selected = tournaments.get(pk=selected_id)
                request.session["selected_tournament_id"] = selected.pk
                return selected
    fallback = _get_available_tournaments().first()
    if (
        fallback
        and request
        and getattr(request, "user", None)
        and request.user.is_authenticated
        and _is_organizer(request.user)
    ):
        request.session["selected_tournament_id"] = fallback.pk
    return fallback


def _tournament_context(request, tournament=None):
    if not request.user.is_authenticated or not _is_organizer(request.user):
        return {}
    return {
        "available_tournaments": _get_available_tournaments(),
        "selected_tournament": tournament,
    }


def _get_team(user):
    try:
        return user.team
    except (Team.DoesNotExist, AttributeError):
        return None


def _is_organizer(user):
    return user.is_staff or user.is_superuser


def _safe_page_param(request, default=1):
    """Return a safe positive page number from query params."""
    raw = request.GET.get("page", default)
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return default
    return page if page > 0 else default


def _validate_tournament_ready(tournament):
    """Return a list of human-friendly reasons a tournament cannot start yet."""
    errors = []
    active_teams = list(
        tournament.teams.filter(status="active").prefetch_related("players", "preferred_courts")
    )
    active_count = len(active_teams)

    if active_count < 2:
        errors.append("Need at least 2 active teams.")

    if tournament.expected_teams_count and active_count != tournament.expected_teams_count:
        errors.append(
            f"Registered teams ({active_count}) must match the expected team count ({tournament.expected_teams_count})."
        )

    required_players = max(1, tournament.players_per_team or 1)
    insufficient_players = [team.name for team in active_teams if team.players.count() < required_players]
    if insufficient_players:
        errors.append(
            "These teams do not have enough registered players: " + ", ".join(insufficient_players[:5]) + "."
        )

    if not tournament.courts.filter(is_available=True).exists():
        errors.append("Add at least one available court before starting.")
    else:
        missing_preferences = [team.name for team in active_teams if not team.preferred_courts.exists()]
        if missing_preferences:
            errors.append(
                "These teams still need court preferences: " + ", ".join(missing_preferences[:5]) + "."
            )

    has_schedule_source = (
        CourtAvailability.objects.filter(court__tournament=tournament, is_active=True).exists()
        or tournament.time_slots.exists()
    )
    if not has_schedule_source:
        errors.append("Add court availability or manual time slots before starting.")
    else:
        required_matches = estimate_required_matches(tournament, team_count=active_count)
        available_slots = count_available_slots(tournament)
        if required_matches and available_slots < required_matches:
            errors.append(
                f"Not enough court availability to schedule this tournament ({available_slots} available slots for about {required_matches} matches)."
            )

    return errors


# -- Auth Views --

def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            log_action(request, "login", f"User '{username}' logged in")
            return redirect("dashboard")
        messages.error(request, "Invalid credentials.")
    return render(request, "core/login.html")


def logout_view(request):
    if request.user.is_authenticated:
        log_action(request, "logout", f"User '{request.user.username}' logged out")
    logout(request)
    return redirect("login")


def register_view(request, pk=None):
    open_tournaments = Tournament.objects.filter(status="registration_open").order_by("start_date", "created_at")
    tournament = get_object_or_404(Tournament, pk=pk) if pk is not None else None
    if tournament is None and open_tournaments.count() == 1:
        tournament = open_tournaments.first()

    if not tournament:
        return render(request, "core/register.html", {
            "form": TeamRegistrationForm(),
            "open_tournaments": open_tournaments,
        })

    if tournament.status != "registration_open":
        messages.error(request, "Registration is currently closed for this tournament.")
        return render(request, "core/register.html", {
            "form": TeamRegistrationForm(tournament=tournament),
            "tournament": tournament,
            "players_per_team": tournament.players_per_team if tournament else 1,
            "registration_closed": True,
            "open_tournaments": open_tournaments,
        })

    if request.method == "POST":
        form = TeamRegistrationForm(request.POST, tournament=tournament)
        if form.is_valid():
            team_name = form.cleaned_data["team_name"]
            if Team.objects.filter(tournament=tournament, name=team_name).exists():
                form.add_error("team_name", "Team name already exists in this tournament.")
                return render(request, "core/register.html", {
                    "form": form,
                    "tournament": tournament,
                    "players_per_team": tournament.players_per_team if tournament else 1,
                    "open_tournaments": open_tournaments,
                })
            user = User.objects.create_user(
                username=form.cleaned_data["username"],
                password=form.cleaned_data["password"],
            )
            team = Team.objects.create(
                user=user,
                tournament=tournament,
                name=team_name,
            )
            team.preferred_courts.set(form.cleaned_data.get("preferred_courts", []))
            player_names_text = form.cleaned_data.get("player_names", "").strip()
            if player_names_text:
                for pname in player_names_text.split("\n"):
                    pname = pname.strip()
                    if pname:
                        Player.objects.create(team=team, name=pname)
            log_action(request, "team_registered",
                       f"Team '{form.cleaned_data['team_name']}' registered",
                       tournament=tournament)
            login(request, user)
            return redirect("dashboard")
    else:
        form = TeamRegistrationForm(tournament=tournament)
    return render(request, "core/register.html", {
        "form": form,
        "tournament": tournament,
        "players_per_team": tournament.players_per_team if tournament else 1,
        "open_tournaments": open_tournaments,
    })


# -- Dashboard --

@login_required
def dashboard_view(request):
    tournament = _get_tournament(request)
    team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    context = {
        "tournament": tournament,
        "team": team,
        "is_organizer": is_organizer,
    }
    if tournament and team:
        team_matches = Match.objects.filter(
            tournament=tournament
        ).filter(Q(team1=team) | Q(team2=team)).order_by("scheduled_time", "match_number")
        context["upcoming_matches"] = team_matches.filter(
            status__in=["upcoming", "in_progress"]
        )[:5]
        context["pending_matches"] = team_matches.filter(
            status="pending_confirmation"
        ).exclude(submitted_by=team)
        context["recent_matches"] = team_matches.filter(
            status__in=["confirmed", "forfeited"]
        ).order_by("-updated_at")[:5]
        context["pending_reschedules"] = RescheduleRequest.objects.filter(
            match__in=team_matches, status="pending",
        ).exclude(requested_by=team)
    if is_organizer:
        all_tournaments = _get_available_tournaments()
        context["all_tournaments"] = all_tournaments
        context["active_tournaments_count"] = all_tournaments.filter(status="active").count()
        context["setup_tournaments_count"] = all_tournaments.filter(
            status__in=["setup", "registration_open", "ready", "scheduled"]
        ).count()
        context["completed_tournaments_count"] = all_tournaments.filter(status="completed").count()
    if tournament and is_organizer:
        context["total_teams"] = tournament.teams.count()
        context["total_matches"] = tournament.matches.count()
        context["confirmed_matches"] = tournament.matches.filter(status="confirmed").count()
        context["pending_matches_count"] = tournament.matches.filter(status="pending_confirmation").count()
        context["disputed_matches"] = tournament.matches.filter(status="disputed").count()
    context.update(_tournament_context(request, tournament))
    return render(request, "core/dashboard.html", context)


# -- Tournament Setup --

@login_required
def tournament_setup(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can set up tournaments.")
        return redirect("dashboard")
    if request.method == "POST":
        form = TournamentForm(request.POST)
        if form.is_valid():
            t = form.save()
            request.session["selected_tournament_id"] = t.pk
            log_action(request, "tournament_created",
                       f"Tournament '{t.name}' created ({t.get_format_display()})",
                       tournament=t)
            messages.success(request, f"Tournament '{t.name}' created.")
            return redirect("tournament_config", pk=t.pk)
    else:
        form = TournamentForm()
    return render(request, "core/tournament_setup.html", {
        "form": form,
        **_tournament_context(request, _get_tournament(request)),
    })


@login_required
def tournament_config(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    request.session["selected_tournament_id"] = tournament.pk
    teams = tournament.teams.prefetch_related("players", "preferred_courts").all()
    return render(request, "core/tournament_config.html", {
        "tournament": tournament,
        "courts": tournament.courts.all(),
        "court_availabilities": CourtAvailability.objects.filter(court__tournament=tournament).select_related("court"),
        "teams": teams,
        "time_slots": tournament.time_slots.select_related("court").all(),
        "court_form": CourtForm(),
        "timeslot_form": TimeSlotForm(tournament=tournament),
        "court_availability_form": CourtAvailabilityForm(tournament=tournament),
        "bulk_team_form": BulkTeamForm(),
        "bulk_team_file_form": BulkTeamFileForm(),
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def add_court(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = CourtForm(request.POST)
    if form.is_valid():
        court = form.save(commit=False)
        court.tournament = tournament
        court.save()
        log_action(request, "court_added", f"Court '{court.name}' added", tournament=tournament)
        messages.success(request, f"Court '{court.name}' added.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def add_court_availability(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = CourtAvailabilityForm(request.POST, tournament=tournament)
    if form.is_valid():
        courts = list(form.cleaned_data["courts"])
        weekdays = [int(day) for day in form.cleaned_data["weekdays"]]
        start_time = form.cleaned_data["start_time"]
        end_time = form.cleaned_data["end_time"]
        start_date = form.cleaned_data.get("start_date")
        end_date = form.cleaned_data.get("end_date")
        is_active = form.cleaned_data.get("is_active", False)

        existing_keys = set(
            CourtAvailability.objects.filter(
                court__in=courts,
                weekday__in=weekdays,
                start_time=start_time,
                end_time=end_time,
                start_date=start_date,
                end_date=end_date,
            ).values_list("court_id", "weekday")
        )

        to_create = []
        skipped_count = 0
        for court in courts:
            for weekday in weekdays:
                key = (court.id, weekday)
                if key in existing_keys:
                    skipped_count += 1
                    continue
                to_create.append(CourtAvailability(
                    court=court,
                    weekday=weekday,
                    start_time=start_time,
                    end_time=end_time,
                    start_date=start_date,
                    end_date=end_date,
                    is_active=is_active,
                ))
                existing_keys.add(key)

        created_count = len(to_create)
        if to_create:
            CourtAvailability.objects.bulk_create(to_create)
            log_action(
                request,
                "court_availability_added",
                f"Added {created_count} availability entries across {len(courts)} court(s)",
                tournament=tournament,
            )
            messages.success(request, f"Added {created_count} availability entr{'y' if created_count == 1 else 'ies'}.")
        if skipped_count:
            messages.warning(request, f"Skipped {skipped_count} duplicate entr{'y' if skipped_count == 1 else 'ies'}.")
        if not created_count and not skipped_count:
            messages.warning(request, "No court availability was added.")
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def add_timeslot(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    form = TimeSlotForm(request.POST, tournament=tournament)
    if form.is_valid():
        date = form.cleaned_data["date"]
        start = form.cleaned_data["start_time"]
        end = form.cleaned_data["end_time"]
        court = form.cleaned_data.get("court")
        if end <= start:
            messages.error(request, "End time must be after start time.")
            return redirect("tournament_config", pk=pk)
        start_dt = timezone.make_aware(datetime.combine(date, start))
        end_dt = timezone.make_aware(datetime.combine(date, end))
        TimeSlot.objects.create(
            tournament=tournament,
            court=court,
            start_time=start_dt,
            end_time=end_dt,
        )
        details = f"Time slot added: {start_dt} - {end_dt}"
        if court:
            details += f" on {court.name}"
        log_action(request, "timeslot_added", details, tournament=tournament)
        messages.success(request, "Time slot added.")
    return redirect("tournament_config", pk=pk)


def _parse_team_line(line):
    """Parse a single team line: team_name,username,password[,player1;player2;...]."""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    team_name, username, password = parts[0], parts[1], parts[2]
    player_names = []
    if len(parts) >= 4 and parts[3]:
        player_names = [p.strip() for p in parts[3].split(";") if p.strip()]
    return {"team_name": team_name, "username": username, "password": password, "player_names": player_names}


def _create_teams_from_data(tournament, team_data_list, request):
    """Create teams and players from parsed data. Returns count of added teams."""
    added = 0
    for data in team_data_list:
        team_name = data["team_name"]
        username = data["username"]
        password = data["password"]
        player_names = data.get("player_names", [])
        if User.objects.filter(username=username).exists():
            messages.warning(request, f"Username '{username}' already exists, skipped.")
            continue
        if tournament.teams.filter(name=team_name).exists():
            messages.warning(request, f"Team '{team_name}' already exists, skipped.")
            continue
        user = User.objects.create_user(username=username, password=password)
        team = Team.objects.create(user=user, tournament=tournament, name=team_name)
        for pname in player_names:
            Player.objects.create(team=team, name=pname)
        added += 1
    return added


@login_required
@require_POST
def add_teams_bulk(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)

    team_data_list = []

    # Handle text input
    form = BulkTeamForm(request.POST)
    if form.is_valid():
        text = form.cleaned_data.get("teams_text", "").strip()
        if text:
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parsed = _parse_team_line(line)
                if parsed:
                    team_data_list.append(parsed)

    # Handle file upload
    file_form = BulkTeamFileForm(request.POST, request.FILES)
    if file_form.is_valid() and request.FILES.get("file"):
        uploaded = request.FILES["file"]
        content = uploaded.read().decode("utf-8", errors="ignore")
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_team_line(line)
            if parsed:
                team_data_list.append(parsed)

    if team_data_list:
        added = _create_teams_from_data(tournament, team_data_list, request)
        log_action(request, "teams_bulk_added", f"Added {added} teams", tournament=tournament)
        messages.success(request, f"{added} teams added.")
    else:
        messages.warning(request, "No valid team data found.")

    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def open_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    tournament.status = "registration_open"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_opened", f"Registration opened for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Registration is now open.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def close_registration(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    tournament.status = "ready"
    tournament.save(update_fields=["status"])
    log_action(request, "registration_closed", f"Registration closed for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Registration closed. The tournament is ready for scheduling checks.")
    return redirect("tournament_config", pk=pk)


@login_required
@require_POST
def generate_schedule(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    readiness_errors = _validate_tournament_ready(tournament)
    if readiness_errors:
        for error in readiness_errors:
            messages.error(request, error)
        return redirect("tournament_config", pk=pk)
    generate_fixtures(tournament)
    tournament.status = "scheduled"
    tournament.save(update_fields=["status"])
    log_action(request, "schedule_generated", f"Draft schedule generated for '{tournament.name}'", tournament=tournament)
    messages.success(request, "Draft schedule generated. Review fixtures before publishing.")
    return redirect("fixtures")


@login_required
@require_POST
def start_tournament(request, pk):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = get_object_or_404(Tournament, pk=pk)
    if tournament.status != "scheduled":
        readiness_errors = _validate_tournament_ready(tournament)
        if readiness_errors:
            for error in readiness_errors:
                messages.error(request, error)
            return redirect("tournament_config", pk=pk)
        generate_fixtures(tournament)
        tournament.status = "scheduled"
        tournament.save(update_fields=["status"])
        messages.info(request, "Draft schedule was generated automatically before publishing.")
    tournament.status = "active"
    tournament.started_at = timezone.now()
    tournament.save(update_fields=["status", "started_at"])
    log_action(request, "tournament_started",
               f"Tournament '{tournament.name}' started with "
               f"{tournament.teams.filter(status='active').count()} teams",
               tournament=tournament)
    messages.success(request, "Tournament started! Fixtures are now live.")
    return redirect("fixtures")


@login_required
@require_POST
def select_tournament(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can switch tournaments.")
        return redirect("dashboard")

    tournament_id = request.POST.get("tournament_id")
    next_url = request.POST.get("next") or "dashboard"
    tournament = Tournament.objects.filter(pk=tournament_id).first()
    if not tournament:
        messages.error(request, "Tournament not found.")
        return redirect(next_url)

    request.session["selected_tournament_id"] = tournament.pk
    messages.success(request, f"Now viewing '{tournament.name}'.")
    return redirect(next_url)


# -- Fixtures --

@login_required
def fixtures_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/fixtures.html", {
            "matches": [],
            **_tournament_context(request, tournament),
        })
    matches = tournament.matches.select_related("team1", "team2", "court", "winner")
    status_filter = request.GET.get("status", "")
    team_filter = request.GET.get("team", "")
    court_filter = request.GET.get("court", "")
    group_filter = request.GET.get("group", "")
    if status_filter:
        matches = matches.filter(status=status_filter)
    if team_filter:
        matches = matches.filter(Q(team1_id=team_filter) | Q(team2_id=team_filter))
    if court_filter:
        matches = matches.filter(court_id=court_filter)
    if group_filter:
        matches = matches.filter(group=group_filter)
    sort = request.GET.get("sort", "match_number")
    if sort == "time":
        matches = matches.order_by("scheduled_time", "match_number")
    elif sort == "status":
        matches = matches.order_by("status", "match_number")
    else:
        matches = matches.order_by("match_number")
    page = _safe_page_param(request)
    per_page = 25
    total = matches.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    matches = matches[(page - 1) * per_page : page * per_page]
    teams = tournament.teams.all()
    courts = tournament.courts.all()
    groups = sorted(set(tournament.teams.exclude(group="").values_list("group", flat=True)))
    return render(request, "core/fixtures.html", {
        "tournament": tournament,
        "matches": matches,
        "teams": teams,
        "courts": courts,
        "groups": groups,
        "status_filter": status_filter,
        "team_filter": team_filter,
        "court_filter": court_filter,
        "group_filter": group_filter,
        "sort": sort,
        "page": page,
        "total_pages": total_pages,
        "page_range": range(1, total_pages + 1),
        "team": _get_team(request.user),
        **_tournament_context(request, tournament),
    })


# -- Match Detail & Score Submission --

@login_required
def match_detail(request, pk):
    match = get_object_or_404(
        Match.objects.select_related("team1", "team2", "court", "winner", "submitted_by", "confirmed_by"),
        pk=pk,
    )
    team = _get_team(request.user)
    is_participant = team and (match.team1 == team or match.team2 == team)
    can_submit = is_participant and match.status in ("upcoming", "in_progress")
    can_confirm = (is_participant and match.status == "pending_confirmation" and match.submitted_by != team)
    can_dispute = can_confirm
    can_mark_no_show = _is_organizer(request.user) and bool(match.team1_id and match.team2_id) and match.status in ("upcoming", "in_progress")
    return render(request, "core/match_detail.html", {
        "match": match,
        "team": team,
        "tournament": match.tournament,
        "is_participant": is_participant,
        "can_submit": can_submit,
        "can_confirm": can_confirm,
        "can_dispute": can_dispute,
        "can_mark_no_show": can_mark_no_show,
        "score_form": ScoreSubmitForm(),
        "reschedule_form": RescheduleForm(tournament=match.tournament),
        "reschedule_requests": match.reschedule_requests.order_by("-created_at"),
        "is_organizer": _is_organizer(request.user),
        **_tournament_context(request, match.tournament),
    })


@login_required
@require_POST
def submit_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    team = _get_team(request.user)
    if not team or (match.team1 != team and match.team2 != team):
        messages.error(request, "You are not a participant in this match.")
        return redirect("match_detail", pk=pk)
    if match.status not in ("upcoming", "in_progress"):
        messages.error(request, "Score cannot be submitted for this match.")
        return redirect("match_detail", pk=pk)
    form = ScoreSubmitForm(request.POST)
    if form.is_valid():
        match.score_team1 = form.cleaned_data["score_team1"]
        match.score_team2 = form.cleaned_data["score_team2"]
        match.submitted_by = team
        match.status = "pending_confirmation"
        if form.cleaned_data["notes"]:
            match.notes = form.cleaned_data["notes"]
        match.save()
        log_action(request, "score_submitted",
                   f"Score submitted for {match}: {match.score_team1}-{match.score_team2}",
                   tournament=match.tournament)
        messages.success(request, "Score submitted. Waiting for opponent confirmation.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def confirm_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    team = _get_team(request.user)
    if not team or match.submitted_by == team:
        messages.error(request, "Cannot confirm your own submission.")
        return redirect("match_detail", pk=pk)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return redirect("match_detail", pk=pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "You are not a participant in this match.")
        return redirect("match_detail", pk=pk)
    is_elimination = (
        tournament := match.tournament
    ).format in ("knockout", "double_elimination", "consolation") or (
        tournament.format == "hybrid" and not match.group
    )
    if is_elimination and match.score_team1 == match.score_team2:
        messages.error(request, "Draws are not allowed in elimination matches.")
        return redirect("match_detail", pk=pk)
    match.confirmed_by = team
    match.status = "confirmed"
    if match.score_team1 > match.score_team2:
        match.winner = match.team1
    elif match.score_team2 > match.score_team1:
        match.winner = match.team2
    match.save()
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        advance_winner(match)
    if tournament.format == "consolation":
        generate_consolation_if_ready(tournament)
    if tournament.format == "hybrid" and match.group:
        check_group_stage_complete(tournament)
    log_action(request, "score_confirmed",
               f"Score confirmed for {match}: {match.score_team1}-{match.score_team2}",
               tournament=tournament)
    messages.success(request, "Score confirmed!")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def dispute_score(request, pk):
    match = get_object_or_404(Match, pk=pk)
    team = _get_team(request.user)
    if not team or match.submitted_by == team:
        messages.error(request, "Cannot dispute your own submission.")
        return redirect("match_detail", pk=pk)
    if match.status != "pending_confirmation":
        messages.error(request, "Match is not pending confirmation.")
        return redirect("match_detail", pk=pk)
    dispute_note = request.POST.get("dispute_notes", "").strip()
    match.status = "disputed"
    match.notes = f"DISPUTED by {team.name}: {dispute_note}" if dispute_note else f"DISPUTED by {team.name}"
    match.save()
    log_action(request, "score_disputed",
               f"Score disputed for {match} by {team.name}: {dispute_note}",
               tournament=match.tournament)
    messages.warning(request, "Score has been disputed. An organizer will review.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def resolve_dispute(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can resolve disputes.")
        return redirect("match_detail", pk=pk)
    match = get_object_or_404(Match, pk=pk)
    score1 = request.POST.get("final_score_team1")
    score2 = request.POST.get("final_score_team2")
    if score1 is not None and score2 is not None:
        try:
            final_score1 = int(score1)
            final_score2 = int(score2)
        except (TypeError, ValueError):
            messages.error(request, "Scores must be valid whole numbers.")
            return redirect("match_detail", pk=pk)
        if final_score1 < 0 or final_score2 < 0:
            messages.error(request, "Scores cannot be negative.")
            return redirect("match_detail", pk=pk)
        tournament = match.tournament
        is_elimination = tournament.format in ("knockout", "double_elimination", "consolation") or (
            tournament.format == "hybrid" and not match.group
        )
        if is_elimination and final_score1 == final_score2:
            messages.error(request, "Draws are not allowed in elimination matches.")
            return redirect("match_detail", pk=pk)

        match.score_team1 = final_score1
        match.score_team2 = final_score2
        match.status = "confirmed"
        if match.score_team1 > match.score_team2:
            match.winner = match.team1
        elif match.score_team2 > match.score_team1:
            match.winner = match.team2
        match.notes += f"\nResolved by organizer."
        match.save()
        if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
            advance_winner(match)
        if tournament.format == "consolation":
            generate_consolation_if_ready(tournament)
        if tournament.format == "hybrid" and match.group:
            check_group_stage_complete(tournament)
        log_action(request, "dispute_resolved",
                   f"Dispute resolved for {match}: {match.score_team1}-{match.score_team2}",
                   tournament=tournament)
        messages.success(request, "Dispute resolved.")
    return redirect("match_detail", pk=pk)


# -- Rescheduling --

@login_required
@require_POST
def request_reschedule(request, pk):
    match = get_object_or_404(Match, pk=pk)
    team = _get_team(request.user)
    if not team or (match.team1 != team and match.team2 != team):
        messages.error(request, "Not a participant.")
        return redirect("match_detail", pk=pk)
    if match.status not in ("upcoming",):
        messages.error(request, "Only upcoming matches can be rescheduled.")
        return redirect("match_detail", pk=pk)
    form = RescheduleForm(request.POST, tournament=match.tournament)
    if form.is_valid():
        new_dt = timezone.make_aware(
            datetime.combine(form.cleaned_data["new_date"], form.cleaned_data["new_time"])
        )
        new_court = form.cleaned_data.get("new_court") or match.court
        duration = timedelta(minutes=match.tournament.default_match_duration)
        end_dt = new_dt + duration
        conflicts = Match.objects.filter(
            tournament=match.tournament, court=new_court,
            scheduled_time__lt=end_dt, scheduled_end_time__gt=new_dt,
        ).exclude(pk=match.pk).exclude(status__in=["cancelled", "forfeited"])
        if conflicts.exists():
            messages.error(request, "The selected slot has a conflict.")
            return redirect("match_detail", pk=pk)
        RescheduleRequest.objects.create(
            match=match, requested_by=team, new_time=new_dt,
            new_court=new_court, reason=form.cleaned_data.get("reason", ""),
        )
        log_action(request, "reschedule_requested",
                   f"Reschedule requested for {match} to {new_dt}",
                   tournament=match.tournament)
        messages.success(request, "Reschedule request sent.")
    return redirect("match_detail", pk=pk)


@login_required
@require_POST
def respond_reschedule(request, pk):
    rr = get_object_or_404(RescheduleRequest, pk=pk)
    team = _get_team(request.user)
    match = rr.match
    if not team or rr.requested_by == team:
        messages.error(request, "Cannot respond to your own request.")
        return redirect("match_detail", pk=match.pk)
    if match.team1 != team and match.team2 != team:
        messages.error(request, "Not a participant.")
        return redirect("match_detail", pk=match.pk)
    action = request.POST.get("action")
    if action == "approve":
        rr.status = "approved"
        rr.responded_at = timezone.now()
        rr.save()
        if match.scheduled_time and match.court:
            OpenSlot.objects.create(
                tournament=match.tournament, court=match.court,
                start_time=match.scheduled_time,
                end_time=match.scheduled_end_time or match.scheduled_time,
                reason=f"Rescheduled: {match}",
            )
        duration = timedelta(minutes=match.tournament.default_match_duration)
        match.scheduled_time = rr.new_time
        match.scheduled_end_time = rr.new_time + duration
        if rr.new_court:
            match.court = rr.new_court
        match.save()
        log_action(request, "reschedule_approved", f"Reschedule approved for {match}",
                   tournament=match.tournament)
        messages.success(request, "Reschedule approved!")
    elif action == "reject":
        rr.status = "rejected"
        rr.responded_at = timezone.now()
        rr.save()
        log_action(request, "reschedule_rejected", f"Reschedule rejected for {match}",
                   tournament=match.tournament)
        messages.info(request, "Reschedule rejected.")
    return redirect("match_detail", pk=match.pk)


# -- Standings --

@login_required
def standings_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/standings.html", _tournament_context(request, tournament))
    context = {"tournament": tournament}
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        if tournament.format == "hybrid":
            groups = sorted(set(
                tournament.teams.exclude(group="").values_list("group", flat=True)
            ))
            group_standings = {}
            for g in groups:
                group_standings[g] = calculate_standings(tournament, group=g)
            context["group_standings"] = group_standings
            ko_matches = tournament.matches.filter(group="", bracket_type="winners")
            if ko_matches.exists():
                context["bracket"] = get_bracket_data(tournament)
        else:
            context["standings"] = calculate_standings(tournament)
    if tournament.format in ("knockout", "double_elimination", "consolation"):
        context["bracket"] = get_bracket_data(tournament)
    context.update(_tournament_context(request, tournament))
    return render(request, "core/standings.html", context)


# -- Teams --

@login_required
def teams_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/teams.html", {
            "teams": [],
            **_tournament_context(request, tournament),
        })
    teams = tournament.teams.select_related("user").prefetch_related("players").order_by("name")
    return render(request, "core/teams.html", {
        "tournament": tournament, "teams": teams,
        "is_organizer": _is_organizer(request.user),
        **_tournament_context(request, tournament),
    })


@login_required
def team_detail(request, pk):
    team = get_object_or_404(Team.objects.select_related("tournament", "user").prefetch_related("players"), pk=pk)
    tournament = team.tournament
    matches = Match.objects.filter(tournament=tournament).filter(
        Q(team1=team) | Q(team2=team)
    ).select_related("team1", "team2", "court", "winner").order_by("match_number")
    stats = {
        "played": matches.filter(status__in=["confirmed", "forfeited"]).count(),
        "wins": matches.filter(winner=team).count(),
        "upcoming": matches.filter(status__in=["upcoming", "in_progress"]).count(),
    }
    stats["losses"] = stats["played"] - stats["wins"]
    return render(request, "core/team_detail.html", {
        "team": team, "tournament": tournament, "matches": matches, "stats": stats,
        "players": team.players.all(),
        "is_organizer": _is_organizer(request.user),
        "is_own_team": _get_team(request.user) == team,
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def withdraw_team(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    is_organizer = _is_organizer(request.user)
    if team != user_team and not _is_organizer(request.user):
        messages.error(request, "Not authorized.")
        return redirect("team_detail", pk=pk)
    if team.status == "withdrawn":
        messages.info(request, f"Team '{team.name}' is already withdrawn.")
        return redirect("team_detail", pk=pk)

    # Team self-withdrawal requires explicit confirmation + password check.
    if team == user_team and not is_organizer:
        if request.POST.get("confirm_withdraw") != "yes":
            messages.error(request, "Please confirm withdrawal before continuing.")
            return redirect("team_detail", pk=pk)
        password = request.POST.get("password", "")
        if not password or not request.user.check_password(password):
            messages.error(request, "Incorrect password. Withdrawal cancelled.")
            return redirect("team_detail", pk=pk)

    handle_withdrawal(request, team, team.tournament)
    messages.success(request, f"Team '{team.name}' has been withdrawn.")
    return redirect("teams")


@login_required
@require_POST
def mark_no_show(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can mark no-shows.")
        return redirect("match_detail", pk=pk)

    match = get_object_or_404(Match, pk=pk)
    if match.status not in ("upcoming", "in_progress", "pending_confirmation"):
        messages.error(request, "No-show can only be recorded for active/upcoming matches.")
        return redirect("match_detail", pk=pk)

    no_show_team_id = request.POST.get("no_show_team")
    if str(match.team1_id) == str(no_show_team_id):
        loser = match.team1
        winner = match.team2
    elif str(match.team2_id) == str(no_show_team_id):
        loser = match.team2
        winner = match.team1
    else:
        messages.error(request, "Invalid team selected for no-show.")
        return redirect("match_detail", pk=pk)

    if not winner:
        messages.error(request, "Cannot mark no-show: opponent not assigned.")
        return redirect("match_detail", pk=pk)

    match.status = "forfeited"
    match.winner = winner
    match.notes = (match.notes + "\n" if match.notes else "") + f"No-show: {loser.name}"
    match.save(update_fields=["status", "winner", "notes"])

    tournament = match.tournament
    if tournament.format in ("knockout", "double_elimination", "consolation", "hybrid"):
        advance_winner(match)
    if tournament.format == "consolation":
        generate_consolation_if_ready(tournament)
    if tournament.format == "hybrid" and match.group:
        check_group_stage_complete(tournament)

    log_action(
        request,
        "match_no_show",
        f"No-show recorded for {match}. Loser: {loser.name}, Winner: {winner.name}",
        tournament=tournament,
    )
    messages.success(request, f"No-show recorded. {winner.name} wins by forfeit.")
    return redirect("match_detail", pk=pk)


@login_required
def team_preferences(request, pk):
    team = get_object_or_404(Team, pk=pk)
    user_team = _get_team(request.user)
    if team != user_team and not _is_organizer(request.user):
        messages.error(request, "Not authorized.")
        return redirect("team_detail", pk=pk)
    if request.method == "POST":
        form = TeamPreferencesForm(request.POST, tournament=team.tournament)
        if form.is_valid():
            team.preferred_courts.set(form.cleaned_data["preferred_courts"])
            team.availability_notes = form.cleaned_data["availability_notes"]
            team.save(update_fields=["availability_notes"])
            messages.success(request, "Preferences saved.")
            return redirect("team_detail", pk=pk)
    else:
        form = TeamPreferencesForm(
            tournament=team.tournament,
            initial={"preferred_courts": team.preferred_courts.all(), "availability_notes": team.availability_notes},
        )
    return render(request, "core/team_preferences.html", {
        "team": team,
        "form": form,
        "tournament": team.tournament,
        **_tournament_context(request, team.tournament),
    })


# -- Open Slots --

@login_required
def open_slots_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/open_slots.html", {
            "slots": [],
            **_tournament_context(request, tournament),
        })
    return render(request, "core/open_slots.html", {
        "tournament": tournament,
        "slots": tournament.open_slots.select_related("court").all(),
        **_tournament_context(request, tournament),
    })


# -- Analytics --

@login_required
def analytics_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/analytics.html", _tournament_context(request, tournament))
    matches = tournament.matches.all()
    teams = tournament.teams.all()
    match_stats = {
        "total": matches.count(),
        "confirmed": matches.filter(status="confirmed").count(),
        "upcoming": matches.filter(status="upcoming").count(),
        "in_progress": matches.filter(status="in_progress").count(),
        "pending": matches.filter(status="pending_confirmation").count(),
        "disputed": matches.filter(status="disputed").count(),
        "forfeited": matches.filter(status="forfeited").count(),
        "cancelled": matches.filter(status="cancelled").count(),
    }
    courts = tournament.courts.all()
    court_stats = []
    for court in courts:
        total = matches.filter(court=court).count()
        confirmed = matches.filter(court=court, status="confirmed").count()
        court_stats.append({
            "court": court, "total_matches": total, "confirmed_matches": confirmed,
            "utilization": round(confirmed / total * 100, 1) if total > 0 else 0,
        })
    team_stats = []
    for team in teams.filter(status="active"):
        team_matches = matches.filter(Q(team1=team) | Q(team2=team))
        wins = team_matches.filter(winner=team).count()
        played = team_matches.filter(status__in=["confirmed", "forfeited"]).count()
        team_stats.append({
            "team": team, "played": played, "wins": wins, "losses": played - wins,
            "win_rate": round(wins / played * 100, 1) if played > 0 else 0,
        })
    team_stats.sort(key=lambda x: x["win_rate"], reverse=True)
    schedule_density = defaultdict(int)
    for m in matches.filter(scheduled_time__isnull=False):
        day = m.scheduled_time.strftime("%Y-%m-%d")
        schedule_density[day] += 1
    schedule_density = dict(sorted(schedule_density.items()))
    withdrawn = teams.filter(status="withdrawn")
    withdrawal_info = []
    for team in withdrawn:
        affected = matches.filter(Q(team1=team) | Q(team2=team), status__in=["forfeited", "cancelled"]).count()
        withdrawal_info.append({"team": team, "affected_matches": affected, "withdrawn_at": team.withdrawn_at})
    recent_logs = AuditLog.objects.filter(tournament=tournament).order_by("-timestamp")[:20]
    context = {
        "tournament": tournament, "match_stats": match_stats, "court_stats": court_stats,
        "team_stats": team_stats, "schedule_density": json.dumps(schedule_density),
        "withdrawal_info": withdrawal_info, "recent_logs": recent_logs,
    }
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        context["standings"] = calculate_standings(tournament)
    context.update(_tournament_context(request, tournament))
    return render(request, "core/analytics.html", context)


# -- Rescheduling View --

@login_required
def rescheduling_view(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/rescheduling.html", _tournament_context(request, tournament))
    team = _get_team(request.user)
    requests_qs = RescheduleRequest.objects.filter(
        match__tournament=tournament
    ).select_related("match", "requested_by", "new_court").order_by("-created_at")
    if team and not _is_organizer(request.user):
        requests_qs = requests_qs.filter(
            Q(requested_by=team) | Q(match__team1=team) | Q(match__team2=team)
        )
    return render(request, "core/rescheduling.html", {
        "tournament": tournament, "requests": requests_qs,
        "open_slots": tournament.open_slots.select_related("court").all(), "team": team,
        **_tournament_context(request, tournament),
    })


# -- Backup & Restore --

@login_required
def backup_view(request):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can manage backups.")
        return redirect("dashboard")
    tournament = _get_tournament(request)
    return render(request, "core/backup.html", {
        "backups": list_backups(), "records": BackupRecord.objects.all()[:20],
        **_tournament_context(request, tournament),
    })


@login_required
@require_POST
def create_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    notes = request.POST.get("notes", "")
    record = create_backup(user=request.user, notes=notes)
    log_action(request, "backup_created", f"Backup created: {record.filename}", tournament=_get_tournament(request))
    messages.success(request, f"Backup created: {record.filename}")
    return redirect("backup")


@login_required
@require_POST
def restore_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    filename = request.POST.get("filename", "")
    filepath = settings.BACKUP_DIR / filename
    if not filepath.exists() or not filename.endswith(".json"):
        messages.error(request, "Invalid backup file.")
        return redirect("backup")
    valid, msg = validate_backup(filepath)
    if not valid:
        messages.error(request, f"Backup validation failed: {msg}")
        return redirect("backup")
    create_backup(user=request.user, is_auto=True, notes="Auto-backup before restore")
    restore_backup(filepath)
    log_action(request, "backup_restored", f"Restored from: {filename}")
    messages.success(request, f"Data restored from {filename}")
    return redirect("dashboard")


@login_required
@require_POST
def delete_backup_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    filename = request.POST.get("filename", "")
    if delete_backup(filename):
        log_action(request, "backup_deleted", f"Backup deleted: {filename}")
        messages.success(request, f"Backup deleted: {filename}")
    else:
        messages.error(request, "Backup not found.")
    return redirect("backup")


# -- Audit Log --

@login_required
def audit_log_view(request):
    tournament = _get_tournament(request)
    logs = AuditLog.objects.select_related("user")
    if tournament:
        logs = logs.filter(Q(tournament=tournament) | Q(tournament__isnull=True))
    action_filter = request.GET.get("action", "")
    if action_filter:
        logs = logs.filter(action=action_filter)
    page = _safe_page_param(request)
    per_page = 50
    total = logs.count()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    logs = logs[(page - 1) * per_page : page * per_page]
    actions = AuditLog.objects.values_list("action", flat=True).distinct()
    return render(request, "core/audit_log.html", {
        "logs": logs, "actions": actions, "action_filter": action_filter,
        "page": page, "total_pages": total_pages, "page_range": range(1, total_pages + 1),
        **_tournament_context(request, tournament),
    })


# -- Settings --

@login_required
@require_POST
def delete_tournament(request, pk):
    if not _is_organizer(request.user):
        messages.error(request, "Only organizers can delete tournaments.")
        return redirect("dashboard")

    tournament = get_object_or_404(Tournament, pk=pk)
    if request.POST.get("confirm_delete", "").strip().upper() != "DELETE":
        messages.error(request, "Tournament deletion was not confirmed.")
        return redirect("settings")

    tournament_name = tournament.name
    if request.session.get("selected_tournament_id") == tournament.pk:
        request.session.pop("selected_tournament_id", None)
    team_user_ids = list(
        tournament.teams.exclude(user__is_staff=True).values_list("user_id", flat=True)
    )
    tournament.delete()
    if team_user_ids:
        User.objects.filter(
            id__in=team_user_ids,
            is_staff=False,
            is_superuser=False,
        ).delete()

    log_action(request, "tournament_deleted", f"Tournament '{tournament_name}' deleted")
    messages.success(request, f"Tournament '{tournament_name}' deleted.")
    return redirect("dashboard")


@login_required
def settings_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/settings.html", _tournament_context(request, tournament))
    if request.method == "POST":
        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            form.save()
            log_action(request, "settings_updated", "Tournament settings updated", tournament=tournament)
            messages.success(request, "Settings updated.")
            return redirect("settings")
    else:
        form = TournamentForm(instance=tournament)
    return render(request, "core/settings.html", {
        "tournament": tournament,
        "form": form,
        **_tournament_context(request, tournament),
    })


# -- Public Views --

def public_standings(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_standings.html", {})
    context = {"tournament": tournament}
    if tournament.format in ("round_robin", "double_round_robin", "hybrid"):
        if tournament.format == "hybrid":
            groups = sorted(set(tournament.teams.exclude(group="").values_list("group", flat=True)))
            context["group_standings"] = {g: calculate_standings(tournament, group=g) for g in groups}
            ko_matches = tournament.matches.filter(group="", bracket_type="winners")
            if ko_matches.exists():
                context["bracket"] = get_bracket_data(tournament)
        else:
            context["standings"] = calculate_standings(tournament)
    if tournament.format in ("knockout", "double_elimination", "consolation"):
        context["bracket"] = get_bracket_data(tournament)
    return render(request, "core/public_standings.html", context)


def public_fixtures(request):
    tournament = _get_tournament(request)
    if not tournament:
        return render(request, "core/public_fixtures.html", {"matches": []})
    matches = tournament.matches.select_related("team1", "team2", "court", "winner").order_by("scheduled_time", "match_number")
    return render(request, "core/public_fixtures.html", {"tournament": tournament, "matches": matches})
