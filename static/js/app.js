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
});
