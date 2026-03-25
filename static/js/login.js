/* ------------------------------------------------------------------ */
/*  login.html — Login page JS                                        */
/* ------------------------------------------------------------------ */

document.querySelector('form').addEventListener('submit', function(e) {
    var btn = this.querySelector('.btn');
    btn.textContent = 'Logging in...';
    btn.disabled = true;
});
