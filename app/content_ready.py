"""Bounded, read-only readiness check before archiving dynamic page content."""
from time import monotonic


class ContentNotReady(RuntimeError):
    pass


_STATE = r'''selector => {
    const roots = selector
        ? Array.from(document.querySelectorAll(selector))
        : [document.querySelector('main,[role="main"]') || document.body].filter(Boolean);
    if (!roots.length) return {state: 'missing'};
    const visible = e => {
        const r = e.getBoundingClientRect(), s = getComputedStyle(e);
        return e.getClientRects().length && r.width > 0 && r.height > 0
            && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const selected = roots.filter(visible);
    if (!selected.length) return {state: 'hidden'};
    const excluded = 'nav,header,footer,aside,[role="complementary"],[role="dialog"],.advertisement,.ad-container,.cookie-banner';
    const usable = e => visible(e) && !e.closest(excluded);
    const placeholder = /^(?:(?:[\p{L}\p{N}][\p{L}\p{N}\s.:/_-]{0,100}\s+)?(?:wird|werden)\s+geladen|loading(?:\s+(?:content|data|page))?)[\s.\u2026]*$/iu;
    let loading = false;
    for (const root of selected) {
        const busy = root.closest('[aria-busy="true"]');
        if (busy && visible(busy)) loading = true;
        for (const e of [root, ...root.querySelectorAll('[aria-busy="true"],h1,h2,h3,p,div,span,section,article')]) {
            if (!usable(e)) continue;
            if (e.getAttribute('aria-busy') === 'true') loading = true;
            const text = (e.innerText || '').replace(/\s+/g, ' ').trim();
            if (text && text.length <= 160 && placeholder.test(text)) loading = true;
        }
    }
    const text = selected.map(e => e.innerText || '').join('\n').replace(/\s+/g, ' ').trim();
    let hash = 2166136261;
    for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
    return {state: loading ? 'loading' : text ? 'ready' : 'empty', signature: text.length + ':' + (hash >>> 0)};
}'''


async def wait_content_ready(page, selector=None, *, timeout_ms=8000, settle_ms=350, poll_ms=150):
    """Wait for actual content, never archive a persistent loading placeholder.

    Hidden or absent selected regions are deferred to the existing agent/tab
    handling. This function does not change the selected editorial scope. Only
    state and a local fingerprint leave the DOM; no source text is returned.
    """
    if selector is not None and (not isinstance(selector, str) or not selector.strip()):
        raise ValueError('Ungültiger Inhaltsbereich für Bereitschaftsprüfung')
    if not (0 < poll_ms <= timeout_ms and 0 <= settle_ms < timeout_ms):
        raise ValueError('Ungültige Grenzen für Bereitschaftsprüfung')
    started = monotonic()
    stable_since, last_signature = None, None
    while True:
        try:
            state = await page.evaluate(_STATE, selector)
        except Exception:
            raise ContentNotReady('Inhaltsbereitschaft konnte nicht geprüft werden.') from None
        current = monotonic()
        kind = state.get('state')
        if kind in ('hidden', 'missing'):
            return {'state': 'deferred_' + kind}
        if kind == 'ready':
            signature = state.get('signature')
            if stable_since is None or signature != last_signature:
                stable_since, last_signature = current, signature
            if (current - stable_since) * 1000 >= settle_ms:
                return {'state': 'ready'}
        else:
            stable_since, last_signature = None, None
        elapsed = (current - started) * 1000
        if elapsed >= timeout_ms:
            raise ContentNotReady('Fachinhalt innerhalb der Wartezeit nicht bereit; Ladezustand oder instabiler/leerer Inhalt. Quelle nicht archiviert.')
        await page.wait_for_timeout(min(poll_ms, timeout_ms - elapsed))


__all__ = ['ContentNotReady', 'wait_content_ready']
