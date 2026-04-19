"""Scheduling engine for generating tournament fixtures."""
import math
import itertools
from datetime import timedelta
from django.utils import timezone
from .models import Match, Court, TimeSlot, Team


def generate_round_robin(tournament):
    """Generate all-play-all fixtures."""
    teams = list(tournament.teams.filter(status="active").order_by("seed", "id"))
    n = len(teams)
    if n < 2:
        return

    matches_data = []
    if n % 2 == 1:
        teams.append(None)  # bye placeholder
        n += 1

    # Standard round-robin circle method
    fixed = teams[0]
    rotating = teams[1:]
    match_num = 1

    for round_idx in range(n - 1):
        round_pairs = []
        round_pairs.append((fixed, rotating[0]))
        for i in range(1, n // 2):
            round_pairs.append((rotating[i], rotating[n - 1 - i]))
        rotating = [rotating[-1]] + rotating[:-1]

        for t1, t2 in round_pairs:
            if t1 is None or t2 is None:
                continue
            matches_data.append({
                "team1": t1,
                "team2": t2,
                "round_number": round_idx + 1,
                "match_number": match_num,
            })
            match_num += 1

    _assign_schedule(tournament, matches_data)


def _bracket_seed_order(size):
    """Return standard bracket seed positions for given bracket size.

    For size=8: [1,8,4,5,3,6,2,7] — ensures top seeds are spread apart
    and byes go to the highest seeds.
    """
    if size == 1:
        return [1]
    half = _bracket_seed_order(size // 2)
    return [item for seed in half for item in (seed, size + 1 - seed)]


def generate_knockout(tournament, teams=None, start_match=1, bracket_type="winners",
                      round_offset=0, group=""):
    """Generate single-elimination bracket."""
    if teams is None:
        teams = list(tournament.teams.filter(status="active").order_by("seed", "id"))

    n = len(teams)
    if n < 2:
        return []

    # Calculate bracket size (next power of 2)
    bracket_size = 1
    while bracket_size < n:
        bracket_size *= 2

    num_rounds = int(math.log2(bracket_size))

    # Seed teams into bracket positions using standard bracket ordering
    seed_order = _bracket_seed_order(bracket_size)
    seeded = [None] * bracket_size
    for pos, seed_num in enumerate(seed_order):
        if seed_num <= n:
            seeded[pos] = teams[seed_num - 1]

    all_matches = []
    match_num = start_match
    current_round_matches = []

    # First round
    for i in range(0, bracket_size, 2):
        t1 = seeded[i]
        t2 = seeded[i + 1]
        is_bye = t1 is None or t2 is None
        m = Match(
            tournament=tournament,
            match_number=match_num,
            team1=t1,
            team2=t2,
            round_number=1 + round_offset,
            bracket_position=i // 2,
            bracket_type=bracket_type,
            status="bye" if is_bye else "upcoming",
            winner=t1 if (is_bye and t1) else (t2 if (is_bye and t2) else None),
            group=group,
        )
        current_round_matches.append(m)
        all_matches.append(m)
        match_num += 1

    # Save first round
    Match.objects.bulk_create(current_round_matches)

    # Subsequent rounds
    for round_num in range(2, num_rounds + 1):
        prev_matches = current_round_matches
        current_round_matches = []
        for i in range(0, len(prev_matches), 2):
            m = Match(
                tournament=tournament,
                match_number=match_num,
                round_number=round_num + round_offset,
                bracket_position=i // 2,
                bracket_type=bracket_type,
                status="upcoming",
                group=group,
            )
            current_round_matches.append(m)
            all_matches.append(m)
            match_num += 1

        Match.objects.bulk_create(current_round_matches)

        # Link previous round to next
        for i in range(0, len(prev_matches), 2):
            next_match = current_round_matches[i // 2]
            prev_matches[i].next_match = next_match
            prev_matches[i + 1].next_match = next_match
            prev_matches[i].save(update_fields=["next_match"])
            prev_matches[i + 1].save(update_fields=["next_match"])

            # Auto-advance byes
            if prev_matches[i].status == "bye" and prev_matches[i].winner:
                next_match.team1 = prev_matches[i].winner
            if prev_matches[i + 1].status == "bye" and prev_matches[i + 1].winner:
                next_match.team2 = prev_matches[i + 1].winner
            next_match.save(update_fields=["team1", "team2"])

    _assign_schedule_to_matches(tournament, all_matches)
    return all_matches


def generate_hybrid(tournament):
    """Generate group stage (round robin) then knockout brackets."""
    teams = list(tournament.teams.filter(status="active").order_by("seed", "id"))
    n = len(teams)
    num_groups = tournament.num_groups

    if n < num_groups * 2:
        num_groups = max(1, n // 2)

    # Assign groups using snake-draft seeding
    groups = {chr(65 + i): [] for i in range(num_groups)}
    group_keys = list(groups.keys())
    for idx, team in enumerate(teams):
        cycle = idx // num_groups
        if cycle % 2 == 0:
            g = group_keys[idx % num_groups]
        else:
            g = group_keys[num_groups - 1 - (idx % num_groups)]
        groups[g].append(team)
        team.group = g
        team.save(update_fields=["group"])

    # Generate round-robin for each group
    match_num = 1
    for group_name, group_teams in groups.items():
        gt = group_teams
        gn = len(gt)
        if gn < 2:
            continue

        if gn % 2 == 1:
            gt = gt + [None]
            gn += 1

        fixed = gt[0]
        rotating = gt[1:]
        for round_idx in range(gn - 1):
            pairs = [(fixed, rotating[0])]
            for i in range(1, gn // 2):
                pairs.append((rotating[i], rotating[gn - 1 - i]))
            rotating = [rotating[-1]] + rotating[:-1]

            for t1, t2 in pairs:
                if t1 is None or t2 is None:
                    continue
                Match.objects.create(
                    tournament=tournament,
                    match_number=match_num,
                    team1=t1,
                    team2=t2,
                    round_number=round_idx + 1,
                    group=group_name,
                    status="upcoming",
                )
                match_num += 1

    # Knockout matches will be generated after group stage completes
    _assign_schedule_to_existing(tournament)


def generate_double_elimination(tournament):
    """Generate double elimination bracket (winners + losers)."""
    # Start with a standard winners bracket
    teams = list(tournament.teams.filter(status="active").order_by("seed", "id"))
    generate_knockout(tournament, teams=teams, bracket_type="winners")
    # Losers bracket matches are created dynamically as teams are eliminated


def generate_fixtures(tournament):
    """Main entry point for fixture generation."""
    # Clear existing matches
    tournament.matches.all().delete()
    tournament.open_slots.all().delete()

    fmt = tournament.format
    if fmt == "round_robin":
        generate_round_robin(tournament)
    elif fmt == "knockout":
        generate_knockout(tournament)
    elif fmt == "double_elimination":
        generate_double_elimination(tournament)
    elif fmt == "hybrid":
        generate_hybrid(tournament)


def _build_slots(tournament, courts, duration):
    """Build available schedule slots from configured time slots or defaults."""
    time_slots = list(tournament.time_slots.order_by("start_time"))
    slots = []
    if time_slots:
        for ts in time_slots:
            current = ts.start_time
            while current + duration <= ts.end_time:
                for court in courts:
                    slots.append((current, current + duration, court))
                current += duration
    else:
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
        for day_offset in range(30):
            day_start = start + timedelta(days=day_offset)
            for hour_offset in range(12):
                slot_start = day_start + timedelta(minutes=30 * hour_offset)
                slot_end = slot_start + duration
                for court in courts:
                    slots.append((slot_start, slot_end, court))
    return slots


def _find_preferred_slot(slots, used_slots, pending_matches, t1, t2, preferred_court_ids=None):
    """Find next available slot, optionally restricted to preferred courts."""
    for start_t, end_t, court in slots:
        slot_key = (start_t, court.id)
        if slot_key in used_slots:
            continue
        if preferred_court_ids is not None and court.id not in preferred_court_ids:
            continue
        if _has_team_conflict(t1, t2, start_t, end_t, used_slots, pending_matches):
            continue
        return start_t, end_t, court
    return None, None, None


def _assign_schedule(tournament, matches_data):
    """Assign times and courts to match data dicts, then bulk create."""
    courts = list(tournament.courts.filter(is_available=True))
    duration = timedelta(minutes=tournament.default_match_duration)

    if not courts:
        # Create matches without schedule
        Match.objects.bulk_create([
            Match(tournament=tournament, **md) for md in matches_data
        ])
        return

    slots = _build_slots(tournament, courts, duration)

    # Assign slots to matches respecting preferences and conflicts
    used_slots = set()
    matches_to_create = []

    for md in matches_data:
        t1 = md["team1"]
        t2 = md["team2"]
        t1_prefs = set(t1.preferred_courts.values_list("id", flat=True)) if t1 else set()
        t2_prefs = set(t2.preferred_courts.values_list("id", flat=True)) if t2 else set()
        mutual_prefs = t1_prefs & t2_prefs

        start_t = end_t = court = None
        if mutual_prefs:
            start_t, end_t, court = _find_preferred_slot(
                slots, used_slots, matches_to_create, t1, t2, mutual_prefs
            )

        if start_t is None:
            start_t, end_t, court = _find_preferred_slot(
                slots, used_slots, matches_to_create, t1, t2
            )

        if start_t is not None:
            md["scheduled_time"] = start_t
            md["scheduled_end_time"] = end_t
            md["court"] = court
            used_slots.add((start_t, court.id))

        matches_to_create.append(Match(tournament=tournament, **md))

    Match.objects.bulk_create(matches_to_create)


def _has_team_conflict(t1, t2, start, end, used_slots, pending_matches):
    """Check if either team is already scheduled at this time."""
    for m in pending_matches:
        if m.scheduled_time and m.scheduled_end_time:
            if start < m.scheduled_end_time and end > m.scheduled_time:
                if m.team1 in (t1, t2) or m.team2 in (t1, t2):
                    return True
    return False


def _assign_schedule_to_matches(tournament, matches):
    """Assign schedule to already-created Match objects."""
    _assign_schedule_to_existing_matches(tournament, matches)


def _assign_schedule_to_existing(tournament):
    """Assign schedule to all upcoming matches of a tournament."""
    matches = list(tournament.matches.filter(status="upcoming").order_by("group", "round_number", "match_number"))
    _assign_schedule_to_existing_matches(tournament, matches)


def _assign_schedule_to_existing_matches(tournament, matches):
    """Assign schedule to existing Match objects using configured slots and preferences."""
    courts = list(tournament.courts.filter(is_available=True))
    if not courts or not matches:
        return

    duration = timedelta(minutes=tournament.default_match_duration)
    slots = _build_slots(tournament, courts, duration)
    used_slots = set()
    pending_matches = []

    for match in matches:
        if match.status == "bye":
            continue

        t1 = match.team1
        t2 = match.team2
        t1_prefs = set(t1.preferred_courts.values_list("id", flat=True)) if t1 else set()
        t2_prefs = set(t2.preferred_courts.values_list("id", flat=True)) if t2 else set()
        mutual_prefs = t1_prefs & t2_prefs

        start_t = end_t = court = None
        if mutual_prefs:
            start_t, end_t, court = _find_preferred_slot(
                slots, used_slots, pending_matches, t1, t2, mutual_prefs
            )
        if start_t is None:
            start_t, end_t, court = _find_preferred_slot(
                slots, used_slots, pending_matches, t1, t2
            )
        if start_t is None:
            pending_matches.append(match)
            continue

        match.scheduled_time = start_t
        match.scheduled_end_time = end_t
        match.court = court
        match.save(update_fields=["scheduled_time", "scheduled_end_time", "court"])
        used_slots.add((start_t, court.id))
        pending_matches.append(match)
