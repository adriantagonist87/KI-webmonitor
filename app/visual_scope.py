"""Screenshot selected content strips without unrelated intervening page areas."""
from io import BytesIO
from PIL import Image


MAX_NODES = 100
MAX_WIDTH = 2400
MAX_HEIGHT = 16000
GAP = 8


async def capture_scoped(page, selector, masks):
    """Return PNG bytes and a comparison tag; preserve the original scroll."""
    async def viewport():
        return await page.screenshot(mask=masks, animations='disabled'), 'viewport-v1'

    if selector is None:
        return await viewport()
    position = await page.evaluate('() => ({x: window.scrollX, y: window.scrollY})')
    try:
        locator = page.locator(selector)
        if await locator.count() > MAX_NODES:
            return await viewport()
        boxes = await locator.evaluate_all('''nodes => {
            const visible = nodes.filter(e => {
                const r = e.getBoundingClientRect();
                const s = getComputedStyle(e);
                return e.getClientRects().length && r.width > 0 && r.height > 0
                    && s.visibility !== 'hidden' && s.display !== 'none';
            });
            const selected = new Set(visible);
            return visible.filter(e => {
                for (let p = e.parentElement; p; p = p.parentElement) {
                    if (selected.has(p)) return false;
                }
                return true;
            }).map(e => {
                const r = e.getBoundingClientRect();
                return {index: nodes.indexOf(e), width: Math.ceil(r.width), height: Math.ceil(r.height)};
            });
        }''')
        if (not boxes or len(boxes) > MAX_NODES
                or any(box['width'] > MAX_WIDTH for box in boxes)
                or sum(box['height'] for box in boxes) + GAP * (len(boxes) - 1) > MAX_HEIGHT):
            return await viewport()
        strips = []
        height = 0
        for box in boxes:
            raw = await locator.nth(box['index']).screenshot(
                mask=masks, animations='disabled', scale='css')
            with Image.open(BytesIO(raw)) as opened:
                width, strip_height = opened.size
                height += strip_height + (GAP if strips else 0)
                if width > MAX_WIDTH or height > MAX_HEIGHT:
                    return await viewport()
                strips.append(opened.convert('RGBA'))
        canvas = Image.new('RGB', (max(strip.width for strip in strips), height), 'white')
        top = 0
        for strip in strips:
            canvas.paste(strip, (0, top), strip)
            top += strip.height + GAP
        result = BytesIO()
        canvas.save(result, format='PNG')
        return result.getvalue(), 'content-strips-v1'
    finally:
        await page.evaluate('position => window.scrollTo(position.x, position.y)', position)


__all__ = ['capture_scoped']
