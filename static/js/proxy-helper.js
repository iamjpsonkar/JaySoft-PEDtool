/* ------------------------------------------------------------------ */
/*  proxy_helper.html — API Documentation page JS                     */
/* ------------------------------------------------------------------ */

function toggleAccordion(header) {
    header.parentElement.classList.toggle('open');
}

function copyBlock(btn) {
    const pre = btn.parentElement;
    const text = pre.textContent.replace('Copy', '').trim();
    navigator.clipboard.writeText(text);
    btn.textContent = 'Copied!';
    setTimeout(() => btn.textContent = 'Copy', 1500);
}

// Open accordion if linked via hash
if (window.location.hash) {
    const el = document.querySelector(window.location.hash);
    if (el && el.classList.contains('accordion')) {
        el.classList.add('open');
        setTimeout(() => el.scrollIntoView({ behavior: 'smooth' }), 100);
    }
}

// TOC scroll-spy: highlight active link based on scroll position
(function() {
    var sidebar = document.getElementById('tocSidebar');
    if (!sidebar) return;
    var links = sidebar.querySelectorAll('a[href^="#"]');
    var sections = [];
    links.forEach(function(link) {
        var target = document.querySelector(link.getAttribute('href'));
        if (target) sections.push({ link: link, el: target });
    });
    if (sections.length === 0) return;

    var debounce;
    window.addEventListener('scroll', function() {
        clearTimeout(debounce);
        debounce = setTimeout(function() {
            var scrollY = window.scrollY + 100;
            var active = sections[0];
            for (var i = 0; i < sections.length; i++) {
                if (sections[i].el.offsetTop <= scrollY) active = sections[i];
            }
            links.forEach(function(l) { l.classList.remove('active'); });
            if (active) active.link.classList.add('active');
        }, 50);
    });
})();
