/* Only the dashboard container resizes; the saved mail HTML has no script. */
(() => {
  const frame = document.querySelector('.email-preview');
  if (!frame) return;
  let observer;
  const resize = () => {
    const body = frame.contentDocument?.body;
    if (body) frame.style.height = `${Math.ceil(body.getBoundingClientRect().height + 36)}px`;
  };
  frame.addEventListener('load', () => {
    observer?.disconnect();
    resize();
    if (frame.contentDocument?.body) {
      observer = new ResizeObserver(resize);
      observer.observe(frame.contentDocument.body);
    }
  });
  window.addEventListener('resize', resize);
})();
