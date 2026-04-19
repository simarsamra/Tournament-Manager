// Sidebar toggle for mobile
document.addEventListener('DOMContentLoaded', function() {
    const toggle = document.querySelector('.menu-toggle');
    const sidebar = document.querySelector('.sidebar');
    if (toggle && sidebar) {
        toggle.addEventListener('click', function() {
            sidebar.classList.toggle('open');
        });
        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', function(e) {
            if (window.innerWidth <= 768 && sidebar.classList.contains('open') 
                && !sidebar.contains(e.target) && !toggle.contains(e.target)) {
                sidebar.classList.remove('open');
            }
        });
    }

    // Auto-dismiss alerts after 5 seconds
    document.querySelectorAll('.alert').forEach(function(alert) {
        setTimeout(function() {
            alert.style.transition = 'opacity 0.3s';
            alert.style.opacity = '0';
            setTimeout(function() { alert.remove(); }, 300);
        }, 5000);
    });

    // Filter form auto-submit
    document.querySelectorAll('.filters select').forEach(function(select) {
        select.addEventListener('change', function() {
            this.closest('form').submit();
        });
    });

    // Bulk checkbox shortcuts for availability forms
    document.querySelectorAll('[data-check-target]').forEach(function(button) {
        button.addEventListener('click', function() {
            const target = this.getAttribute('data-check-target');
            const mode = this.getAttribute('data-check-mode');
            const container = document.querySelector('[data-check-group="' + target + '"]');
            if (!container) {
                return;
            }

            container.querySelectorAll('input[type="checkbox"]').forEach(function(checkbox) {
                if (mode === 'all') {
                    checkbox.checked = true;
                } else if (mode === 'none') {
                    checkbox.checked = false;
                } else if (mode === 'weekdays') {
                    checkbox.checked = ['0', '1', '2', '3', '4'].includes(checkbox.value);
                } else if (mode === 'weekend') {
                    checkbox.checked = ['5', '6'].includes(checkbox.value);
                }
            });
        });
    });

    // Live registration review summary
    const registrationForm = document.querySelector('#registration-form');
    if (registrationForm) {
        const teamInput = registrationForm.querySelector('[name="team_name"]');
        const usernameInput = registrationForm.querySelector('[name="username"]');
        const playersInput = registrationForm.querySelector('[name="player_names"]');
        const preferredInputs = registrationForm.querySelectorAll('[name="preferred_courts"]');

        const updateRegistrationPreview = function() {
            const teamTarget = document.querySelector('[data-preview="team_name"]');
            const usernameTarget = document.querySelector('[data-preview="username"]');
            const playerCountTarget = document.querySelector('[data-preview="player_count"]');
            const courtsTarget = document.querySelector('[data-preview="preferred_courts"]');

            if (teamTarget) {
                teamTarget.textContent = teamInput && teamInput.value.trim() ? teamInput.value.trim() : '-';
            }
            if (usernameTarget) {
                usernameTarget.textContent = usernameInput && usernameInput.value.trim() ? usernameInput.value.trim() : '-';
            }
            if (playerCountTarget) {
                const players = playersInput && playersInput.value
                    ? playersInput.value.split('\n').map(function(name) { return name.trim(); }).filter(Boolean)
                    : [];
                playerCountTarget.textContent = String(players.length);
            }
            if (courtsTarget) {
                const selectedCourts = Array.from(preferredInputs)
                    .filter(function(input) { return input.checked; })
                    .map(function(input) {
                        const label = registrationForm.querySelector('label[for="' + input.id + '"]');
                        return label ? label.textContent.trim() : input.value;
                    });
                courtsTarget.textContent = selectedCourts.length ? selectedCourts.join(', ') : 'None selected';
            }
        };

        [teamInput, usernameInput, playersInput].forEach(function(input) {
            if (input) {
                input.addEventListener('input', updateRegistrationPreview);
            }
        });
        preferredInputs.forEach(function(input) {
            input.addEventListener('change', updateRegistrationPreview);
        });
        updateRegistrationPreview();
    }
});
