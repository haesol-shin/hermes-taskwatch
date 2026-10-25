"""A fake embed page with the DOM surface that src/player.py uses."""

PLAY_BUTTON = ".play-button"
CONSENT_OVERLAY = ".consent-overlay"
ACCEPT_BUTTON = "button.accept"
PLAYING_PROBE = "document.querySelector('video').currentTime > 0"


class FakeLocator:
    def __init__(self, page, selector, parent=None):
        self.page = page
        self.selector = selector
        self.parent = parent

    def locator(self, selector):
        return FakeLocator(self.page, selector, parent=self)

    async def count(self):
        return 1 if self.is_present() else 0

    async def is_visible(self):
        return self.is_present()

    def is_present(self):
        if self.selector == CONSENT_OVERLAY:
            return self.page.overlay_shown
        return self.selector in (PLAY_BUTTON, ACCEPT_BUTTON)

    async def click(self, timeout=None, force=False):
        if self.selector == ACCEPT_BUTTON:
            if not self.page.overlay_shown:
                raise TimeoutError(f"{self.selector} is not visible")
            self.page.overlay_shown = False
            self.page.clicks.append(ACCEPT_BUTTON)
        elif self.selector == PLAY_BUTTON:
            await self.press_play_button(force)
        else:
            raise TimeoutError(f"no element matches {self.selector}")

    async def press_play_button(self, force):
        if self.page.overlay_shown and not force:
            raise TimeoutError(f"{PLAY_BUTTON} is covered by {CONSENT_OVERLAY}")
        if self.page.overlay_shown:
            self.page.clicks.append(CONSENT_OVERLAY)
            return
        self.page.playing = not self.page.playing
        self.page.clicks.append(PLAY_BUTTON)


class FakeFrame:
    """Loading or reloading the page shows the consent overlay again."""

    def __init__(self, starts_on_its_own):
        self.starts_on_its_own = starts_on_its_own
        self.urls = []
        self.clicks = []
        self.overlay_shown = False
        self.playing = False
        self.current_time = 0.0

    async def goto(self, url):
        self.urls.append(url)
        self.load()

    async def reload(self):
        self.load()

    def load(self):
        self.overlay_shown = True
        self.playing = self.starts_on_its_own
        self.current_time = 1.0 if self.starts_on_its_own else 0.0

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def evaluate(self, expression):
        if expression != PLAYING_PROBE:
            raise ValueError(f"unsupported expression: {expression}")
        return self.current_time > 0

    def advance(self, seconds):
        if self.playing:
            self.current_time += seconds


class FakeClock:
    def __init__(self, frame):
        self.frame = frame
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        self.frame.advance(seconds)
