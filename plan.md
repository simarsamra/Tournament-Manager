## Plan: Full Individual/Team Decoupling

Implement true participant-level enrollment for individual tournaments while preserving the existing team-based match engine through internal shadow competitors. This removes user-facing team coupling in individual mode, fixes enrollment checks, and makes Test Maker mode-aware.

**Steps**
1. Add participant data model (blocks all downstream logic):
- Add `Team.is_internal` to mark hidden/internal competitors.
- Add `TournamentIndividualRegistration` with: `tournament`, `user`, `display_name`, `shadow_team`, `status`, `withdrawn_at`, `group`, `seed`, timestamps.
- Add constraints:
: unique `(tournament, user)`
: unique `(tournament, display_name)`
: unique `(tournament, shadow_team)`
2. Add migration and data safety hooks (*depends on 1*):
- Create schema migration for the new field/model.
- Add a management command to normalize existing individual-mode tournaments:
: detect team-based enrollments in individual tournaments
: create participant registrations
: assign/create shadow teams where missing
: optionally deactivate legacy rows not linked to registrations.
3. Introduce registration-mode-aware helper layer in `views.py` (*depends on 1*):
- `_get_user_tournament_ids(user)` combines:
: team-mode enrollments from memberships/participations
: individual-mode enrollments from `TournamentIndividualRegistration`
- `_is_user_enrolled_in_tournament(user, tournament)` checks by `registration_mode`.
- `_get_individual_registration(user, tournament)` returns active registration.
- `_ensure_shadow_team_for_registration(registration, sport_type)` creates/links internal competitor and participation.
- Update `_get_team(user, tournament)`:
: team mode -> membership team
: individual mode -> registration shadow team
4. Rewire selection and context flows (*depends on 3*):
- `_get_tournament` for non-organizers uses `_is_user_enrolled_in_tournament` and `user_tournament_ids` helper.
- `_tournament_context` computes switcher/joinable from unified enrollment IDs.
- `select_tournament` uses enrollment helper.
- `join_tournament_list_view` computes `already_joined` using unified IDs.
5. Rework join/register flow for individual mode (*depends on 3,4*):
- `join_tournament_view`:
: individual mode uses registration object (`user_registration`) not membership.
: participants list should come from registrations, not generic teams.
- `create_team_view` individual branch:
: create/update `TournamentIndividualRegistration`
: ensure linked shadow team + participation
: avoid creating TeamMembership for individual registrations.
- Keep team-mode logic unchanged for team tournaments.
6. Update templates to remove team semantics in individual mode (*depends on 5*):
- `join_tournament.html`:
: use participant phrasing and statuses (Registered/You)
: remove “View Team” affordance for individual mode
- `join_tournament_list.html`:
: show “participants” label for individual tournaments
: show joined-state copy as participant-centric
- `teams.html` / team-facing surfaces:
: for individual tournaments, either hide page entry or clearly relabel as Participants and hide internal teams.
7. Make Test Maker mode-aware (core requirement) (*depends on 1,3*):
- In `test_maker_view` action `create_test_teams`:
: team mode -> existing behavior (teams + memberships)
: individual mode -> create users + participant registrations + shadow teams/participations (no multi-member teams)
- Disable/guard actions incompatible with individual mode:
: randomize court preferences (team-level) either skip or explicit warning
- Update counters/messages to “participants” in individual mode.
- Update `test_maker.html` labels and fields dynamically by `registration_mode`.
8. Admin and visibility hardening (*parallel with 6/7 once model exists*):
- Register `TournamentIndividualRegistration` in admin.
- Update admin list displays/filters for visibility.
- Ensure internal teams (`is_internal=True`) are excluded from normal team pages unless explicit debug/admin context.
9. Validate and backfill test2 scenario (*depends on 1-8*):
- Run migration and backfill command on test data.
- Verify `t1p1` in individual tournament is represented as participant registration.
- Ensure dashboard/join status does not present team enrollment wording for individual mode.

**Relevant files**
- `core/models.py` — add participant model + internal team marker.
- `core/migrations/0018_*.py` — schema migration.
- `core/management/commands/*.py` — normalization/backfill command.
- `core/views.py` — helper layer + enrollment/join/test-maker rewiring.
- `core/admin_config.py` — register participant model.
- `templates/core/join_tournament_list.html` — participant-centric labels.
- `templates/core/join_tournament.html` — participant flow rendering.
- `templates/core/test_maker.html` — mode-aware UI/wording.
- `templates/core/teams.html` and `templates/core/partials/dashboard_content.html` — hide internal teams and mode-aware copy.

**Verification**
1. `python manage.py makemigrations` then `python manage.py migrate`.
2. `python manage.py check`.
3. Individual tournament flow:
- register a user -> registration row created
- no TeamMembership required
- dashboard shows participant context without team confusion.
4. Team tournament flow remains unchanged:
- join/create team, memberships, scheduling, score submit, reschedule.
5. Test Maker:
- team mode creates teams+members
- individual mode creates participants+shadow competitors.
6. Backfill command on `test2`:
- converts/normalizes legacy team-coupled individual rows.

**Decisions**
- Keep the existing match/standings engine team-based for now (risk-controlled).
- Use `TournamentIndividualRegistration` as the source of truth for individual mode.
- Use internal shadow competitors only as execution detail; never as user-facing enrollment identity.
- Exclude internal teams from normal user-facing lists.

**Further Considerations**
1. Legacy data policy recommendation:
Option A (recommended): Preserve existing match history by mapping legacy teams to registrations and marking teams internal.
Option B: Hard reset test tournaments only and regenerate with mode-aware Test Maker.
2. Future architecture:
Option A (recommended for now): Keep shadow-team bridge.
Option B (later): Replace team-only match schema with generic competitor abstraction.
