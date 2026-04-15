from django import forms
from django.contrib.auth.models import User
from .models import Tournament, Court, Team, Match, RescheduleRequest, TimeSlot


class TournamentForm(forms.ModelForm):
    class Meta:
        model = Tournament
        fields = [
            "name", "format", "points_per_win", "points_per_loss",
            "points_per_draw", "num_groups", "teams_per_group_advance",
            "withdrawal_policy", "default_match_duration",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "format": forms.Select(attrs={"class": "form-select"}),
            "points_per_win": forms.NumberInput(attrs={"class": "form-control"}),
            "points_per_loss": forms.NumberInput(attrs={"class": "form-control"}),
            "points_per_draw": forms.NumberInput(attrs={"class": "form-control"}),
            "num_groups": forms.NumberInput(attrs={"class": "form-control"}),
            "teams_per_group_advance": forms.NumberInput(attrs={"class": "form-control"}),
            "withdrawal_policy": forms.Select(attrs={"class": "form-select"}),
            "default_match_duration": forms.NumberInput(attrs={"class": "form-control"}),
        }


class CourtForm(forms.ModelForm):
    class Meta:
        model = Court
        fields = ["name", "is_available"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "is_available": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class TimeSlotForm(forms.Form):
    date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    start_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    end_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))


class TeamRegistrationForm(forms.Form):
    team_name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Team Name"})
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Username"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Password"})
    )
    password_confirm = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control", "placeholder": "Confirm Password"})
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("password_confirm"):
            raise forms.ValidationError("Passwords do not match.")
        if User.objects.filter(username=cleaned.get("username")).exists():
            raise forms.ValidationError("Username already taken.")
        return cleaned


class ScoreSubmitForm(forms.Form):
    score_team1 = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "Score"})
    )
    score_team2 = forms.IntegerField(
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "placeholder": "Score"})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2, "placeholder": "Notes (optional)"})
    )


class RescheduleForm(forms.Form):
    new_date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    new_time = forms.TimeField(widget=forms.TimeInput(attrs={"class": "form-control", "type": "time"}))
    new_court = forms.ModelChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 2})
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tournament:
            self.fields["new_court"].queryset = Court.objects.filter(
                tournament=tournament, is_available=True
            )


class TeamPreferencesForm(forms.Form):
    preferred_courts = forms.ModelMultipleChoiceField(
        queryset=Court.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple(),
    )
    availability_notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3})
    )

    def __init__(self, *args, tournament=None, **kwargs):
        super().__init__(*args, **kwargs)
        if tournament:
            self.fields["preferred_courts"].queryset = Court.objects.filter(
                tournament=tournament
            )


class BulkTeamForm(forms.Form):
    """Add multiple teams at once."""
    teams_text = forms.CharField(
        widget=forms.Textarea(attrs={
            "class": "form-control",
            "rows": 10,
            "placeholder": "One team per line: team_name,username,password"
        }),
        help_text="Format: team_name,username,password (one per line)"
    )
