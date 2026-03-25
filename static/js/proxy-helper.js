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
