from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("register/", views.register_view, name="register"),

    # Dashboard
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("", views.dashboard_view, name="home"),

    # Tournament setup
    path("tournament/setup/", views.tournament_setup, name="tournament_setup"),
    path("tournament/<int:pk>/config/", views.tournament_config, name="tournament_config"),
    path("tournament/<int:pk>/add-court/", views.add_court, name="add_court"),
    path("tournament/<int:pk>/add-timeslot/", views.add_timeslot, name="add_timeslot"),
    path("tournament/<int:pk>/add-teams/", views.add_teams_bulk, name="add_teams_bulk"),
    path("tournament/<int:pk>/start/", views.start_tournament, name="start_tournament"),

    # Fixtures
    path("fixtures/", views.fixtures_view, name="fixtures"),

    # Match
    path("match/<int:pk>/", views.match_detail, name="match_detail"),
    path("match/<int:pk>/submit-score/", views.submit_score, name="submit_score"),
    path("match/<int:pk>/confirm-score/", views.confirm_score, name="confirm_score"),
    path("match/<int:pk>/dispute-score/", views.dispute_score, name="dispute_score"),
    path("match/<int:pk>/resolve-dispute/", views.resolve_dispute, name="resolve_dispute"),
    path("match/<int:pk>/mark-no-show/", views.mark_no_show, name="mark_no_show"),

    # Rescheduling
    path("match/<int:pk>/reschedule/", views.request_reschedule, name="request_reschedule"),
    path("reschedule/<int:pk>/respond/", views.respond_reschedule, name="respond_reschedule"),
    path("rescheduling/", views.rescheduling_view, name="rescheduling"),

    # Teams
    path("teams/", views.teams_view, name="teams"),
    path("team/<int:pk>/", views.team_detail, name="team_detail"),
    path("team/<int:pk>/withdraw/", views.withdraw_team, name="withdraw_team"),
    path("team/<int:pk>/preferences/", views.team_preferences, name="team_preferences"),

    # Standings
    path("standings/", views.standings_view, name="standings"),

    # Open Slots
    path("open-slots/", views.open_slots_view, name="open_slots"),

    # Analytics
    path("analytics/", views.analytics_view, name="analytics"),

    # Backup & Restore
    path("backup/", views.backup_view, name="backup"),
    path("backup/create/", views.create_backup_view, name="create_backup"),
    path("backup/restore/", views.restore_backup_view, name="restore_backup"),
    path("backup/delete/", views.delete_backup_view, name="delete_backup"),

    # Audit Log
    path("audit-log/", views.audit_log_view, name="audit_log"),

    # Settings
    path("settings/", views.settings_view, name="settings"),

    # Public views (no login required)
    path("public/standings/", views.public_standings, name="public_standings"),
    path("public/fixtures/", views.public_fixtures, name="public_fixtures"),
]
